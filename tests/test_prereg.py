"""Template claim: entity-blind text, a kill test backed by a CALC record, and text the numbers gate accepts."""
import pandas as pd
import pytest
from conftest import fixed_clock, run_payload
from test_gap_sensor import LAST, TARGET, make_panel

from signalcourt.calc import periods as P
from signalcourt.gates.schema_gate import check_proposal
from signalcourt.gates.unbacked import exempt_spans, find_unbacked, number_tokens
from signalcourt.judge.grade import grade_claim
from signalcourt.ledger import Author, Ledger, RecordType
from signalcourt.prereg import draft_claim, draft_claim_with_calc
from signalcourt.sensors.gap import scan_metric
from signalcourt.settings import load_config, load_registry

S = load_config().model_dump()
SENSORS = Author.code("sensors")
PREREG = Author.code("prereg")


def new_ledger(tmp_path, **kw):
    return Ledger.create(tmp_path / "run", run_payload(**kw), clock=fixed_clock())


def signal_on(led, panel, metric):
    sig = scan_metric(panel, metric, S)
    assert sig is not None, "the synthetic panel must produce a signal"
    return led.append(RecordType.SIGNAL, sig, SENSORS, sig.inputs).rid


def pre_break_median(panel, metric, break_period):
    s = panel.series(panel.target, metric)
    return float(s[s.index < P.index(break_period)].median())


