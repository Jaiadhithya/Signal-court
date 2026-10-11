"""CALC records: the only place a derived number is made (implementation.md 7.7).

Agents never write numbers; they send a CALC request and read back the result. Identical
formula version + inputs + params return the existing CALC instead of a duplicate.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from ..extract.registry import load_yaml
from . import formulas as F
from . import periods as P


class CalcSink(Protocol):
    """B1's ledger adapter implements this: append a CALC envelope, return its rid."""

    def append_calc(self, payload: dict[str, Any], parents: list[str]) -> str: ...


class InMemorySink:
    """Test/offline sink with deterministic rids."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def append_calc(self, payload: dict[str, Any], parents: list[str]) -> str:
        blob = json.dumps({"p": payload, "parents": parents}, sort_keys=True, default=str)
        rid = "CA-" + hashlib.sha256(blob.encode()).hexdigest()[:8]
        self.records[rid] = payload
        return rid


@dataclass
class FactRef:
    rid: str
    value: float
    recon_status: str = "pass"  # pass | fail | single_source


class FactBook:
    """Quarterly facts by (entity, metric, period index) with the FACT rid behind each."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], dict[int, FactRef]] = {}

    def add(self, entity: str, metric: str, period: str, value: float, rid: str, recon_status: str = "pass") -> None:
        self._d.setdefault((entity, metric), {})[P.index(period)] = FactRef(rid, float(value), recon_status)

    def metrics_for(self, entity: str) -> list[str]:
        return sorted({m for (e, m) in self._d if e == entity})

    def entities(self, metric: str | None = None) -> list[str]:
        return sorted({e for (e, m) in self._d if metric is None or m == metric})

    def series(self, entity: str, metric: str) -> pd.Series:
        d = self._d.get((entity, metric), {})
        if not d:
            return pd.Series(dtype="float64")
        return pd.Series({i: r.value for i, r in sorted(d.items())}, dtype="float64")

    def ref(self, entity: str, metric: str, idx: int) -> FactRef | None:
        return self._d.get((entity, metric), {}).get(idx)


@dataclass
class CalcResult:
    rid: str
    formula_id: str
    value: float
    unit: str
    entity: str
    period: str
    inputs: list[str]
    recon_status: str


@dataclass
class CalcRequest:
    """What an agent may ask for: a registered formula, an entity, a period, and parameters."""

    formula_id: str
    entity: str
    period: str
    params: dict[str, Any] = field(default_factory=dict)


def _max_lag(expr: str) -> int:
    tree = ast.parse(expr, mode="eval")
    best = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in ("yoy",):
                best = max(best, 4)
            elif n.func.id == "qoq":
                best = max(best, 1)
            elif n.func.id == "lag" and len(n.args) == 2 and isinstance(n.args[1], ast.Constant):
                best = max(best, int(n.args[1].value))
    return best


class Calculator:
    def __init__(self, book: FactBook, sink: CalcSink, formulas: list[dict[str, Any]] | None = None) -> None:
        self.book = book
        self.sink = sink
        rows = formulas if formulas is not None else load_yaml("formulas.yaml")
        self.formulas: dict[str, dict[str, Any]] = {r["id"]: r for r in rows}
        self._seen: dict[str, CalcResult] = {}

    # -- environment ---------------------------------------------------------------
    def _env(self, entity: str, expr: str, depth: int = 0) -> tuple[dict[str, Any], set]:
        """Series for every metric id in ``expr``; formula ids are expanded recursively."""
        if depth > 6:
            raise F.FormulaError(f"formula nesting too deep: {expr!r}")
        env: dict[str, Any] = {}
        metrics_used: set = set()
        for name in F.names_in(expr):
            # A reported series wins over a formula of the same id (e.g. reported PPOP vs the derived one).
            series = self.book.series(entity, name)
            if not series.empty:
                env[name] = series
                metrics_used.add(name)
            elif name in self.formulas:
                sub = self.formulas[name]["expr"]
                sub_env, sub_used = self._env(entity, sub, depth + 1)
                env[name] = F.evaluate(sub, sub_env)
                metrics_used |= sub_used
            else:
                raise F.FormulaError(f"no {name} series for the requested entity")
        return env, metrics_used

    def series(self, entity: str, expr: str, periods: list[str]) -> pd.Series:
        """Values of ``expr`` at ``periods`` (NaN where an input is missing)."""
        env, _ = self._env(entity, expr)
        out = F.evaluate(expr, env)
        idx = [P.index(p) for p in periods]
        if not isinstance(out, pd.Series):
            return pd.Series([out] * len(idx), index=idx, dtype="float64")
        return out.reindex(idx)

    # -- CALC records --------------------------------------------------------------
    def compute(self, formula_id: str, entity: str, period: str, **params: Any) -> CalcResult:
        if formula_id not in self.formulas:
            raise F.FormulaError(f"unknown formula {formula_id!r}")
        fm = self.formulas[formula_id]
        expr = fm["expr"]
        env, used = self._env(entity, expr)
        out = F.evaluate(expr, env)
        idx = P.index(period)
        value = out.get(idx, np.nan) if isinstance(out, pd.Series) else out
        if value is None or (isinstance(value, float) and np.isnan(value)):
            raise F.FormulaError(f"{formula_id} is undefined for {entity} at {period} (missing input)")
        lag_n = max(
            [_max_lag(expr)] + [_max_lag(self.formulas[n]["expr"]) for n in F.names_in(expr) if n in self.formulas]
        )
        refs: list[FactRef] = []
        for m in sorted(used):
            for k in range(0, lag_n + 1):
                r = self.book.ref(entity, m, idx - k)
                if r is not None:
                    refs.append(r)
        inputs = sorted({r.rid for r in refs})
        status = "pass"
        if any(r.recon_status == "fail" for r in refs):
            status = "fail"
        elif any(r.recon_status == "single_source" for r in refs):
            status = "single_source"
        key = hashlib.sha256(
            json.dumps(
                [formula_id, fm.get("v", 1), entity, period, inputs, params], sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        if key in self._seen:
            return self._seen[key]
        payload = {
            "formula_id": formula_id,
            "formula_version": fm.get("v", 1),
            "formula_text": fm["text"],
            "inputs": inputs,
            "params": params,
            "value": float(value),
            "unit": fm["unit"],
            "period_end": P.end(period).isoformat(),
            "entity": entity,
            "recon_status": status,
        }
        rid = self.sink.append_calc(payload, inputs)
        res = CalcResult(rid, formula_id, float(value), fm["unit"], entity, period, inputs, status)
        self._seen[key] = res
        return res

    def request(self, req: CalcRequest) -> CalcResult:
        return self.compute(req.formula_id, req.entity, req.period, **req.params)
