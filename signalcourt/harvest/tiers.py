"""Harvest plan per run. Idempotent: a URL already in the store is not re-downloaded.

T1 results XBRL (legacy + integrated)  | T2 announcements + annual reports | T3 ratings + regulator events.
T3b (site-restricted search) and T4 (datasets, Wayback) belong to the tools layer.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..net.http import Blocked
from . import announcements as A
from . import filings as Fl
from . import ratings as R
from .ports import DocStorePort

TIER_ORDER = ("T1", "T2", "T3")
SOURCE_ID = {
    "results_xbrl": "exchange_results_xbrl",
    "annual_report": "exchange_annual_reports",
    "announcement": "exchange_announcements",
    "rating": "exchange_credit_rating_feed",
}
FAMILY = {
    "results_xbrl": "exchange_xbrl",
    "annual_report": "company_filing",
    "announcement": "company_filing",
    "rating": "rating_agency",
}


@dataclass
class EntityRef:
    symbol: str
    issuer: str  # exact legal name for the integrated-filing endpoint
    subtype: str
    isin: str | None = None
    legal_names: list[str] = field(default_factory=list)


@dataclass
class HarvestReport:
    by_doc_type: dict[str, int] = field(default_factory=dict)
    by_family: dict[str, int] = field(default_factory=dict)
    new_files: int = 0
    known_files: int = 0
    time_gate_dropped: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_tiers: list[str] = field(default_factory=list)
    parsed: list[Fl.ParsedFiling] = field(default_factory=list)
    classified: list[A.ClassifiedAnnouncement] = field(default_factory=list)

    def count(self, doc_type: str, family: str, new: bool) -> None:
        self.by_doc_type[doc_type] = self.by_doc_type.get(doc_type, 0) + 1
        self.by_family[family] = self.by_family.get(family, 0) + 1
        if new:
            self.new_files += 1
        else:
            self.known_files += 1


class _Budget:
    def __init__(self, cap_s: float, clock: Callable[[], float]) -> None:
        self.cap, self.clock, self.t0 = cap_s, clock, clock()

    def exceeded(self) -> bool:
        return self.clock() - self.t0 > self.cap


def _download(
    exch,
    store: DocStorePort,
    rep: HarvestReport,
    url: str,
    *,
    doc_type: str,
    kind: str,
    symbol: str,
    period_end: date | None,
    published: datetime | None,
    title: str | None = None,
    mime: str = "application/octet-stream",
) -> str | None:
    sha = store.known_url(url)
    if sha:
        rep.count(doc_type, FAMILY[kind], False)
        return sha
    try:
        r = exch.download(url)
    except Blocked as e:
        rep.errors.append(f"blocked: {e}")
        return None
    except Exception as e:
        rep.errors.append(f"download {url}: {type(e).__name__}")
        return None
    meta = store.put_bytes(
        r.content,
        url=url,
        final_url=r.url,
        mime=mime,
        source_id=SOURCE_ID[kind],
        source_family=FAMILY[kind],
        doc_type=doc_type,
        entity_symbol=symbol,
        period_end=period_end,
        publish_date=published.date() if published else None,
        publish_date_source="exchange_broadcast" if published else "none",
        publish_ts=published.isoformat() if published else None,
        title=title,
    )
    rep.count(doc_type, FAMILY[kind], True)
    return meta.sha256


def harvest_t1(
    exch, store: DocStorePort, ent: EntityRef, as_of: date, rep: HarvestReport, quarters: int, budget: _Budget
) -> None:
    try:
        listing = Fl.list_filings(
            exch, ent.symbol, ent.issuer, legacy_index="insurance" if ent.subtype.startswith("insurer") else "equities"
        )
    except Fl.IssuerMismatch as e:
        rep.errors.append(str(e))
        return
    horizon = as_of - timedelta(days=int(91.3 * (quarters + 1)))
    for f in listing:
        if budget.exceeded():
            rep.errors.append("time cap hit during T1")
            return
        if not f.xbrl_url or (f.period_end and f.period_end < horizon):
            continue
        if f.broadcast and f.broadcast.date() > as_of:
            rep.time_gate_dropped += 1
            continue
        sha = _download(
            exch,
            store,
            rep,
            f.xbrl_url,
            doc_type="results_xbrl",
            kind="results_xbrl",
            symbol=ent.symbol,
            period_end=f.period_end,
            published=f.broadcast,
            mime="application/xml",
        )
        if not sha:
            continue
        try:
            rep.parsed.append(Fl.parse_filing(f, store.read_bytes(sha), ent.subtype, sha))
        except Exception as e:
            rep.errors.append(f"parse {f.xbrl_url}: {type(e).__name__}")
        if f.pdf_url and (f.source == "integrated"):
            _download(
                exch,
                store,
                rep,
                f.pdf_url,
                doc_type="results_pdf",
                kind="announcement",
                symbol=ent.symbol,
                period_end=f.period_end,
                published=f.broadcast,
                mime="application/pdf",
            )


def harvest_t2(
    exch, store: DocStorePort, ent: EntityRef, as_of: date, rep: HarvestReport, years: int, budget: _Budget
) -> None:
    frm = date(as_of.year - years, as_of.month, 1)
    rows: list[dict[str, Any]] = []
    cur = frm
    while cur <= as_of:  # one request per half year keeps responses bounded
        nxt = min(date(cur.year + (cur.month > 6), (cur.month + 6 - 1) % 12 + 1, 1) - timedelta(days=1), as_of)
        try:
            rows.extend(exch.announcements(ent.symbol, cur, nxt))
        except Exception as e:
            rep.errors.append(f"announcements {cur}..{nxt}: {type(e).__name__}")
        cur = nxt + timedelta(days=1)
    items = A.classify_all(rows, ent.symbol)
    rep.classified = items
    for it in items:
        if budget.exceeded():
            rep.errors.append("time cap hit during T2")
            return
        if not it.fetch or not it.url:
            continue
        if it.broadcast and it.broadcast.date() > as_of:
            rep.time_gate_dropped += 1
            continue
        _download(
            exch,
            store,
            rep,
            it.url,
            doc_type=it.doc_type,
            kind="announcement",
            symbol=ent.symbol,
            period_end=None,
            published=it.broadcast,
            title=it.desc[:200],
            mime="application/pdf",
        )
    try:
        for url, d, lbl in Fl.annual_report_urls(exch.annual_reports(ent.symbol)):
            if budget.exceeded():
                return
            if d and d > as_of:
                rep.time_gate_dropped += 1
                continue
            _download(
                exch,
                store,
                rep,
                url,
                doc_type="annual_report",
                kind="annual_report",
                symbol=ent.symbol,
                period_end=None,
                published=datetime.combine(d, datetime.min.time()) if d else None,
                title="Annual report " + lbl,
                mime="application/pdf",
            )
    except Exception as e:
        rep.errors.append(f"annual reports: {type(e).__name__}")


def harvest_t3(exch, store: DocStorePort, ent: EntityRef, as_of: date, rep: HarvestReport, budget: _Budget) -> None:
    try:
        feed = exch.credit_rating_feed(as_of - timedelta(days=365 * 2), as_of)
        mine = R.filter_feed(feed, ent.isin or "", ent.legal_names + [ent.issuer])
        for url in R.rating_urls(mine):
            if budget.exceeded():
                return
            _download(
                exch,
                store,
                rep,
                url,
                doc_type="rating_letter",
                kind="rating",
                symbol=ent.symbol,
                period_end=None,
                published=None,
                mime="application/pdf",
            )
    except Exception as e:
        rep.errors.append(f"rating feed: {type(e).__name__}")


def harvest(
    ent: EntityRef,
    as_of: date,
    tiers: set[str],
    time_cap_s: float,
    mode: str,
    *,
    exch,
    store: DocStorePort,
    quarters: int = 20,
    years: int = 5,
    clock: Callable[[], float] = time.monotonic,
) -> HarvestReport:
    rep = HarvestReport()
    if mode in ("replay", "offline"):
        rep.skipped_tiers = [t for t in TIER_ORDER if t in tiers]
        return rep
    budget = _Budget(time_cap_s, clock)
    for t in TIER_ORDER:
        if t not in tiers:
            continue
        if budget.exceeded():
            rep.skipped_tiers.append(t)
            continue
        if t == "T1":
            harvest_t1(exch, store, ent, as_of, rep, quarters, budget)
        elif t == "T2":
            harvest_t2(exch, store, ent, as_of, rep, years, budget)
        elif t == "T3":
            harvest_t3(exch, store, ent, as_of, rep, budget)
    return rep


def prewarm(
    entities: Sequence[EntityRef],
    tiers: set[str],
    as_of: date,
    *,
    exch,
    store: DocStorePort,
    per_entity_cap_s: float = 900,
    incremental: bool = True,
) -> dict[str, HarvestReport]:
    """Nightly job: harvest every entity; ``incremental`` is satisfied by store idempotency (known URLs skip)."""
    return {e.symbol: harvest(e, as_of, tiers, per_entity_cap_s, "warm", exch=exch, store=store) for e in entities}
