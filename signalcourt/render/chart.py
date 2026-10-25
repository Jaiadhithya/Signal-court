"""Inline gap chart rebuilt with the sensor's scale and baseline rules."""

from __future__ import annotations

import math
from html import escape
from typing import Any

import numpy as np
import pandas as pd

from ..calc import periods as P
from ..calc.panel import Panel
from ..ledger.schema import SignalPayload
from ..sensors.baseline import fit_baseline
from ..sensors.gap import normalise
from ..sensors.per_share import adjust_per_share
from ..settings import load_registry


def _payload(signal_payload: SignalPayload | dict[str, Any]) -> dict[str, Any]:
    return signal_payload.model_dump() if hasattr(signal_payload, "model_dump") else dict(signal_payload)


def _empty() -> pd.Series:
    return pd.Series(dtype="float64")


def chart_series(
    panel: Panel | None, signal_payload: SignalPayload | dict[str, Any], settings: dict[str, Any]
) -> tuple[pd.Series, pd.Series, str]:
    """Return target and reconstructed baseline in metric units, with the baseline's source.

    Missing optional inputs leave the recorded target alone. Notes and the donor count are attached as
    Series attrs so the chart can explain a fallback without changing this three-part public interface.
    """
    target = _empty()
    try:
        payload = _payload(signal_payload)
        metric = payload["metrics"][0]
        entity = payload.get("entity") or panel.target
        target = panel.series(entity, metric).replace([np.inf, -np.inf], np.nan).dropna()
        adjusted, notes = adjust_per_share(panel, settings)
        target = adjusted.series(entity, metric).replace([np.inf, -np.inf], np.nan).dropna()
        target.attrs["notes"] = notes
        if target.empty:
            target.attrs["baseline_note"] = "No usable target observations after the sensor's share-base checks."
            return target, _empty(), "unavailable"
        pre_end = P.index(payload["break_period"]) - 1
        normalized, scale = normalise(target, pre_end, metric)
        if normalized is None or scale is None:
            raise ValueError("the target has no measurable pre-break scale")
        rows = adjusted.df[(adjusted.df.metric == metric) & (adjusted.df.entity != entity)]
        wide = rows.pivot_table(index="pidx", columns="entity", values="value", aggfunc="last")
        donors = {}
        for donor in wide.columns:
            series, _ = normalise(wide[donor], pre_end, metric)
            if series is not None:
                donors[donor] = series
        fitted = fit_baseline(normalized, pd.DataFrame(donors, index=wide.index), pre_end, settings)
        baseline = fitted.series.reindex(target.index) * scale
        baseline.attrs.update(n_donors=fitted.n_donors, note=fitted.note)
        return target, baseline, fitted.source
    except Exception as exc:  # old runs may lack the panel, settings, break, or optional metric
        target.attrs["baseline_note"] = f"Baseline unavailable: {type(exc).__name__}."
        return target, _empty(), "unavailable"


def _quarter(index: int) -> str:
    return P.from_index(int(index)).replace("FY", " FY")


def _indian(value: float, decimals: int = 1) -> str:
    whole, dot, fraction = f"{abs(value):.{decimals}f}".partition(".")
    last, earlier = whole[-3:], whole[:-3]
    groups = []
    while earlier:
        groups.insert(0, earlier[-2:])
        earlier = earlier[:-2]
    number = ",".join(groups + [last]) + (dot + fraction if dot else "")
    return ("-" if value < 0 else "") + number


def _number(value: float, unit: str, *, suffix: bool = True) -> str:
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    if unit == "inr_per_share":
        return ("Rs " if suffix else "") + _indian(value, 2)
    if unit == "inr_cr":
        return _indian(value) + (" cr" if suffix else "")
    return _indian(value, 2)


def _line(series: pd.Series, x, y, class_name: str) -> str:
    """Separate missing observations rather than drawing a fictitious bridge across them."""
    segments, points = [], []
    for index, value in series.items():
        if not math.isfinite(float(value)):
            if points:
                segments.append(points)
                points = []
        else:
            points.append(f"{x(int(index)):.2f},{y(float(value)):.2f}")
    if points:
        segments.append(points)
    return "".join(f'<polyline class="{class_name}" points="{" ".join(part)}"/>' for part in segments)


