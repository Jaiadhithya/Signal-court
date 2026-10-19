"""Exchange master lists and index CSVs -> entities table.

No hand-written company lists: the financial universe is every symbol whose index row says
Industry = Financial Services, plus (added by ``mark_financial``) any symbol whose results
XBRL uses a banking, NBFC or insurance format.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS entities (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL, isin TEXT, industry TEXT, series TEXT,
  alt_exchange_code TEXT, indices TEXT DEFAULT '', financial INTEGER DEFAULT 0, subtype TEXT);
CREATE TABLE IF NOT EXISTS entity_names (
  symbol TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (symbol, name));
"""

FINANCIAL_INDUSTRY = "financial services"
# Index CSVs read on refresh. Names are exchange index files, not companies.
DEFAULT_INDEX_FILES = (
    "ind_nifty50list",
    "ind_niftynext50list",
    "ind_niftybanklist",
    "ind_niftyfinancelist",
    "ind_nifty500list",
)


@dataclass
class Entity:
    symbol: str
    name: str
    isin: str | None = None
    industry: str | None = None
    series: str | None = None
    alt_exchange_code: str | None = None
    indices: list[str] = field(default_factory=list)
    financial: bool = False
    subtype: str | None = None


def _norm_keys(row: dict[str, str]) -> dict[str, str]:
    return {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(DDL)
    return con


def ingest(
    con: sqlite3.Connection, equity_master: Iterable[dict[str, str]], index_rows: dict[str, Iterable[dict[str, str]]]
) -> int:
    """Upsert master rows, then overlay industry and index membership. Returns entity count."""
    for raw in equity_master:
        r = _norm_keys(raw)
        sym, name = r.get("symbol"), r.get("name of company") or r.get("company name")
        if not sym or not name:
            continue
        con.execute(
            "INSERT INTO entities(symbol,name,isin,series) VALUES(?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, "
            "isin=COALESCE(excluded.isin,isin), series=excluded.series",
            (sym, name, r.get("isin number") or r.get("isin code"), r.get("series")),
        )
        con.execute("INSERT OR IGNORE INTO entity_names VALUES(?,?,?)", (sym, name, "equity_master"))
    for idx_name, rows in index_rows.items():
        for raw in rows:
            r = _norm_keys(raw)
            sym = r.get("symbol")
            if not sym:
                continue
            ind = r.get("industry")
            cur = con.execute("SELECT indices FROM entities WHERE symbol=?", (sym,)).fetchone()
            if cur is None:
                con.execute(
                    "INSERT INTO entities(symbol,name,isin,industry,series,indices) VALUES(?,?,?,?,?,?)",
                    (sym, r.get("company name", sym), r.get("isin code"), ind, r.get("series"), idx_name),
                )
            else:
                have = [x for x in (cur[0] or "").split(",") if x]
                if idx_name not in have:
                    have.append(idx_name)
                con.execute(
                    "UPDATE entities SET indices=?, industry=COALESCE(?, industry) WHERE symbol=?",
                    (",".join(have), ind, sym),
                )
            if r.get("company name"):
                con.execute("INSERT OR IGNORE INTO entity_names VALUES(?,?,?)", (sym, r["company name"], idx_name))
    con.execute("UPDATE entities SET financial=1 WHERE lower(COALESCE(industry,''))=?", (FINANCIAL_INDUSTRY,))
    con.commit()
    return con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]


def mark_financial(con: sqlite3.Connection, symbol: str, subtype: str) -> None:
    """Called when a symbol's results XBRL turns out to be a bank/NBFC/insurer format."""
    con.execute("UPDATE entities SET financial=1, subtype=? WHERE symbol=?", (subtype, symbol))
    con.commit()


def add_name(con: sqlite3.Connection, symbol: str, name: str, source: str) -> None:
    """Record a legal name an entity has used (filings, name-change announcements)."""
    con.execute("INSERT OR IGNORE INTO entity_names VALUES(?,?,?)", (symbol, name, source))
    con.commit()


def refresh_universe(exch, con: sqlite3.Connection, index_files: Iterable[str] = DEFAULT_INDEX_FILES) -> int:
    """Network entry point (laptop): equity master + index CSVs from the exchange archive."""
    idx: dict[str, list[dict[str, str]]] = {}
    for name in index_files:
        try:
            idx[name] = exch.index_csv(name)
        except Exception:
            continue  # an index list that moved is not fatal; the master still has every symbol
    return ingest(con, exch.equity_master(), idx)


def financial_symbols(con: sqlite3.Connection, subtype: str | None = None) -> list[str]:
    q = "SELECT symbol FROM entities WHERE financial=1"
    args: tuple = ()
    if subtype:
        q += " AND subtype=?"
        args = (subtype,)
    return [r[0] for r in con.execute(q + " ORDER BY symbol", args)]
