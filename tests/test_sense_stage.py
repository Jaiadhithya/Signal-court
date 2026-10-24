"""Real sense stage: top-k SIGNALs, the time cap, the empty cases and the ledger lineage of every input."""
import itertools
import time
from datetime import date

import numpy as np
import pytest
from conftest import build_workspace, make_ctx, put_xbrl, quarter_ends
from test_gap_sensor import make_panel

import signalcourt.stages  # noqa: F401  (registers the real stages)
from signalcourt.calc import periods as P
from signalcourt.ledger import RecordType, verify_run
from signalcourt.pipeline import STAGES

sense_stage = None  # the module, imported by the fixture below


@pytest.fixture(scope="module", autouse=True)
def _real_sense_registered():
    """Importing the stage registers it as the real sense; undo that afterwards so other modules see the stub."""
    global sense_stage
    from signalcourt.stages import sense_stage as mod

    sense_stage = mod
    yield
    STAGES["sense"].pop("real", None)


AS_OF = date(2026, 10, 1)
LAST = date(2026, 6, 30)


def ctx_with_panel(tmp_path, panel, **kw):
    ctx = make_ctx(tmp_path, {"sense": "real"}, symbol=panel.target if panel is not None else "TARGETCO", **kw)
    ctx.panel = panel
    return ctx


def signals_of(ctx):
    return [ctx.ledger.get(r) for r in ctx.signals]


def test_sense_appends_top_k_by_surprise(tmp_path):
    metrics = [f"m{i}" for i in range(8)]
    steps = {m: 1.0 + i for i, m in enumerate(metrics)}
    panel = make_panel(metrics=metrics, n_peers=8, step=0.0, seed=1)
    for m, st in steps.items():  # a different step per metric, so a different surprise score
        panel.df.loc[(panel.df.entity == "TARGETCO") & (panel.df.metric == m) & (panel.df.pidx >= P.index("Q2FY25")),
                     "value"] += st
    ctx = ctx_with_panel(tmp_path, panel)
    sense_stage.sense(ctx)
    sigs = signals_of(ctx)
    k = ctx.settings["rank"]["k"]
    assert 0 < len(sigs) <= k and all(s.type == RecordType.SIGNAL for s in sigs)
    scores = [s.payload["surprise"]["score"] for s in sigs]
    assert scores == sorted(scores, reverse=True)
    assert verify_run(ctx.run_dir).ok


def test_sense_stage_is_registered_as_real():
    assert STAGES["sense"]["real"] is sense_stage.sense


def test_sense_stage_appends_top_k_and_respects_time_cap(tmp_path):
    metrics = [f"m{i}" for i in range(6)]
    panel = make_panel(metrics=metrics, n_peers=6, seed=2)
    for m in metrics:
        panel.df.loc[(panel.df.entity == "TARGETCO") & (panel.df.metric == m), "value"] += np.where(
            panel.df[(panel.df.entity == "TARGETCO") & (panel.df.metric == m)].pidx >= P.index("Q2FY25"), 4.0, 0.0
        )
    notes = []
    ctx = ctx_with_panel(tmp_path, panel, on_event=lambda kind, data: notes.append((kind, data)))
    cap = ctx.settings["demo"]["sense_cap_s"]
    ticks = itertools.count(0.0, cap / 3)  # every clock read advances a third of the cap

    sense_stage.run(ctx, clock=lambda: next(ticks))
    sigs = signals_of(ctx)
    assert 0 < len(sigs) < len(metrics)  # stopped before every metric was scanned
    assert all("time cap" in s.payload["surprise"]["coverage_status"] for s in sigs)
    assert any(k == "stage_note" and "time cap" in d["note"] for k, d in notes)

    full = ctx_with_panel(tmp_path / "full", panel)
    sense_stage.sense(full)
    assert len(signals_of(full)) > len(sigs)
    assert not any("time cap" in s.payload["surprise"]["coverage_status"] for s in signals_of(full))


def test_time_cap_with_no_signal_is_logged(tmp_path):
    notes = []
    ctx = ctx_with_panel(tmp_path, make_panel(), on_event=lambda kind, data: notes.append(data["note"]))
    ticks = itertools.chain([0.0], itertools.repeat(1e9))  # past the cap at the first check
    sense_stage.run(ctx, clock=lambda: next(ticks))
    assert ctx.signals == [] and any("time cap" in n for n in notes)


