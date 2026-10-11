"""Namespace-agnostic XBRL instance parser (implementation.md 7.6).

Works on both exchange taxonomies (legacy banking/Ind-AS entry points and the
integrated-filing one) because facts are keyed by local name, never by prefix.

Quirk found in a real legacy bank file: the context called ``FourD`` carries the
quarter's dates in its ``<period>`` but holds year-to-date values; the true YTD
window is stated by the ``DateOfStartOfReportingPeriod`` / ``DateOfEndOfReportingPeriod``
facts filed *inside* that context. Those facts therefore override the context dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from lxml import etree

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"

# Contexts for segment/member detail look like OneReportableSegmentRevenue01D,
# FourOperatingExpenses01D, ...: a two-digit member index followed by D or I.
_MEMBER_CTX = re.compile(r"\d\d[DI]$")

# Some ratio facts are filed without a unitRef (e.g. the net NPA ratio); accept plain numbers.
_PLAIN_NUM = re.compile(r"^-?\d+(\.\d+)?$")

_START_FACT = "DateOfStartOfReportingPeriod"
_END_FACT = "DateOfEndOfReportingPeriod"


@dataclass
class Context:
    id: str
    start: date | None = None
    end: date | None = None
    instant: date | None = None
    has_dimension: bool = False
    # Effective dates after applying the in-context reporting-period facts.
    eff_start: date | None = None
    eff_end: date | None = None

    @property
    def is_instant(self) -> bool:
        return self.instant is not None

    @property
    def is_member(self) -> bool:
        return self.has_dimension or bool(_MEMBER_CTX.search(self.id))

    @property
    def days(self) -> int | None:
        if self.eff_start and self.eff_end:
            return (self.eff_end - self.eff_start).days + 1
        return None


@dataclass
class XbrlFact:
    name: str
    context_id: str
    unit: str | None
    decimals: str | None
    text: str
    value: float | None

    @property
    def is_numeric(self) -> bool:
        return self.value is not None


@dataclass
class XbrlDoc:
    schema_ref: str
    entry_point: str
    contexts: dict[str, Context]
    units: dict[str, str]
    facts: list[XbrlFact]
    _by_name: dict[str, list[XbrlFact]] = field(default_factory=dict, repr=False)

    def text_fact(self, name: str) -> str | None:
        for f in self._by_name.get(name, []):
            if f.text:
                return f.text
        return None

    def get(self, name: str, context_id: str) -> XbrlFact | None:
        for f in self._by_name.get(name, []):
            if f.context_id == context_id:
                return f
        return None

    def names(self) -> list[str]:
        return sorted(self._by_name)

    @property
    def result_type(self) -> str | None:
        return self.text_fact("ResultType")

    @property
    def nature(self) -> str | None:
        """'Standalone' or 'Consolidated' as filed."""
        return self.text_fact("NatureOfReportStandaloneConsolidated")

    @property
    def symbol(self) -> str | None:
        return self.text_fact("Symbol")

    @property
    def isin(self) -> str | None:
        return self.text_fact("ISIN")

    @property
    def company_name(self) -> str | None:
        return self.text_fact("NameOfTheCompany") or self.text_fact("NameOfBank")

    @property
    def audited(self) -> bool | None:
        v = self.text_fact("WhetherResultsAreAuditedOrUnaudited")
        if v is None:
            return None
        return v.strip().lower() == "audited"


@dataclass
class XbrlFactView:
    """A fact selected for a period, with the context that supplied it."""

    fact: XbrlFact
    context: Context


def _pdate(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _num(s: str) -> float | None:
    s = s.strip().replace(",", "")
    if not s:
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def parse(xml: bytes) -> XbrlDoc:
    root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, huge_tree=True))
    schema_ref = ""
    ref = root.find(f"{{{LINK}}}schemaRef")
    if ref is not None:
        schema_ref = ref.get(f"{{{XLINK}}}href") or ""
    entry_point = re.split(r"[\\/]", schema_ref)[-1] if schema_ref else ""

    contexts: dict[str, Context] = {}
    for c in root.findall(f"{{{XBRLI}}}context"):
        cid = c.get("id") or ""
        ctx = Context(id=cid)
        p = c.find(f"{{{XBRLI}}}period")
        if p is not None:
            inst = p.find(f"{{{XBRLI}}}instant")
            st = p.find(f"{{{XBRLI}}}startDate")
            en = p.find(f"{{{XBRLI}}}endDate")
            if inst is not None:
                ctx.instant = _pdate(inst.text)
            else:
                ctx.start = _pdate(st.text if st is not None else None)
                ctx.end = _pdate(en.text if en is not None else None)
        ent = c.find(f"{{{XBRLI}}}entity")
        if ent is not None and ent.find(f"{{{XBRLI}}}segment") is not None:
            ctx.has_dimension = True
        if c.find(f"{{{XBRLI}}}scenario") is not None:
            ctx.has_dimension = True
        ctx.eff_start, ctx.eff_end = ctx.start, ctx.end
        contexts[cid] = ctx

    units: dict[str, str] = {}
    for u in root.findall(f"{{{XBRLI}}}unit"):
        measures = [m.text.split(":")[-1] for m in u.iter(f"{{{XBRLI}}}measure") if m.text]
        units[u.get("id") or ""] = "/".join(measures) if measures else (u.get("id") or "")

    facts: list[XbrlFact] = []
    by_name: dict[str, list[XbrlFact]] = {}
    for el in root:
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if q.namespace in (XBRLI, LINK):
            continue
        cref = el.get("contextRef")
        if not cref:
            continue
        text = (el.text or "").strip()
        f = XbrlFact(
            name=q.localname,
            context_id=cref,
            unit=el.get("unitRef"),
            decimals=el.get("decimals"),
            text=text,
            value=_num(text) if (el.get("unitRef") or _PLAIN_NUM.match(text)) else None,
        )
        facts.append(f)
        by_name.setdefault(f.name, []).append(f)

    doc = XbrlDoc(
        schema_ref=schema_ref, entry_point=entry_point, contexts=contexts, units=units, facts=facts, _by_name=by_name
    )
    for f in by_name.get(_START_FACT, []):
        c = contexts.get(f.context_id)
        if c and _pdate(f.text):
            c.eff_start = _pdate(f.text)
    for f in by_name.get(_END_FACT, []):
        c = contexts.get(f.context_id)
        if c and _pdate(f.text):
            c.eff_end = _pdate(f.text)
    return doc


def period_end(doc: XbrlDoc) -> date | None:
    """Reporting-quarter end: the end of the shortest undimensioned duration context."""
    best: Context | None = None
    for c in doc.contexts.values():
        if c.is_instant or c.is_member or not c.eff_end or not c.days:
            continue
        if best is None or c.days < (best.days or 10**9):
            best = c
    return best.eff_end if best else None


def _main_durations(doc: XbrlDoc, end: date) -> list[Context]:
    return sorted(
        (c for c in doc.contexts.values() if not c.is_instant and not c.is_member and c.eff_end == end and c.days),
        key=lambda c: c.days or 0,
    )


def quarter_context(doc: XbrlDoc, end: date) -> Context | None:
    """3-month (75..105 day) undimensioned duration ending at ``end``."""
    for c in _main_durations(doc, end):
        if 75 <= (c.days or 0) <= 105:
            return c
    return None


def ytd_context(doc: XbrlDoc, end: date) -> Context | None:
    """Longest undimensioned duration ending at ``end`` and longer than a quarter."""
    ds = [c for c in _main_durations(doc, end) if (c.days or 0) > 105]
    return ds[-1] if ds else None


def instant_context(doc: XbrlDoc, end: date) -> Context | None:
    for c in doc.contexts.values():
        if c.is_instant and not c.is_member and c.instant == end:
            return c
    return None


def quarter_facts(doc: XbrlDoc, end: date) -> dict[str, XbrlFactView]:
    """Undimensioned facts for the quarter ending ``end``: 3-month duration or instant at ``end``."""
    out: dict[str, XbrlFactView] = {}
    qc, ic = quarter_context(doc, end), instant_context(doc, end)
    for f in doc.facts:
        c = doc.contexts.get(f.context_id)
        if c is None:
            continue
        if (qc and c.id == qc.id) or (ic and c.id == ic.id):
            out.setdefault(f.name, XbrlFactView(f, c))
    return out


def ytd_facts(doc: XbrlDoc, end: date) -> dict[str, XbrlFactView]:
    out: dict[str, XbrlFactView] = {}
    yc = ytd_context(doc, end)
    if yc is None:
        return out
    for f in doc.facts:
        if f.context_id == yc.id:
            out.setdefault(f.name, XbrlFactView(f, yc))
    return out


def ytd_to_quarter(ytd_now: float, ytd_prev: float | None) -> float:
    """Quarter value from two consecutive year-to-date values of the same fiscal year.

    Q1 has no previous YTD (``ytd_prev`` is None). The caller records a CALC with
    formula ``ytd_to_quarter`` and flags the derived fact."""
    return ytd_now - (ytd_prev or 0.0)


@dataclass
class SegmentFact:
    segment: str
    metric: str  # SegmentRevenue, SegmentProfitBeforeTax..., SegmentAssets, SegmentLiabilities
    value: float
    context_id: str


_SEGMENT_METRICS = (
    "SegmentRevenue",
    "SegmentProfitLossBeforeTaxAndFinanceCosts",
    "SegmentAssets",
    "SegmentLiabilities",
    "SegmentProfitBeforeTax",
)


def segment_facts(doc: XbrlDoc, end: date) -> list[SegmentFact]:
    """Per-segment values for the quarter. Each segment context also carries a
    ``DescriptionOfReportableSegment`` text fact naming the segment."""
    qc, ic = quarter_context(doc, end), instant_context(doc, end)
    out: list[SegmentFact] = []
    names: dict[str, str] = {}
    for f in doc.facts:
        if f.name in ("DescriptionOfReportableSegment", "DescriptionOfOtherExpenses") and f.text:
            names[f.context_id] = f.text
    for f in doc.facts:
        if f.name not in _SEGMENT_METRICS or f.value is None:
            continue
        c = doc.contexts.get(f.context_id)
        if c is None or not _MEMBER_CTX.search(c.id):
            continue
        if c.is_instant:
            ok = ic is not None and c.instant == end
        else:
            ok = c.eff_end == end and (c.days or 0) <= 105 and qc is not None
            if c.id.startswith("Four"):
                ok = False  # year-to-date copies of the same segment
        if not ok:
            continue
        # The liability facts sit in an ``...I`` context whose label fact is in the matching ``...D``.
        label = names.get(c.id) or names.get(c.id[:-1] + "D") or c.id
        out.append(SegmentFact(segment=label, metric=f.name, value=f.value, context_id=c.id))
    return out
