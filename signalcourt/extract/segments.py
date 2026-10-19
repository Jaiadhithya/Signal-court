"""Segment note parsing for holding companies and segment-mix series."""

from __future__ import annotations

from datetime import date

from ..extract.registry import load_yaml
from ..resolve.subtype import _classify_segment
from . import xbrl as X


def segment_labels(doc: X.XbrlDoc, end: date) -> list[str]:
    return sorted({s.segment for s in X.segment_facts(doc, end) if s.metric == "SegmentRevenue"})


def segment_revenue_mix(doc: X.XbrlDoc, end: date) -> dict[str, float]:
    """Share of segment revenue by segment label (fractions summing to 1)."""
    rev = {s.segment: s.value for s in X.segment_facts(doc, end) if s.metric == "SegmentRevenue"}
    tot = sum(rev.values())
    return {k: v / tot for k, v in rev.items()} if tot else {}


def typed_segments(labels: list[str]) -> dict[str, str | None]:
    lex = load_yaml("subtypes.yaml")["segment_lexicon"]
    return {lab: _classify_segment(lab, lex) for lab in labels}
