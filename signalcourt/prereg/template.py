"""Template claim: one entity-blind hypothesis per SIGNAL, with a dated kill test backed by a CALC record.

The text names only the metric, the direction, the baseline and the break quarter, never the company, and
carries no digits other than quarter labels, so the unbacked-numbers gate passes it without citations.
The kill test: the claim dies if, at or after two quarters past the latest reported one, the metric falls back
past halfway to where it stood before the break. The threshold is the median of the target's values before
the break plus half the signal's size (already in the metric's own units), written as a CALC so every number
on the card traces to a record.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import pandas as pd

from ..calc import periods as P
from ..calc.panel import Panel
from ..gates.unbacked import number_tokens
from ..ledger import Ledger, LedgerCalcSink, RecordType
from ..ledger.schema import RID_PATTERN, ClaimPayload, DrafterRef, Falsification
from ..sensors.per_share import adjust_per_share, per_share_metrics
from ..settings import load_config, load_registry

DRAFTER = DrafterRef(model="template", family="code")
SOURCE_RULE = "next two exchange results filings"
DEADLINE_QUARTERS = 2
THRESHOLD_FORMULA = {
    "formula_id": "kill_threshold",
    "formula_version": 1,
    "formula_text": "median of the target's values before the break plus half the signal's gap size",
}
_RID = re.compile(RID_PATTERN)
BASELINES = {"twin": "synthetic twin", "peer_median": "peer median", "na": "own history"}
COMPARATORS = {"up": "<", "down": ">"}
# Readable labels for metrics whose registry synonyms are missing or terse.
LABELS = {
    "gnpa_amt": "gross NPA",
    "nnpa_amt": "net NPA",
    "gnpa_pct": "gross NPA ratio",
    "nnpa_pct": "net NPA ratio",
    "cet1": "CET1 ratio",
    "roa": "return on assets",
    "interest_earned": "interest earned",
    "interest_expended": "interest expended",
    "other_income": "other income",
    "opex": "operating expenses",
    "ppop": "pre-provision operating profit",
    "total_income": "total income",
    "advances": "advances",
    "deposits": "deposits",
    "total_assets": "total assets",
    "pat": "profit after tax",
    "pat_owners": "profit attributable to owners",
    "pbt": "profit before tax",
    "tax_expense": "tax expense",
    "revenue": "revenue from operations",
    "finance_costs": "finance costs",
    "total_expenses": "total expenses",
    "eps_basic": "basic earnings per share",
    "interest_income": "interest income",
    "impairment_fin_instr": "impairment on financial instruments",
    "premium_income": "gross premium income",
    "net_premium_income": "net premium income",
    "solvency": "solvency ratio",
    "persistency_13m": "thirteenth-month persistency",
    "persistency_25m": "twenty-fifth-month persistency",
    "persistency_61m": "sixty-first-month persistency",
}


def _digit_free(text: str) -> bool:
    return not number_tokens(text)


@lru_cache(maxsize=1)
def _metrics() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in load_registry("metrics.yaml")}


@lru_cache(maxsize=1)
def alternatives() -> tuple[str, ...]:
    return tuple(a["id"] for a in load_registry("alternatives.yaml"))


def metric_label(metric: str) -> str:
    if metric in LABELS:
        return LABELS[metric]
    for syn in _metrics().get(metric, {}).get("synonyms", []):
        if _digit_free(syn):
            return syn
    return metric.replace("_", " ")


def match_angle(metric: str, subtype: str | None = None) -> dict[str, Any] | None:
    """The first registry angle that needs this metric, applies to the sub-type (when known) and has no figures."""
    for angle in load_registry("angles.yaml"):
        if subtype is not None and subtype not in angle.get("subtypes", []):
            continue
        if metric in angle.get("needs", []) and _digit_free(angle["question"]):
            return angle
    return None


def _subtype(ledger: Ledger, entity: str) -> str | None:
    """The sub-type harvest recorded on the target's ENTITY record, if there is one."""
    for env in ledger.query(RecordType.ENTITY, symbol=entity):
        return env.payload["subtype"]
    return None