def render_chart(
    panel: Panel | None,
    signal_payload: SignalPayload | dict[str, Any],
    settings: dict[str, Any],
    *,
    metric_label: str | None = None,
    unit: str | None = None,
) -> str:
    """Draw an accessible SVG; point links and native titles work without JavaScript."""
    target, baseline, source = chart_series(panel, signal_payload, settings)
    if target.empty:
        note = " ".join(target.attrs.get("notes", []))
        return f'<p class="chart-note">Chart unavailable: no usable target series. {escape(note)}</p>'
    payload = _payload(signal_payload)
    metric = payload["metrics"][0]
    spec = next((row for row in load_registry("metrics.yaml") if row["id"] == metric), {})
    label = metric_label or spec.get("label") or metric.replace("_", " ")
    unit = unit or spec.get("unit", "")
    unit_label = {"inr_cr": "cr", "inr_per_share": "Rs per share", "fraction": "%"}.get(unit, unit)
    finite = pd.concat([target, baseline]).replace([np.inf, -np.inf], np.nan).dropna()
    low, high = float(finite.min()), float(finite.max())
    padding = (high - low) * 0.12 if high > low else max(abs(high) * 0.1, 1.0)
    low, high = low - padding, high + padding
    first, last = int(target.index.min()), int(target.index.max())
    left, right, top, bottom = 104.0, 972.0, 54.0, 282.0

    def x(index: int) -> float:
        return left + (index - first) / max(last - first, 1) * (right - left)

    def y(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    styles = """
      .gap-chart{color:#F4EFE3;font-family:'JetBrains Mono',Consolas,monospace}
      .gap-chart text{fill:currentColor;font-size:11px}
      .gap-chart .chart-muted{fill:#A9B1C3}
      .gap-chart .chart-grid{stroke:#22304D;stroke-width:1}
      .gap-chart .chart-target-line{fill:none;stroke:currentColor;stroke-width:2.7;stroke-linejoin:round}
      .gap-chart .chart-baseline-line{fill:none;stroke:#E2B94F;stroke-width:2.3;stroke-dasharray:7 5}
      .gap-chart .chart-break{stroke:#A9B1C3;stroke-width:1;stroke-dasharray:3 4}
      .gap-chart .chart-post-break{fill:#E2B94F;opacity:.055}
      .gap-chart .chart-point{fill:currentColor;stroke:#111B30;stroke-width:1.5}
      .gap-chart a:hover .chart-point,.gap-chart a:focus .chart-point{fill:#E2B94F;r:6}
      @media print{.gap-chart{color:#0B1324}.gap-chart .chart-muted{fill:#455168}
        .gap-chart .chart-grid{stroke:#D6DAE3}.gap-chart .chart-baseline-line{stroke:#98731F}
        .gap-chart .chart-point{stroke:#FFF}.gap-chart .chart-break{stroke:#455168}}
    """
    parts = [
        '<figure class="chart-figure">',
        f'<svg class="gap-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 360" '
        f'role="img" aria-label="{escape(label, quote=True)} against its reconstructed baseline" '
        'style="width:100%;height:auto;display:block">',
        f"<title>{escape(label)}: recorded target and sensor baseline</title><style>{styles}</style>",
        f'<text x="{left:g}" y="22" style="font-size:13px">{escape(label)}'
        f'{escape(" (" + unit_label + ")") if unit_label else ""}</text>',
    ]
    try:
        break_index = P.index(payload["break_period"])
    except (KeyError, TypeError, ValueError, AttributeError):
        break_index = None
    if break_index is not None and first <= break_index <= last:
        bx = x(break_index)
        parts += [
            f'<rect class="chart-post-break" x="{bx:.2f}" y="{top:g}" '
            f'width="{right - bx:.2f}" height="{bottom - top:g}"/>',
            f'<line class="chart-break" data-break-period="{escape(payload["break_period"], quote=True)}" '
            f'x1="{bx:.2f}" x2="{bx:.2f}" y1="{top:g}" y2="{bottom:g}"/>',
            f'<text class="chart-muted" x="{min(bx + 8, right - 140):.2f}" y="43">'
            f'Break: {escape(_quarter(break_index))}</text>',
        ]
    for value in np.linspace(low, high, 5):
        py = y(float(value))
        parts.append(f'<line class="chart-grid" x1="{left:g}" x2="{right:g}" y1="{py:.2f}" y2="{py:.2f}"/>')
        parts.append(f'<text class="chart-muted" x="{left - 12:g}" y="{py + 4:.2f}" text-anchor="end">'
                     f'{escape(_number(float(value), unit, suffix=False))}</text>')
    indices = list(target.index)
    step = max(1, math.ceil(len(indices) / 8))
    shown = list(indices[::step])
    if indices[-1] not in shown:
        shown.append(indices[-1])
    for index in shown:
        parts.append(f'<text class="chart-muted" x="{x(int(index)):.2f}" y="304" text-anchor="middle">'
                     f'{escape(_quarter(int(index)))}</text>')
    if not baseline.empty:
        parts.append(_line(baseline, x, y, "chart-baseline-line"))
    parts.append(_line(target, x, y, "chart-target-line"))
    rows = panel.df[(panel.df.entity == payload.get("entity", panel.target)) & (panel.df.metric == metric)]
    rids = (rows.sort_values("pidx").drop_duplicates("pidx", keep="last").set_index("pidx").rid.to_dict()
            if "rid" in rows else {})
    notes = target.attrs.get("notes", [])
    for index, value in target.items():
        rid = rids.get(index)
        tooltip = f'{_quarter(int(index))}: {_number(float(value), unit)}'
        if notes:
            tooltip += "; adjusted onto a consistent share base"
        title = escape(tooltip)
        circle = (f'<circle class="chart-point" cx="{x(int(index)):.2f}" cy="{y(float(value)):.2f}" r="3.8">'
                  f'<title>{title}</title></circle>')
        if isinstance(rid, str) and rid:
            parts.append(f'<a href="#{escape(rid, quote=True)}" data-rid="{escape(rid, quote=True)}" '
                         f'aria-label="{escape(tooltip, quote=True)}">{circle}</a>')
        else:
            parts.append(circle)
    parts += ['<line class="chart-target-line" x1="104" x2="132" y1="337" y2="337"/>',
              '<text x="142" y="341">Target</text>']
    if not baseline.empty:
        n = baseline.attrs.get("n_donors", 0)
        legend = {"twin": f"twin of {n} peers", "peer_median": f"peer median of {n} peers",
                  "own_history": "own history"}.get(source, source)
        parts += ['<line class="chart-baseline-line" x1="250" x2="278" y1="337" y2="337"/>',
                  f'<text x="288" y="341">{escape(legend)}</text>']
    parts.append("</svg>")
    if baseline.empty:
        parts.append('<figcaption class="chart-note">Baseline unavailable; '
                     'target observations are shown alone.</figcaption>')
    if not rids:
        parts.append('<figcaption class="chart-note">Source links unavailable in the saved panel.</figcaption>')
    if notes:
        parts.append(f'<figcaption class="chart-note">{escape(" ".join(notes))}</figcaption>')
    parts.append("</figure>")
    return "".join(parts)
