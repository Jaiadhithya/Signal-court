"""Rating letters: the market-wide feed filtered by ISIN or legal name, plus rating announcements."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..resolve.entity import normalise_name


def filter_feed(rows: Iterable[dict[str, Any]], isin: str, legal_names: Iterable[str]) -> list[dict[str, Any]]:
    """the exchange ignores ``symbol`` on this feed, so match client-side on ISIN or a normalised legal name."""
    names = {normalise_name(n) for n in legal_names}
    out = []
    for r in rows:
        r_isin = str(r.get("ISIN") or r.get("isin") or "").strip()
        r_name = normalise_name(str(r.get("NameOfTheCompany") or r.get("company") or r.get("CompanyName") or ""))
        if (isin and r_isin == isin) or (r_name and r_name in names):
            out.append(r)
    return out


def rating_urls(rows: Iterable[dict[str, Any]]) -> list[str]:
    urls = []
    for r in rows:
        for k in ("XbrlFileName", "PdfFileName", "attachment", "Attachment"):
            u = r.get(k)
            if u and str(u).lower().startswith("http") and u not in urls:
                urls.append(u)
    return urls
