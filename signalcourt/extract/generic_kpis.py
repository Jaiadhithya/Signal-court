"""Generic KPI extraction (non-lender path): list every numeric KPI on a page.

Labels become ``kpi:<slug>``; slugs stay stable across quarters by fuzzy-matching new labels (>= 92)
to slugs already seen for the entity.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

STABLE_MIN = 92


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return s[:60] or "kpi"


class StableSlugs:
    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.by_label: dict[str, str] = dict(known or {})  # normalised label -> slug

    def slug_for(self, label: str) -> str:
        norm = re.sub(r"\s+", " ", label.lower().strip())
        if norm in self.by_label:
            return self.by_label[norm]
        best, best_sc = None, 0.0
        for seen, slug in self.by_label.items():
            sc = fuzz.ratio(norm, seen)
            if sc > best_sc:
                best, best_sc = slug, sc
        slug = best if best is not None and best_sc >= STABLE_MIN else slugify(label)
        self.by_label[norm] = slug
        return slug

    def metric_id(self, label: str) -> str:
        return "kpi:" + self.slug_for(label)


def parse_response(resp: dict) -> list[dict]:
    """Keep well-formed {label, period_label, value_as_written, pid} items; code parses the numbers later."""
    out = []
    for it in resp.get("kpis", []) if isinstance(resp, dict) else []:
        if all(k in it for k in ("label", "period_label", "value_as_written", "pid")):
            out.append(it)
    return out
