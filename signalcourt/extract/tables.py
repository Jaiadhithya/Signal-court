"""Deck table extraction (implementation.md 7.6): period headers, Indian number formats, label matching.

Works on page text lines (from PyMuPDF/pdfplumber word-line reconstruction), so it is testable without
PDFs. Candidate pages are chosen elsewhere by BM25 over metric synonyms. Output is FACT proposals with
``table_cell`` spans: the row label and the value text on the same line, which the quote gate re-finds.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

from ..calc import periods as P
from .registry import metrics
from .types import FactProposal, SpanProposal

LABEL_MATCH_MIN = 90

_NUM = re.compile(
    r"""
    (?P<neg_open>\()?\s*
    (?P<sign>[-−–])?\s*
    (?P<cur>[₹`]|rs\.?|inr)?\s*
    (?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*(?P<unit>%|bps|bp|cr(?:ore)?s?|bn|billion|mn|million|lakhs?|x)?
    \s*(?P<neg_close>\))?
""",
    re.I | re.X,
)

_MONTHS = {
    m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)
}

_QFY = re.compile(r"\b(?:Q\s*([1-4])|([1-4])\s*Q)\s*[-']?\s*FY\s*[-']?\s*(\d{4}|\d{2})\b", re.I)
_MONYY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-'.]*(\d{4}|\d{2})\b", re.I)
_DMY = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")
_CMP = re.compile(r"\b(yoy|y-o-y|qoq|q-o-q|growth|change|chg)\b", re.I)


@dataclass
class ParsedNumber:
    value: float
    unit: str  # plain | percent | bps | crore | billion | million | lakh | multiple
    text: str


def parse_number(text: str) -> ParsedNumber | None:
    """Parse the first number in ``text``. Indian grouping, brackets and minus signs are negatives."""
    m = _NUM.search(text)
    if not m:
        return None
    v = float(m.group("num").replace(",", ""))
    if m.group("sign") or (m.group("neg_open") and m.group("neg_close")):
        v = -v
    u = (m.group("unit") or "").lower()
    kind = {
        "%": "percent",
        "bps": "bps",
        "bp": "bps",
        "bn": "billion",
        "billion": "billion",
        "mn": "million",
        "million": "million",
        "x": "multiple",
    }.get(u)
    if kind is None:
        kind = "crore" if u.startswith("cr") else ("lakh" if u.startswith("lakh") else "plain")
    return ParsedNumber(v, kind, m.group(0).strip())


def to_registry_unit(p: ParsedNumber, metric_unit: str, page_hint: str | None = None) -> float | None:
    """Convert a displayed number to the metric's registry unit; None if the unit cannot be reconciled."""
    kind = p.unit
    if metric_unit == "inr_cr":
        if kind == "plain":
            kind = page_hint or "crore"  # decks state the unit once in a page caption
        if kind == "crore":
            return p.value
        if kind == "lakh":
            return p.value / 100.0
        if kind == "billion":
            return p.value * 100.0
        if kind == "million":
            return p.value / 10.0
        return None
    if metric_unit in ("fraction",):
        if kind == "percent":
            return p.value / 100.0
        if kind == "bps":
            return p.value / 10000.0
        return None
    if metric_unit in ("ratio", "inr_per_share", "pp"):
        if kind in ("plain", "multiple", "percent"):
            return p.value
    return None


def detect_unit_hint(page_text: str) -> str | None:
    t = page_text.lower()
    m = re.search(r"(?:₹|rs\.?|inr)\s*(?:in\s*)?(crore|cr|billion|bn|million|mn|lakh)", t) or re.search(
        r"\bin\s*(?:₹|rs\.?|inr)?\s*(crore|cr|billion|bn|million|mn|lakh)", t
    )
    if not m:
        return None
    return {
        "crore": "crore",
        "cr": "crore",
        "billion": "billion",
        "bn": "billion",
        "million": "million",
        "mn": "million",
        "lakh": "lakh",
    }[m.group(1)]


def parse_period_label(header: str) -> str | None:
    """Quarter label for a column header, or None (YoY/QoQ columns, unknown text)."""
    h = header.strip()
    m = _QFY.search(h)
    if m:
        q = int(m.group(1) or m.group(2))
        fy = m.group(3)
        fy = int(fy) + (2000 if len(fy) == 2 else 0)
        return f"Q{q}FY{fy % 100:02d}"
    m = _DMY.search(h)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        try:
            dt = date(y, mo, d)
        except ValueError:
            return None
        return P.label(dt) if P.is_quarter_end(dt) else None
    m = _MONYY.search(h)
    if m:
        mo, y = _MONTHS[m.group(1).lower()], int(m.group(2))
        y += 2000 if y < 100 else 0
        if mo in (3, 6, 9, 12):
            return P.label(date(y, mo, 1))
    return None


def _period_tokens(line: str) -> list[tuple[int, str | None]]:
    """(position, label-or-None) for every period/comparison token in a header line, left to right."""
    toks: list[tuple[int, str | None]] = []
    spans: list[tuple[int, int]] = []
    for rx in (_QFY, _DMY, _MONYY):
        for m in rx.finditer(line):
            if any(s <= m.start() < e for s, e in spans):
                continue
            lbl = parse_period_label(m.group(0))
            spans.append((m.start(), m.end()))
            toks.append((m.start(), lbl))
    for m in _CMP.finditer(line):
        toks.append((m.start(), None))
    toks.sort()
    return toks


@dataclass
class TableFact:
    metric: str
    period_label: str
    parsed: ParsedNumber
    row_label: str
    line: str
    page: int
    score: float


def _synonym_index() -> list[tuple[str, str, str]]:
    out = []
    for m in metrics():
        for s in m.get("synonyms", []):
            out.append((s.lower(), m["id"], m["unit"]))
    return out


def match_label(label: str, subtype: str | None = None) -> tuple[str, str, float] | None:
    """(metric id, unit, score) for a row label, fuzzy >= 90 against registry synonyms."""
    lab = re.sub(r"[^a-z0-9%&/ \-]+", " ", label.lower()).strip()
    lab = re.sub(r"\s+", " ", lab)
    if not lab:
        return None
    allowed = None
    if subtype:
        allowed = {m["id"] for m in metrics() if subtype in m.get("subtypes", [])}
    best = None
    for syn, mid, unit in _synonym_index():
        if allowed is not None and mid not in allowed:
            continue
        sc = 100.0 if lab == syn else fuzz.ratio(lab, syn)
        if sc >= LABEL_MATCH_MIN and (best is None or sc > best[2]):
            best = (mid, unit, sc)
    return best


def _split_row(line: str) -> tuple[str, list[str]] | None:
    """Row label (text before the first number) and the numeric cell strings after it."""
    first = None
    for m in re.finditer(r"(?<![A-Za-z])[-−–(]?\s*(?:[₹`]\s*)?\d", line):
        first = m.start()
        break
    if first is None or first < 3:
        return None
    label = line[:first].strip(" :|\t")
    cells = [c.group(0).strip() for c in _NUM.finditer(line[first:]) if c.group(0).strip()]
    if not label or not cells:
        return None
    return label, cells


def extract_from_lines(
    lines: list[str], page: int = 1, subtype: str | None = None, unit_hint: str | None = None
) -> list[TableFact]:
    """Header line with >= 2 period tokens sets the columns; following rows are matched to metrics."""
    out: list[TableFact] = []
    cols: list[str | None] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        toks = _period_tokens(s)
        if len([1 for _, lab in toks if lab]) >= 2 or (len(toks) >= 2 and any(lab for _, lab in toks)):
            cols = [lab for _, lab in toks]
            continue
        if not cols:
            continue
        row = _split_row(s)
        if not row:
            continue
        label, cells = row
        hit = match_label(label, subtype)
        if not hit:
            continue
        mid, mun, sc = hit
        if len(cells) < len([c for c in cols]):
            continue  # ragged row: do not guess the alignment
        for lab, cell in zip(cols, cells, strict=False):
            if lab is None:
                continue
            p = parse_number(cell)
            if p is None:
                continue
            out.append(TableFact(mid, lab, p, label, s, page, sc))
    return out


def to_fact_proposals(
    tf: Iterable[TableFact], entity: str, basis: str, doc_sha: str | None, unit_hint: str | None = None
) -> list[FactProposal]:
    units = {m["id"]: m["unit"] for m in metrics()}
    out: list[FactProposal] = []
    for t in tf:
        mu = units.get(t.metric)
        v = to_registry_unit(t.parsed, mu, unit_hint) if mu else None
        if v is None:
            continue
        out.append(
            FactProposal(
                entity=entity,
                metric=t.metric,
                value=v,
                unit=mu,
                period_end=P.end(t.period_label),
                basis=basis,
                extractor="table",
                value_as_written=t.parsed.text,
                span=SpanProposal(
                    kind="table_cell",
                    doc_sha256=doc_sha,
                    page=t.page,
                    text=t.line,
                    row_label=t.row_label,
                    value_text=t.parsed.text,
                ),
                notes={"label_score": t.score, "unit_hint": unit_hint},
            )
        )
    return out


def page_lines(pdf_path: str, page_no: int) -> list[str]:
    """Reconstruct text lines from word positions with pdfplumber (imported lazily)."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no - 1]
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 3), []).append(w)
    return [" ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"])) for _, ws in sorted(rows.items())]
