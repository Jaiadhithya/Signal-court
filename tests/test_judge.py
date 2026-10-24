import json

import pandas as pd
import pytest
from conftest import fixed_clock, run_payload
from statsmodels.stats.multitest import multipletests

from signalcourt.calc import periods as P
from signalcourt.judge.grade import admissible, grade_claim, holm
from signalcourt.ledger import Author, Ledger, RecordType
from signalcourt.settings import load_config

SETTINGS = load_config().model_dump()
INVESTIGATOR = Author(component="agents", role="investigator")
REDTEAM = Author(component="agents", role="redteam")
ADVOCATE = Author(component="agents", role="advocate")


class FakePanel:
    """Just enough of Panel for the falsification check: a target and its series."""

    def __init__(self, target, data):
        self.target = target
        self._data = data

    def series(self, entity, metric):
        pts = self._data.get((entity, metric), {})
        return pd.Series(list(pts.values()), index=[P.index(p) for p in pts], dtype="float64")


class Court:
    """A hand-built ledger: SIGNAL, CLAIM, sealed prereg_head, then whatever the test adds."""

    def __init__(self, tmp_path, *, specificity=0.8, p_raw=0.01, n=60, comparator=">", threshold=0.03):
        self.led = Ledger.create(tmp_path / "run", run_payload(), clock=fixed_clock())
        self.signal = self.led.append(
            RecordType.SIGNAL,
            {
                "sensor": "t",
                "entity": "TESTCO",
                "metrics": ["m"],
                "direction": "up",
                "specificity": specificity,
                "specificity_source": "twin",
                "placebo": {"p_raw": p_raw, "n": n},
            },
            Author.code("sensors"),
        ).rid
        self.claim = self.led.append(
            RecordType.CLAIM,
            {
                "hypothesis_index": 0,
                "statement": "s",
                "mechanism": "m",
                "signal_rid": self.signal,
                "falsification": {
                    "metric": "gnpa_pct",
                    "comparator": comparator,
                    "threshold": threshold,
                    "deadline_period": "Q4FY27",
                    "source_rule": "r",
                },
                "drafter": {"model": "x", "family": "f"},
            },
            Author.code("prereg"),
        ).rid
        self.led.seal_head("prereg_head")

    def evidence(self, family, stance="support", entail=None):
        payload = {
            "claim_rid": self.claim,
            "span_rid": "SP-00000000",
            "stance": stance,
            "sentence": "x",
            "source_family": family,
            "origin_id": f"o-{family}-{len(self.led)}",
        }
        if entail:
            payload["entail"] = {"label": entail}
        return self.led.append(RecordType.EVIDENCE, payload, INVESTIGATOR).rid

    def calc(self):
        return self.led.append(
            RecordType.CALC,
            {
                "formula_id": "f",
                "formula_version": 1,
                "formula_text": "t",
                "inputs": [],
                "value": 1.0,
                "unit": "x",
                "recon_status": "pass",
            },
            Author.code("calculator"),
        ).rid

    def challenge(self, alt_type="one_off", severity="substantive"):
        return self.led.append(
            RecordType.CHALLENGE,
            {"claim_rid": self.claim, "alt_type": alt_type, "argument": "a", "severity": severity},
            REDTEAM,
        ).rid

    def rebut(self, challenge_rid, *, admissible_=True, evidence=(), calc=()):
        return self.led.append(
            RecordType.REBUTTAL,
            {
                "challenge_rid": challenge_rid,
                "argument": "a",
                "evidence": list(evidence),
                "calc": list(calc),
                "admissible": admissible_,
                "admissible_reason": "r",
            },
            ADVOCATE,
        ).rid

    def answered_challenge(self, alt_type="one_off"):
        ch = self.challenge(alt_type)
        self.rebut(ch, evidence=[self.evidence("exchange")], calc=[self.calc()])
        return ch

    def make_strong(self):
        for fam in ("exchange", "filing", "news"):
            self.evidence(fam)
        self.answered_challenge()
        self.answered_challenge("base_effect")

    def grade(self, p_holm=0.04, panel=None):
        return grade_claim(self.led, self.claim, p_holm, SETTINGS, panel)


@pytest.fixture
def court(tmp_path):
    c = Court(tmp_path)
    yield c
    c.led.close()


def test_grade_strong(court):
    court.make_strong()
    v = court.grade()
    assert v.grade == "Strong"
    assert v.reasons == []
    assert all(c["pass"] for c in v.checks.values())
    assert v.stat.p_raw == 0.01 and v.stat.p_holm == 0.04 and v.stat.n_placebo == 60


def test_grade_moderate(court):
    court.evidence("exchange")
    court.evidence("filing")
    v = court.grade(p_holm=0.4)
    assert v.grade == "Moderate"
    assert any("Holm" in r for r in v.reasons)


def test_grade_weak(court):
    court.evidence("exchange")
    v = court.grade()
    assert v.grade == "Weak"
    assert any("source famil" in r for r in v.reasons)


