"""Indian fiscal-year quarter labels and arithmetic.

FY27 runs April 2026 to March 2027, so Q1FY27 = Apr-Jun 2026. A quarter is identified by
its label ("Q1FY27") or by an integer index (fy * 4 + q - 1) used for series arithmetic.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

_LABEL = re.compile(r"^Q([1-4])\s*FY\s*(\d{2}|\d{4})$", re.I)


def _fy_of(d: date) -> int:
    return d.year + 1 if d.month >= 4 else d.year


def _q_of(d: date) -> int:
    return ((d.month - 4) % 12) // 3 + 1


def index(label_or_date) -> int:
    """Integer quarter index; consecutive quarters differ by 1."""
    if isinstance(label_or_date, date):
        return _fy_of(label_or_date) * 4 + _q_of(label_or_date) - 1
    m = _LABEL.match(label_or_date.strip())
    if not m:
        raise ValueError(f"not a quarter label: {label_or_date!r}")
    fy = int(m.group(2))
    if fy < 100:
        fy += 2000
    return fy * 4 + int(m.group(1)) - 1


def from_index(i: int) -> str:
    fy, q = divmod(i, 4)
    return f"Q{q + 1}FY{fy % 100:02d}"


def label(d: date) -> str:
    """Quarter label of the quarter containing ``d`` (normally its end date)."""
    return from_index(index(d))


def end(lbl: str) -> date:
    fy, q = divmod(index(lbl), 4)
    month = 6 + 3 * q  # Q1 -> Jun, Q2 -> Sep, Q3 -> Dec, Q4 -> Mar of next calendar year
    year = fy - 1 if month <= 12 else fy
    if month > 12:
        month -= 12
        year = fy
    return date(year, month, calendar.monthrange(year, month)[1])


def start(lbl: str) -> date:
    e = end(lbl)
    m = e.month - 2
    y = e.year
    if m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


def shift(lbl: str, n: int) -> str:
    return from_index(index(lbl) + n)


def span(first: str, last: str) -> list[str]:
    return [from_index(i) for i in range(index(first), index(last) + 1)]


def last_n(latest: str, n: int) -> list[str]:
    return span(shift(latest, -(n - 1)), latest)


def is_quarter_end(d: date) -> bool:
    return d.month in (3, 6, 9, 12) and d.day == calendar.monthrange(d.year, d.month)[1]


def fy_start_quarter(lbl: str) -> str:
    """Q1 of the fiscal year containing ``lbl`` (used for year-to-date arithmetic)."""
    fy, _ = divmod(index(lbl), 4)
    return from_index(fy * 4)
