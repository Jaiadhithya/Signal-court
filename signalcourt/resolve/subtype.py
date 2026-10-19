"""Sub-type from filing format + segment note. Never by name.

Order: insurer (insurance format, or L-/NL-form disclosures) -> holding (consolidated segment
note has both a lending-type and an insurance-type segment, or a CIC phrase) -> bank / nbfc /
generic from the XBRL entry point -> generic. The decision and its evidence go on the ENTITY record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..extract.registry import load_yaml


@dataclass
class SubtypeEvidence:
    entry_point: str = ""
    result_type: str = ""
    xbrl_url: str = ""
    insurance_type: str = ""  # legacy insurer listing field, e.g. "Life-Insurance"
    announcement_texts: list[str] = field(default_factory=list)  # descriptions, for L-/NL-form markers
    segments: list[str] = field(default_factory=list)  # consolidated segment labels
    text_snippets: list[str] = field(default_factory=list)  # e.g. page-1 text of annual report
    consolidated: bool = False


@dataclass
class SubtypeDecision:
    subtype: str
    rule: str
    evidence: dict[str, Any]


def _hay(ev: SubtypeEvidence) -> str:
    return " ".join([ev.entry_point, ev.result_type, ev.xbrl_url, ev.insurance_type]).lower()


def _classify_segment(label: str, lex: dict[str, list[str]]) -> str | None:
    low = label.lower()
    if any(t in low for t in lex["insurance"]):
        return "insurance"
    if any(t in low for t in lex["lending"]):
        return "lending"
    return None


def classify_subtype(ev: SubtypeEvidence) -> SubtypeDecision:
    cfg = load_yaml("subtypes.yaml")
    hay = _hay(ev)
    rules = cfg["format_rules"]
    by_type = {r["subtype"]: r for r in rules}
    base = None
    # Specific insurer/nbfc/bank tokens first; generic Ind-AS last.
    for st in ("insurer_life", "insurer_general", "insurer", "bank", "nbfc"):
        r = by_type.get(st)
        if r and any(t in hay for t in r["tokens"]):
            base = st
            break
    if base is None and any(m in " ".join(ev.announcement_texts).lower() for m in cfg["insurer_form_markers"]):
        base = "insurer"
    if base == "insurer":
        txt = (hay + " " + " ".join(ev.announcement_texts)).lower()
        base = "insurer_general" if ("nl-form" in txt or "general" in txt) and "life" not in txt else "insurer_life"
    # Holding test applies to non-insurer, non-bank filers with a consolidated segment note.
    if base in (None, "nbfc"):
        kinds = {_classify_segment(s, cfg["segment_lexicon"]) for s in ev.segments}
        cic = any(p in " ".join(ev.text_snippets).lower() for p in cfg["cic_phrases"])
        if ("lending" in kinds and "insurance" in kinds) or cic:
            return SubtypeDecision(
                "holding", "segment_note_or_cic", {"segments": ev.segments, "cic": cic, "entry_point": ev.entry_point}
            )
    if base:
        return SubtypeDecision(base, "format", {"entry_point": ev.entry_point, "result_type": ev.result_type})
    gen = by_type["generic"]
    if any(t in hay for t in gen["tokens"]):
        return SubtypeDecision("generic", "format", {"entry_point": ev.entry_point})
    return SubtypeDecision("generic", "default", {"entry_point": ev.entry_point})
