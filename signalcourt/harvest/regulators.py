"""Regulator events: RBI press releases (penalties, directions, restrictions) and IRDAI penalties
(implementation.md 7.5 T3). Monthly listings are crawled once and cached forever; matching is regex on
the title/body plus the generic entity-name normaliser. The listing URL pattern is [verify 12 Oct]."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from lxml import html

from ..resolve.entity import normalise_name

LISTING_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"  # monthly archive page [verify]
KIND_PATTERNS = (
    ("penalty", re.compile(r"monetary\s+penalty|imposes\s+penalty|penalty\s+on", re.I)),
    ("direction", re.compile(r"\bdirections?\b|supersession|cancels?\s+licen[cs]e", re.I)),
    ("restriction", re.compile(r"restrictions?|all[- ]inclusive\s+directions|moratorium", re.I)),
)


@dataclass
class ReleaseLink:
    title: str
    url: str
    date: date | None = None


@dataclass
class RegulatorEvent:
    kind: str  # penalty | direction | restriction
    entity_match: str
    title: str
    url: str
    date: date | None


def parse_listing(html_text: str, base: str = "https://www.rbi.org.in") -> list[ReleaseLink]:
    tree = html.fromstring(html_text)
    out: list[ReleaseLink] = []
    for a in tree.iter("a"):
        href = a.get("href") or ""
        if "pressrelease" in href.lower() and "prid" in href.lower():
            url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href
            out.append(ReleaseLink((a.text_content() or "").strip(), url))
    return out


def classify_release(title: str, body: str = "") -> str | None:
    text = title + " " + body[:2000]
    for kind, pat in KIND_PATTERNS:
        if pat.search(text):
            return kind
    return None


def events_for_entity(
    links: Iterable[ReleaseLink], legal_names: Iterable[str], body_of: Callable[[ReleaseLink], str] | None = None
) -> list[RegulatorEvent]:
    names = [normalise_name(n) for n in legal_names if normalise_name(n)]
    out: list[RegulatorEvent] = []
    for link in links:
        body = body_of(link) if body_of else ""
        hay = normalise_name(link.title + " " + body[:3000])
        hit = next((n for n in names if n and n in hay), None)
        if not hit:
            continue
        kind = classify_release(link.title, body)
        if kind:
            out.append(RegulatorEvent(kind, hit, link.title, link.url, link.date))
    return out
