"""Real measure stage: target FACT lineage, the peer panel DOC, the time gate and the time cap."""
import time
from datetime import date

import pandas as pd
import pytest
from conftest import build_workspace, make_ctx, put_xbrl, quarter_ends

import signalcourt.stages  # noqa: F401  (registers the real stages)
from signalcourt.ledger import RecordType, verify_run
from signalcourt.pipeline import STAGES

AS_OF = date(2026, 10, 1)


def run_stages(ctx, stages=("harvest", "measure")):
    for s in stages:
        STAGES[s]["real"](ctx)
    return ctx


def ctx_with_synthetic(tmp_path, subtype, n_peers=3, quarters=8, as_of=AS_OF):
    spec = [("TARGETCO", "Target Company Limited", subtype, quarters, 1.0)]
    spec += [(f"PEER{i:02d}", f"Peer Number {i} Limited", subtype, quarters, 0.55 + i / 10) for i in range(n_peers)]  # never 1.0: identical bytes
    home = tmp_path / "home"
    store = build_workspace(home, spec)
    ctx = make_ctx(
        tmp_path, {"harvest": "real", "measure": "real"}, store=store, home=home, company="Target Company",
        symbol=None, as_of=as_of,
    )
    return ctx


@pytest.mark.parametrize("subtype", ["bank", "nbfc", "insurer_life", "generic"])
def test_measure_builds_a_panel_for_each_subtype(tmp_path, subtype):
    ctx = run_stages(ctx_with_synthetic(tmp_path, subtype))
    panel = ctx.panel
    assert panel.target == "TARGETCO" and panel.metrics("TARGETCO")
    assert len(panel.periods("TARGETCO")) == 8
    tgt = panel.df[panel.df.entity == "TARGETCO"]
    peer = panel.df[panel.df.entity != "TARGETCO"]
    fact = ctx.ledger.get(tgt.rid.iloc[0])
    assert fact.type == RecordType.FACT and fact.payload["extractor"] == "xbrl"
    span = ctx.ledger.get(fact.payload["span_rid"])
    assert span.type == RecordType.SPAN and span.payload["kind"] == "xbrl" and span.payload["page"] == 0
    assert span.payload["text"] == fact.payload["value_as_written"] and span.payload["xbrl"]["element"]
    assert set(peer.entity) == {"PEER00", "PEER01", "PEER02"}
    peer_doc = ctx.ledger.get(peer.rid.iloc[0])
    assert peer_doc.type == RecordType.DOC and peer_doc.payload["doc_type"] == "peer_panel"
    assert set(peer.rid) == {peer_doc.rid}
    assert ctx.store.has(peer_doc.payload["sha256"])
    assert (ctx.run_dir / "panel.parquet").exists() and (ctx.run_dir / "peer_panel.parquet").exists()
    assert pd.read_parquet(ctx.run_dir / "peer_panel.parquet").entity.nunique() == 3
    assert not ctx.ledger.query(RecordType.FACT, entity="PEER00")
    assert verify_run(ctx.run_dir).ok


def test_time_gate_hides_quarters_published_after_as_of(tmp_path):
    ctx = run_stages(ctx_with_synthetic(tmp_path, "bank", as_of=date(2026, 5, 15)))
    # the June quarter is published 30 days after quarter end, so it is invisible on 15 May
    assert ctx.panel.latest_period() == "Q4FY26"
    assert not ctx.ledger.query(RecordType.FACT, period_end="2026-06-30")
    assert ctx.panel.df.published.max() <= date(2026, 5, 15)


def test_restated_quarter_keeps_both_facts_and_the_panel_takes_the_latest(tmp_path):
    ctx = ctx_with_synthetic(tmp_path, "bank")
    put_xbrl(ctx.store, "TARGETCO", "bank", date(2026, 3, 31), scale=1.1, publish=date(2026, 7, 1))
    run_stages(ctx)
    facts = ctx.ledger.query(RecordType.FACT, period_end="2026-03-31", metric="pat")
    assert len(facts) == 2
    pat = ctx.panel.series("TARGETCO", "pat")
    assert pat.iloc[-2] == max(f.payload["value"] for f in facts)


def test_measure_computes_registry_formulas_for_the_latest_quarter(tmp_path):
    ctx = run_stages(ctx_with_synthetic(tmp_path, "bank"))
    calcs = [e.payload for e in ctx.ledger.query(RecordType.CALC)]
    assert calcs and {c["entity"] for c in calcs} == {"TARGETCO"}
    assert any(c["formula_id"] == "yoy_pat" for c in calcs)
    for c in calcs:
        assert all(ctx.ledger.get(r).type == RecordType.FACT for r in c["inputs"])


def test_measure_with_forty_peers_fits_the_time_cap(tmp_path):
    ctx = ctx_with_synthetic(tmp_path, "nbfc", n_peers=40, quarters=20)
    STAGES["harvest"]["real"](ctx)
    t = time.perf_counter()
    STAGES["measure"]["real"](ctx)
    took = time.perf_counter() - t
    assert took < ctx.settings["demo"]["measure_cap_s"]
    assert ctx.panel.df.entity.nunique() == 41
    assert len(ctx.panel.periods("TARGETCO")) == 20


def test_quarter_ends_helper():
    assert quarter_ends(date(2026, 6, 30), 2) == [date(2026, 3, 31), date(2026, 6, 30)]


def test_peer_contributes_its_consolidated_filings_when_it_files_no_standalone(tmp_path):
    ctx = ctx_with_synthetic(tmp_path, "bank")  # synthetic files are all consolidated
    run_stages(ctx)
    assert ctx.panel.df[ctx.panel.df.entity == "PEER00"].basis.unique().tolist() == ["consolidated"]
