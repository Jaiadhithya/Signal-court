from datetime import date

import pytest
from conftest import fixed_clock, run_payload

from signalcourt import pipeline
from signalcourt.ledger import Ledger
from signalcourt.pipeline import STAGE_ORDER, RunContext, resolve_impl, run_pipeline
from signalcourt.settings import load_config


def test_resolve_impl_falls_back_to_stub_when_real_missing():
    impl = resolve_impl({"pipeline": {"sense": "real"}}, stub_override=set())
    assert impl["sense"] == "stub"  # no real sense registered yet


def test_stub_override_wins():
    impl = resolve_impl({"pipeline": {"harvest": "real"}}, stub_override={"harvest"})
    assert impl["harvest"] == "stub"


def test_resolve_impl_uses_real_when_registered(monkeypatch):
    monkeypatch.setitem(pipeline.STAGES, "judge", {"stub": lambda ctx: None, "real": lambda ctx: None})
    impl = resolve_impl({"pipeline": {"judge": "real"}}, stub_override=set())
    assert impl["judge"] == "real"
    assert set(impl) == set(STAGE_ORDER)


def test_settings_toml_has_a_pipeline_section():
    cfg = load_config()
    assert set(cfg.pipeline.model_dump()) == set(STAGE_ORDER)
    assert cfg.pipeline.measure == "stub"


def test_stage_error_writes_stop_and_reraises(tmp_path, monkeypatch):
    calls = []

    def ok(ctx):
        calls.append("ok")

    def boom(ctx):
        raise RuntimeError("sense failed")

    stages = {s: {"stub": ok} for s in STAGE_ORDER}
    stages["sense"] = {"stub": boom}
    monkeypatch.setattr(pipeline, "STAGES", stages)

    run_dir = tmp_path / "run"
    led = Ledger.create(run_dir, run_payload(), clock=fixed_clock())
    ctx = RunContext(
        ledger=led,
        store=None,
        settings={},
        company="Test Co",
        symbol=None,
        as_of=date(2026, 10, 1),
        mode="replay",
        run_dir=run_dir,
        impl={s: "stub" for s in STAGE_ORDER},
    )
    with pytest.raises(RuntimeError, match="sense failed"):
        run_pipeline(ctx)
    stops = led.query("STOP")
    assert len(stops) == 1
    assert stops[0].payload["reason"] == "error"
    assert stops[0].payload["open_items"] == ["sense"]
    assert stops[0].author.component == "scheduler"
    assert calls == ["ok", "ok"]  # harvest and measure ran, nothing after sense
    led.close()
