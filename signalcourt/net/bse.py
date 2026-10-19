"""BSE endpoints, kept as a fallback only.

The announcements API returned 403 from the laptop on 10 Oct even with Referer/Origin, so this
client first loads the exchange homepage to collect cookies (untested). If that also fails the
caller falls back to ``workspace/manual/``; nothing here is on the critical path.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .http import Blocked, PoliteClient

API = "https://api.bseindia.com/BseIndiaAPI/api"
HOME = "https://www.bseindia.com/"
HEADERS = {"Referer": HOME, "Origin": "https://www.bseindia.com"}


class BSE:
    def __init__(self, client: PoliteClient) -> None:
        self.c = client
        self._primed = False

    def _prime(self) -> None:
        self.c.get(HOME)
        self._primed = True

    def _get(self, path: str, params: dict[str, Any]):
        if not self._primed:
            try:
                self._prime()
            except Exception:
                pass
        return self.c.get(API + path, params=params, headers=HEADERS, prime=self._prime)

    def announcements(self, scrip_code: str, frm: date, to: date, page: int = 1) -> list[dict[str, Any]]:
        r = self._get(
            "/AnnSubCategoryGetData/w",
            {
                "pageno": page,
                "strCat": "-1",
                "strPrevDate": frm.strftime("%Y%m%d"),
                "strScrip": scrip_code,
                "strSearch": "P",
                "strToDate": to.strftime("%Y%m%d"),
                "strType": "C",
                "subcategory": "-1",
            },
        )
        return r.json().get("Table", [])

    def attachment(self, name: str):
        """Try AttachLive, then AttachHis."""
        last = None
        for folder in ("AttachLive", "AttachHis"):
            try:
                return self.c.get(f"https://www.bseindia.com/xml-data/corpfiling/{folder}/{name}", headers=HEADERS)
            except Blocked as e:
                last = e
            except Exception as e:  # 404 moves to the next folder
                last = e
        raise last or RuntimeError("attachment not found")

    def scrip_lookup(self, isin: str) -> list[dict[str, Any]]:
        r = self._get("/PeerSmartSearch/w", {"Type": "SS", "text": isin})
        return r.json() if r.text.strip().startswith("[") else []
