"""Report stage integration with a real ledger and saved report inputs."""

import importlib
import json

import pytest
from conftest import make_ctx

from signalcourt import stubs
from signalcourt.ledger import Author, RecordType, verify_run
from signalcourt.pipeline import STAGE_ORDER


def _stage():
    return importlib.import_module("signalcourt.stages.report_stage")


def _context(tmp_path, *, on_event=None):
    impl = dict.fromkeys(STAGE_ORDER, "stub")
    impl["report"] = "real"
    return make_ctx(tmp_path, impl, on_event=on_event)


def _judged_claims(ctx):
    stubs.measure(ctx)
    stubs.sense(ctx)
    signal = ctx.signals[0]
    for index in range(3):
        claim = ctx.ledger.append(
            RecordType.CLAIM,
            {
                "hypothesis_index": index,
                "statement": "STUB",
                "mechanism": "STUB",
                "signal_rid": signal,
                "falsification": {
                    "metric": "stub",
                    "comparator": "none",
                    "threshold": 0,
                    "deadline_period": "Q4FY27",
                    "source_rule": "stub",
                },
                "drafter": {"model": "stub", "family": "code"},
            },
            Author.code("prereg"),
        ).rid
        ctx.claims.append(claim)
    ctx.ledger.seal_head("prereg_head")
    for claim in ctx.claims[:2]:
        ctx.ledger.append(
            RecordType.VERDICT,
            {"claim_rid": claim, "grade": "Weak", "reasons": ["Court did not run."]},
            Author.code("judge"),
        )


def test_report_stage_seals_run_head_and_verify_passes(tmp_path):
    ctx = _context(tmp_path)
    try:
        _judged_claims(ctx)
        _stage().report(ctx)
        findings = ctx.ledger.query(RecordType.FINDING)
        assert [item.payload["claim_rid"] for item in findings] == ctx.claims[:2]
        assert ctx.findings == [item.rid for item in findings]
        assert ctx.ledger.heads()["run_head"] == ctx.ledger.head()
        assert all(item.seq <= ctx.ledger.heads()["run_head"][0] for item in findings)
        assert verify_run(ctx.run_dir).ok
        assert ctx.card_path == ctx.run_dir / "report.html"
        html = ctx.card_path.read_text(encoding="utf-8")
        assert (ctx.run_dir / "finding.html").read_text(encoding="utf-8") == html
        assert ctx.ledger.heads()["run_head"][1] in html
    finally:
        ctx.ledger.close()


def test_report_stage_no_signal_preserves_impl_without_new_records(tmp_path):
    ctx = _context(tmp_path)
    try:
        before = len(ctx.ledger)
        _stage().report(ctx)
        assert len(ctx.ledger) == before
        assert ctx.findings == []
        meta = json.loads((ctx.run_dir / "report-meta.json").read_text(encoding="utf-8"))
        assert meta["impl"] == ctx.impl
        assert meta["settings"] == ctx.settings
        assert meta["report_seconds"] >= 0
        assert any("panel" in note.lower() for note in meta["notes"])
        html = ctx.card_path.read_text(encoding="utf-8")
        assert "No signal cleared the bar" in html
        assert verify_run(ctx.run_dir).ok
    finally:
        ctx.ledger.close()


def test_report_stage_does_not_swallow_writer_failures(tmp_path, monkeypatch):
    import signalcourt.writer

    ctx = _context(tmp_path)
    try:
        _judged_claims(ctx)

        def fail(*args, **kwargs):
            raise RuntimeError("writer integrity failure")

        monkeypatch.setattr(signalcourt.writer, "append_finding", fail, raising=False)
        with pytest.raises(RuntimeError, match="writer integrity failure"):
            _stage().report(ctx)
        assert "run_head" not in ctx.ledger.heads()
        assert not ctx.ledger.query(RecordType.FINDING)
    finally:
        ctx.ledger.close()


def test_report_stage_records_early_stop_and_renders_it(tmp_path):
    ctx = _context(tmp_path)
    try:
        ctx.ledger.append(
            RecordType.STOP,
            {"reason": "time_cap", "calls_used": 0, "tools_used": 0, "elapsed_s": 0, "open_items": ["court"]},
            Author.code("scheduler"),
        )
        _stage().report(ctx)
        assert "time_cap" in ctx.card_path.read_text(encoding="utf-8")
        assert ctx.ledger.heads()["run_head"] == ctx.ledger.head()
    finally:
        ctx.ledger.close()
