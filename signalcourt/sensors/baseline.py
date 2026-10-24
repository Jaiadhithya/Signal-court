"""Baselines for the gap sensor: what the target "should" have looked like without a break.

Choice rule, first that applies:
1. ``twin``: enough donors and pre-period quarters, and the pre-period fit is close (pre-RMSPE as a share of
   the target's own pre-period sd). Weights come from non-negative least squares, normalised to sum to 1.
2. ``peer_median``: the per-quarter median of the donors, when there are at least ``demo.peer_median_min_donors``.
3. ``own_history``: the target's own pre-break median held flat.

Every fallback leaves a plain-language reason in ``Baseline.note`` so the signal can say what it was measured
against. Donors are used on the levels given; a donor missing any pre-period quarter is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.optimize import nnls

Source = Literal["twin", "peer_median", "own_history"]


@dataclass
class Baseline:
    series: pd.Series  # indexed like the target
    source: Source
    weights: dict[str, float] = field(default_factory=dict)
    pre_rmspe_sd: float | None = None  # pre-period RMSPE in units of the target's pre-period sd (None: not fitted)
    n_donors: int = 0
    note: str = ""  # why a fallback was taken, empty for the twin


def _plural(n: int) -> str:
    return f"{n} donor" + ("" if n == 1 else "s")


def _weighted(donors: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Weighted mean per quarter over the donors that have a value, so a late-reporting donor never skews it."""
    ok = donors.notna()
    num = donors.fillna(0.0).mul(weights, axis=1).sum(axis=1)
    den = ok.mul(weights, axis=1).sum(axis=1)
    return (num / den.where(den > 0)).astype("float64")


def _try_twin(
    pre_t: pd.Series, pre_d: pd.DataFrame, donors: pd.DataFrame, tw: dict[str, Any]
) -> tuple[Baseline | None, float | None, str]:
    """(baseline, ratio, reason): the baseline is None when the twin is not available or fails its gate."""
    sd = float(pre_t.std(ddof=1)) if len(pre_t) > 1 else 0.0
    try:
        w, _ = nnls(pre_d.to_numpy(dtype=float), pre_t.to_numpy(dtype=float))
    except (RuntimeError, ValueError):
        return None, None, "twin fit did not converge"
    total = float(w.sum())
    if not np.isfinite(total) or total <= 0:
        return None, None, "twin fit found no usable donor"
    weights = pd.Series(w / total, index=donors.columns)
    fit = _weighted(pre_d, weights)
    rmspe = float(np.sqrt(np.mean((pre_t.to_numpy() - fit.to_numpy()) ** 2)))
    if not np.isfinite(sd) or sd <= 1e-12:
        return None, None, "twin gate not assessable: target is flat in the pre-period"
    ratio = rmspe / sd
    if ratio > tw["max_pre_rmspe_sd"]:
        return None, ratio, f"twin gate failed (pre-RMSPE {ratio:.2f} sd > {tw['max_pre_rmspe_sd']})"
    series = _weighted(donors, weights)
    kept = {str(k): float(v) for k, v in weights.items() if v > 1e-9}
    return Baseline(series, "twin", kept, ratio, donors.shape[1]), ratio, ""


def fit_baseline(target: pd.Series, donors: pd.DataFrame, pre_end_pidx: int, settings: dict[str, Any]) -> Baseline:
    """Baseline for ``target`` (indexed by quarter index); only quarters up to ``pre_end_pidx`` are used to fit."""
    tw, demo = settings["twin"], settings["demo"]
    pre_t = target[(target.index <= pre_end_pidx)].dropna()
    d = donors.reindex(target.index) if donors.shape[1] else pd.DataFrame(index=target.index)
    if d.shape[1]:
        d = d.loc[:, d.loc[pre_t.index].notna().all(axis=0)] if len(pre_t) else d.iloc[:, :0]
    n = d.shape[1]
    ratio: float | None = None
    reason = ""

    if n >= tw["min_donors"] and len(pre_t) >= tw["pre_period_min_q"]:
        base, ratio, reason = _try_twin(pre_t, d.loc[pre_t.index], d, tw)
        if base is not None:
            return base
    elif n >= tw["min_donors"]:
        reason = f"twin needs {tw['pre_period_min_q']} pre-period quarters, have {len(pre_t)}"
    else:
        reason = f"twin needs {tw['min_donors']} donors, have {n}"

    if n >= demo["peer_median_min_donors"]:
        series = d.median(axis=1, skipna=True)
        return Baseline(series, "peer_median", {}, ratio, n, f"peer_median: {_plural(n)}; {reason}")

    level = float(pre_t.median()) if len(pre_t) else float("nan")
    series = pd.Series(level, index=target.index, dtype="float64")
    why = f"fewer than {demo['peer_median_min_donors']} donors for a peer median"
    return Baseline(series, "own_history", {}, ratio, n, f"own_history: {_plural(n)}; {why}")
