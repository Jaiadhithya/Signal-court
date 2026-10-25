"""Real prereg stage: one gated CLAIM per SIGNAL, REJECT on failure, prereg_head sealed before any evidence."""
import itertools

import pytest
from conftest import make_ctx
from test_gap_sensor import make_panel

from signalcourt.ledger import Author, OrderingError, RecordType, verify_run
from signalcourt.pipeline import STAGES
from signalcourt.sensors.gap import scan_metric

prereg_stage = None  # the module, imported by the fixture below
SENSORS = Author.code("sensors")


@pytest.fixture(scope="module", autouse=True)
def _real_prereg_registered():
    """Importing the stage registers it as the real prereg; undo that afterwards so other modules see the stub."""
    global prereg_stage
    from signalcourt.stages import prereg_stage as mod

    prereg_stage = mod
    yield
    STAGES["prereg"].pop("real", None)


def ctx_with_signals(tmp_path, metrics=("gnpa_pct", "advances"), **kw):
    panel = make_panel(metrics=metrics)
    ctx = make_ctx(tmp_path, {"prereg": "real"}, symbol=panel.target, **kw)
    ctx.panel = panel
    for m in metrics:
        sig = scan_metric(panel, m, ctx.settings)
        ctx.signals.append(ctx.ledger.append(RecordType.SIGNAL, sig, SENSORS, sig.inputs).rid)
    return ctx


def evidence(claim_rid):
    return {
        "claim_rid": claim_rid, "span_rid": "SP-00000000", "stance": "support", "sentence": "x",
        "source_family": "exchange_xbrl", "origin_id": "o",
    }


def test_prereg_stage_is_registered_as_real():
    assert STAGES["prereg"]["real"] is prereg_stage.prereg


def test_claim_passes_gates_and_seals_before_any_evidence(tmp_path):
    ctx = ctx_with_signals(tmp_path)
    led = ctx.ledger
    with pytest.raises(OrderingError):
        led.append(RecordType.EVIDENCE, evidence("CL-00000000"), Author(component="agents", role="investigator"))

    prereg_stage.prereg(ctx)
    claims = [led.get(r) for r in ctx.claims]
    assert len(claims) == len(ctx.signals) and not led.query(RecordType.REJECT)
    assert [c.payload["hypothesis_index"] for c in claims] == list(range(len(claims)))
    for claim, sig_rid in zip(claims, ctx.signals, strict=True):
        assert claim.payload["signal_rid"] == sig_rid and claim.author.writer == "prereg"
        calc = led.get(claim.parents[1])
        assert claim.parents == [sig_rid, calc.rid] and calc.type == RecordType.CALC
        assert calc.payload["value"] == claim.payload["falsification"]["threshold"]
    seq, h = led.heads()["prereg_head"]
    assert (seq, h) == (claims[-1].seq, claims[-1].hash)
    led.append(RecordType.EVIDENCE, evidence(ctx.claims[0]), Author(component="agents", role="investigator"))
    assert verify_run(ctx.run_dir).ok


def test_no_signals_means_no_claim_and_no_seal(tmp_path):
    ctx = make_ctx(tmp_path, {"prereg": "real"})
    ctx.panel = make_panel()
    before = len(ctx.ledger)
    ctx.claims = ["stale"]
    prereg_stage.prereg(ctx)
    assert ctx.claims == [] and len(ctx.ledger) == before and "prereg_head" not in ctx.ledger.heads()


def test_rejected_proposal_writes_reject_and_stage_continues(tmp_path, monkeypatch):
    ctx = ctx_with_signals(tmp_path)
    real = prereg_stage.draft_claim_with_calc

    def leaky(ledger, signal_rid, index, panel, settings=None):
        claim, calc_rid = real(ledger, signal_rid, index, panel, settings)
        if index == 0:  # an invented figure in free text
            claim = claim.model_copy(update={"statement": claim.statement + " It rose by 4.7%."})
        return claim, calc_rid

    monkeypatch.setattr(prereg_stage, "draft_claim_with_calc", leaky)
    prereg_stage.prereg(ctx)
    led = ctx.ledger
    rejects = led.query(RecordType.REJECT)
    assert len(rejects) == 1 and rejects[0].payload["reason"] == "unbacked_number"
    assert rejects[0].payload["proposal_type"] == "CLAIM" and rejects[0].parents[0] == ctx.signals[0]
    assert len(ctx.claims) == 1 and led.get(ctx.claims[0]).payload["signal_rid"] == ctx.signals[1]
    assert led.get(ctx.claims[0]).payload["hypothesis_index"] == 1
    assert "prereg_head" in led.heads() and verify_run(ctx.run_dir).ok


def test_undraftable_signal_is_rejected_and_nothing_is_sealed(tmp_path):
    ctx = make_ctx(tmp_path, {"prereg": "real"})
    ctx.panel = make_panel()
    sig = {"sensor": "t", "entity": "TARGETCO", "metrics": ["m1"], "direction": "none", "specificity_source": "na"}
    ctx.signals = [ctx.ledger.append(RecordType.SIGNAL, sig, SENSORS).rid]
    prereg_stage.prereg(ctx)
    assert ctx.claims == [] and "prereg_head" not in ctx.ledger.heads()
    assert [r.payload["reason"] for r in ctx.ledger.query(RecordType.REJECT)] == ["no_kill_test"]


def test_time_cap_stops_drafting_and_says_so(tmp_path):
    notes = []
    ctx = ctx_with_signals(tmp_path, on_event=lambda kind, data: notes.append(data["note"]))
    cap = ctx.settings["demo"]["prereg_cap_s"]
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(cap + 1))  # past the cap after the first claim
    prereg_stage.run(ctx, clock=lambda: next(ticks))
    assert len(ctx.claims) == 1 and "prereg_head" in ctx.ledger.heads()
    assert any("time cap" in n for n in notes)


def test_prereg_after_real_sense_on_a_synthetic_panel(tmp_path):
    from signalcourt.stages import sense_stage

    panel = make_panel(metrics=("gnpa_pct", "advances", "pat"), seed=3)
    ctx = make_ctx(tmp_path, {"sense": "real", "prereg": "real"}, symbol=panel.target)
    ctx.panel = panel
    sense_stage.sense(ctx)
    assert ctx.signals
    prereg_stage.prereg(ctx)
    claims = [ctx.ledger.get(r).payload for r in ctx.claims]
    assert [c["signal_rid"] for c in claims] == ctx.signals
    assert "prereg_head" in ctx.ledger.heads() and verify_run(ctx.run_dir).ok