def _statement(label: str, direction: str, baseline: str, break_period: str) -> str:
    subject = f"{label[0].upper()}{label[1:]}"
    return f"{subject} moved {direction} against its {baseline} from {break_period} and stayed there."


def _mechanism(label: str, direction: str, baseline: str, angle: dict[str, Any] | None) -> str:
    if baseline == BASELINES["na"]:
        shift = f"A lasting {direction}ward shift in {label} away from its own history"
    else:
        shift = f"A lasting {direction}ward shift in {label} that its {baseline} did not share"
    if angle is not None:
        question = angle["question"].rstrip(".")
        return f"{shift} points to a change specific to this company; the angle to test: {question}."
    return f"{shift} points to a change in how the company earns, funds or reports this line, not a sector-wide move."


def _recon_status(rows: pd.DataFrame) -> str:
    st = set(rows.recon_status.dropna()) if "recon_status" in rows else set()
    return "fail" if "fail" in st else "single_source" if "single_source" in st or not st else "pass"


def draft_claim_with_calc(
    ledger: Ledger, signal_rid: str, index: int, panel: Panel, settings: dict[str, Any] | None = None
) -> tuple[ClaimPayload, str]:
    """(claim, rid of the threshold CALC). Writes the CALC; the claim itself is only proposed."""
    sig = ledger.get(signal_rid).payload
    metric, direction, break_period = sig["metrics"][0], sig["direction"], sig.get("break_period")
    if direction not in COMPARATORS or not break_period or sig.get("size") is None:
        raise ValueError(f"signal {signal_rid} has no direction, break or size to build a kill test from")
    if panel is None:
        raise ValueError("no panel to read the pre-break level from")
    if metric in per_share_metrics():  # the sensor compared per-share values on one share base; do the same
        panel, _ = adjust_per_share(panel, settings or load_config().model_dump())

    rows = panel.df[(panel.df.entity == panel.target) & (panel.df.metric == metric)].dropna(subset=["value"])
    pre = rows[rows.pidx < P.index(break_period)].sort_values("pidx")
    if pre.empty or panel.latest_period() is None:
        raise ValueError(f"no {metric} values before {break_period} for the threshold")
    pre_median = float(pre.value.median())
    threshold = pre_median + float(sig["size"]) / 2
    latest = panel.latest_period()

    fact_rids = list(dict.fromkeys(r for r in pre.rid if isinstance(r, str) and _RID.match(r)))
    inputs = [*fact_rids, signal_rid]
    calc_rid = LedgerCalcSink(ledger).append_calc(
        {
            **THRESHOLD_FORMULA,
            "inputs": inputs,
            "params": {
                "metric": metric,
                "break_period": break_period,
                "pre_quarters": len(pre),
                "pre_median": pre_median,
                "size": float(sig["size"]),
            },
            "value": threshold,
            "unit": _metrics().get(metric, {}).get("unit", "same"),
            "period_end": P.end(latest).isoformat(),
            "entity": panel.target,
            "recon_status": _recon_status(pre),
        },
        inputs,
    )

    label = metric_label(metric)
    baseline = BASELINES.get(sig.get("specificity_source"), BASELINES["na"])
    angle = match_angle(metric, _subtype(ledger, sig["entity"]))
    claim = ClaimPayload(
        hypothesis_index=index,
        statement=_statement(label, direction, baseline, break_period),
        mechanism=_mechanism(label, direction, baseline, angle),
        signal_rid=signal_rid,
        angle_id=angle["id"] if angle else None,
        falsification=Falsification(
            metric=metric,
            comparator=COMPARATORS[direction],
            threshold=threshold,
            deadline_period=P.shift(latest, DEADLINE_QUARTERS),
            source_rule=SOURCE_RULE,
        ),
        alt_checklist=list(alternatives()),
        drafter=DRAFTER,
        entity_blind=True,
    )
    return claim, calc_rid


def draft_claim(ledger: Ledger, signal_rid: str, index: int, panel: Panel) -> ClaimPayload:
    """The template claim for one SIGNAL (its threshold CALC is appended to the ledger)."""
    return draft_claim_with_calc(ledger, signal_rid, index, panel)[0]
