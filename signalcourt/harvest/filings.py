"""Results + XBRL (legacy and integrated) and annual reports (implementation.md 7.5).

Listing rows are read defensively: key names differ between the legacy feed (``toDate``,
``broadCastDate``) and the integrated one (``qe_Date``, ``broadcast_Date``, ``pdf_attach``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..extract import xbrl as X
from ..extract import xbrl_map as XM
from ..extract.types import FactProposal
from .announcements import parse_dt


class IssuerMismatch(Exception):
    """A company with legacy results rows got zero integrated rows: the issuer string is probably wrong."""


@dataclass
class ResultFiling:
    symbol: str
    period_end: date | None
    consolidated: bool
    audited: bool | None
    broadcast: datetime | None
    xbrl_url: str | None
    pdf_url: str | None
    source: str  # legacy | integrated
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if row.get(k) not in (None, "", "-"):
            return row[k]
    return None


def _is_cons(v: Any) -> bool:
    return str(v or "").strip().lower().startswith("consol")


def filing_from_row(symbol: str, row: dict[str, Any], source: str) -> ResultFiling:
    pe = parse_dt(_first(row, "toDate", "qe_Date", "qeDate", "period_ended"))
    aud = _first(row, "audited")
    return ResultFiling(
        symbol=symbol,
        period_end=pe.date() if pe else None,
        consolidated=_is_cons(_first(row, "consolidated")),
        audited=None if aud is None else str(aud).strip().lower().startswith("audited"),
        broadcast=parse_dt(_first(row, "broadCastDate", "broadcast_Date", "broadcastDate")),
        xbrl_url=_first(row, "xbrl"),
        pdf_url=_first(row, "pdf_attach", "pdf", "attachment"),
        source=source,
        raw=row,
    )


def list_filings(
    exch, symbol: str, issuer: str, include_legacy: bool = True, min_integrated_after: date = date(2025, 3, 31)
) -> list[ResultFiling]:
    """Legacy + integrated, newest first. Raises IssuerMismatch on legacy>0 and integrated==0 when the
    legacy feed already reaches past the integrated start (so a recent filer must have integrated rows)."""
    legacy = [filing_from_row(symbol, r, "legacy") for r in exch.results_legacy(symbol)] if include_legacy else []
    integ = [filing_from_row(symbol, r, "integrated") for r in exch.results_integrated(symbol, issuer)]
    if legacy and not integ:
        newest = max((f.period_end for f in legacy if f.period_end), default=None)
        if newest is not None and newest >= date(2024, 9, 30):
            raise IssuerMismatch(
                f"{symbol} has {len(legacy)} legacy rows to {newest} but 0 integrated rows for issuer {issuer!r}"
            )
    allf = legacy + integ
    allf.sort(key=lambda f: (f.period_end or date.min, f.broadcast or datetime.min), reverse=True)
    return allf


@dataclass
class ParsedFiling:
    filing: ResultFiling
    sha256: str | None
    subtype: str
    quarter: list[FactProposal]
    ytd: dict[str, tuple[float, str, str, str]]
    entry_point: str
    result_type: str | None
    period_end: date | None


def parse_filing(filing: ResultFiling, xml: bytes, subtype: str, sha256: str | None = None) -> ParsedFiling:
    doc = X.parse(xml)
    end = X.period_end(doc) or filing.period_end
    basis = "consolidated" if filing.consolidated or (doc.nature or "").lower().startswith("consol") else "standalone"
    props = XM.to_fact_proposals(doc, filing.symbol, subtype, end, basis)
    for p in props:
        p.span.doc_sha256 = sha256
    return ParsedFiling(
        filing, sha256, subtype, props, XM.ytd_values(doc, subtype, end), doc.entry_point, doc.result_type, end
    )


def derive_missing_quarters(parsed: Iterable[ParsedFiling]) -> list[FactProposal]:
    """Quarter = YTD now minus YTD at the previous quarter end of the same fiscal year, for duration
    metrics a filing has only year-to-date (typically annual Q4 filings with no 3-month context).

    Derived facts carry ``derived_from='ytd_to_quarter'`` and are never read directly; the controller
    records them through a CALC."""
    from ..calc import periods as P

    by_key: dict[tuple[str, str, int], ParsedFiling] = {}
    for pf in parsed:
        if pf.period_end:
            by_key[(pf.filing.symbol, "c" if pf.filing.consolidated else "s", P.index(pf.period_end))] = pf
    out: list[FactProposal] = []
    for (sym, b, idx), pf in sorted(by_key.items()):
        have = {p.metric for p in pf.quarter}
        for metric, (val, text, element, ctx) in pf.ytd.items():
            if metric in have:
                continue
            q_in_fy = idx % 4
            prev_ytd = 0.0
            if q_in_fy > 0:
                prev = by_key.get((sym, b, idx - 1))
                if prev is None or metric not in prev.ytd:
                    continue
                prev_ytd = prev.ytd[metric][0]
            out.append(
                FactProposal(
                    entity=sym,
                    metric=metric,
                    value=X.ytd_to_quarter(val, prev_ytd if q_in_fy else None),
                    unit="inr_cr",
                    period_end=pf.period_end,
                    basis="consolidated" if b == "c" else "standalone",
                    extractor="xbrl",
                    value_as_written=text,
                    span=SpanProposal_xbrl(pf.sha256, text, element, ctx),
                    derived_from="ytd_to_quarter",
                    notes={"ytd_now": val, "ytd_prev": prev_ytd},
                )
            )
    return out


def SpanProposal_xbrl(sha: str | None, text: str, element: str, ctx: str):
    from ..extract.types import SpanProposal

    return SpanProposal(
        kind="xbrl",
        doc_sha256=sha,
        text=text,
        value_text=text,
        xbrl={"element": element, "context_id": ctx, "unit": "INR", "decimals": None},
    )


def annual_report_urls(rows: Iterable[dict[str, Any]], last_n: int = 5) -> list[tuple[str, date | None, str]]:
    """(url, broadcast date, label) for the newest ``last_n`` annual reports."""
    out = []
    for r in rows:
        url = r.get("fileName") or r.get("file_name")
        if not url:
            continue
        dt = parse_dt(r.get("broadcast_dttm") or r.get("broadcastDate"))
        out.append((url, dt.date() if dt else None, "{}-{}".format(r.get("fromYr", ""), r.get("toYr", ""))))
    out.sort(key=lambda t: t[1] or date.min, reverse=True)
    return out[:last_n]
