"""A saved response cache must replay the real pipeline without attempting any network I/O."""

import importlib
import json
from datetime import date
from pathlib import Path

import httpx
import tiktoken
from conftest import build_workspace, put_xbrl, quarter_ends

from signalcourt.ledger import RecordType, verify_run
from signalcourt.llm.cache import ResponseCache
from signalcourt.pipeline import STAGE_ORDER, STAGES, run_pipeline
from signalcourt.runner import close_run, start_run
from signalcourt.settings import Env, Workspace

CACHE_FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "responses.json"
MODELS = {
    "providers": {"fixture": {"base_url": "https://provider.invalid", "keys_env": "FIXTURE"}},
    "deployments": {
        "author": ["fixture", "fixture-author", "fixture_author", 100000, None],
        "checker": ["fixture", "fixture-checker", "fixture_checker", 100000, None],
    },
    "roles": {role: ["author", "checker"] for role in ("investigate", "entail", "redteam", "advocate")},
}
DISCLOSURE = (
    "Quarterly update\n\nInterest income grew strongly after the expansion of the lending business. "
    "The company maintained stable credit quality and a diversified funding profile during the period."
)


def replay_workspace(tmp_path, monkeypatch):
    home = tmp_path / "workspace"
    spec = [("TESTBANK", "Test Bank Limited", "bank", 0, 1.0)] + [
        (f"PEER{i}", f"Peer Bank {i} Limited", "bank", 20, 0.5 + i * 0.2) for i in range(5)
    ]
    store = build_workspace(home, spec)
    try:
        for i, end in enumerate(quarter_ends(date(2026, 6, 30), 20)):
            put_xbrl(store, "TESTBANK", "bank", end, scale=1.0 + 0.02 * i + (0.8 if i >= 12 else 0))
        # Stable bytes give stable document hashes and passage IDs across machines and run directories.
        store.put_bytes(
            DISCLOSURE.encode("utf-8"), url="https://example.test/update.txt",
            final_url="https://example.test/update.txt", mime="text/plain", source_id="company_update",
            source_family="company_filing", doc_type="deck", entity_symbol="TESTBANK",
            publish_date=date(2026, 8, 1), publish_date_source="exchange_broadcast",
        )
    finally:
        store.close()

    # Some stage unit tests remove registrations during collection. Restore only within this test's scope.
    for stage in STAGE_ORDER:
        module = importlib.import_module(f"signalcourt.stages.{stage}_stage")
        monkeypatch.setitem(STAGES[stage], "real", getattr(module, stage))
    from signalcourt.court import pack
    from signalcourt.stages import court_stage

    monkeypatch.setattr(court_stage, "load_models", lambda: MODELS)
    monkeypatch.setattr(Env, "keys", lambda self, name: ["synthetic-fixture-key"])
    # A byte vocabulary uses the real tokenizer without downloading cl100k's vocabulary on cold offline CI.
    encoder = tiktoken.Encoding(
        name="fixture_bytes", pat_str=r"(?s).", mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )
    monkeypatch.setattr(pack, "_encoder", lambda: encoder)
    return home


def block_network(monkeypatch):
    from signalcourt.court import transport
    from signalcourt.llm import router
    from signalcourt.stages import court_stage, harvest_stage

    attempts = []

    def blocked(*args, **kwargs):
        attempts.append("network/provider/exchange")
        raise AssertionError("replay attempted network I/O")

    async def async_blocked(*args, **kwargs):
        return blocked(*args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
    monkeypatch.setattr(router, "openai_transport", blocked)
    monkeypatch.setattr(transport, "openai_call", blocked)
    # bounded() captures its default transport at definition time; block that route explicitly as well.
    monkeypatch.setattr(court_stage, "bounded", lambda: blocked)
    monkeypatch.setattr(harvest_stage, "make_exchange", blocked)
    return attempts


def test_full_replay_with_network_blocked(tmp_path, monkeypatch):
    attempts = block_network(monkeypatch)
    home = replay_workspace(tmp_path, monkeypatch)
    entries = json.loads(CACHE_FIXTURE.read_text(encoding="utf-8"))
    cache = ResponseCache(Workspace(home=home).llm_db)
    try:
        for entry in entries:
            cache.put(**entry)
    finally:
        cache.close()

    ctx = start_run("Test Bank", "TESTBANK", None, date(2026, 10, 1), "replay", set(), home)
    # The byte tokenizer is deliberately conservative; leave room for the complete tiny context pack.
    ctx.settings["court"]["pack_tokens"] = 20000
    try:
        run_pipeline(ctx)
        assert attempts == [], "Swallowed transport errors must not pass as successful offline replay"
        assert [stage.stage for stage in ctx.stage_runs] == list(STAGE_ORDER)
        assert all(stage.impl == "real" for stage in ctx.stage_runs)
        assert ctx.claims, "Real sense and prereg must produce claims, rather than bypassing the court"
        calls = ctx.ledger.query(RecordType.LLMCALL)
        assert calls, "Replay must consume the committed cache; an empty no-finding report is insufficient"
        assert all(call.payload["cache_hit"] and call.payload["error"] is None for call in calls)
        assert {call.payload["role"] for call in calls} == {"investigate", "entail", "redteam", "advocate"}
        assert all(call.payload["prompt_hash"] in {entry["key"] for entry in entries} for call in calls)
        evidence = ctx.ledger.query(RecordType.EVIDENCE)
        assert evidence and all(item.payload["entail"]["label"] == "entails" for item in evidence)
        assert ctx.ledger.query(RecordType.CHALLENGE)
        assert any(item.payload["admissible"] for item in ctx.ledger.query(RecordType.REBUTTAL))
        assert not [stop for stop in ctx.ledger.query(RecordType.STOP) if stop.payload["reason"] == "error"]
        assert ctx.ledger.query(RecordType.VERDICT)
        assert ctx.ledger.query(RecordType.FINDING)
        assert ctx.card_path.is_file() and ctx.card_path.stat().st_size > 0
        verification = verify_run(ctx.run_dir, store=ctx.store)
        assert verification.ok, verification.problems
    finally:
        close_run(ctx)
