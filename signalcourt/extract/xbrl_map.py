"""XBRL element -> metric mapping through registry/metrics.yaml (implementation.md 5.2, 7.6).

Monetary values are full rupees in the files (the declared rounding level of crore does
not change that), ratios are fractions. Conversion to the registry unit happens here and
nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from . import xbrl as X
from .registry import metrics
from .types import FactProposal, SpanProposal

CRORE = 1.0e7


def to_registry_unit(value: float, unit: str) -> float:
    if unit == "inr_cr":
        return value / CRORE
    return value


def format_as_written(f: X.XbrlFact) -> str:
    return f.text


def _basis(doc: X.XbrlDoc) -> str:
    return "consolidated" if (doc.nature or "").strip().lower().startswith("consol") else "standalone"


def to_fact_proposals(
    doc: X.XbrlDoc,
    entity: str,
    subtype: str,
    end: date | None = None,
    basis: str | None = None,
    metric_ids: Iterable[str] | None = None,
) -> list[FactProposal]:
    """One FACT proposal per mapped metric for the quarter ending ``end``.

    Duration metrics are read from the 3-month context, balance-sheet metrics from the
    instant at ``end``. If a duration metric exists only as year-to-date (no quarter
    context) it is *not* emitted here; the panel builder derives it through a CALC.
    """
    end = end or X.period_end(doc)
    if end is None:
        return []
    basis = basis or _basis(doc)
    qf = X.quarter_facts(doc, end)
    want = set(metric_ids) if metric_ids else None
    out: list[FactProposal] = []
    for m in metrics():
        if subtype not in m.get("subtypes", []):
            continue
        if want is not None and m["id"] not in want:
            continue
        hit = None
        for name in m.get("xbrl", []):
            v = qf.get(name)
            if v is None or v.fact.value is None:
                continue
            if bool(m.get("instant")) != v.context.is_instant:
                continue
            hit = v
            break
        if hit is None:
            continue
        f = hit.fact
        out.append(
            FactProposal(
                entity=entity,
                metric=m["id"],
                value=to_registry_unit(f.value, m["unit"]),
                unit=m["unit"],
                period_end=end,
                basis=basis,
                extractor="xbrl",
                value_as_written=f.text,
                span=SpanProposal(
                    kind="xbrl",
                    text=f.text,
                    value_text=f.text,
                    xbrl={"element": f.name, "context_id": f.context_id, "unit": f.unit, "decimals": f.decimals},
                ),
                notes={"status": m.get("status", "verify")},
            )
        )
    return out


def unmapped_numeric(doc: X.XbrlDoc, end: date | None = None) -> list[tuple[str, str, float]]:
    """Numeric facts in the quarter that no registry metric claims, for the tag-dump task."""
    end = end or X.period_end(doc)
    if end is None:
        return []
    claimed = {n for m in metrics() for n in m.get("xbrl", [])}
    return sorted(
        (n, v.context.id, v.fact.value)
        for n, v in X.quarter_facts(doc, end).items()
        if v.fact.value is not None and n not in claimed
    )


def ytd_values(doc: X.XbrlDoc, subtype: str, end: date | None = None) -> dict[str, tuple[float, str, str, str]]:
    """Year-to-date duration metrics: metric -> (value in registry unit, text, element, context id).

    Used only to derive a quarter that has no 3-month context (``calc`` records ytd_to_quarter)."""
    end = end or X.period_end(doc)
    out: dict[str, tuple[float, str, str, str]] = {}
    if end is None:
        return out
    yf = X.ytd_facts(doc, end)
    for m in metrics():
        if subtype not in m.get("subtypes", []) or m.get("instant"):
            continue
        if m["unit"] not in ("inr_cr", "inr_per_share"):  # ratios are not additive across periods
            continue
        for name in m.get("xbrl", []):
            v = yf.get(name)
            if v is not None and v.fact.value is not None:
                out[m["id"]] = (to_registry_unit(v.fact.value, m["unit"]), v.fact.text, name, v.context.id)
                break
    return out
