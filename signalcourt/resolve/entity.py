"""name -> symbol/ISIN/legal names.

Keeps every legal name an entity has used, so a pending rename resolves to the same symbol.
Fuzzy match is ``rapidfuzz.token_set_ratio`` over normalised names; a tie within 3 points
between different symbols returns Ambiguous with the top 3.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

TIE_POINTS = 3.0
MIN_SCORE = 80.0
_STOP = re.compile(r"\b(limited|ltd|the|and)\b|[&.,()]")


def normalise_name(s: str) -> str:
    s = s.lower()
    s = _STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class EntityMatch:
    symbol: str
    name: str
    isin: str | None
    score: float
    legal_names: list[str]


@dataclass
class Ambiguous:
    candidates: list[EntityMatch]


def _names_by_symbol(con: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sym, name in con.execute("SELECT symbol, name FROM entity_names"):
        out.setdefault(sym, []).append(name)
    return out


def resolve(con: sqlite3.Connection, query: str, financial_only: bool = False) -> EntityMatch | Ambiguous | None:
    """``financial_only`` limits candidates to the financial universe (sector-level, never per company)."""
    q = query.strip()
    names = _names_by_symbol(con)
    where = " WHERE financial=1" if financial_only else ""
    ents = {r[0]: r for r in con.execute("SELECT symbol, name, isin FROM entities" + where)}
    up = q.upper().replace(" ", "")
    if up in ents:  # exact symbol (spaces ignored)
        s, n, isin = ents[up]
        return EntityMatch(s, n, isin, 100.0, sorted(set(names.get(s, []) + [n])))
    qn = normalise_name(q)
    scored: list[tuple[float, str]] = []
    for sym, (_, n, _isin) in ents.items():
        best = 0.0
        for cand in set(names.get(sym, []) + [n, sym]):
            cn = normalise_name(cand)
            if not cn:
                continue
            sc = fuzz.token_set_ratio(qn, cn)
            # token_set_ratio is 100 for any subset ("bank" ⊂ "x bank"); damp by length ratio
            if sc == 100 and qn != cn:
                sc = 90 + 10 * min(len(qn), len(cn)) / max(len(qn), len(cn))
            best = max(best, sc)
        if best >= MIN_SCORE:
            scored.append((best, sym))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))

    def mk(sc: float, sym: str) -> EntityMatch:
        s, n, isin = ents[sym]
        return EntityMatch(s, n, isin, sc, sorted(set(names.get(s, []) + [n])))

    top = scored[0]
    close = [t for t in scored if top[0] - t[0] <= TIE_POINTS]
    if len(close) > 1:
        return Ambiguous([mk(*t) for t in scored[:3]])
    return mk(*top)


def legal_names(con: sqlite3.Connection, symbol: str) -> list[str]:
    return sorted({r[0] for r in con.execute("SELECT name FROM entity_names WHERE symbol=?", (symbol,))})
