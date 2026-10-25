"""A blocked exchange refresh must still produce a verified report from the warm store."""

import importlib
import json
from datetime import date

from conftest import build_workspace, put_pdf, put_xbrl, quarter_ends
from test_court_stage import MODELS

from signalcourt.ledger import RecordType, verify_run
from signalcourt.llm.router import TransportResponse
from signalcourt.net.http import Blocked
from signalcourt.pipeline import STAGE_ORDER, STAGES, run_pipeline
from signalcourt.runner import close_run, start_run
from signalcourt.settings import Env


class UnreachableExchange:
    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise Blocked("NSE unreachable in offline integration fixture")
        return unavailable

    def close(self):
        pass


def warm_offline_workspace(tmp_path, monkeypatch):
    home = tmp_path / "workspace"
    spec = [("TESTBANK", "Test Bank Limited", "bank", 0, 1.0)] + [
        (f"PEER{i}", f"Peer Bank {i} Limited", "bank", 20, 0.5 + i * 0.2) for i in range(5)
    ]
    store = build_workspace(home, spec)
    for i, end in enumerate(quarter_ends(date(2026, 6, 30), 20)):
        put_xbrl(store, "TESTBANK", "bank", end, scale=1.0 + 0.02 * i + (0.8 if i >= 12 else 0))
    put_pdf(
        store,
        ["Quarterly update\n\nInterest income grew strongly after the expansion of the lending business. "
         "The company maintained stable credit quality and a diversified funding profile during the period."],
        entity_symbol="TESTBANK", doc_type="deck", source_family="company_filing",
    )
    store.close()

    # Restore registrations locally: some stage-unit modules deliberately remove their real registration.
    for stage in STAGE_ORDER:
        module = importlib.import_module(f"signalcourt.stages.{stage}_stage")
        monkeypatch.setitem(STAGES[stage], "real", getattr(module, stage))
    from signalcourt.stages import court_stage, harvest_stage

    monkeypatch.setattr(harvest_stage, "make_exchange", UnreachableExchange)
    monkeypatch.setattr(court_stage, "load_models", lambda: MODELS)
    monkeypatch.setattr(Env, "keys", lambda self, name: ["fixture-key"])

    def provider_transport(base_url, api_key, model, messages, max_tokens, timeout_s):
        prompt = messages[0]["content"]
        if prompt.startswith("You are the investigator"):
            pack = json.JSONDecoder().raw_decode(prompt.split("PACK:", 1)[1].lstrip())[0]
            p = pack["passages"][0]
            answer = {"evidence": [{
                "pid": p["pid"], "quote": "Interest income grew strongly after the expansion of the lending business.",
                "stance": "support", "sentence": "The disclosure attributes interest income growth to business expansion.",
            }]}
        elif prompt.startswith("You are the red team"):
            answer = {"challenges": []}
        else:
            answer = {"labels": [{"index": i, "label": "entails"} for i in range(prompt.count('"index"'))]}
        return TransportResponse(json.dumps(answer), tokens_in=10, tokens_out=10)

    # Keep real router, model validation, gates, caching, scheduler, and all seven stages.
    monkeypatch.setattr(court_stage, "bounded", lambda: provider_transport)
    return home


def test_warm_run_with_nse_unreachable_finishes(tmp_path, monkeypatch):
    home = warm_offline_workspace(tmp_path, monkeypatch)
    ctx = start_run("Test Bank", "TESTBANK", None, date(2026, 10, 1), "warm", set(), home)
    try:
        run_pipeline(ctx)
        assert [r.stage for r in ctx.stage_runs] == list(STAGE_ORDER)
        assert all(r.impl == "real" for r in ctx.stage_runs)
        refresh_failures = [r.payload for r in ctx.ledger.query(RecordType.GATE)
                            if r.payload["gate"] == "harvest_refresh"]
        assert refresh_failures and all(r["result"] == "fail" for r in refresh_failures)
        assert any("NSE unreachable" in r["reason"] for r in refresh_failures)
        assert ctx.claims, "The fixture must reach court with real pre-registered claims"
        calls = ctx.ledger.query(RecordType.LLMCALL)
        assert any(r.payload["role"] == "investigate" and not r.payload["error"] for r in calls)
        assert any(r.payload["role"] == "entail" and not r.payload["error"] for r in calls)
        assert ctx.ledger.query(RecordType.EVIDENCE)
        assert ctx.card_path.is_file()
        assert verify_run(ctx.run_dir, store=ctx.store).ok
    finally:
        close_run(ctx)



def test_worker_report_time_cap_keeps_completed_court_claim_count(tmp_path, monkeypatch):
    from scripts.sweep import worker
    from signalcourt import runner

    home = warm_offline_workspace(tmp_path, monkeypatch)
    real_start = runner.start_run

    def start_with_report_cap(*args, **kwargs):
        ctx = real_start(*args, **kwargs)
        ctx.settings["demo"]["report_cap_s"] = 0
        return ctx

    monkeypatch.setattr(runner, "start_run", start_with_report_cap)
    row = worker("Test Bank", "TESTBANK", "warm", home, date(2026, 10, 1))
    assert row["error"] == "" and row["verify_ok"] is True
    assert "time_cap" in row["stop_reason"]
    assert row["claims_attempted"] == 2
    assert row["claims_argued"] == 2
