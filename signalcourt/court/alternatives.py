"""Code tests for the standard alternatives, run before any LLM call.

Each test is a CALC on the claim's SIGNAL, so both sides of the court see the same numbers and can cite them:

- ``sector_shock``: how much of the target's move across the break its peers shared. Peers and target are put
  on the scale-free footing the sensor used (an amount divided by its own pre-break median), then the change
  in the mean from the quarters before the break to the quarters from the break on is compared. The value is
  the peer median change as a fraction of the target's change: near 0, peers did not move; near 1, they all did.
- ``corporate_action``: 1 when the sensor found a scheme filing or a share-count move near the break, else 0.
- ``base_effect``: the robust z of the last pre-break quarter against the quarters before it. A large value
  says the break was measured from an unusual starting point.
- ``one_off``: the number of quarters the gap held after the break.

A sector-shock challenge can only be answered by citing the sector_shock CALC (the judge's rule).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from ..calc import periods as P
from ..calc.panel import Panel
from ..ledger import Ledger, LedgerCalcSink, RecordType
from ..sensors.gap import MAD_TO_SD, normalise

PRE_QUARTERS = 4  # quarters before the break that form the "before" level
FORMULAS = {
    "sector_shock": "peer median change across the break as a fraction of the target's change (scale-free units)",
    "corporate_action": "1 if a scheme filing or share-count move lies within the sensor's window of the break",
    "base_effect": "robust z of the last pre-break quarter against the quarters before it",
    "one_off": "quarters the gap held after the break",
}


def _calc(ledger: Ledger, formula_id: str, inputs: list[str], value: float | None, unit: str, entity: str,
          period: str | None, params: dict[str, Any]) -> str:
    return LedgerCalcSink(ledger).append_calc(
        {
            "formula_id": formula_id,
            "formula_version": 1,
            "formula_text": FORMULAS[formula_id],
            "inputs": inputs,
            "params": params,
            "value": value,
            "unit": unit,
            "period_end": P.end(period).isoformat() if period else None,
            "entity": entity,
            "recon_status": "single_source",
        },
        inputs,
    )


def _change(series: pd.Series, brk: int) -> float | None:
    pre = series[(series.index < brk) & (series.index >= brk - PRE_QUARTERS)].dropna()
    post = series[series.index >= brk].dropna()
    if pre.empty or post.empty:
        return None
    return float(post.mean() - pre.mean())


def sector_shock(ledger: Ledger, panel: Panel, signal_rid: str, sig: dict[str, Any]) -> str:
    metric, brk = sig["metrics"][0], P.index(sig["break_period"])
    pre_end = brk - 1
    target, _ = normalise(panel.series(panel.target, metric).dropna(), pre_end, metric)
    t_change = _change(target, brk) if target is not None else None
    peer_rows = panel.df[(panel.df.metric == metric) & (panel.df.entity != panel.target)]
    changes = []
    for peer in sorted(peer_rows.entity.unique()):
        s, _ = normalise(panel.series(peer, metric).dropna(), pre_end, metric)
        c = _change(s, brk) if s is not None else None
        if c is not None and math.isfinite(c):
            changes.append(c)
    peer_median = float(pd.Series(changes).median()) if changes else None
    value = None
    if peer_median is not None and t_change is not None and abs(t_change) > 1e-12:
        value = peer_median / t_change
    target_rids = sorted({r for r in panel.df[(panel.df.entity == panel.target) & (panel.df.metric == metric)].rid
                          if isinstance(r, str) and r})
    peer_rids = sorted({r for r in peer_rows.rid if isinstance(r, str) and r})
    params = {
        "metric": metric,
        "break_period": sig["break_period"],
        "target_change": t_change,
        "peer_median_change": peer_median,
        "n_peers": len(changes),
        "pre_quarters": PRE_QUARTERS,
    }
    inputs = [signal_rid, *peer_rids, *target_rids]
    return _calc(ledger, "sector_shock", inputs, value, "fraction", panel.target, sig["break_period"], params)


def corporate_action(ledger: Ledger, signal_rid: str, sig: dict[str, Any]) -> str:
    status = ((sig.get("surprise") or {}).get("coverage_status") or "").lower()
    marked = sig.get("corp_action_rid") is not None or "corporate action" in status
    inputs = [signal_rid, *([sig["corp_action_rid"]] if sig.get("corp_action_rid") else [])]
    params = {"marker": "corporate action near the break" if marked else "none found"}
    return _calc(ledger, "corporate_action", inputs, 1.0 if marked else 0.0, "flag", sig["entity"],
                 sig.get("break_period"), params)


def base_effect(ledger: Ledger, panel: Panel, signal_rid: str, sig: dict[str, Any]) -> str:
    metric, brk = sig["metrics"][0], P.index(sig["break_period"])
    s = panel.series(panel.target, metric).dropna()
    last = s[s.index == brk - 1]
    earlier = s[s.index < brk - 1]
    z = None
    if len(last) and len(earlier) >= 3:
        med = float(earlier.median())
        scale = MAD_TO_SD * float((earlier - med).abs().median())
        if not scale > 1e-12:
            scale = float(earlier.std(ddof=1))
        if math.isfinite(scale) and scale > 1e-12:
            z = (float(last.iloc[0]) - med) / scale
    rids = panel.df[(panel.df.entity == panel.target) & (panel.df.metric == metric) & (panel.df.pidx < brk)].rid
    inputs = [signal_rid, *sorted({r for r in rids if isinstance(r, str) and r})]
    params = {"metric": metric, "pre_break_period": P.from_index(brk - 1), "earlier_quarters": int(len(earlier))}
    return _calc(ledger, "base_effect", inputs, z, "z", panel.target, P.from_index(brk - 1), params)


def one_off(ledger: Ledger, signal_rid: str, sig: dict[str, Any]) -> str:
    q = sig.get("persistence_q")
    params = {"break_period": sig.get("break_period")}
    return _calc(ledger, "one_off", [signal_rid], float(q) if q is not None else None, "quarters", sig["entity"],
                 sig.get("break_period"), params)


def run_code_tests(ledger: Ledger, panel: Panel | None, claim_rid: str) -> list[str]:
    """Write the code-test CALCs for one claim and return their rids. Tests that need the panel are skipped
    without one; a test already written for this signal is not repeated."""
    signal_rid = ledger.get(claim_rid).payload["signal_rid"]
    sig = ledger.get(signal_rid).payload
    done = {e.payload["formula_id"] for e in ledger.query(RecordType.CALC) if signal_rid in e.payload["inputs"]}
    out = []
    if "corporate_action" not in done:
        out.append(corporate_action(ledger, signal_rid, sig))
    if "one_off" not in done:
        out.append(one_off(ledger, signal_rid, sig))
    if panel is not None and sig.get("break_period"):
        if "sector_shock" not in done:
            out.append(sector_shock(ledger, panel, signal_rid, sig))
        if "base_effect" not in done:
            out.append(base_effect(ledger, panel, signal_rid, sig))
    return out
