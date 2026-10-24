"""Real harvest stage: name resolution, sub-type from the filing format, peers, ENTITY and DOC records."""
from datetime import date

import pytest
from conftest import make_ctx, put_xbrl
from typer.testing import CliRunner

from signalcourt.ledger import RecordType
from signalcourt.net.http import Blocked
from signalcourt.pipeline import STAGES
from signalcourt.stages import harvest_stage as H


def run_harvest(tmp_path, home, store, company, mode="replay", **kw):
    ctx = make_ctx(tmp_path, {"harvest": "real"}, store=store, home=home, company=company, symbol=None, mode=mode, **kw)
    STAGES["harvest"]["real"](ctx)
    return ctx


def test_loose_name_resolves_and_records_entity(tmp_path, workspace):
    home, store = workspace
    ctx = run_harvest(tmp_path, home, store, "acme bank ltd")
    assert ctx.symbol == "ACMEBANK" and ctx.subtype == "bank"
    ent = ctx.ledger.query(RecordType.ENTITY)[0].payload
    assert ent["symbol"] == "ACMEBANK" and ent["subtype"] == "bank"
    assert ent["subtype_evidence"]["taxonomy"].startswith("INTEGRATED_FILING_BANKING_")
    assert {p["symbol"] for p in ent["peers"]} == {"BETABANK", "GAMMABANK"}
    assert all(p["subtype"] == "bank" for p in ent["peers"]) and ent["peer_rule"]
    assert sorted(ctx.peers) == ["BETABANK", "GAMMABANK"]


def test_ambiguous_name_raises_with_candidates(tmp_path, workspace):
    home, store = workspace
    with pytest.raises(H.AmbiguousCompany) as ei:
        run_harvest(tmp_path, home, store, "Acme")
    assert {"ACMEFIN", "ACMEFSV"} <= {c.symbol for c in ei.value.candidates}


def test_unknown_name_raises(tmp_path, workspace):
    home, store = workspace
    with pytest.raises(H.UnknownCompany):
        run_harvest(tmp_path, home, store, "Nothing Like It Corp")


def test_subtype_comes_from_filing_format_not_name(tmp_path, workspace):
    home, store = workspace
    # a "Bank" by name whose newest filing is in the life-insurance format
    put_xbrl(store, "BETABANK", "insurer_life", date(2026, 9, 30), publish=date(2026, 9, 30))
    ctx = run_harvest(tmp_path, home, store, "Beta Bank")
    assert ctx.subtype == "insurer_life"
    ent = ctx.ledger.query(RecordType.ENTITY)[0].payload
    assert ent["subtype_evidence"]["taxonomy"].startswith("INTEGRATED_FILING_LI_")
    assert ctx.peers == ["ZETALIFE"]


def test_peer_universe_is_capped_at_the_largest(tmp_path, workspace):
    home, store = workspace
    ctx0 = make_ctx(tmp_path / "a", {})
    settings = dict(ctx0.settings)
    settings["panel"] = {**settings["panel"], "max_peers": 1}
    ctx = run_harvest(tmp_path, home, store, "Acme Bank", settings=settings)
    assert ctx.peers == ["GAMMABANK"]  # scale 2.0 beats 0.5


def test_docs_recorded_with_time_gate(tmp_path, workspace):
    home, store = workspace
    ctx = run_harvest(tmp_path, home, store, "Acme Bank", as_of=date(2026, 5, 15))
    docs = [e.payload for e in ctx.ledger.query(RecordType.DOC)]
    assert len(docs) == 8 and all(d["entity_symbol"] == "ACMEBANK" for d in docs)
    gates = {d["period_end"]: d["time_gate"] for d in docs}
    assert gates["2026-06-30"] == "dropped" and gates["2026-03-31"] == "ok"
    assert len(ctx.doc_shas) == 7 and all(d["origin_id"] == d["url"] for d in docs)


def test_replay_makes_no_network_calls(tmp_path, workspace, monkeypatch):
    home, store = workspace

    def boom():
        raise AssertionError("replay must not build an exchange client")

    monkeypatch.setattr(H, "make_exchange", boom)
    ctx = run_harvest(tmp_path, home, store, "Acme Bank", mode="replay")
    assert ctx.symbol == "ACMEBANK"


class BlockedExchange:
    def __getattr__(self, name):
        def call(*a, **k):
            raise Blocked("bot wall")

        return call

    def close(self):
        pass


def test_warm_refresh_failure_is_recorded_and_the_run_continues(tmp_path, workspace, monkeypatch):
    home, store = workspace
    monkeypatch.setattr(H, "make_exchange", lambda: BlockedExchange())
    ctx = run_harvest(tmp_path, home, store, "Acme Bank", mode="warm")
    gate = ctx.ledger.query(RecordType.GATE)[0].payload
    assert gate["gate"] == "harvest_refresh" and gate["result"] == "fail" and "Blocked" in gate["reason"]
    assert len(ctx.ledger.query(RecordType.DOC)) == 8 and ctx.subtype == "bank"


def test_cli_ambiguous_name_prints_candidates_and_exits_2(tmp_path, workspace, monkeypatch):
    from signalcourt import runner
    from signalcourt.cli import app
    from signalcourt.settings import load_config

    home, store = workspace
    store.close()
    cfg = load_config()
    real = cfg.model_copy(update={"pipeline": cfg.pipeline.model_copy(update={"harvest": "real"})})
    monkeypatch.setattr(runner, "load_config", lambda: real)
    monkeypatch.setenv("SIGNAL_HOME", str(home))
    r = CliRunner().invoke(app, ["run", "--company", "Acme", "--mode", "replay"])
    assert r.exit_code == 2, r.output
    assert "Did you mean:" in r.output and "ACMEFIN" in r.output and "ACMEFSV" in r.output
    assert "Traceback" not in r.output