def test_grade_killed_by_low_specificity(tmp_path):
    c = Court(tmp_path, specificity=0.1)
    c.make_strong()
    v = c.grade()
    assert v.grade == "Killed"
    assert any("Specificity" in r for r in v.reasons)
    c.led.close()


def test_grade_killed_by_falsification_met(court):
    court.make_strong()
    panel = FakePanel("TESTCO", {("TESTCO", "gnpa_pct"): {"Q3FY27": 0.09, "Q4FY27": 0.05}})
    v = court.grade(panel=panel)
    assert v.grade == "Killed"
    assert v.checks["falsification_not_met"]["pass"] is False
    assert any("kill test" in r for r in v.reasons)


def test_falsification_not_met_before_deadline_or_when_value_misses(court):
    court.make_strong()
    before = FakePanel("TESTCO", {("TESTCO", "gnpa_pct"): {"Q3FY27": 0.09}})
    assert court.grade(panel=before).grade == "Strong"
    below = FakePanel("TESTCO", {("TESTCO", "gnpa_pct"): {"Q4FY27": 0.01}})
    assert court.grade(panel=below).grade == "Strong"
    assert court.grade(panel=None).checks["falsification_not_met"]["pass"] is True


def test_holm_matches_statsmodels():
    p = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5}
    got = holm(p)
    want = multipletests(list(p.values()), method="holm")[1]
    assert [got[k] for k in p] == pytest.approx(list(want))
    assert holm({}) == {}


def test_strong_unreachable_with_few_placebos_and_reason_says_why(tmp_path):
    c = Court(tmp_path, n=12)
    c.make_strong()
    v = c.grade()
    assert v.grade == "Moderate"
    assert "Strong unreachable: only 12 placebo comparisons (need 40)" in v.reasons
    assert v.checks["placebo_count"] == {"value": 12, "need": 40, "pass": False}
    c.led.close()


def test_contradicted_entailment_does_not_count_as_support(court):
    court.evidence("exchange", entail="entails")
    court.evidence("filing", entail="contradicts")
    court.evidence("news", stance="contradict")
    court.evidence("press")  # missing entail counts
    v = court.grade()
    assert v.checks["support_families_strong"]["value"] == 2
    assert v.grade == "Moderate"


def test_unanswered_substantive_challenge_blocks_moderate(court):
    court.evidence("exchange")
    court.evidence("filing")
    court.challenge()
    v = court.grade()
    assert v.grade == "Weak"
    assert any("unanswered" in r for r in v.reasons)


def test_minor_and_inadmissibly_rebutted_challenges(court):
    court.evidence("exchange")
    court.evidence("filing")
    court.challenge(severity="minor")
    assert court.grade().grade == "Moderate"
    ch = court.challenge()
    court.rebut(ch, admissible_=False)
    assert court.grade().grade == "Weak"


def test_judge_rechecks_admissibility_instead_of_trusting_the_flag(court):
    ch = court.challenge()
    court.rebut(ch, admissible_=True)  # says admissible, cites nothing
    from signalcourt.judge.grade import challenge_counts

    assert challenge_counts(court.led, court.claim) == (1, 0)


def test_admissible_requires_existing_cited_rid(court):
    ch = court.challenge()
    ev = court.evidence("exchange")
    ok, why = admissible(court.led, {"challenge_rid": ch, "evidence": [ev], "calc": []})
    assert ok is True and why
    ok, why = admissible(court.led, {"challenge_rid": ch, "evidence": ["EV-deadbeef"], "calc": []})
    assert ok is False and "exist" in why
    ok, why = admissible(court.led, {"challenge_rid": ch, "evidence": [], "calc": []})
    assert ok is False and "cite" in why
    ok, _ = admissible(court.led, {"challenge_rid": ch, "evidence": [court.claim], "calc": []})
    assert ok is False  # a CLAIM is neither EVIDENCE nor CALC


def test_sector_shock_needs_a_calc(court):
    ch = court.challenge("sector_shock")
    ev = court.evidence("exchange")
    ok, why = admissible(court.led, {"challenge_rid": ch, "evidence": [ev], "calc": []})
    assert ok is False and "CALC" in why
    ok, _ = admissible(court.led, {"challenge_rid": ch, "evidence": [], "calc": [court.calc()]})
    assert ok is True


def test_checks_list_every_test_even_after_first_match(tmp_path):
    c = Court(tmp_path, specificity=0.1, n=5)
    v = c.grade(p_holm=None)
    assert v.grade == "Killed"
    assert set(v.checks) == {
        "specificity",
        "falsification_not_met",
        "support_families_moderate",
        "support_families_strong",
        "challenges_answered",
        "no_unanswered_challenge",
        "p_holm",
        "placebo_count",
        "p_raw",
    }
    for chk in v.checks.values():
        assert set(chk) == {"value", "need", "pass"}
    c.led.close()


def test_verdict_payload_example(tmp_path, capsys):
    c = Court(tmp_path, n=12)
    c.make_strong()
    v = c.grade()
    print(json.dumps(v.model_dump(mode="json", exclude={"claim_rid"}), indent=2))
    assert v.grade == "Moderate"
    c.led.close()
