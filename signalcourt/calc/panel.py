"""Panel view: long format (entity, metric, period, basis) -> value, rid, tier (implementation.md 7.7).

Tier 1 for every financial entity is persisted to ``panels/tier1.parquet`` by the nightly job;
a run's panel is the target plus peers, filtered by ``as_of`` (a quarter is in the panel only
if its source document was published on or before ``as_of``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

from . import periods as P
from .calculator import FactBook

COLUMNS = ["entity", "metric", "period", "pidx", "basis", "value", "rid", "tier", "published", "recon_status"]


@dataclass
class PanelFact:
    entity: str
    metric: str
    period: str  # quarter label, e.g. Q2FY24
    basis: str
    value: float
    rid: str
    tier: int
    published: date  # exchange broadcast date of the source document
    recon_status: str = "pass"


class FactSource(Protocol):
    def panel_facts(self, entities: list[str]) -> Iterable[PanelFact]: ...


class Panel:
    def __init__(self, df: pd.DataFrame, target: str, peers: list[str], as_of: date) -> None:
        self.df, self.target, self.peers, self.as_of = df, target, peers, as_of

    def series(self, entity: str, metric: str) -> pd.Series:
        d = self.df[(self.df.entity == entity) & (self.df.metric == metric)].sort_values("pidx")
        return pd.Series(d.value.values, index=d.pidx.values, dtype="float64")

    def metrics(self, entity: str) -> list[str]:
        return sorted(self.df[self.df.entity == entity].metric.unique())

    def periods(self, entity: str | None = None) -> list[str]:
        d = self.df if entity is None else self.df[self.df.entity == entity]
        return [P.from_index(i) for i in sorted(d.pidx.unique())]

    def latest_period(self) -> str | None:
        t = self.df[self.df.entity == self.target]
        return P.from_index(int(t.pidx.max())) if len(t) else None

    def to_factbook(self) -> FactBook:
        fb = FactBook()
        for r in self.df.itertuples(index=False):
            fb.add(r.entity, r.metric, r.period, r.value, r.rid, r.recon_status)
        return fb

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_parquet(path, index=False)


def _frame(rows: Iterable[PanelFact]) -> pd.DataFrame:
    recs = [
        dict(
            entity=r.entity,
            metric=r.metric,
            period=r.period,
            pidx=P.index(r.period),
            basis=r.basis,
            value=r.value,
            rid=r.rid,
            tier=r.tier,
            published=r.published,
            recon_status=r.recon_status,
        )
        for r in rows
    ]
    return pd.DataFrame.from_records(recs, columns=COLUMNS)


def build_panel(
    entity: str,
    peers: list[str],
    as_of: date,
    source: FactSource,
    quarters: int = 20,
    basis_default: str = "standalone",
) -> Panel:
    ents = [entity] + [p for p in peers if p != entity]
    df = _frame(source.panel_facts(ents))
    if df.empty:
        return Panel(df, entity, peers, as_of)
    df = df[df.published <= as_of]  # Time Gate: quarters not yet public are invisible
    # Restatements: for one key the latest-published version wins (earlier ones stay in the ledger).
    df = df.sort_values("published").drop_duplicates(["entity", "metric", "pidx", "basis"], keep="last")
    # One basis per entity: the default if it has any facts, else the other.
    keep = []
    for _, g in df.groupby("entity"):
        pref = basis_default if (g.basis == basis_default).any() else g.basis.iloc[0]
        keep.append(g[g.basis == pref])
    df = pd.concat(keep, ignore_index=True) if keep else df
    tgt = df[df.entity == entity]
    if len(tgt):
        last = int(tgt.pidx.max())
        df = df[df.pidx > last - quarters]
    return Panel(df.sort_values(["entity", "metric", "pidx"]).reset_index(drop=True), entity, list(peers), as_of)


def load_tier1(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def save_tier1(rows: Iterable[PanelFact], path: Path) -> int:
    df = _frame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return len(df)


def placebo_entities(panel_df: pd.DataFrame, metric: str, exclude: str) -> list[str]:
    """Every same-sub-type entity in the panel with the metric; caller passes a sub-type-filtered frame."""
    d = panel_df[(panel_df.metric == metric) & (panel_df.entity != exclude)]
    return sorted(d.entity.unique())
