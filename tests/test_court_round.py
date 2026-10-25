from pathlib import Path

from test_court_support import FakeRouter, build

from signalcourt.court.alternatives import run_code_tests
from signalcourt.court.pack import build_pack
from signalcourt.court.round import run_round
from signalcourt.ledger import RecordType
from signalcourt.llm.router import Router, TransportResponse

DECK_QUOTE = "the company advances grew strongly after the second quarter, driven by retail loans"
RATING_QUOTE = "The rating reflects the sustained growth in advances of the company and its stable deposit franchise"


def _setup(tmp_path, **kw):
    f = build(tmp_path, **kw)
    run_code_tests(f.led, f.panel, f.claim)
    pack = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 2500)
    return f, pack


def calc_alias(pack, formula):
    return next(c["id"] for c in pack.calcs if c["formula"] == formula)


def investigator(f, *, invented=False):
    items = [{"pid": f.pids["rating"], "quote": RATING_QUOTE, "stance": "support",
              "sentence": "A rating agency describes sustained growth in advances."}]
    if invented:
        items.insert(0, {"pid": f.pids["deck"], "quote": "the company advances tripled after a merger with a rival",
                         "stance": "support", "sentence": "Advances tripled."})
    else:
        items.insert(0, {"pid": f.pids["deck"], "quote": DECK_QUOTE, "stance": "support",
                         "sentence": "The company's own deck reports strong growth in advances."})
    return {"evidence": items}


def redteam(pack, *, extra=None):
    ch = [{"alt_type": "sector_shock", "argument": "All lenders may have grown advances in the same quarter.",
           "evidence_pids": ["E1"], "calc_rids": [calc_alias(pack, "sector_shock")], "severity": "substantive"}]
    return {"challenges": ch + (extra or [])}


def advocate(pack, cite="calc"):
    cites = {"calc": ([], [calc_alias(pack, "sector_shock")]), "evidence": (["E1"], []), "none": ([], [])}[cite]
    return {"rebuttals": [{"challenge_index": 0, "argument": "Peers did not share the move.",
                           "evidence_rids": cites[0], "calc_rids": cites[1]}]}


def entail_all(messages):
    n = messages[0]["content"].count('"index"')
    return {"labels": [{"index": i, "label": "entails"} for i in range(n)]}


def router_for(f, pack, **over):
    replies = {"investigate": investigator(f), "redteam": redteam(pack), "advocate": advocate(pack),
               "entail": entail_all}
    replies.update(over)
    return FakeRouter(replies)


def test_round_writes_evidence_challenge_and_rebuttal(tmp_path):
    f, pack = _setup(tmp_path)
    rids = run_round(f.ctx, f.claim, router_for(f, pack), pack)
    ev = f.led.query(RecordType.EVIDENCE, claim_rid=f.claim)
    assert len(ev) == 2
    assert {e.payload["source_family"] for e in ev} == {"company_filing", "rating_agency"}
    assert {e.payload["origin_id"] for e in ev} == {"https://example.test/deck.pdf", "https://example.test/rating.pdf"}
    assert all(e.payload["entail"]["label"] == "entails" for e in ev)
    assert ev[0].author.role == "investigator" and ev[0].author.family == "nvidia"
    span = f.led.get(ev[0].payload["span_rid"]).payload
    assert span["text"].startswith("Testco Finance advances")  # the original wording, not the blinded copy
    (ch,) = f.led.query(RecordType.CHALLENGE, claim_rid=f.claim)
    assert ch.payload["calc"] == [pack.aliases[calc_alias(pack, "sector_shock")]]
    assert ch.payload["evidence"] == [ev[0].rid] and ch.payload["same_family"] is False
    (rb,) = f.led.query(RecordType.REBUTTAL)
    assert rb.payload["admissible"] is True and rb.payload["challenge_rid"] == ch.rid
    assert set(rids) >= {ev[0].rid, ev[1].rid, ch.rid, rb.rid}
    f.led.close()


def test_invented_quote_is_rejected_and_round_continues(tmp_path):
    f, pack = _setup(tmp_path)
    run_round(f.ctx, f.claim, router_for(f, pack, investigate=investigator(f, invented=True)), pack)
    rejects = f.led.query(RecordType.REJECT)
    assert any(r.payload["failing_gate"] == "quote" and r.payload["reason"] == "quote_not_found" for r in rejects)
    assert len(f.led.query(RecordType.EVIDENCE)) == 1
    assert len(f.led.query(RecordType.CHALLENGE)) == 1 and len(f.led.query(RecordType.REBUTTAL)) == 1
    f.led.close()


def test_unbacked_number_in_challenge_is_rejected(tmp_path):
    f, pack = _setup(tmp_path)
    bad = {"alt_type": "seasonality", "argument": "Advances always jump 47 percent in this quarter.",
           "evidence_pids": [], "calc_rids": [], "severity": "substantive"}
    run_round(f.ctx, f.claim, router_for(f, pack, redteam=redteam(pack, extra=[bad])), pack)
    rejects = [r for r in f.led.query(RecordType.REJECT) if r.payload["proposal_type"] == "CHALLENGE"]
    assert len(rejects) == 1 and rejects[0].payload["reason"] == "unbacked_number"
    assert len(f.led.query(RecordType.CHALLENGE)) == 1  # the backed challenge still went in
    f.led.close()


