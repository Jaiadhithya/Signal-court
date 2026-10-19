"""Time Gate. Applied in the tool layer so no stage can bypass it."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

GateResult = Literal["ok", "dropped", "undated"]


def _as_date(v: None | str | date) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(v[:10])


def allowed(meta: Any, as_of: str | date) -> GateResult:
    """ok if published on or before as_of; dropped if after; undated if unknown.

    ``meta`` is anything with a ``publish_date`` attribute or key (date, ISO string or None).
    """
    pd_ = meta.get("publish_date") if isinstance(meta, dict) else getattr(meta, "publish_date", None)
    d = _as_date(pd_)
    if d is None:
        return "undated"
    return "ok" if d <= _as_date(as_of) else "dropped"


def audit_max_publish_date(metas: list, as_of: str | date) -> None:
    """S9 audit: the max publish date over every cited DOC must be <= as_of, or fail loudly."""
    bad = [m for m in metas if allowed(m, as_of) == "dropped"]
    if bad:
        raise AssertionError(f"time leak: {len(bad)} cited documents are dated after {as_of}")
