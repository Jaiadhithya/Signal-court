"""Real court: argue the pre-registered claims in surprise order under a hard time and call budget.

Per claim: the code tests for the standard alternatives (CALC), the context pack, then one round of investigator,
red team and advocate (signalcourt.court.round). Claims run one after another, since the ledger is not
thread-safe. In demo mode only the first ``court.max_claims_demo`` claims are argued.

The stage never fails the run for a provider problem: a spent budget, the time cap, every provider down or a
replay cache miss ends the court with a STOP record (reason budget, time_cap or error, open_items = the claims
not finished), and the judge then grades what was admitted.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..court.alternatives import run_code_tests
from ..court.pack import build_pack
from ..court.round import run_round
from ..ledger import Author, RecordType
from ..llm.router import BudgetExhausted, DeadlineExceeded, Router, RouterError
from ..pipeline import RunContext, register
from ..settings import Workspace, get_settings, load_models

SCHEDULER = Author.code("scheduler")


def make_router(ctx: RunContext, deadline: float, clock: Callable[[], float]) -> Router:
    s = get_settings()
    ws = Workspace(home=ctx.home) if ctx.home is not None else s.workspace
    ws.cache.mkdir(parents=True, exist_ok=True)
    return Router(
        load_models(),
        s.env.keys,
        ws.llm_db,
        mode=ctx.mode,
        ledger=ctx.ledger,
        call_budget=ctx.settings["budgets"]["reasoning_calls"],
        deadline=deadline,
        clock=clock,
        demo=bool(ctx.settings["court"]["demo_mode"]),
    )


def _reason(e: Exception) -> str:
    if isinstance(e, BudgetExhausted):
        return "budget"
    if isinstance(e, DeadlineExceeded):
        return "time_cap"
    return "error"


def run(ctx: RunContext, router=None, clock: Callable[[], float] = time.monotonic) -> None:
    """Argue ``ctx.claims``; ``router`` and ``clock`` are injectable for tests."""
    if not ctx.claims:
        ctx.on_event("stage_note", {"stage": "court", "note": "no claim to argue"})
        return
    court = ctx.settings["court"]
    t0 = clock()
    deadline = t0 + float(ctx.settings["demo"]["court_cap_s"])
    own = router is None
    if own:
        router = make_router(ctx, deadline, clock)
    claims = list(ctx.claims)
    if court["demo_mode"]:
        claims = claims[: court["max_claims_demo"]]
    done: list[str] = []
    stop: tuple[str, str] | None = None
    try:
        for i, claim_rid in enumerate(claims, start=1):
            if clock() >= deadline:
                raise DeadlineExceeded("court time cap reached")
            ctx.court.setdefault(claim_rid, [])
            run_code_tests(ctx.ledger, ctx.panel, claim_rid)
            pack = build_pack(ctx.ledger, ctx.store, claim_rid, ctx.doc_shas, court["pack_tokens"])
            fams = sorted({p["source_family"] for p in pack.passages})
            ctx.on_event("stage_note", {"stage": "court", "note": f"claim {i}: pack of {len(pack.passages)} "
                                        f"passages ({', '.join(fams) or 'none'}), {len(pack.calcs)} code tests"})
            run_round(ctx, claim_rid, router, pack)
            done.append(claim_rid)
    except RouterError as e:
        stop = (_reason(e), f"{type(e).__name__}: {e}"[:240])
    except Exception as e:  # noqa: BLE001 - a court bug must not take the live run down; the judge still grades
        stop = ("error", f"{type(e).__name__}: {e}"[:240])
    finally:
        if own:
            router.close()
    open_items = [c for c in ctx.claims if c not in done]
    if stop is None and open_items:
        stop = ("budget", f"demo mode argues {len(claims)} of {len(ctx.claims)} claims")
    if stop is not None:
        ctx.ledger.append(
            RecordType.STOP,
            {"reason": stop[0], "calls_used": router.calls_used, "tools_used": 0,
             "elapsed_s": round(clock() - t0, 3), "open_items": open_items},
            SCHEDULER,
        )
        ctx.on_event("stage_note", {"stage": "court", "note": f"court stopped ({stop[0]}): {stop[1]}"})
    ctx.on_event("stage_note", {"stage": "court", "note": f"court: {len(done)} claims argued, "
                                f"{router.calls_used} LLM calls, {clock() - t0:.1f}s"})


@register("court", "real")
def court(ctx: RunContext) -> None:
    run(ctx)
