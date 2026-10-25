"""Regenerate responses.json locally using only deterministic synthetic replies.

Run from the repository root: uv run python tests/fixtures/replay/generate_cache.py
This helper is never imported or executed by the regression test.
"""

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_full_replay import CACHE_FIXTURE, block_network, replay_workspace  # noqa: E402

from signalcourt.ledger import RecordType, verify_run  # noqa: E402
from signalcourt.llm.router import TransportResponse  # noqa: E402
from signalcourt.pipeline import run_pipeline  # noqa: E402
from signalcourt.runner import close_run, start_run  # noqa: E402
from signalcourt.settings import Workspace  # noqa: E402
from signalcourt.stages import court_stage  # noqa: E402


def synthetic_transport(base_url, api_key, model, messages, max_tokens, timeout_s):
    prompt = messages[0]["content"]
    if "PACK:" in prompt:
        pack = json.JSONDecoder().raw_decode(prompt.split("PACK:", 1)[1].lstrip())[0]
        calc = next(c["id"] for c in pack["calcs"] if c["formula"] == "sector_shock")
    if prompt.startswith("You are the investigator"):
        answer = {"evidence": [{
            "pid": pack["passages"][0]["pid"],
            "quote": "Interest income grew strongly after the expansion of the lending business.",
            "stance": "support", "sentence": "The disclosure attributes interest income growth to business expansion.",
        }]}
    elif prompt.startswith("You are the red team"):
        answer = {"challenges": [{
            "alt_type": "sector_shock", "argument": "Every lender may have grown at once.",
            "evidence_pids": ["E1"], "calc_rids": [calc], "severity": "substantive",
        }]}
    elif prompt.startswith("You are the advocate"):
        answer = {"rebuttals": [{
            "challenge_index": 0, "argument": "The sector-shock test shows peers did not move.",
            "evidence_rids": [], "calc_rids": [calc],
        }]}
    else:
        answer = {"labels": [{"index": 0, "label": "entails"}]}
    return TransportResponse(json.dumps(answer), tokens_in=10, tokens_out=10)


if __name__ == "__main__":
    with TemporaryDirectory() as root, pytest.MonkeyPatch.context() as mp:
        attempts = block_network(mp)
        home = replay_workspace(Path(root), mp)
        make_router = court_stage.make_router

        def recording_router(ctx, deadline, clock):
            router = make_router(ctx, deadline, clock)
            # Only the real router switches to warm to create cache entries; harvest remains replay-only.
            router.mode = "warm"
            return router

        mp.setattr(court_stage, "make_router", recording_router)
        mp.setattr(court_stage, "bounded", lambda: synthetic_transport)
        ctx = start_run("Test Bank", "TESTBANK", None, date(2026, 10, 1), "replay", set(), home)
        ctx.settings["court"]["pack_tokens"] = 20000
        try:
            run_pipeline(ctx)
            assert not attempts
            assert verify_run(ctx.run_dir, store=ctx.store).ok
            assert ctx.ledger.query(RecordType.EVIDENCE)
            assert not [s for s in ctx.ledger.query(RecordType.STOP) if s.payload["reason"] == "error"]
            calls = ctx.ledger.query(RecordType.LLMCALL)
            assert {c.payload["role"] for c in calls} == {"investigate", "entail", "redteam", "advocate"}
            with closing(sqlite3.connect(Workspace(home=home).llm_db)) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT key, model, family, provider, text, tokens_in, tokens_out FROM responses ORDER BY key"
                ).fetchall()
            CACHE_FIXTURE.write_text(json.dumps([dict(row) for row in rows], indent=2) + "\n", encoding="utf-8")
            print(f"Wrote {len(rows)} synthetic responses; {len(calls)} real LLMCALL records; no network attempts")
        finally:
            close_run(ctx)
