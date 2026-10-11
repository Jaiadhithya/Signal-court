"""Announcements -> typed documents (implementation.md 7.5, announcement_kinds.yaml)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..extract.registry import load_yaml

_DATE_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d %b %Y %H:%M:%S",
)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


@dataclass
class ClassifiedAnnouncement:
    symbol: str
    kind: str
    doc_type: str
    fetch: bool
    event_type: str | None
    broadcast: datetime | None
    desc: str
    text: str
    url: str | None


def classify(row: dict[str, Any], symbol: str) -> ClassifiedAnnouncement:
    desc = str(row.get("desc") or row.get("subject") or "")
    text = str(row.get("attchmntText") or row.get("body") or "")
    url = row.get("attchmntFile") or row.get("attachment") or None
    if url and not str(url).lower().startswith("http"):
        url = None
    d, t = desc.lower(), text.lower()
    for rule in load_yaml("announcement_kinds.yaml"):
        if any(p in d for p in rule["desc"]) or any(p in t for p in rule.get("text", [])):
            # A transcript is filed under a generic call-update description; trust text for that one.
            if rule["kind"] == "deck" and "transcript" in t:
                continue
            return ClassifiedAnnouncement(
                symbol,
                rule["kind"],
                rule["doc_type"],
                bool(rule["fetch"]),
                rule.get("event_type"),
                parse_dt(row.get("an_dt") or row.get("sort_date")),
                desc,
                text,
                url,
            )
    return ClassifiedAnnouncement(
        symbol, "other", "other", False, None, parse_dt(row.get("an_dt") or row.get("sort_date")), desc, text, url
    )


def classify_all(rows: Iterable[dict[str, Any]], symbol: str) -> list[ClassifiedAnnouncement]:
    return [classify(r, symbol) for r in rows]


def counts_by_kind(items: Iterable[ClassifiedAnnouncement]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i.kind] = out.get(i.kind, 0) + 1
    return out
