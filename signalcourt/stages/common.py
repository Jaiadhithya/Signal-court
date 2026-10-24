"""Filings read back from the DocStore, shared by the real stages and the pre-warm script.

Everything here works offline: sub-type and size come from results XBRL already in the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ..extract import xbrl as X
from ..extract.segments import segment_labels
from ..harvest import filings as Fl
from ..resolve.subtype import SubtypeDecision, SubtypeEvidence, classify_subtype
from ..store import DocMeta, DocStore

RESULTS = "results_xbrl"
# Metrics that measure company size, in order of preference (banks and insurers report total income,
# Ind AS filers revenue from operations; the rest are fallbacks for filings without either).
# Used only to rank peers within one sub-type.
SIZE_METRICS = ("total_income", "revenue", "interest_earned", "interest_income", "premium_income")
# Newest documents searched for a consolidated filing (both bases of the last few quarters).
SEGMENT_LOOKBACK = 6


@dataclass
class StoreSubtype:
    decision: SubtypeDecision
    xbrl_file: str  # file name of the newest results XBRL: the main sub-type signal
    entry_point: str
    segments: list[str]


def file_name(url: str) -> str:
    return PurePosixPath(urlsplit(url).path).name or url


def filing_of(meta: DocMeta, symbol: str) -> Fl.ResultFiling:
    """A ResultFiling rebuilt from the manifest; the XBRL's own nature field decides the basis."""
    return Fl.ResultFiling(
        symbol=symbol,
        period_end=date.fromisoformat(meta.period_end) if meta.period_end else None,
        consolidated=False,
        audited=None,
        broadcast=None,
        xbrl_url=meta.first_url,
        pdf_url=None,
        source="store",
    )


def parse_doc(store: DocStore, meta: DocMeta, symbol: str, subtype: str) -> Fl.ParsedFiling:
    return Fl.parse_filing(filing_of(meta, symbol), store.read_bytes(meta.sha256), subtype, meta.sha256)


def subtype_from_store(store: DocStore, symbol: str, insurance_type: str = "") -> StoreSubtype | None:
    """Classify from the newest results XBRL file name plus the newest consolidated segment note."""
    docs = store.docs_for(symbol, RESULTS)
    if not docs:
        return None
    newest = docs[0]
    entry, result_type, segments = "", "", []
    for i, m in enumerate(docs[:SEGMENT_LOOKBACK]):
        try:
            doc = X.parse(store.read_bytes(m.sha256))
        except Exception:
            continue
        if i == 0:
            entry, result_type = doc.entry_point or "", doc.result_type or ""
        end = X.period_end(doc)
        if end and (doc.nature or "").strip().lower().startswith("consol"):
            segments = segment_labels(doc, end)
            break
    ev = SubtypeEvidence(
        entry_point=entry,
        result_type=result_type,
        xbrl_url=newest.first_url,
        insurance_type=insurance_type,
        segments=segments,
        consolidated=bool(segments),
    )
    return StoreSubtype(classify_subtype(ev), file_name(newest.first_url), entry, segments)


def latest_size(store: DocStore, symbol: str, subtype: str) -> float | None:
    """Latest quarterly total income (or revenue) found in the newest few results filings."""
    for m in store.docs_for(symbol, RESULTS)[:SEGMENT_LOOKBACK]:
        try:
            pf = parse_doc(store, m, symbol, subtype)
        except Exception:
            continue
        vals = {p.metric: p.value for p in pf.quarter}
        for k in SIZE_METRICS:
            if vals.get(k, 0) > 0:
                return vals[k]
    return None


def xbrl_quarters(store: DocStore, symbol: str, as_of: date | None = None) -> int:
    """Distinct quarter ends with a results XBRL in the store (published on or before ``as_of`` if given)."""
    ends = set()
    for m in store.docs_for(symbol, RESULTS):
        if not m.period_end:
            continue
        if as_of and m.publish_date and m.publish_date > as_of.isoformat():
            continue
        ends.add(m.period_end)
    return len(ends)
