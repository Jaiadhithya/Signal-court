"""Plain dataclasses that carry extraction output to the gates.

This module deliberately does not import ledger or store types: the controller adapts these to
FACT / SPAN / REGEVENT payloads and the quote gate verifies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class SpanProposal:
    kind: str  # prose | table_cell | xbrl | dataset_cell
    doc_sha256: str | None = None
    page: int | None = None
    text: str = ""  # exact text as written in the source
    row_label: str | None = None
    value_text: str | None = None
    xbrl: dict[str, Any] | None = None  # {element, context_id, unit, decimals}
    pid: str | None = None


@dataclass
class FactProposal:
    entity: str
    metric: str
    value: float
    unit: str  # registry unit: inr_cr | fraction | ratio | pp | inr_per_share | ...
    period_end: date
    basis: str  # standalone | consolidated
    extractor: str  # xbrl | table | llm | dataset
    value_as_written: str
    span: SpanProposal
    derived_from: str | None = None  # e.g. "ytd_to_quarter" when computed, never read directly
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventProposal:
    event_type: str  # penalty | direction | scheme | merger | ... (REGEVENT enum)
    entity: str | None
    date: date | None
    summary: str
    span: SpanProposal
