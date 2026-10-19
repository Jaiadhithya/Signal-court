"""LLM extraction for metrics still missing after tables.

One call per document, all missing metrics together. The model returns only
{metric, period_label, value_as_written, unit_as_written, pid, row_label}; code checks that the
written value really appears in the cited passage, parses the number and converts the unit. Every
proposal still goes through the quote and reconciliation gates, so a wrong answer cannot become a FACT.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..calc import periods as P
from . import tables as T
from .registry import metrics
from .types import FactProposal, SpanProposal

PROMPT = """Extract the requested metrics from the numbered passages of one company document.
Return JSON: {{"facts": [{{"metric": str, "period_label": str, "value_as_written": str,
"unit_as_written": str, "pid": str, "row_label": str}}]}}.
Rules: copy the number exactly as written (no conversion, no rounding); cite the passage id it appears in;
period_label is a quarter like Q2FY27; omit anything you are not sure of; never invent a value.
Metrics wanted (id: synonyms):
{metric_lines}
Passages:
{passages}
"""


class LlmPort(Protocol):
    def complete_json(self, role: str, prompt: str) -> dict[str, Any]: ...


@dataclass
class Passage:
    pid: str
    text: str
    page: int = 1


def build_prompt(missing: Sequence[str], passages: Sequence[Passage], max_chars: int = 40000) -> str:
    by_id = {m["id"]: m for m in metrics()}
    lines = ["- {}: {}".format(mid, ", ".join(by_id.get(mid, {}).get("synonyms", [])) or mid) for mid in missing]
    body, used = [], 0
    for p in passages:
        chunk = f"[{p.pid}] {p.text.strip()}"
        if used + len(chunk) > max_chars:
            break
        body.append(chunk)
        used += len(chunk)
    return PROMPT.format(metric_lines="\n".join(lines), passages="\n".join(body))


def _norm(s: str) -> str:
    return "".join(s.lower().split())


def parse_response(
    resp: dict[str, Any],
    missing: Sequence[str],
    passages: Sequence[Passage],
    entity: str,
    basis: str,
    doc_sha: str | None,
    unit_hint: str | None = None,
) -> list[FactProposal]:
    units = {m["id"]: m["unit"] for m in metrics()}
    by_pid = {p.pid: p for p in passages}
    want = set(missing)
    out: list[FactProposal] = []
    seen = set()
    for item in resp.get("facts", []) if isinstance(resp, dict) else []:
        try:
            mid, pid = item["metric"], item["pid"]
            written, plabel = str(item["value_as_written"]), item["period_label"]
            unit_w = str(item.get("unit_as_written", ""))
            P.index(plabel)
        except (KeyError, ValueError, TypeError):
            continue
        p = by_pid.get(pid)
        if mid not in want or p is None or (mid, plabel) in seen:
            continue
        if _norm(written) not in _norm(p.text):  # pre-gate: the value must be in the passage
            continue
        pn = T.parse_number((written + " " + unit_w).strip())
        if pn is None:
            continue
        v = T.to_registry_unit(pn, units[mid], unit_hint)
        if v is None:
            continue
        seen.add((mid, plabel))
        out.append(
            FactProposal(
                entity=entity,
                metric=mid,
                value=v,
                unit=units[mid],
                period_end=P.end(plabel),
                basis=basis,
                extractor="llm",
                value_as_written=written,
                span=SpanProposal(
                    kind="table_cell",
                    doc_sha256=doc_sha,
                    page=p.page,
                    text=p.text,
                    row_label=item.get("row_label"),
                    value_text=written,
                    pid=pid,
                ),
                notes={"unit_as_written": unit_w},
            )
        )
    return out


def extract_missing(
    llm: LlmPort,
    missing: Sequence[str],
    passages: Sequence[Passage],
    entity: str,
    basis: str,
    doc_sha: str | None,
    unit_hint: str | None = None,
) -> list[FactProposal]:
    if not missing or not passages:
        return []
    resp = llm.complete_json("extract", build_prompt(missing, passages))
    return parse_response(resp, missing, passages, entity, basis, doc_sha, unit_hint)
