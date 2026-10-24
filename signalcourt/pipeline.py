"""The seven-stage pipeline: run context, stage registry and the runner loop.

Each stage has a ``stub`` and (once its sprint lands) a ``real`` implementation, registered with
``@register(stage, impl)``. ``resolve_impl`` picks one per stage from settings ``[pipeline]`` and the
``--stub`` override; a stage whose ``real`` is not registered yet runs its stub.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from signalcourt.calc.panel import Panel
from signalcourt.ledger import Author, Ledger, RecordType
from signalcourt.ledger.schema import StopPayload
from signalcourt.store import DocStore

STAGE_ORDER = ("harvest", "measure", "sense", "prereg", "court", "judge", "report")

StageFn = Callable[["RunContext"], None]
STAGES: dict[str, dict[str, StageFn]] = {s: {} for s in STAGE_ORDER}


@dataclass
class StageRun:
    stage: str
    impl: str
    records: int
    seconds: float


@dataclass
class RunContext:
    ledger: Ledger
    store: DocStore | None
    settings: dict[str, Any]
    company: str
    symbol: str | None
    as_of: date
    mode: str
    run_dir: Path
    impl: dict[str, str]
    # filled by stages
    doc_shas: list[str] = field(default_factory=list)
    panel: Panel | None = None
    signals: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    court: dict[str, list[str]] = field(default_factory=dict)
    verdicts: dict[str, str] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    card_path: Path | None = None
    # filled by run_pipeline, one entry per stage that finished
    stage_runs: list[StageRun] = field(default_factory=list)


def register(stage: str, impl: str) -> Callable[[StageFn], StageFn]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if impl not in ("stub", "real"):
        raise ValueError(f"unknown impl {impl!r}")

    def deco(fn: StageFn) -> StageFn:
        STAGES[stage][impl] = fn
        return fn

    return deco


def resolve_impl(settings: dict[str, Any], stub_override: set[str]) -> dict[str, str]:
    """Per stage: "stub" if overridden, else settings["pipeline"][stage], else "stub"; "real" needs a registration."""
    unknown = stub_override - set(STAGE_ORDER)
    if unknown:
        raise ValueError(f"unknown stage(s) in --stub: {sorted(unknown)}")
    wanted = settings.get("pipeline", {})
    out: dict[str, str] = {}
    for s in STAGE_ORDER:
        impl = "stub" if s in stub_override else wanted.get(s, "stub")
        if impl == "real" and "real" not in STAGES.get(s, {}):
            impl = "stub"
        out[s] = impl
    return out


def run_pipeline(ctx: RunContext) -> RunContext:
    """Run every stage in order. A failing stage writes STOP(reason="error") and the exception propagates."""
    t0 = time.perf_counter()
    for stage in STAGE_ORDER:
        impl = ctx.impl.get(stage, "stub")
        before = len(ctx.ledger)
        t = time.perf_counter()
        try:
            STAGES[stage][impl](ctx)
        except Exception:
            stop = StopPayload(
                reason="error", calls_used=0, tools_used=0, elapsed_s=round(time.perf_counter() - t0, 3),
                open_items=[stage],
            )
            ctx.ledger.append(RecordType.STOP, stop, Author.code("scheduler"))
            raise
        ctx.stage_runs.append(StageRun(stage, impl, len(ctx.ledger) - before, time.perf_counter() - t))
    return ctx
