from datetime import date

from conftest import fixed_clock, run_payload
from test_judge import SETTINGS, Court

from signalcourt.ledger import Author, Ledger, RecordType
from signalcourt.pipeline import RunContext
from signalcourt.stages import judge_stage


def ctx_for(led, run_dir, claims):
    return RunContext(
        ledger=led,
        store=None,
        settings=SETTINGS,
        company="Test Co",
        symbol="TESTCO",
        as_of=date(2026, 10, 1),
        mode="replay",
        run_dir=run_dir,
        impl={"judge": "real"},
        claims=list(claims),
    )


def add_claim(c, signal_p, n=60):
    sig = c.led.append(
        RecordType.SIGNAL,
        {
            "sensor": "t",
            "entity": "TESTCO",
            "metrics": ["m"],
            "direction": "up",
            "specificity": 0.8,
            "specificity_source": "twin",
            "placebo": {"p_raw": signal_p, "n": n},
        },
        Author.code("sensors"),
    ).rid
    return sig


def test_judge_stage_appends_one_verdict_per_claim_and_sets_ctx(tmp_path):
    c = Court(tmp_path, p_raw=0.01)
    c.make_strong()
    ctx = ctx_for(c.led, c.led.run_dir, [c.claim])
    judge_stage.judge(ctx)
    verdicts = c.led.query(RecordType.VERDICT)
    assert len(verdicts) == 1
    assert verdicts[0].payload["claim_rid"] == c.claim
    assert verdicts[0].author.component == "judge"
    assert ctx.verdicts == {c.claim: verdicts[0].payload["grade"]}
    # one claim: Holm leaves p unchanged
    assert verdicts[0].payload["stat"]["p_holm"] == 0.01
    assert verdicts[0].payload["grade"] == "Strong"
    c.led.close()


def test_judge_stage_applies_holm_across_claims(tmp_path):
    c = Court(tmp_path, p_raw=0.04)
    c.make_strong()
    # a second claim on a second signal; Holm over {0.04, 0.03} multiplies the smaller by 2
    sig2 = add_claim(c, 0.03)
    claim2 = c.led.append(
        RecordType.CLAIM,
        {
            "hypothesis_index": 1,
            "statement": "s",
            "mechanism": "m",
            "signal_rid": sig2,
            "falsification": {
                "metric": "m",
                "comparator": ">",
                "threshold": 1.0,
                "deadline_period": "Q4FY27",
                "source_rule": "r",
            },
            "drafter": {"model": "x", "family": "f"},
        },
        Author.code("prereg"),
    ).rid
    ctx = ctx_for(c.led, c.led.run_dir, [c.claim, claim2])
    judge_stage.judge(ctx)
    by_claim = {v.payload["claim_rid"]: v.payload for v in c.led.query(RecordType.VERDICT)}
    assert by_claim[claim2]["stat"]["p_holm"] == 0.06
    assert by_claim[c.claim]["stat"]["p_holm"] == 0.06
    assert by_claim[c.claim]["grade"] == "Strong"  # 0.06 <= stat_holm_max
    assert set(ctx.verdicts) == {c.claim, claim2}
    c.led.close()


def test_judge_stage_with_no_claims_writes_nothing(tmp_path):
    led = Ledger.create(tmp_path / "run", run_payload(), clock=fixed_clock())
    before = len(led)
    ctx = ctx_for(led, led.run_dir, [])
    judge_stage.judge(ctx)
    assert len(led) == before and ctx.verdicts == {}
    led.close()
