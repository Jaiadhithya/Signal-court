"""Baselines for the gap sensor: what the target "should" have looked like without a break.

Choice rule, first that applies:
1. ``twin``: enough donors and pre-period quarters, and the pre-period fit is close (pre-RMSPE as a share of
   the target's own pre-period sd). Weights come from non-negative least squares, normalised to sum to 1.
2. ``peer_median``: the per-quarter median of the donors, when there are at least ``demo.peer_median_min_donors``.
3. ``own_history``: the target's own pre-break median held flat.

Every fallback leaves a plain-language reason in ``Baseline.note`` so the signal can say what it was measured
against. Donors are used on the levels given. A donor must report a share of the pre-period quarters
(``sensors.donor_min_coverage``, at least ``sensors.donor_min_quarters``), so one quarter a peer filed late or
could not be read does not drop it; the twin additionally needs a complete pre-period (interior gaps are
interpolated).
"""

from __future__ import annotations

import math
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


def donor_quarters_needed(pre_len: int, settings: dict[str, Any]) -> int:
    """Pre-period quarters a donor must report: ``sensors.donor_min_coverage`` of them, at least
    ``sensors.donor_min_quarters``, and never more than ``twin.pre_period_min_q`` (a long history asks no more
    than a short one would)."""
    sens = settings["sensors"]
    share = math.ceil(sens["donor_min_coverage"] * pre_len - 1e-9)
    return min(settings["twin"]["pre_period_min_q"], max(sens["donor_min_quarters"], share))


def fit_baseline(target: pd.Series, donors: pd.DataFrame, pre_end_pidx: int, settings: dict[str, Any]) -> Baseline:
    """Baseline for ``target`` (indexed by quarter index); only quarters up to ``pre_end_pidx`` are used to fit."""
    tw, demo = settings["twin"], settings["demo"]
    pre_t = target[(target.index <= pre_end_pidx)].dropna()
    d = donors.reindex(target.index) if donors.shape[1] else pd.DataFrame(index=target.index)
    # A donor counts when it reports enough of the target's pre-period quarters; scattered gaps are tolerated
    # (the peer median skips them, the twin interpolates interior ones and drops a donor it cannot complete).
    need = donor_quarters_needed(len(pre_t), settings)
    if d.shape[1]:
        seen = d.loc[pre_t.index].notna().sum(axis=0) if len(pre_t) else pd.Series(0, index=d.columns)
        d = d.loc[:, seen >= need]
    n = d.shape[1]
    ratio: float | None = None
    reason = ""

    if len(pre_t) >= tw["pre_period_min_q"]:
        filled = d.loc[pre_t.index].interpolate(limit_area="inside")
        full = filled.columns[filled.notna().all(axis=0)]
        if len(full) >= tw["min_donors"]:
            base, ratio, reason = _try_twin(pre_t, filled[full], d[full], tw)
            if base is not None:
                return base
        else:
            reason = f"twin needs {tw['min_donors']} donors with complete pre-period history, have {len(full)}"
    else:
        reason = f"twin needs {tw['pre_period_min_q']} pre-period quarters, have {len(pre_t)}"

    if n >= demo["peer_median_min_donors"]:
        series = d.median(axis=1, skipna=True)
        return Baseline(series, "peer_median", {}, ratio, n, f"peer_median: {_plural(n)}; {reason}")

    level = float(pre_t.median()) if len(pre_t) else float("nan")
    series = pd.Series(level, index=target.index, dtype="float64")
    why = f"fewer than {demo['peer_median_min_donors']} donors for a peer median"
    return Baseline(series, "own_history", {}, ratio, n, f"own_history: {_plural(n)}; {why}")