def test_claim_fields_follow_the_signal(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",))
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    claim = draft_claim(led, sig_rid, 0, panel)
    assert claim.signal_rid == sig_rid and claim.hypothesis_index == 0
    assert claim.drafter.model == "template" and claim.drafter.family == "code" and claim.entity_blind is True
    assert claim.statement.startswith("Gross NPA ratio moved up against its")
    assert "Q2FY25" in claim.statement and claim.statement.endswith("and stayed there.")
    assert claim.falsification.source_rule == "next two exchange results filings"
    led.close()


def test_claim_text_contains_no_company_name(tmp_path):
    symbol, names = "HDFCBANK", ("HDFC Bank Limited", "HDFC Bank", "HDFC")
    panel = make_panel(metrics=("advances",))
    panel.df["entity"] = panel.df.entity.replace(TARGET, symbol)
    panel.target = symbol
    led = new_ledger(tmp_path, company_input=names[0], symbol=symbol)
    sig_rid = signal_on(led, panel, "advances")
    claim = draft_claim(led, sig_rid, 0, panel)
    text = f"{claim.statement} {claim.mechanism}".lower()
    for name in (symbol, *names):
        assert name.lower() not in text
    led.close()


def test_threshold_is_backed_by_a_calc_record(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",))
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    claim, calc_rid = draft_claim_with_calc(led, sig_rid, 0, panel)
    calc = led.get(calc_rid)
    sig = led.get(sig_rid).payload
    assert calc.type == RecordType.CALC
    assert calc.payload["value"] == pytest.approx(claim.falsification.threshold)
    assert sig_rid in calc.payload["inputs"] and sig_rid in calc.parents
    pre = [r for r in calc.payload["inputs"] if r != sig_rid]
    assert pre and all(r in sig["inputs"] for r in pre)  # the target facts before the break
    expected = pre_break_median(panel, "gnpa_pct", sig["break_period"]) + sig["size"] / 2
    assert claim.falsification.threshold == pytest.approx(expected)
    assert calc.payload["unit"] == "fraction" and calc.payload["formula_id"] == "kill_threshold"
    assert not any(ch.isdigit() for ch in calc.payload["formula_text"])
    led.close()


@pytest.mark.parametrize("step, direction, comparator", [(4.0, "up", "<"), (-4.0, "down", ">")])
def test_comparator_and_deadline_follow_direction_and_latest_quarter(tmp_path, step, direction, comparator):
    panel = make_panel(metrics=("gnpa_pct",), step=step)
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    assert led.get(sig_rid).payload["direction"] == direction
    f = draft_claim(led, sig_rid, 0, panel).falsification
    assert f.metric == "gnpa_pct" and f.comparator == comparator
    assert f.deadline_period == P.shift(panel.latest_period(), 2) == P.from_index(LAST + 2)
    pre = pre_break_median(panel, "gnpa_pct", "Q2FY25")
    assert (f.threshold > pre) if direction == "up" else (f.threshold < pre)  # halfway back, not past the start
    led.close()


def test_period_labels_do_not_trip_the_numbers_gate(tmp_path):
    for label in ("Q2FY25", "Q4FY26", "Q1FY27"):
        assert number_tokens(f"Gross NPA ratio moved up from {label} and stayed there.") == []
        assert label not in exempt_spans(label)
    assert find_unbacked("It moved by 4 from Q2FY25.", []) != []  # a bare figure is still caught

    panel = make_panel(metrics=("gnpa_pct",))
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    claim, calc_rid = draft_claim_with_calc(led, sig_rid, 0, panel)
    model, rej = check_proposal(led, "CLAIM", claim, PREREG, extra_cited=[calc_rid])
    assert rej is None and model is not None
    led.close()


def test_alt_checklist_lists_every_registry_alternative(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",))
    led = new_ledger(tmp_path)
    claim = draft_claim(led, signal_on(led, panel, "gnpa_pct"), 0, panel)
    ids = [a["id"] for a in load_registry("alternatives.yaml")]
    assert len(ids) == 11 and claim.alt_checklist == ids
    led.close()


def test_mechanism_comes_from_a_matching_angle(tmp_path):
    panel = make_panel(metrics=("advances", "m_unknown"))
    led = new_ledger(tmp_path)
    matched = draft_claim(led, signal_on(led, panel, "advances"), 0, panel)
    angle = next(a for a in load_registry("angles.yaml") if "advances" in a["needs"])
    assert matched.angle_id == angle["id"] and angle["question"] in matched.mechanism
    generic = draft_claim(led, signal_on(led, panel, "m_unknown"), 1, panel)
    assert generic.angle_id is None and generic.mechanism
    led.close()


def test_draft_is_deterministic(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",))
    a, b = new_ledger(tmp_path / "a"), new_ledger(tmp_path / "b")
    ca = draft_claim(a, signal_on(a, panel, "gnpa_pct"), 0, panel)
    cb = draft_claim(b, signal_on(b, panel, "gnpa_pct"), 0, panel)
    assert ca.model_dump(exclude={"signal_rid"}) == cb.model_dump(exclude={"signal_rid"})
    a.close()
    b.close()


def _extend(panel, values):
    """The panel plus later target quarters, as the judge would see it after the deadline."""
    rows = [
        dict(entity=panel.target, metric="gnpa_pct", period=P.from_index(LAST + i), pidx=LAST + i, basis="standalone",
             value=v, rid=f"FA-ff{LAST + i:06x}", tier=1, published=panel.as_of, recon_status="pass")
        for i, v in enumerate(values, start=1)
    ]
    panel.df = pd.concat([panel.df, pd.DataFrame(rows)], ignore_index=True)
    return panel


def test_judge_reads_the_falsification(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",))
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    claim = draft_claim(led, sig_rid, 0, panel)
    claim_rid = led.append(RecordType.CLAIM, claim, PREREG, [sig_rid]).rid
    led.seal_head("prereg_head")
    t = claim.falsification.threshold
    held = grade_claim(led, claim_rid, None, S, _extend(make_panel(metrics=("gnpa_pct",)), [t + 1, t + 1]))
    assert held.checks["falsification_not_met"]["pass"]
    fell = grade_claim(led, claim_rid, None, S, _extend(make_panel(metrics=("gnpa_pct",)), [t + 1, t - 1]))
    assert fell.grade == "Killed" and not fell.checks["falsification_not_met"]["pass"]
    led.close()


@pytest.mark.parametrize("subtype, angle_id", [("bank", "A3"), ("generic", None)])
def test_angle_must_apply_to_the_targets_subtype(tmp_path, subtype, angle_id):
    panel = make_panel(metrics=("pat",))
    led = new_ledger(tmp_path)
    led.append(
        RecordType.ENTITY,
        {"symbol": TARGET, "subtype": subtype, "subtype_evidence": {}, "peer_rule": "same subtype"},
        Author.code("resolve"),
    )
    claim = draft_claim(led, signal_on(led, panel, "pat"), 0, panel)
    assert claim.angle_id == angle_id
    assert ("angle to test" in claim.mechanism) is (angle_id is not None)
    led.close()


def test_own_history_baseline_reads_plainly(tmp_path):
    panel = make_panel(metrics=("gnpa_pct",), n_peers=0)
    led = new_ledger(tmp_path)
    sig_rid = signal_on(led, panel, "gnpa_pct")
    assert led.get(sig_rid).payload["specificity_source"] == "na"
    claim = draft_claim(led, sig_rid, 0, panel)
    assert "against its own history" in claim.statement and "away from its own history" in claim.mechanism
    led.close()
