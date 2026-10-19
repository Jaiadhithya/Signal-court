"""Unbacked-number check.

Every number token in free text (digits with optional %, bps, cr, x, bn) must equal a cited
CALC or FACT value after unit conversion and display rounding (+-1 in the last shown digit).
Dates, years, quarter labels and record ids are exempt. Agents get numbers only by CALC request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_RID = re.compile(r"\b[A-Z]{2}-[0-9a-f]{8,64}\b")
_PID = re.compile(r"\b[0-9a-f]{10}-p\d{4}-\d{3}\b")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_PATTERNS = [
    re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.](?:19|20)?\d{2}\b"),
    re.compile(rf"\b(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[\s-]+{_MONTH}\.?,?(?:[\s-]+(?:19|20)?\d{{2}})?\b", re.I),
    re.compile(rf"\b{_MONTH}\.?\s+(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?(?:\s+(?:19|20)\d{{2}})?\b", re.I),
    re.compile(rf"\b{_MONTH}\.?[-\s]+(?:19|20)?\d{{2}}\b", re.I),
]
_QUARTER = re.compile(
    r"\b(?:Q[1-4]\s*(?:FY)?\s*(?:20)?\d{2}|(?:Q[1-4]|H[12])\b|FY\s?(?:20)?\d{2}(?:-\d{2})?|\d{1,2}Q\s?FY?\d{2})", re.I
)
_YEAR = re.compile(r"(?<![\d.,])(?:19|20)\d{2}(?![\d.,%]|\s?(?:cr|bps|bn|x)\b)")
_ORDINAL = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I)

# number with optional unit suffix; the unit must not run into a following word
_TOKEN = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>%|bps\b|crores?\b|cr\b|bn\b|pp\b|x\b)?", re.I
)


@dataclass(frozen=True)
class CitedValue:
    """A value from a cited CALC or FACT. unit: inr_cr | fraction | pp | ratio | z | count | same."""

    rid: str
    value: float
    unit: str


@dataclass(frozen=True)
class Unbacked:
    token: str
    shown: float
    unit: str | None
    position: int


def _decimals(num: str) -> int:
    return len(num.split(".", 1)[1]) if "." in num else 0


def _candidates(v: float, cited_unit: str, shown_unit: str | None) -> list[float]:
    """Display-scale values the cited number may legitimately be shown as."""
    u = (shown_unit or "").lower()
    a = abs(v)
    if u == "%":
        out = [a * 100] if cited_unit == "fraction" else [a]
    elif u == "bps":
        out = [a * 10000] if cited_unit == "fraction" else [a * 100] if cited_unit == "pp" else [a]
    elif u == "pp":
        out = [a * 100] if cited_unit == "fraction" else [a]
    elif u in ("cr", "crore", "crores"):
        out = [a]
    elif u == "bn":
        out = [a / 100.0] if cited_unit == "inr_cr" else [a]
    elif u == "x":
        out = [a]
    else:
        out = [a]
        if cited_unit == "fraction":
            out.append(a * 100)  # "2.4" for a 2.4% already named as a percentage elsewhere in the sentence
    return out


def exempt_spans(text: str) -> str:
    """Blank out ids, dates, quarter labels, years and ordinals before scanning for numbers."""
    t = _RID.sub(lambda m: " " * len(m.group()), text)
    t = _PID.sub(lambda m: " " * len(m.group()), t)
    for pat in _DATE_PATTERNS:
        t = pat.sub(lambda m: " " * len(m.group()), t)
    t = _QUARTER.sub(lambda m: " " * len(m.group()), t)
    t = _ORDINAL.sub(lambda m: " " * len(m.group()), t)
    t = _YEAR.sub(lambda m: " " * len(m.group()), t)
    return t


def number_tokens(text: str) -> list[tuple[str, float, str | None, int, int]]:
    """(token text, shown value, unit, decimals, position) for every non-exempt number."""
    t = exempt_spans(text)
    out = []
    for m in _TOKEN.finditer(t):
        num = m.group("num")
        shown = float(num.replace(",", ""))
        unit = m.group("unit")
        out.append((m.group(0).strip(), shown, unit.lower() if unit else None, _decimals(num), m.start()))
    return out


def find_unbacked(text: str, cited: Iterable[CitedValue], *, exempt_small_counts: bool = False) -> list[Unbacked]:
    """Number tokens in `text` that no cited value explains. Empty list = text is backed."""
    cited = list(cited)
    bad: list[Unbacked] = []
    for token, shown, unit, dec, pos in number_tokens(text):
        if exempt_small_counts and unit is None and dec == 0 and 0 <= shown <= 10:
            continue
        tol = 10.0 ** (-dec) + 1e-9  # +-1 in the last shown digit
        ok = False
        for cv in cited:
            if any(abs(shown - c) <= tol for c in _candidates(cv.value, cv.unit, unit)):
                ok = True
                break
        if not ok:
            bad.append(Unbacked(token, shown, unit, pos))
    return bad


def cited_values_from_ledger(ledger, rids: Sequence[str]) -> list[CitedValue]:
    """CitedValues from the FACT and CALC records named in `rids` (other types are ignored)."""
    out: list[CitedValue] = []
    for rid in rids:
        try:
            env = ledger.get(rid)
        except KeyError:
            continue
        if env.type.value in ("FACT", "CALC") and env.payload.get("value") is not None:
            out.append(CitedValue(rid, float(env.payload["value"]), env.payload.get("unit", "same")))
    return out
