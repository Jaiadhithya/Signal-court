"""Code grade: a checklist over ledger records, never a model's own confidence (guarantee G4).

``grade_claim`` runs every test, keeps each result in ``checks`` and states each failure in plain words in
``reasons``; the grade is then the first rule that matches (Killed, Strong, Moderate, Weak). All thresholds
come from the ``[judge]`` and ``[placebo]`` settings.
"""

from __future__ import annotations

import operator
from typing import Any

from statsmodels.stats.multitest import multipletests

from ..calc import periods as P
from ..ledger import Ledger, RecordType
from ..ledger.schema import VerdictPayload, VerdictStat

COMPARATORS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values, keyed like the input."""
    if not pvals:
        return {}
    keys = list(pvals)
    adjusted = multipletests([pvals[k] for k in keys], method="holm")[1]
    return {k: float(p) for k, p in zip(keys, adjusted, strict=True)}


def admissible(ledger: Ledger, rebuttal: dict) -> tuple[bool, str]:
    """A rebuttal counts only if it cites an EVIDENCE or CALC record that exists in the ledger.

    A sector-shock challenge can be answered only by a CALC (peers did not move).
    """
    cited = [*rebuttal.get("evidence", []), *rebuttal.get("calc", [])]
    if not cited:
        return False, "The rebuttal cites no evidence or calculation."
    found: dict[str, RecordType] = {}
    for rid in cited:
        try:
            found[rid] = ledger.get(rid).type
        except KeyError:
            continue
    missing = [rid for rid in cited if rid not in found]
    if missing:
        return False, f"Cited record {missing[0]} does not exist in the ledger."
    usable = [rid for rid, t in found.items() if t in (RecordType.EVIDENCE, RecordType.CALC)]
    if not usable:
        return False, "The rebuttal cites no EVIDENCE or CALC record."
    alt_type = _alt_type(ledger, rebuttal.get("challenge_rid"))
    if alt_type == "sector_shock" and not any(found[rid] is RecordType.CALC for rid in usable):
        return False, "A sector-shock challenge needs a CALC showing peers did not move."
    return True, f"Cites {len(usable)} existing record(s): {', '.join(usable)}."


def _alt_type(ledger: Ledger, challenge_rid: str | None) -> str | None:
    if not challenge_rid:
        return None
    try:
        return ledger.get(challenge_rid).payload.get("alt_type")
    except KeyError:
        return None


def support_families(ledger: Ledger, claim_rid: str) -> set[str]:
    """Distinct source families with support evidence that the entailment check did not contradict."""
    fams = set()
    for ev in ledger.query(RecordType.EVIDENCE, claim_rid=claim_rid):
        p = ev.payload
        label = (p.get("entail") or {}).get("label")
        if p["stance"] == "support" and label != "contradicts":
            fams.add(p["source_family"])
    return fams


def challenge_counts(ledger: Ledger, claim_rid: str) -> tuple[int, int]:
    """(substantive challenges, how many of them have at least one admissible rebuttal).

    Admissibility is re-checked here rather than read from the REBUTTAL's own flag: the judge trusts records,
    not another component's verdict on them.
    """
    substantive = [c.rid for c in ledger.query(RecordType.CHALLENGE, claim_rid=claim_rid, severity="substantive")]
    answered = {
        r.payload["challenge_rid"]
        for r in ledger.query(RecordType.REBUTTAL)
        if r.payload["challenge_rid"] in substantive and admissible(ledger, r.payload)[0]
    }
    return len(substantive), len(answered)


def falsification_met(falsification: dict, panel: Any) -> bool | None:
    """Whether data already in the panel meets the kill test; None when it cannot be checked."""
    if panel is None:
        return None
    compare = COMPARATORS.get(falsification["comparator"])
    if compare is None:
        return None
    try:
        deadline = P.index(falsification["deadline_period"])
    except (ValueError, KeyError, TypeError):
        return None
    series = panel.series(panel.target, falsification["metric"])
    return bool(any(compare(v, falsification["threshold"]) for i, v in series.items() if i >= deadline))


def _check(value: Any, need: Any, ok: bool) -> dict[str, Any]:
    return {"value": value, "need": need, "pass": bool(ok)}


def _fmt(x: float | None) -> str:
    return "unavailable" if x is None else f"{x:.3g}"


def grade_claim(
    ledger: Ledger, claim_rid: str, p_holm: float | None, settings: dict, panel: Any = None
) -> VerdictPayload:
    j, pl = settings["judge"], settings["placebo"]
    claim = ledger.get(claim_rid).payload
    signal = ledger.get(claim["signal_rid"]).payload
    fals = claim["falsification"]
    placebo = signal.get("placebo") or {}
    spec, p_raw, n_placebo = signal.get("specificity"), placebo.get("p_raw"), placebo.get("n")
    n_fam = len(support_families(ledger, claim_rid))
    n_subst, n_answered = challenge_counts(ledger, claim_rid)
    met = falsification_met(fals, panel)

    checks = {
        "specificity": _check(spec, j["specificity_kill"], spec is not None and spec >= j["specificity_kill"]),
        "falsification_not_met": _check(
            "not checked" if met is None else ("met" if met else "not met"), "not met", met is not True
        ),
        "support_families_moderate": _check(n_fam, j["moderate_families"], n_fam >= j["moderate_families"]),
        "support_families_strong": _check(n_fam, j["strong_families"], n_fam >= j["strong_families"]),
        "challenges_answered": _check(
            n_answered, j["strong_challenges_answered"], n_answered >= j["strong_challenges_answered"]
        ),
        "no_unanswered_challenge": _check(n_subst - n_answered, 0, n_subst == n_answered),
        "p_holm": _check(p_holm, j["stat_holm_max"], p_holm is not None and p_holm <= j["stat_holm_max"]),
        "placebo_count": _check(
            n_placebo, pl["min_for_strong"], n_placebo is not None and n_placebo >= pl["min_for_strong"]
        ),
        "p_raw": _check(p_raw, j["moderate_p_raw_max"], p_raw is not None and p_raw <= j["moderate_p_raw_max"]),
    }
    ok = {name: c["pass"] for name, c in checks.items()}

    why = {
        "specificity": f"Specificity is {_fmt(spec)}, below the {j['specificity_kill']} needed: "
        "the move is not specific to this company.",
        "falsification_not_met": f"The kill test is already met: {fals['metric']} {fals['comparator']} "
        f"{fals['threshold']} at or after {fals['deadline_period']}.",
        "support_families_moderate": f"Only {n_fam} independent source families support the claim "
        f"(Moderate needs {j['moderate_families']}).",
        "support_families_strong": f"Only {n_fam} independent source families support the claim "
        f"(Strong needs {j['strong_families']}).",
        "challenges_answered": f"Only {n_answered} of {n_subst} substantive challenges have an admissible "
        f"rebuttal (Strong needs {j['strong_challenges_answered']}).",
        "no_unanswered_challenge": f"{n_subst - n_answered} substantive challenge(s) remain unanswered.",
        "p_holm": f"The Holm-adjusted placebo p is {_fmt(p_holm)}, above the {j['stat_holm_max']} allowed for Strong.",
        "placebo_count": f"Strong unreachable: only {n_placebo or 0} placebo comparisons (need {pl['min_for_strong']})",
        "p_raw": f"The raw placebo p is {_fmt(p_raw)}, above the {j['moderate_p_raw_max']} allowed for Moderate.",
    }
    reasons = [why[name] for name, passed in ok.items() if not passed]

    killed = (spec is not None and spec < j["specificity_kill"]) or met is True
    strong = all(ok[k] for k in ("support_families_strong", "challenges_answered", "p_holm", "placebo_count"))
    moderate = all(ok[k] for k in ("support_families_moderate", "p_raw", "no_unanswered_challenge"))
    grade = "Killed" if killed else "Strong" if strong else "Moderate" if moderate else "Weak"

    stat = VerdictStat(p_raw=p_raw, p_holm=p_holm, n_placebo=n_placebo, mode="holm" if p_holm is not None else "raw")
    return VerdictPayload(claim_rid=claim_rid, grade=grade, checks=checks, stat=stat, reasons=reasons)
