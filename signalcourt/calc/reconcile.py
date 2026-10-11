"""Reconciliation checks and silent-restatement detection (implementation.md 7.7).

Never raises on bad data: every problem is a ReconResult so one failing quarter cannot
crash a run. A 'fail' makes dependent CALCs fail (calculator) and the judge's numbers_ok
reads it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..extract.registry import metric_by_id
from ..extract.types import FactProposal

DEFAULT_REL_TOL = 0.005  # 0.5% relative
COMPOSITION_TOL_PP = 0.5


@dataclass
class ReconResult:
    status: str  # pass | fail | single_source
    check: str  # sanity | cross_source | composition | segment_sum | restatement
    subject: str
    detail: str = ""
    restatement: dict[str, Any] | None = None


def rounding_tolerance(value_as_written: str) -> float:
    """Half a unit in the last displayed digit (display rounding)."""
    s = value_as_written.strip().replace(",", "").lstrip("-")
    if "." in s:
        return 0.5 * 10 ** (-len(s.split(".")[1]))
    return 0.5


def sanity(f: FactProposal) -> ReconResult:
    rng = metric_by_id().get(f.metric, {}).get("sanity")
    sub = f"{f.entity} {f.metric} {f.period_end}"
    if not rng:
        return ReconResult("single_source", "sanity", sub, "no sanity range registered")
    lo, hi = rng
    if lo <= f.value <= hi:
        return ReconResult("pass", "sanity", sub)
    return ReconResult("fail", "sanity", sub, f"{f.value} outside [{lo}, {hi}]")


def cross_source(a: FactProposal, b: FactProposal, rel_tol: float = DEFAULT_REL_TOL) -> ReconResult:
    """Two sources for the same entity, metric, period and basis (XBRL vs deck)."""
    sub = f"{a.entity} {a.metric} {a.period_end}"
    tol = max(
        rel_tol * max(abs(a.value), abs(b.value)),
        rounding_tolerance(a.value_as_written) if a.unit != "inr_cr" else 0.0,
        rounding_tolerance(b.value_as_written) if b.unit != "inr_cr" else 0.0,
    )
    if abs(a.value - b.value) <= tol:
        return ReconResult("pass", "cross_source", sub)
    return ReconResult("fail", "cross_source", sub, f"{a.value} ({a.extractor}) vs {b.value} ({b.extractor})")


def composition(parts: dict[str, float], as_fraction: bool = True, subject: str = "") -> ReconResult:
    total = sum(parts.values()) * (100.0 if as_fraction else 1.0)
    if abs(total - 100.0) <= COMPOSITION_TOL_PP:
        return ReconResult("pass", "composition", subject)
    return ReconResult("fail", "composition", subject, f"parts sum to {total:.2f}%")


def segment_sum(segments: Sequence[float], total: float, subject: str = "", rel_tol: float = 0.005) -> ReconResult:
    if total == 0:
        return ReconResult("single_source", "segment_sum", subject, "zero total")
    if abs(sum(segments) - total) <= rel_tol * abs(total):
        return ReconResult("pass", "segment_sum", subject)
    return ReconResult("fail", "segment_sum", subject, f"segments {sum(segments):.2f} vs total {total:.2f}")


@dataclass
class DatedFact:
    fact: FactProposal
    published: date  # exchange broadcast date of the document it came from
    rid: str | None = None


@dataclass
class RestatementOutcome:
    used: DatedFact
    superseded: list[DatedFact]
    results: list[ReconResult] = field(default_factory=list)
    regevent: dict[str, Any] | None = None


def detect_restatement(versions: Iterable[DatedFact], rel_tol: float = DEFAULT_REL_TOL) -> RestatementOutcome:
    """Same entity/metric/period/basis from documents published on different dates.

    Both FACTs are kept (caller writes both), the latest-published is used, and when the
    values differ beyond tolerance a ``restatement`` REGEVENT payload is returned."""
    vs = sorted(versions, key=lambda d: d.published)
    if not vs:
        raise ValueError("no versions")
    latest = vs[-1]
    older = vs[:-1]
    out = RestatementOutcome(used=latest, superseded=older)
    for o in older:
        if o.published == latest.published:
            continue
        r = cross_source(o.fact, latest.fact, rel_tol)
        if r.status == "fail":
            r = ReconResult(
                "fail",
                "restatement",
                r.subject,
                r.detail,
                restatement={
                    "metric": latest.fact.metric,
                    "period_end": latest.fact.period_end.isoformat(),
                    "old": o.fact.value,
                    "new": latest.fact.value,
                    "old_published": o.published.isoformat(),
                    "new_published": latest.published.isoformat(),
                },
            )
            out.results.append(r)
            out.regevent = {
                "event_type": "restatement",
                "entity": latest.fact.entity,
                "date": latest.published.isoformat(),
                "summary": (
                    f"{latest.fact.metric} for the quarter ending {latest.fact.period_end} "
                    f"was restated from {o.fact.value} to {latest.fact.value}"
                ),
                "old_rid": o.rid,
                "new_rid": latest.rid,
            }
    if not out.results:
        out.results.append(ReconResult("pass", "restatement", f"{latest.fact.entity} {latest.fact.metric}"))
    return out
