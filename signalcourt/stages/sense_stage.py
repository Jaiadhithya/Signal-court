"""Real sense: scan every metric of the target and append the strongest gap signals to the ledger.

Works for every sub-type: the baseline falls back from twin to peer median to own history by itself and
each signal records which it used. Per-share metrics are first put on one share base per company, so a bonus
issue or split is never reported as a collapse. A break next to a corporate action (a merger filing or an unexplained
share-count move, see ``sensors.corp_actions``) is discounted before ranking, not dropped, so the court still sees it.
The scan stops before ``demo.sense_cap_s`` and says so.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..calc import periods as P
from ..ledger import Author, RecordType
from ..ledger.schema import SignalPayload
from ..pipeline import RunContext, register
from ..sensors.corp_actions import apply_markers, scheme_markers, share_count_markers
from ..sensors.gap import scan_metric
from ..sensors.per_share import adjust_per_share

SENSORS = Author.code("sensors")


def run(ctx: RunContext, clock: Callable[[], float] = time.monotonic) -> None:
    """Scan, rank and append; ``clock`` is injectable so the time cap can be tested."""
    ctx.signals = []
    panel = ctx.panel
    if panel is None or panel.df.empty:
        ctx.on_event("stage_note", {"stage": "sense", "note": "no panel, nothing to scan"})
        return
    sens = ctx.settings["sensors"]
    markers = scheme_markers(ctx.ledger, ctx.store, panel.target, sens) + share_count_markers(panel, sens)
    for m in markers:
        note = f"corporate action at {P.from_index(m.pidx)}: {m.detail}"
        ctx.on_event("stage_note", {"stage": "sense", "note": note})
    panel, share_notes = adjust_per_share(panel, ctx.settings)
    for note in share_notes:
        ctx.on_event("stage_note", {"stage": "sense", "note": note})
    cap = ctx.settings["demo"]["sense_cap_s"]
    metrics = panel.metrics(panel.target)
    found: list[SignalPayload] = []
    t0 = clock()
    slowest = 0.0
    scanned = 0
    capped = False
    for metric in metrics:
        if clock() - t0 + slowest >= cap:  # the next scan would probably not finish inside the cap
            capped = True
            break
        t = clock()
        try:
            sig = scan_metric(panel, metric, ctx.settings)
        except Exception as e:  # one odd metric never stops the stage
            ctx.on_event("stage_note", {"stage": "sense", "note": f"{metric}: scan failed ({type(e).__name__}: {e})"})
            sig = None
        slowest = max(slowest, clock() - t)
        scanned += 1
        if sig is not None:
            found.append(sig)

    for s in found:
        apply_markers(s, markers, sens)
    found.sort(key=lambda s: (-s.surprise.score, s.metrics[0]))
    top = found[: ctx.settings["rank"]["k"]]
    if capped:
        note = f"time cap: {scanned} of {len(metrics)} metrics scanned"
        ctx.on_event("stage_note", {"stage": "sense", "note": note})
        for s in top:
            s.surprise.coverage_status = f"{s.surprise.coverage_status}; {note}"
    for s in top:  # say on the signal itself when its per-share series was restated
        for note in share_notes:
            if note.startswith(f"{s.metrics[0]}:"):
                s.surprise.coverage_status = f"{s.surprise.coverage_status}; {note}"
    if not top:
        ctx.on_event("stage_note", {"stage": "sense", "note": "no signal cleared the bar"})
    for s in top:
        ctx.signals.append(ctx.ledger.append(RecordType.SIGNAL, s, SENSORS, s.inputs).rid)


@register("sense", "real")
def sense(ctx: RunContext) -> None:
    run(ctx)
