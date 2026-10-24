"""Real judge: Holm-correct the placebo p-values across every claim of the run, then grade each claim in code."""

from __future__ import annotations

from ..judge.grade import grade_claim, holm
from ..ledger import Author, RecordType
from ..pipeline import RunContext, register

JUDGE = Author.code("judge")


@register("judge", "real")
def judge(ctx: RunContext) -> None:
    if not ctx.claims:
        return
    led = ctx.ledger
    p_raw: dict[str, float] = {}
    for claim_rid in ctx.claims:
        placebo = led.get(led.get(claim_rid).payload["signal_rid"]).payload.get("placebo") or {}
        if placebo.get("p_raw") is not None:
            p_raw[claim_rid] = placebo["p_raw"]
    p_holm = holm(p_raw)
    for claim_rid in ctx.claims:
        verdict = grade_claim(led, claim_rid, p_holm.get(claim_rid), ctx.settings, ctx.panel)
        led.append(RecordType.VERDICT, verdict, JUDGE)
        ctx.verdicts[claim_rid] = verdict.grade
