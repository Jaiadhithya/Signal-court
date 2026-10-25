"""Per-share metrics across bonus issues and splits.

A bonus issue or split changes the share count, so EPS steps down although nothing happened to the business.
Quarterly filings are not restated, so the panel mixes share bases. The share count is read from the data as
profit / EPS each quarter. Between consecutive quarters it must either stay flat (``sensors.share_flat_tolerance``)
or step by a standard factor (``sensors.share_step_factors``, within ``sensors.share_step_tolerance``); a step is
a bonus issue or split and every earlier quarter is restated onto the new share base. Any other move (a merger,
a large issue, a profit series that does not match the EPS) makes the share count unreadable, and the per-share
metric is dropped for that company rather than risk a false signal. So is one with no profit series at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from ..calc import periods as P
from ..calc.panel import Panel
from ..settings import load_registry

PER_SHARE_UNITS = frozenset({"inr_per_share"})
PROFIT_METRICS = ("pat_owners", "pat")  # first one the entity reports is the EPS numerator


@lru_cache(maxsize=1)
def per_share_metrics() -> frozenset[str]:
    return frozenset(m["id"] for m in load_registry("metrics.yaml") if m.get("unit") in PER_SHARE_UNITS)


def read_share_count(
    profit: pd.Series, per_share: pd.Series, sens: dict[str, Any]
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(steps, unexplained): each is (first quarter on the new base, ratio to the quarter before).

    A step's ratio is reported as its standard factor; an unexplained move keeps its measured ratio.
    """
    both = pd.concat([profit, per_share], axis=1, keys=["p", "e"]).dropna()
    both = both[(both.e != 0) & (both.p != 0) & (np.sign(both.p) == np.sign(both.e))]
    shares = (both.p / both.e).sort_index()
    steps: list[tuple[int, float]] = []
    unexplained: list[tuple[int, float]] = []
    pairs = list(shares.items())
    for (_, before), (q, after) in zip(pairs, pairs[1:], strict=False):
        ratio = after / before
        if abs(ratio - 1) <= sens["share_flat_tolerance"]:
            continue
        f = next((f for f in sens["share_step_factors"] if abs(ratio / f - 1) <= sens["share_step_tolerance"]), None)
        (steps if f is not None else unexplained).append((int(q), float(f if f is not None else ratio)))
    return steps, unexplained


def share_count_steps(profit: pd.Series, per_share: pd.Series, sens: dict[str, Any]) -> list[tuple[int, float]]:
    """Bonus-issue or split steps, only when every other quarter-on-quarter move is flat."""
    steps, unexplained = read_share_count(profit, per_share, sens)
    return [] if unexplained else steps


def adjust_per_share(panel: Panel, settings: dict[str, Any]) -> tuple[Panel, list[str]]:
    """A copy of the panel with per-share metrics on one share base per entity, and notes about the target."""
    targets = per_share_metrics() & set(panel.df.metric.unique())
    if not targets:
        return panel, []
    df = panel.df.copy()
    notes: list[str] = []
    drop = pd.Series(False, index=df.index)
    for metric in sorted(targets):
        for entity in sorted(df.loc[df.metric == metric, "entity"].unique()):
            rows = (df.metric == metric) & (df.entity == entity)
            profit = next(
                (s for s in (panel.series(entity, m) for m in PROFIT_METRICS) if not s.dropna().empty), None
            )
            if profit is None:
                drop |= rows
                if entity == panel.target:
                    notes.append(f"{metric}: no profit series to check for bonus issues or splits; skipped")
                continue
            steps, unexplained = read_share_count(profit, panel.series(entity, metric), settings["sensors"])
            if unexplained:
                drop |= rows
                if entity == panel.target:
                    q, r = unexplained[0]
                    notes.append(
                        f"{metric}: share count moved x{r:.2f} at {P.from_index(q)} without a bonus or split "
                        "pattern; skipped"
                    )
                continue
            for q, f in steps:
                earlier = rows & (df.pidx < q)
                df.loc[earlier, "value"] = df.loc[earlier, "value"] / f
                if entity == panel.target:
                    notes.append(
                        f"{metric}: restated for a x{f:g} share-count step at {P.from_index(q)} (bonus issue or split)"
                    )
    df = df[~drop].reset_index(drop=True)
    return Panel(df, panel.target, panel.peers, panel.as_of), notes
