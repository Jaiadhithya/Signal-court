import json
from pathlib import Path

from test_court_round import RATING_QUOTE, entail_all, investigator
from test_court_support import FakeRouter, build

from signalcourt.ledger import RecordType, verify_run
from signalcourt.llm.router import DeadlineExceeded, Router, TransportError, TransportResponse
from signalcourt.stages import court_stage, judge_stage

MODELS = {
    "providers": {"pa": {"base_url": "http://a", "keys_env": "A"}, "pb": {"base_url": "http://b", "keys_env": "B"}},
    "deployments": {"a1": ["pa", "model-a", "fam_a", 100000, None], "b1": ["pb", "model-b", "fam_b", 100000, None]},
    "roles": {"investigate": ["a1", "b1"], "advocate": ["a1", "b1"], "redteam": ["a1", "b1"], "entail": ["a1", "b1"]},
}


def _alias(messages, formula):
    """The calc alias the pack gave a formula, read back from the prompt the agent got."""
    text = messages[0]["content"]
    start = text.index("PACK:") + len("PACK:")
    end = text.find("ADMITTED EVIDENCE:")
    pack = json.loads(text[start:end if end > 0 else None])
    return next(c["id"] for c in pack["calcs"] if c["formula"] == formula)


def redteam_reply(messages):
    return {"challenges": [{"alt_type": "sector_shock", "argument": "Every lender may have grown at once.",
                            "evidence_pids": ["E1"], "calc_rids": [_alias(messages, "sector_shock")],
                            "severity": "substantive"}]}


def advocate_reply(messages):
    return {"rebuttals": [{"challenge_index": 0, "argument": "The sector-shock test shows peers did not move.",
                           "evidence_rids": [], "calc_rids": [_alias(messages, "sector_shock")]}]}


def replies(f):
    return {"investigate": investigator(f), "redteam": redteam_reply, "advocate": advocate_reply,
            "entail": entail_all}


def role_of(messages):
    text = messages[0]["content"]
    for prefix, role in (("You are the investigator", "investigate"), ("You are the red team", "redteam"),
                         ("You are the advocate", "advocate")):
        if text.startswith(prefix):
            return role
    return "entail"


def transport_for(f):
    table = replies(f)

    def transport(base_url, api_key, model, messages, max_tokens, timeout_s):
        r = table[role_of(messages)]
        return TransportResponse(json.dumps(r(messages) if callable(r) else r))

    return transport


def router(tmp_path, f, mode="warm", transport=None, name="llm.sqlite"):
    return Router(MODELS, lambda env: ["k"], Path(tmp_path) / name, mode, ledger=f.led,
                  transport=transport or transport_for(f))


def stops(f):
    return [s.payload for s in f.led.query(RecordType.STOP)]


def test_court_stage_with_no_claims_does_nothing(tmp_path):
    f = build(tmp_path)
    f.ctx.claims = []
    before = len(f.led)
    court_stage.run(f.ctx, router=FakeRouter({}))
    assert len(f.led) == before and f.ctx.court == {}
    f.led.close()


def test_full_pipeline_with_fake_router_gives_moderate_when_evidence_and_rebuttals_qualify(tmp_path):
    f = build(tmp_path)
    notes = []
    f.ctx.on_event = lambda kind, data: notes.append(data["note"]) if kind == "stage_note" else None
    court_stage.run(f.ctx, router=FakeRouter(replies(f)))
    judge_stage.judge(f.ctx)
    (v,) = f.led.query(RecordType.VERDICT)
    assert v.payload["grade"] == "Moderate", v.payload["reasons"]
    assert v.payload["checks"]["support_families_moderate"]["value"] == 2
    assert v.payload["checks"]["no_unanswered_challenge"]["pass"]
    assert f.ctx.court[f.claim] and stops(f) == []
    assert any("investigator" in n and "2 evidence admitted" in n for n in notes)
    f.led.close()
    assert verify_run(f.ctx.run_dir).ok


def test_all_providers_down_writes_stop_and_judge_still_grades(tmp_path):
    f = build(tmp_path)

    def down(*a, **k):
        raise TransportError(503, "service unavailable")

    court_stage.run(f.ctx, router=router(tmp_path, f, transport=down))
    (stop,) = stops(f)
    assert stop["reason"] == "error" and stop["open_items"] == [f.claim]
    judge_stage.judge(f.ctx)
    (v,) = f.led.query(RecordType.VERDICT)
    assert v.payload["grade"] == "Weak"
    f.led.close()


