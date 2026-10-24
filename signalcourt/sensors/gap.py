"""Gap sensor: a break in a metric that the target's baseline (twin, peer median or own history) does not share.

Per metric: find the last break in the target series, build a baseline from the quarters before it, measure
the robust z of the gap after the break, require it to persist, then ask how often placebo units (every peer
as if it were the target, and the target at earlier break dates) look at least as extreme.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import ruptures as rpt

from ..calc import periods as P
from ..calc.panel import Panel, placebo_entities
from ..ledger.schema import PlaceboStat, SignalPayload, Surprise
from .baseline import Baseline, fit_baseline

MAD_TO_SD = 1.4826
SENSOR = "gap"


@dataclass
class GapStat:
    z: float
    size: float  # mean post-break gap less the median pre-break gap, in the metric's own units
    persistence_q: int
    median_pre: float
    scale: float


def _gap_stat(series: pd.Series, base: pd.Series, pre_end: int, threshold: float) -> GapStat | None:
    """Robust z of the gap after ``pre_end``; None when the spread before it cannot be measured."""
    gap = (series - base.reindex(series.index)).dropna()
    pre, post = gap[gap.index <= pre_end], gap[gap.index > pre_end]
    if len(pre) < 2 or post.empty:
        return None
    med = float(pre.median())
    tiny = 1e-9 * max(1.0, abs(float(series.median())))
    scale = MAD_TO_SD * float((pre - med).abs().median())
    if not scale > tiny:  # MAD is zero for a mostly constant history: fall back to the plain spread
        scale = float(pre.std(ddof=1))
    if not math.isfinite(scale) or scale <= tiny:
        return None
    z = (float(post.mean()) - med) / scale
    sign = 1.0 if z > 0 else -1.0
    persistence = int(((post - med) / scale * sign >= threshold).sum())
    return GapStat(z, float(post.mean()) - med, persistence, med, scale)


def _last_break(values: np.ndarray, min_size: int) -> int | None:
    """Position of the first quarter after the last change point, or None."""
    n = len(values)
    if n < 2 * min_size:
        return None
    diffs = np.diff(values)
    var = float(np.var(diffs)) if len(diffs) else 0.0
    pen = math.log(n) * var
    if not math.isfinite(pen) or pen <= 0:
        return None
    try:
        bkps = rpt.Pelt(model="l2", min_size=min_size, jump=1).fit(values.reshape(-1, 1)).predict(pen=pen)
    except (rpt.exceptions.BadSegmentationParameters, ValueError):
        return None
    inner = [b for b in bkps if 0 < b < n]
    return inner[-1] if inner else None


def _donors(panel: Panel, metric: str) -> pd.DataFrame:
    d = panel.df[(panel.df.metric == metric) & (panel.df.entity != panel.target)]
    if d.empty:
        return pd.DataFrame()
    return d.pivot_table(index="pidx", columns="entity", values="value", aggfunc="last")


def _space_placebos(
    panel: Panel, metric: str, wide: pd.DataFrame, pre_end: int, cfg: dict[str, Any]
) -> list[float]:
    out: list[float] = []
    for peer in placebo_entities(panel.df, metric, panel.target):
        s = panel.series(peer, metric).dropna()
        if len(s) < cfg["panel"]["min_quarters_to_run"]:
            continue
        base = fit_baseline(s, wide.drop(columns=[peer], errors="ignore"), pre_end, cfg)
        st = _gap_stat(s, base.series, pre_end, cfg["sensors"]["robust_z_threshold"])
        if st is not None:
            out.append(st.z)
    return out


def _time_placebos(
    target: pd.Series, wide: pd.DataFrame, pos: int, cfg: dict[str, Any]
) -> list[float]:
    """The same test with the break moved earlier, on data from before the real break only (a valid null).

    A later shift would put the placebo break inside the real post-break window, so it is skipped.
    """
    out: list[float] = []
    before = target.iloc[:pos]
    for shift in cfg["placebo"]["in_time_shifts"]:
        p = pos + shift
        if shift >= 0 or p < cfg["twin"]["pre_period_min_q"] or pos - p < cfg["sensors"]["persistence_min_quarters"]:
            continue
        pre_end = int(target.index[p - 1])
        base = fit_baseline(before, wide, pre_end, cfg)
        st = _gap_stat(before, base.series, pre_end, cfg["sensors"]["robust_z_threshold"])
        if st is not None:
            out.append(st.z)
    return out


def _status(base: Baseline) -> str:
    if base.note:
        return base.note
    return f"{base.source}: {base.n_donors} donors, pre-RMSPE {base.pre_rmspe_sd:.2f} sd"


def _rids(panel: Panel, metric: str, target_only: bool) -> list[str]:
    d = panel.df[panel.df.metric == metric]
    d = d[d.entity == panel.target] if target_only else d[d.entity != panel.target]
    seen: dict[str, None] = {}
    for r in d.sort_values("pidx").rid:
        if isinstance(r, str) and r:
            seen[r] = None
    return list(seen)


def scan_metric(panel: Panel, metric: str, settings: dict[str, Any]) -> SignalPayload | None:
    """The gap signal for one metric of the panel's target, or None when nothing clears the bar."""
    sens, rank = settings["sensors"], settings["rank"]
    target = panel.series(panel.target, metric).dropna()
    if len(target) < settings["panel"]["min_quarters_to_run"]:
        return None
    pos = _last_break(target.to_numpy(dtype=float), sens["pelt_min_size"])
    if pos is None or pos < 2 or len(target) - pos < sens["persistence_min_quarters"]:
        return None
    pre_end = int(target.index[pos - 1])
    break_pidx = int(target.index[pos])

    wide = _donors(panel, metric)
    base = fit_baseline(target, wide, pre_end, settings)
    st = _gap_stat(target, base.series, pre_end, sens["robust_z_threshold"])
    if st is None or abs(st.z) < sens["robust_z_threshold"] or st.persistence_q < sens["persistence_min_quarters"]:
        return None

    space = _space_placebos(panel, metric, wide, pre_end, settings) if wide.shape[1] else []
    placebos = space + _time_placebos(target, wide, pos, settings)
    n = len(placebos)
    az = abs(st.z)
    exceed = sum(1 for z in placebos if abs(z) >= az)
    p_raw = (1 + exceed) / (1 + n)
    specificity = 1.0 - p_raw
    persistence = min(1.0, st.persistence_q / 4)
    score = max(az * persistence * specificity, rank["surprise_floor"])

    inputs = _rids(panel, metric, True)
    if space or base.source != "own_history":  # the peer panel DOC, once, when peers fed the baseline or placebos
        inputs += [r for r in _rids(panel, metric, False) if r not in inputs]
    return SignalPayload(
        sensor=SENSOR,
        entity=panel.target,
        metrics=[metric],
        direction="up" if st.z > 0 else "down",
        break_period=P.from_index(break_pidx),
        size=st.size,
        z=st.z,
        persistence_q=st.persistence_q,
        specificity=specificity,
        specificity_source="na" if base.source == "own_history" else base.source,
        placebo=PlaceboStat(p_raw=p_raw, n=n, rank=1 + sum(1 for z in placebos if abs(z) > az)),
        inputs=inputs,
        surprise=Surprise(
            score=score, z=st.z, persistence=persistence, specificity=specificity, coverage=None,
            coverage_status=_status(base),
        ),
    )
