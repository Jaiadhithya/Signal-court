"""One court round on one claim: investigator, red team, advocate, with entailment between quote and evidence.

The order is fixed by code and agents never talk to each other: each call gets the claim's context pack plus the
records admitted so far, and returns JSON proposals. A proposal becomes a record only after the gates: the quote
gate (the words exist in the hashed document), then schema, capability and unbacked numbers, then the ledger's
ordering rule (nothing argued before ``prereg_head``). A proposal that fails becomes a REJECT and the round goes
on. Router errors (budget, deadline, every provider down, a replay cache miss) propagate to the stage, which
writes the STOP; records admitted before the error stay in the ledger.
"""

from __future__ import annotations

from typing import Any

from ..gates.common import GATES_AUTHOR, Rejection, write_reject
from ..gates.quote_gate import admit_quote
from ..gates.schema_gate import check_proposal
from ..judge.grade import admissible
from ..ledger import Author, OrderingError, RecordType
from ..llm.router import BudgetExhausted, CacheMiss, DeadlineExceeded, RouterError
from ..pipeline import RunContext
from .agents import (
    AdvocateOut,
    EntailOut,
    InvestigatorOut,
    RedteamOut,
    alternative_ids,
    alternatives_text,
    evidence_text,
    fill,
)
from .pack import Pack, blind, blind_pattern, restore_quote

STOPPING = (BudgetExhausted, DeadlineExceeded, CacheMiss)  # end the court; anything else only skips the step


def _author(role: str, res) -> Author:
    return Author(component="court", role=role, model=res.model, family=res.family, provider=res.provider)


