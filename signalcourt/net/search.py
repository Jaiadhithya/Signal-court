"""Web search leads (implementation.md 7.3). A Lead is never citable; only a fetched, hashed DOC is.

Backends are injected so the module is testable offline and the router order is data:
Tavily while the run's Tavily count is under the budget, then ddgs, then Google News RSS / GDELT.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass
class Lead:
    url: str
    title: str
    snippet: str = ""
    date: date | None = None
    backend: str = ""


Backend = Callable[[str, str | None, date | None, int], list[Lead]]


class SearchRouter:
    def __init__(self, backends: Sequence[tuple], tavily_budget: int = 20) -> None:
        """``backends``: ordered (name, callable, counts_against_budget) triples."""
        self.backends = list(backends)
        self.budget = tavily_budget
        self.used = 0

    def web_search(self, query: str, site: str | None = None, before: date | None = None, k: int = 8) -> list[Lead]:
        for name, fn, metered in self.backends:
            if metered and self.used >= self.budget:
                continue
            try:
                leads = fn(query, site, before, k)
            except Exception:
                continue
            if metered:
                self.used += 1
            if leads:
                for lead in leads:
                    lead.backend = name
                return leads[:k]
        return []

    def news_search(self, query: str, before: date, k: int = 20) -> list[Lead]:
        return self.web_search(query, None, before, k)


def drop_after(leads: list[Lead], before: date | None) -> list[Lead]:
    """Time Gate for leads: drop dated results after ``before``; undated ones stay (they are leads only)."""
    if before is None:
        return leads
    return [lead for lead in leads if lead.date is None or lead.date <= before]
