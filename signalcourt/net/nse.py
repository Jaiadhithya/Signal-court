"""NSE endpoints. Exchange names are allowed in net/ only.

Plain ``requests``-style access with a browser UA worked from the laptop on 10 Oct without
cookies; the client keeps a ``prime`` path (homepage visit) for 401/403 anyway. Endpoint
shapes come from data/data-sources-guide.md Part 2. Never guess XBRL URLs: always read the
``xbrl`` field of a listing row.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .http import PoliteClient, Response

API = "https://www.nseindia.com/api"
ARCHIVES = "https://nsearchives.nseindia.com"
HOME = "https://www.nseindia.com/"
LISTING_TTL_S = 6 * 3600


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "rows", "Table"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


class NSE:
    def __init__(self, client: PoliteClient, listing_ttl_s: int = LISTING_TTL_S) -> None:
        self.c = client
        self.ttl = listing_ttl_s
        self.c.host_rps.setdefault("www.nseindia.com", 1.0)
        self.c.host_rps.setdefault("nsearchives.nseindia.com", 1.0)

    # -- plumbing -------------------------------------------------------------------
    def _prime(self) -> None:
        self.c.get(HOME)

    def _json(self, path: str, params: dict[str, Any], symbol_ref: str = "") -> Any:
        r = self.c.get(
            API + path,
            params=params,
            prime=self._prime,
            listing_ttl_s=self.ttl,
            headers={"Referer": "https://www.nseindia.com/get-quotes/equity?symbol=" + (symbol_ref or "")},
        )
        return r.json()

    # -- listings -------------------------------------------------------------------
    def announcements(self, symbol: str, frm: date, to: date) -> list[dict[str, Any]]:
        d = self._json(
            "/corporate-announcements",
            {"index": "equities", "symbol": symbol, "from_date": _ddmmyyyy(frm), "to_date": _ddmmyyyy(to)},
            symbol,
        )
        return _rows(d)

    def results_legacy(self, symbol: str, period: str = "Quarterly", index: str = "equities") -> list[dict[str, Any]]:
        """Results to Dec 2024. Insurers need ``index="insurance"`` (equities returns 0 rows; confirmed 11 Oct)."""
        d = self._json("/corporates-financial-results", {"index": index, "period": period, "symbol": symbol}, symbol)
        return _rows(d)

    def results_integrated(self, symbol: str, issuer: str, size: int = 50, max_pages: int = 10) -> list[dict[str, Any]]:
        """Results from Mar 2025. ``issuer`` is the exact legal name; a wrong string silently returns 0 rows."""
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            d = self._json(
                "/integrated-filing-results",
                {
                    "index": "equities",
                    "symbol": symbol,
                    "issuer": issuer,
                    "period_ended": "all",
                    "type": "Integrated Filing- Financials",
                    "page": page,
                    "size": size,
                },
                symbol,
            )
            rows = _rows(d)
            out.extend(rows)
            total = d.get("totalCount") if isinstance(d, dict) else None
            if not rows or len(rows) < size or (isinstance(total, int) and len(out) >= total):
                break
        return out

    def annual_reports(self, symbol: str) -> list[dict[str, Any]]:
        return _rows(self._json("/annual-reports", {"index": "equities", "symbol": symbol}, symbol))

    def shareholding(self, symbol: str) -> list[dict[str, Any]]:
        return _rows(self._json("/corporate-share-holdings-master", {"index": "equities", "symbol": symbol}, symbol))

    def sast(self, symbol: str) -> list[dict[str, Any]]:
        return _rows(self._json("/corporate-sast-reg29", {"index": "equities", "symbol": symbol}, symbol))

    def board_meetings(self, symbol: str) -> list[dict[str, Any]]:
        return _rows(self._json("/corporate-board-meetings", {"index": "equities", "symbol": symbol}, symbol))

    def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        return _rows(self._json("/corporates-corporateActions", {"index": "equities", "symbol": symbol}, symbol))

    def credit_rating_feed(self, frm: date, to: date) -> list[dict[str, Any]]:
        """Market-wide feed: NSE ignores ``symbol`` here [V]. Callers filter by ISIN."""
        return _rows(
            self._json(
                "/corporate-credit-rating", {"index": "equities", "from_date": _ddmmyyyy(frm), "to_date": _ddmmyyyy(to)}
            )
        )

    # -- archive files ----------------------------------------------------------------
    def index_csv(self, name: str) -> list[dict[str, str]]:
        import csv
        import io

        r = self.c.get(f"{ARCHIVES}/content/indices/{name}.csv", prime=self._prime)
        return list(csv.DictReader(io.StringIO(r.text)))

    def equity_master(self) -> list[dict[str, str]]:
        """EQUITY_L.csv [K, verify day 1]."""
        import csv
        import io

        r = self.c.get(ARCHIVES + "/content/equities/EQUITY_L.csv", prime=self._prime)
        return list(csv.DictReader(io.StringIO(r.text)))

    def download(self, url: str) -> Response:
        """nsearchives files: announcement attachments, XBRL, annual reports. Never guess URLs."""
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        if not url.startswith("http"):
            url = ARCHIVES + ("" if url.startswith("/") else "/") + url
        return self.c.get(url, prime=self._prime)