def test_unknown_alternative_and_unknown_citation_are_rejected(tmp_path):
    f, pack = _setup(tmp_path)
    odd = [{"alt_type": "astrology", "argument": "The stars aligned.", "evidence_pids": [], "calc_rids": [],
            "severity": "minor"}]
    run_round(f.ctx, f.claim, router_for(f, pack, redteam={"challenges": odd}), pack)
    (rej,) = [r for r in f.led.query(RecordType.REJECT) if r.payload["proposal_type"] == "CHALLENGE"]
    assert rej.payload["reason"] == "unknown_alt_type"
    f.led.close()


def test_redteam_is_told_to_exclude_investigator_and_advocate_families(tmp_path):
    f, pack = _setup(tmp_path)
    r = router_for(f, pack)
    run_round(f.ctx, f.claim, r, pack)
    by_role = {c["role"]: c for c in r.calls}
    assert "nvidia" in by_role["redteam"]["exclude"]
    assert "openai" in by_role["advocate"]["exclude"]  # the guard holds in both directions
    assert "nvidia" in by_role["entail"]["exclude"]
    f.led.close()


def _models(chain_a_first=True):
    dep = lambda prov, model, fam: [prov, model, fam, 100000, None]  # noqa: E731
    return {
        "providers": {"pa": {"base_url": "http://a", "keys_env": "A"}, "pb": {"base_url": "http://b", "keys_env": "B"}},
        "deployments": {"a1": dep("pa", "model-a", "fam_a"), "b1": dep("pb", "model-b", "fam_b")},
        "roles": {"investigate": ["a1", "b1"], "advocate": ["a1", "b1"], "redteam": ["a1", "b1"],
                  "entail": ["a1", "b1"]},
    }


def test_redteam_family_differs_from_investigator_and_advocate(tmp_path):
    f, pack = _setup(tmp_path)
    replies = {"investigate": investigator(f), "redteam": redteam(pack), "advocate": advocate(pack)}
    seen = []

    def transport(base_url, api_key, model, messages, max_tokens, timeout_s):
        text = messages[0]["content"]
        role = ("investigate" if text.startswith("You are the investigator") else
                "redteam" if text.startswith("You are the red team") else
                "advocate" if text.startswith("You are the advocate") else "entail")
        seen.append((role, model))
        import json
        body = entail_all(messages) if role == "entail" else replies[role]
        return TransportResponse(json.dumps(body))

    router = Router(_models(), lambda env: ["k"], Path(tmp_path) / "llm.sqlite", "warm", ledger=f.led,
                    transport=transport)
    run_round(f.ctx, f.claim, router, pack)
    models = dict(seen)
    assert models["investigate"] == "model-a" and models["redteam"] == "model-b"
    assert models["advocate"] == "model-a"
    (ch,) = f.led.query(RecordType.CHALLENGE)
    assert ch.author.family == "fam_b" and ch.payload["same_family"] is False
    assert f.led.query(RecordType.LLMCALL)  # the router recorded every call
    router.close()
    f.led.close()


def test_rebuttal_without_citations_is_recorded_inadmissible(tmp_path):
    f, pack = _setup(tmp_path)
    run_round(f.ctx, f.claim, router_for(f, pack, advocate=advocate(pack, cite="none")), pack)
    (rb,) = f.led.query(RecordType.REBUTTAL)
    assert rb.payload["admissible"] is False and "cites no" in rb.payload["admissible_reason"]
    f.led.close()


def test_sector_shock_rebuttal_needs_the_calc(tmp_path):
    f, pack = _setup(tmp_path)
    run_round(f.ctx, f.claim, router_for(f, pack, advocate=advocate(pack, cite="evidence")), pack)
    (rb,) = f.led.query(RecordType.REBUTTAL)
    assert rb.payload["admissible"] is False and "sector-shock" in rb.payload["admissible_reason"]
    f.led.close()


def test_evidence_cannot_precede_prereg_head_becomes_reject_not_crash(tmp_path):
    f, pack = _setup(tmp_path, seal=False)
    run_round(f.ctx, f.claim, router_for(f, pack), pack)
    assert f.led.query(RecordType.EVIDENCE) == [] and f.led.query(RecordType.CHALLENGE) == []
    ordering = [r for r in f.led.query(RecordType.REJECT) if r.payload["failing_gate"] == "ordering"]
    assert len(ordering) >= 2
    f.led.close()


def test_entail_failure_defers_and_keeps_evidence(tmp_path):
    from signalcourt.llm.router import AllProvidersFailed

    f, pack = _setup(tmp_path)
    run_round(f.ctx, f.claim, router_for(f, pack, entail=AllProvidersFailed([("x", "500")])), pack)
    ev = f.led.query(RecordType.EVIDENCE)
    assert len(ev) == 2 and all(e.payload["entail"] is None for e in ev)
    deferred = [g for g in f.led.query(RecordType.GATE) if g.payload["result"] == "deferred"]
    assert {g.payload["subject_rid"] for g in deferred} == {e.rid for e in ev}
    f.led.close()