def test_sense_stage_with_no_panel_sets_empty_signals(tmp_path):
    ctx = ctx_with_panel(tmp_path, None)
    ctx.signals = ["stale"]
    sense_stage.sense(ctx)
    assert ctx.signals == []


def test_sense_stage_with_nothing_found_sets_empty_signals(tmp_path):
    ctx = ctx_with_panel(tmp_path, make_panel(flat=True, break_q=None))
    sense_stage.sense(ctx)
    assert ctx.signals == [] and not ctx.ledger.query(RecordType.SIGNAL)


def test_one_failing_metric_does_not_stop_the_stage(tmp_path, monkeypatch):
    panel = make_panel(metrics=["good", "bad"])
    real = sense_stage.scan_metric

    def flaky(p, m, s):
        if m == "bad":
            raise ValueError("boom")
        return real(p, m, s)

    monkeypatch.setattr(sense_stage, "scan_metric", flaky)
    notes = []
    ctx = ctx_with_panel(tmp_path, panel, on_event=lambda kind, data: notes.append(data["note"]))
    sense_stage.sense(ctx)
    assert len(ctx.signals) == 1 and any("bad" in n for n in notes)


def test_sense_on_40_peers_x_20_quarters_x_20_metrics_is_under_cap(tmp_path):
    panel = make_panel(metrics=[f"m{i}" for i in range(20)], n_peers=40, quarters=20, seed=4)
    ctx = ctx_with_panel(tmp_path, panel)
    t = time.perf_counter()
    sense_stage.sense(ctx)
    took = time.perf_counter() - t
    assert took < ctx.settings["demo"]["sense_cap_s"]
    assert len(ctx.signals) <= ctx.settings["rank"]["k"]


def planted_workspace(tmp_path, n_peers=6, quarters=20):
    """Synthetic filings: every entity follows one common path; the target steps up from Q2FY25."""
    rng = np.random.default_rng(7)
    spec = [("TARGETCO", "Target Company Limited", "bank", 0, 1.0)]
    spec += [(f"PEER{i:02d}", f"Peer Number {i} Limited", "bank", 0, 1.0) for i in range(n_peers)]
    home = tmp_path / "home"
    store = build_workspace(home, spec)
    common = 1.0 + 0.03 * np.arange(quarters) + rng.normal(0, 0.08, quarters)
    for k, end in enumerate(quarter_ends(LAST, quarters)):
        step = 1.8 if P.index(end) >= P.index("Q2FY25") else 1.0
        put_xbrl(store, "TARGETCO", "bank", end, scale=float(common[k] * step))
        for i in range(n_peers):
            put_xbrl(store, f"PEER{i:02d}", "bank", end, scale=float(common[k] * (1 + 0.01 * i) * rng.normal(1, 0.02)))
    ctx = make_ctx(
        tmp_path, {"harvest": "real", "measure": "real", "sense": "real"}, store=store, home=home,
        company="Target Company", symbol=None, as_of=AS_OF,
    )
    return ctx


def test_signal_inputs_are_ledger_rids(tmp_path):
    ctx = planted_workspace(tmp_path)
    for s in ("harvest", "measure", "sense"):
        STAGES[s]["real"](ctx)
    sigs = signals_of(ctx)
    assert sigs, "a planted level shift in the target must produce a signal"
    assert any(s.payload["break_period"] == "Q2FY25" for s in sigs)
    for s in sigs:
        assert s.payload["inputs"]
        for rid in s.payload["inputs"]:
            assert ctx.ledger.get(rid) is not None
        assert ctx.ledger.get(s.payload["inputs"][0]).type in (RecordType.FACT, RecordType.CALC)
    assert verify_run(ctx.run_dir).ok


@pytest.mark.parametrize("n_peers", [0, 2, 5])
def test_sense_finishes_for_a_subtype_with_few_peers(tmp_path, n_peers):
    ctx = planted_workspace(tmp_path, n_peers=n_peers)
    for s in ("harvest", "measure", "sense"):
        STAGES[s]["real"](ctx)
    assert signals_of(ctx)
    for s in signals_of(ctx):
        want = {0: "na", 2: "na", 5: "peer_median"}[n_peers]
        assert s.payload["specificity_source"] == want
        assert s.payload["surprise"]["coverage_status"]
