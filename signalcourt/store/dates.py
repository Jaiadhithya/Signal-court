"""Publish-date resolution, in order of trust."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from dateutil import parser as _dp

SOURCES = ("exchange_broadcast", "pdf_metadata", "page_text", "url", "none")

_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})?(\d{2})?")
_PAGE_DATE = re.compile(
    r"(?:\bdate[d]?\b\s*[:\-]?\s*|\bdated\b\s*)"
    r"(\d{1,2}[\s./-]+(?:[A-Za-z]{3,9}|\d{1,2})[\s./,-]+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_URL_DATE = re.compile(r"(?<!\d)(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])(?!\d)")
_URL_DATE_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)")


@dataclass(frozen=True)
class PublishDate:
    date: date | None
    source: str
    ts: str | None = None


def parse_pdf_date(s: str | None) -> date | None:
    if not s:
        return None
    m = _PDF_DATE.match(s.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def _parse_loose(s: str) -> date | None:
    try:
        return _dp.parse(s, dayfirst=True, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


def date_from_page_text(page1: str) -> date | None:
    m = _PAGE_DATE.search(page1[:3000])
    return _parse_loose(m.group(1)) if m else None


def date_from_url(url: str) -> date | None:
    m = _URL_DATE.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _URL_DATE_DMY.search(url)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def resolve_publish_date(
    *,
    broadcast_ts: str | None = None,
    pdf_creation: str | None = None,
    period_end: date | None = None,
    page1_text: str | None = None,
    url: str | None = None,
) -> PublishDate:
    """Order of trust: exchange broadcast; PDF metadata (only 0-120 days after period_end);
    a dated line on page 1; a date in the URL; else unknown."""
    if broadcast_ts:
        try:
            d = _dp.parse(broadcast_ts).date()
            return PublishDate(d, "exchange_broadcast", broadcast_ts)
        except (ValueError, OverflowError):
            pass
    pd_ = parse_pdf_date(pdf_creation)
    if pd_ and period_end and timedelta(0) <= (pd_ - period_end) <= timedelta(days=120):
        return PublishDate(pd_, "pdf_metadata")
    if page1_text:
        d = date_from_page_text(page1_text)
        if d:
            return PublishDate(d, "page_text")
    if url:
        d = date_from_url(url)
        if d:
            return PublishDate(d, "url")
    return PublishDate(None, "none")
