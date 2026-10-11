"""Safe formula evaluator over named quarterly series (implementation.md 5.3, 7.7).

An AST walker over ``+ - * / **``, numbers, metric ids and the whitelisted functions
``lag, mean, yoy, qoq, abs, min, max``. There is no ``eval``. A series is a pandas Series
indexed by integer quarter index (``periods.index``); missing quarters are NaN.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd

Number = int | float
Value = Number | pd.Series


class FormulaError(ValueError):
    pass


def _as_series_like(*vals: Value):
    for v in vals:
        if isinstance(v, pd.Series):
            return v
    return None


def lag(x: Value, k: Value) -> Value:
    if not isinstance(x, pd.Series):
        return x
    return _lag_series(x, int(k))


def _lag_series(x: pd.Series, k: int) -> pd.Series:
    """Value k quarters earlier, aligned to the original index (so lag(x, 4) at Q1FY27 is Q1FY26)."""
    return pd.Series([x.get(i - k, np.nan) for i in x.index], index=x.index, dtype="float64")


def _nanmean(*vals: Value) -> Value:
    if not any(isinstance(v, pd.Series) for v in vals):
        return float(np.mean(vals))
    ref = _as_series_like(*vals)
    frame = pd.concat([v if isinstance(v, pd.Series) else pd.Series(v, index=ref.index) for v in vals], axis=1)
    out = frame.mean(axis=1, skipna=False)  # require every input: a half-missing mean is a lie
    return out


def _yoy(x: Value) -> Value:
    return x / lag(x, 4) - 1


def _qoq(x: Value) -> Value:
    return x / lag(x, 1) - 1


def _abs(x: Value) -> Value:
    return x.abs() if isinstance(x, pd.Series) else abs(x)


def _minmax(fn):
    def inner(*vals: Value) -> Value:
        if not any(isinstance(v, pd.Series) for v in vals):
            return fn(vals)
        ref = _as_series_like(*vals)
        frame = pd.concat([v if isinstance(v, pd.Series) else pd.Series(v, index=ref.index) for v in vals], axis=1)
        return frame.min(axis=1, skipna=False) if fn is min else frame.max(axis=1, skipna=False)

    return inner


FUNCS: dict[str, Callable[..., Value]] = {
    "lag": lag,
    "mean": _nanmean,
    "yoy": _yoy,
    "qoq": _qoq,
    "abs": _abs,
    "min": _minmax(min),
    "max": _minmax(max),
}

_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a**b,
}
_UN = {ast.USub: lambda a: -a, ast.UAdd: lambda a: a}


def names_in(expr: str) -> set:
    """Metric ids referenced by ``expr`` (excludes function names)."""
    tree = ast.parse(expr, mode="eval")
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - called


def evaluate(expr: str, env: Mapping[str, Value]) -> Value:
    """Evaluate ``expr`` with metric ids resolved from ``env``. Division by zero gives NaN."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"bad formula {expr!r}: {e}") from None

    def walk(n: ast.AST) -> Value:
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in env:
                raise FormulaError(f"unknown name {n.id!r} in {expr!r}")
            return env[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = walk(n.left), walk(n.right)
            if isinstance(b, pd.Series):
                b = b.where(b != 0) if type(n.op) is ast.Div else b
            elif type(n.op) is ast.Div and b == 0:
                return float("nan")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](walk(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and not n.keywords:
            return FUNCS[n.func.id](*[walk(a) for a in n.args])
        raise FormulaError(f"disallowed syntax {type(n).__name__} in {expr!r}")

    out = walk(tree)
    if isinstance(out, pd.Series):
        out = out.replace([np.inf, -np.inf], np.nan)
    return out
