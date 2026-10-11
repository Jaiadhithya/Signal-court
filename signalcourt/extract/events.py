"""REGEVENT proposals from announcements and regulator releases (implementation.md 7.6)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..harvest.announcements import ClassifiedAnnouncement
from ..harvest.regulators import RegulatorEvent
from .types import EventProposal, SpanProposal

_SCHEME_SUB = (
    ("demerger", re.compile(r"demerger|de-merger", re.I)),
    ("merger", re.compile(r"merger|amalgamation|scheme of arrangement", re.I)),
    ("acquisition", re.compile(r"acqui", re.I)),
    ("scheme", re.compile(r"scheme", re.I)),
)


def _scheme_type(text: str) -> str:
    for name, rx in _SCHEME_SUB:
        if rx.search(text):
            return "merger" if name == "demerger" else name
    return "scheme"


def from_announcement(it: ClassifiedAnnouncement, doc_sha: str | None = None) -> EventProposal | None:
    et = it.event_type
    if it.kind == "rating_letter":
        et = "rating_action"
    if not et:
        return None
    if et == "scheme":
        et = _scheme_type(it.desc + " " + it.text)
    d = it.broadcast.date() if it.broadcast else None
    quote = it.text.strip() or it.desc
    return EventProposal(
        event_type=et,
        entity=it.symbol,
        date=d,
        summary=it.desc.strip()[:300],
        span=SpanProposal(kind="prose", doc_sha256=doc_sha, text=quote[:400]),
    )


def from_regulator(ev: RegulatorEvent, entity: str, doc_sha: str | None = None) -> EventProposal:
    et = {"penalty": "penalty", "direction": "direction", "restriction": "direction"}[ev.kind]
    return EventProposal(
        event_type=et,
        entity=entity,
        date=ev.date,
        summary=ev.title[:300],
        span=SpanProposal(kind="prose", doc_sha256=doc_sha, text=ev.title),
    )


def all_events(items: Iterable[ClassifiedAnnouncement], shas: dict | None = None) -> list[EventProposal]:
    out: list[EventProposal] = []
    for it in items:
        e = from_announcement(it, (shas or {}).get(it.url))
        if e:
            out.append(e)
    return out
