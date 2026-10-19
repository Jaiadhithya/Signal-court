"""Peer selection rule: same sub-type; Tier 2 = the 3 nearest by
|log total income - log target total income| that have decks; placebo set = every same-sub-type
entity in Tier 1 that has the metric."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass
class PeerSet:
    target: str
    subtype: str
    tier2: list[str]
    placebo: list[str]
    rule: str


def select_peers(
    target: str,
    subtype: str,
    subtype_of: dict[str, str],
    total_income: dict[str, float],
    has_deck: Callable[[str], bool],
    has_metric: Callable[[str], bool] | None = None,
    n_tier2: int = 3,
) -> PeerSet:
    same = sorted(s for s, st in subtype_of.items() if st == subtype and s != target)
    t = total_income.get(target)
    cands = [s for s in same if s in total_income and total_income[s] > 0 and has_deck(s)]
    if t and t > 0:
        cands.sort(key=lambda s: (abs(math.log(total_income[s]) - math.log(t)), s))
    else:
        cands.sort()
    placebo = [s for s in same if (has_metric is None or has_metric(s))]
    return PeerSet(target, subtype, cands[:n_tier2], placebo, f"nearest_{n_tier2}_by_log_total_income_with_decks")


def verified_domains(symbol: str, candidate_domains: Iterable[str], filing_texts: Iterable[str]) -> list[str]:
    """Keep only domains that appear in the company's own exchange filings (text of their documents)."""
    blob = " ".join(filing_texts).lower()
    return sorted({d.lower() for d in candidate_domains if d and d.lower() in blob})