class _Round:
    def __init__(self, ctx: RunContext, claim_rid: str, router, pack: Pack):
        self.ctx, self.led, self.router, self.pack = ctx, ctx.ledger, router, pack
        self.claim_rid = claim_rid
        self.court = ctx.settings.get("court", {})
        self.cap = ctx.settings["budgets"]["calls_per_claim_cap"]
        self.max_out = self.court.get("max_output_tokens")
        self.start_calls = router.calls_used
        self.written: list[str] = ctx.court.setdefault(claim_rid, [])
        self.pattern = blind_pattern(pack.names)
        self.evidence: list[dict[str, Any]] = []  # admitted, with alias E1.. and rid
        self.challenges: list[dict[str, Any]] = []
        self.docs = {e.payload["sha256"]: e for e in self.led.query(RecordType.DOC)}
        self.claim_no = ctx.claims.index(claim_rid) + 1 if claim_rid in ctx.claims else 1

    # -- helpers -------------------------------------------------------------------------------------------
    def note(self, text: str) -> None:
        self.ctx.on_event("stage_note", {"stage": "court", "note": f"claim {self.claim_no} {text}"})

    def budget_left(self) -> bool:
        if self.router.calls_used - self.start_calls >= self.cap:
            self.note(f"per-claim call cap of {self.cap} reached")
            return False
        return True

    def call(self, role: str, template: str, schema, messages, exclude: set[str]):
        return self.router.call(role, template, messages, schema, exclude_families=frozenset(exclude),
                                max_tokens=self.max_out)

    def reject(self, proposal: dict, rtype: str, rej: Rejection, parents: tuple = ()) -> str:
        rid = write_reject(self.led, proposal, rtype, rej, parents)
        self.written.append(rid)
        return rid

    def append(self, rtype: RecordType, payload: dict, author: Author, parents: list[str]) -> str | None:
        """Gate 1 then the ledger; a failure of either becomes a REJECT. Returns the new rid or None."""
        model, rej = check_proposal(self.led, rtype, payload, author)
        if rej:
            self.reject(payload, rtype.value, rej, tuple(p for p in parents if p))
            return None
        try:
            rid = self.led.append(rtype, model, author, [p for p in parents if p]).rid
        except OrderingError as e:
            self.reject(payload, rtype.value, Rejection("ordering", "before_prereg_head", {"error": str(e)}))
            return None
        self.written.append(rid)
        return rid

    def resolve(self, ids: list[str], kind: str) -> tuple[list[str], list[str]]:
        """(rids, unknown ids) for aliases an agent cited. kind "E" maps evidence aliases or their pids,
        kind "C" maps calc aliases or formula ids."""
        rids, unknown = [], []
        for i in ids:
            i = str(i).strip()
            rid = None
            if kind == "E":
                rid = next((e["rid"] for e in self.evidence if i in (e["id"], e["pid"])), None)
            else:
                rid = self.pack.aliases.get(i) if i.startswith("C") else None
                rid = rid or next((c["rid"] for c in self.pack.calcs if c["formula"] == i), None)
            (rids if rid else unknown).append(rid or i)
        return list(dict.fromkeys(rids)), unknown

    def evidence_view(self) -> list[dict[str, Any]]:
        return [{k: e[k] for k in ("id", "pid", "stance", "sentence", "source_family")} | {"quote": e["shown"]}
                for e in self.evidence]

    # -- steps ---------------------------------------------------------------------------------------------
    def investigate(self) -> None:
        if not self.budget_left():
            return
        res = self.call("investigate", "investigator", InvestigatorOut,
                        fill("investigator", PACK=self.pack.render(), MAX=str(self.court.get("max_evidence", 3))),
                        set())
        author = _author("investigator", res)
        by_pid = {p["pid"]: p for p in self.pack.passages}
        passed: list[dict[str, Any]] = []
        n_rej = 0
        for item in res.parsed.evidence[: self.court.get("max_evidence", 3)]:
            proposal = item.model_dump()
            passage = self.ctx.store.passage_by_pid(item.pid) if item.pid in by_pid else None
            if passage is None:
                self.reject(proposal, "EVIDENCE", Rejection("quote", "quote_not_found",
                                                            {"why": "pid_not_in_pack", "pid": item.pid}))
                n_rej += 1
                continue
            quote = restore_quote(item.quote, passage.text, self.pack.names)
            span_rid, rej_rid = admit_quote(self.led, self.ctx.store, item.pid, self.ctx.as_of, quote=quote,
                                            proposal=proposal, run_docs=self.ctx.doc_shas)
            if span_rid is None:
                self.written.append(rej_rid)
                n_rej += 1
                continue
            sha = self.led.get(span_rid).payload["doc_sha256"]
            doc = self.docs.get(sha)
            passed.append({"item": item, "span_rid": span_rid, "doc": doc, "author": author,
                           "shown": blind(quote, self.pattern)})
        labels, deferred_why, stop = self.entail(passed)
        for i, p in enumerate(passed):
            doc = p["doc"]
            payload = {
                "claim_rid": self.claim_rid,
                "span_rid": p["span_rid"],
                "stance": p["item"].stance,
                "sentence": blind(p["item"].sentence, self.pattern),
                "source_family": doc.payload["source_family"] if doc else "company_filing",
                "origin_id": doc.payload["origin_id"] if doc else p["span_rid"],
                "entail": labels.get(i),
            }
            rid = self.append(RecordType.EVIDENCE, payload, p["author"], [self.claim_rid, p["span_rid"]])
            if rid is None:
                n_rej += 1
                continue
            if labels.get(i) is None:
                g = self.led.append(RecordType.GATE, {"gate": "entail", "subject_rid": rid, "result": "deferred",
                                                      "reason": deferred_why or "not checked"}, GATES_AUTHOR)
                self.written.append(g.rid)
            self.evidence.append({"id": f"E{len(self.evidence) + 1}", "rid": rid, "pid": p["item"].pid,
                                  "stance": p["item"].stance, "sentence": payload["sentence"],
                                  "source_family": payload["source_family"], "shown": p["shown"]})
        fams = sorted({e["source_family"] for e in self.evidence})
        self.note(f"investigator ({res.family}): {len(self.evidence)} evidence admitted from "
                  f"{len(fams)} source families, {n_rej} rejected")
        if stop is not None:
            raise stop

    def entail(self, passed: list[dict[str, Any]]):
        """({index: EntailRef dict}, reason when deferred, a stopping error to re-raise after the append)."""
        if not passed:
            return {}, None, None
        if not self.budget_left():
            return {}, "per-claim call cap reached", None
        pairs = [{"index": i, "quote": p["shown"], "sentence": blind(p["item"].sentence, self.pattern)}
                 for i, p in enumerate(passed)]
        try:
            res = self.call("entail", "entail", EntailOut, fill("entail", PAIRS=evidence_text(pairs)),
                            self.router.families_used("investigate"))
        except RouterError as e:
            stop = e if isinstance(e, STOPPING) else None
            return {}, f"entailment unavailable: {type(e).__name__}", stop
        out = {}
        for lab in res.parsed.labels:
            if 0 <= lab.index < len(passed) and lab.index not in out:
                out[lab.index] = {"label": lab.label, "checker_model": res.model, "checker_family": res.family,
                                  "batch_rid": res.llmcall_rid}
        return out, "no label returned", None

    def redteam(self) -> None:
        if not self.budget_left():
            return
        exclude = self.router.families_used("investigate") | self.router.families_used("advocate")
        res = self.call("redteam", "redteam", RedteamOut,
                        fill("redteam", PACK=self.pack.render(), EVIDENCE=evidence_text(self.evidence_view()),
                             ALTERNATIVES=alternatives_text(), MAX=str(self.court.get("max_challenges", 2))),
                        exclude)
        author = _author("redteam", res)
        known = alternative_ids()
        n_rej = 0
        for item in res.parsed.challenges[: self.court.get("max_challenges", 2)]:
            proposal = item.model_dump()
            if item.alt_type not in known:
                self.reject(proposal, "CHALLENGE", Rejection("schema", "unknown_alt_type", {"alt_type": item.alt_type}))
                n_rej += 1
                continue
            ev, bad_e = self.resolve(item.evidence_pids, "E")
            calc, bad_c = self.resolve(item.calc_rids, "C")
            if bad_e or bad_c:
                self.reject(proposal, "CHALLENGE", Rejection("schema", "unknown_citation", {"ids": bad_e + bad_c}))
                n_rej += 1
                continue
            payload = {
                "claim_rid": self.claim_rid,
                "alt_type": item.alt_type,
                "argument": blind(item.argument, self.pattern),
                "evidence": ev,
                "calc": calc,
                "severity": item.severity,
                "same_family": res.family in self.router.families_used("investigate"),
            }
            rid = self.append(RecordType.CHALLENGE, payload, author, [self.claim_rid, *ev, *calc])
            if rid is None:
                n_rej += 1
                continue
            self.challenges.append({"index": len(self.challenges), "rid": rid, "alt_type": item.alt_type,
                                    "severity": item.severity, "argument": payload["argument"],
                                    "evidence": [e["id"] for e in self.evidence if e["rid"] in ev],
                                    "calcs": [c["id"] for c in self.pack.calcs if c["rid"] in calc]})
        kinds = ", ".join(f"{c['alt_type']} ({c['severity']})" for c in self.challenges) or "none"
        self.note(f"red team ({res.family}): {len(self.challenges)} challenges [{kinds}], {n_rej} rejected")

    def advocate(self) -> None:
        if not self.challenges or not self.budget_left():
            return
        shown = [{k: c[k] for k in ("index", "alt_type", "severity", "argument", "evidence", "calcs")}
                 for c in self.challenges]
        res = self.call("advocate", "advocate", AdvocateOut,
                        fill("advocate", PACK=self.pack.render(), EVIDENCE=evidence_text(self.evidence_view()),
                             CHALLENGES=evidence_text(shown)),
                        self.router.families_used("redteam"))
        author = _author("advocate", res)
        answered: set[int] = set()
        n_ok = n_bad = n_rej = 0
        for item in res.parsed.rebuttals:
            proposal = item.model_dump()
            if not 0 <= item.challenge_index < len(self.challenges) or item.challenge_index in answered:
                self.reject(proposal, "REBUTTAL", Rejection("schema", "unknown_challenge",
                                                            {"challenge_index": item.challenge_index}))
                n_rej += 1
                continue
            ev, bad_e = self.resolve(item.evidence_rids, "E")
            calc, bad_c = self.resolve(item.calc_rids, "C")
            if bad_e or bad_c:
                self.reject(proposal, "REBUTTAL", Rejection("schema", "unknown_citation", {"ids": bad_e + bad_c}))
                n_rej += 1
                continue
            ch = self.challenges[item.challenge_index]
            payload = {"challenge_rid": ch["rid"], "argument": blind(item.argument, self.pattern), "evidence": ev,
                       "calc": calc}
            ok, why = admissible(self.led, payload)
            payload.update(admissible=ok, admissible_reason=why)
            rid = self.append(RecordType.REBUTTAL, payload, author, [ch["rid"], *ev, *calc])
            if rid is None:
                n_rej += 1
                continue
            answered.add(item.challenge_index)
            n_ok += ok
            n_bad += not ok
        self.note(f"advocate ({res.family}): {n_ok} admissible rebuttals, {n_bad} inadmissible, {n_rej} rejected")


def run_round(ctx: RunContext, claim_rid: str, router, pack: Pack) -> list[str]:
    """Argue one claim and return the rids written for it (also kept in ``ctx.court[claim_rid]``)."""
    r = _Round(ctx, claim_rid, router, pack)
    r.investigate()
    r.redteam()
    r.advocate()
    return list(r.written)

