"""Real prereg: one template CLAIM per SIGNAL, gated, then ``prereg_head`` sealed before any evidence (G3).

Each claim cites its SIGNAL and the CALC behind its kill-test threshold. A claim that fails the gate, or a
signal that cannot carry a kill test, becomes a REJECT and the stage moves on. With no claim nothing is sealed
and later stages skip. Drafting stops before ``demo.prereg_cap_s`` and says so.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..gates.common import Rejection, write_reject
from ..gates.schema_gate import check_proposal
from ..ledger import Author, RecordType
from ..pipeline import RunContext, register
from ..prereg.template import draft_claim_with_calc

PREREG = Author.code("prereg")


def run(ctx: RunContext, clock: Callable[[], float] = time.monotonic) -> None:
    """Draft, gate and append; ``clock`` is injectable so the time cap can be tested."""
    ctx.claims = []
    if not ctx.signals:
        ctx.on_event("stage_note", {"stage": "prereg", "note": "no signal to pre-register"})
        return
    led = ctx.ledger
    cap = ctx.settings["demo"]["prereg_cap_s"]
    t0 = clock()
    for index, signal_rid in enumerate(ctx.signals):
        if clock() - t0 >= cap:
            note = f"time cap: {index} of {len(ctx.signals)} signals pre-registered"
            ctx.on_event("stage_note", {"stage": "prereg", "note": note})
            break
        try:
            claim, calc_rid = draft_claim_with_calc(led, signal_rid, index, ctx.panel, ctx.settings)
        except (ValueError, KeyError) as e:  # no panel, break, size or direction
            rej = Rejection("prereg", "no_kill_test", {"error": f"{type(e).__name__}: {e}"})
            write_reject(led, {"signal_rid": signal_rid, "hypothesis_index": index}, "CLAIM", rej, (signal_rid,))
            continue
        cited = [*led.get(signal_rid).payload.get("inputs", []), calc_rid]
        model, rej = check_proposal(led, RecordType.CLAIM, claim, PREREG, extra_cited=cited)
        if rej:
            write_reject(led, claim.model_dump(mode="json"), "CLAIM", rej, (signal_rid, calc_rid))
            continue
        ctx.claims.append(led.append(RecordType.CLAIM, model, PREREG, [signal_rid, calc_rid]).rid)
    if ctx.claims:
        led.seal_head("prereg_head")
    else:
        ctx.on_event("stage_note", {"stage": "prereg", "note": "no claim passed the gate; nothing sealed"})


@register("prereg", "real")
def prereg(ctx: RunContext) -> None:
    run(ctx)