def test_provider_down_after_investigator_keeps_admitted_evidence(tmp_path):
    f = build(tmp_path)
    good = transport_for(f)

    def flaky(base_url, api_key, model, messages, max_tokens, timeout_s):
        if role_of(messages) in ("investigate", "entail"):
            return good(base_url, api_key, model, messages, max_tokens, timeout_s)
        raise TransportError(500, "boom")

    court_stage.run(f.ctx, router=router(tmp_path, f, transport=flaky))
    assert stops(f)[0]["reason"] == "error"
    assert len(f.led.query(RecordType.EVIDENCE)) == 2
    f.led.close()


def test_deadline_writes_stop_time_cap_and_returns(tmp_path):
    f = build(tmp_path)
    court_stage.run(f.ctx, router=FakeRouter({"investigate": DeadlineExceeded("run deadline reached")}))
    (stop,) = stops(f)
    assert stop["reason"] == "time_cap" and stop["open_items"] == [f.claim]
    f.led.close()


def test_clock_past_the_cap_stops_before_the_next_claim(tmp_path):
    f = build(tmp_path)
    t = iter([0.0])
    court_stage.run(f.ctx, router=FakeRouter(replies(f)), clock=lambda: next(t, 10_000.0))
    (stop,) = stops(f)
    assert stop["reason"] == "time_cap" and f.led.query(RecordType.EVIDENCE) == []
    f.led.close()


def test_budget_exhausted_writes_stop_budget(tmp_path):
    from signalcourt.llm.router import BudgetExhausted

    f = build(tmp_path)
    court_stage.run(f.ctx, router=FakeRouter({"investigate": BudgetExhausted("call budget of 60 used")}))
    assert stops(f)[0]["reason"] == "budget"
    f.led.close()


def test_replay_mode_uses_cache_only_and_misses_write_stop(tmp_path):
    f = build(tmp_path)
    court_stage.run(f.ctx, router=router(tmp_path, f, mode="replay"))
    (stop,) = stops(f)
    assert stop["reason"] == "error" and stop["open_items"] == [f.claim]
    assert f.led.query(RecordType.EVIDENCE) == []
    f.led.close()


def test_replay_reproduces_the_warm_round_from_the_cache(tmp_path):
    def records(fix):
        out = []
        for t in (RecordType.EVIDENCE, RecordType.CHALLENGE, RecordType.REBUTTAL, RecordType.REJECT):
            for e in fix.led.query(t):
                p = dict(e.payload)
                for k in ("claim_rid", "span_rid", "challenge_rid", "evidence", "calc", "admissible_reason"):
                    p.pop(k, None)
                p.pop("entail", None)
                out.append((t.value, json.dumps(p, sort_keys=True)))
        return out

    warm = build(tmp_path / "warm")
    court_stage.run(warm.ctx, router=router(tmp_path, warm))

    def never(*a, **k):
        raise AssertionError("replay must not call a provider")

    rep = build(tmp_path / "replay", reuse=warm)
    r = router(tmp_path, rep, mode="replay", transport=never)
    court_stage.run(rep.ctx, router=r)
    assert stops(rep) == []
    assert records(rep) == records(warm) and records(warm)
    assert all(c.payload["cache_hit"] for c in rep.led.query(RecordType.LLMCALL))
    warm.led.close()
    rep.led.close()


def test_demo_mode_argues_at_most_max_claims(tmp_path):
    f = build(tmp_path)
    f.ctx.settings = {**f.ctx.settings, "court": {**f.ctx.settings["court"], "max_claims_demo": 0}}
    court_stage.run(f.ctx, router=FakeRouter(replies(f)))
    assert f.led.query(RecordType.EVIDENCE) == []
    assert stops(f)[0]["open_items"] == [f.claim]
    f.led.close()


def test_rating_quote_is_admitted_with_rating_family(tmp_path):
    f = build(tmp_path)
    court_stage.run(f.ctx, router=FakeRouter(replies(f)))
    fams = {e.payload["source_family"] for e in f.led.query(RecordType.EVIDENCE)}
    assert "rating_agency" in fams
    assert any(RATING_QUOTE.split()[0] in f.led.get(e.payload["span_rid"]).payload["text"]
               for e in f.led.query(RecordType.EVIDENCE))
    f.led.close()
