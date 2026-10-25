"""Scale-free peer comparison and per-share adjustment for bonus issues and splits."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from signalcourt.calc import periods as P
from signalcourt.calc.panel import Panel
from signalcourt.sensors.gap import normalise, scan_metric
from signalcourt.sensors.per_share import adjust_per_share, share_count_steps
from signalcourt.settings import load_config

S = load_config().model_dump()
LAST = P.index("Q4FY26")
IDX = list(range(LAST - 19, LAST + 1))
TARGET = "BIGCO"


def _rows(entity, metric, values, rid="DC-00000001"):
    return [
        dict(entity=entity, metric=metric, period=P.from_index(q), pidx=q, basis="standalone", value=float(v),
             rid=rid if entity != TARGET else f"FA-{abs(hash((metric, q))) % 16**8:08x}", tier=1,
             published=date(2026, 9, 1), recon_status="pass")
        for q, v in zip(IDX, values, strict=True)
    ]


def _panel(rows):
    df = pd.DataFrame(rows)
    return Panel(df, TARGET, sorted(set(df.entity) - {TARGET}), date(2026, 10, 1))


def scaled_panel(break_q="Q2FY25", lift=1.4, target_scale=60.0, n_peers=30, seed=1):
    """Every company follows one shared shape at its own size; the target is far larger and lifts at the break."""
    rng = np.random.default_rng(seed)
    shape = 10.0 + 0.3 * np.arange(20) + rng.normal(0, 0.6, 20)
    brk = P.index(break_q)
    rows = []
    for i in range(n_peers):
        scale = 1.0 + 4.0 * rng.random()
        rows += _rows(f"PEER{i:02d}", "advances", scale * shape * (1 + rng.normal(0, 0.01, 20)))
    target = target_scale * shape * (1 + rng.normal(0, 0.01, 20))
    target = np.where(np.array(IDX) >= brk, target * lift, target)
    rows += _rows(TARGET, "advances", target)
    return _panel(rows)


def test_large_target_gets_a_twin_on_scale_free_series():
    sig = scan_metric(scaled_panel(), "advances", S)
    assert sig is not None and sig.break_period == "Q2FY25" and sig.direction == "up"
    assert sig.specificity_source == "twin", sig.surprise.coverage_status
    assert sig.placebo.p_raw < 0.1


def test_signal_size_is_reported_in_the_metric_units():
    sig = scan_metric(scaled_panel(), "advances", S)
    # target is about 60 x 14 crore at the break; a 40% lift is well over 100 crore, not a unit-free fraction
    assert sig.size > 100


def test_ratio_metrics_are_compared_as_levels():
    s = pd.Series([0.02, 0.021, 0.019, 0.022], index=IDX[:4])
    out, scale = normalise(s, IDX[2], "gnpa_pct")
    assert scale == 1.0 and out.equals(s)


def test_amounts_are_divided_by_their_own_pre_period_size():
    s = pd.Series([100.0, -50.0, 200.0, 400.0], index=IDX[:4])
    out, scale = normalise(s, IDX[2], "pat")
    assert scale == pytest.approx(100.0)  # median of |100|, |-50|, |200|
    assert out.tolist() == pytest.approx([1.0, -0.5, 2.0, 4.0])


def test_amount_with_no_pre_period_size_cannot_be_normalised():
    s = pd.Series([0.0, 0.0, 5.0], index=IDX[:3])
    out, scale = normalise(s, IDX[1], "pat")
    assert scale is None and out is None


def bonus_panel(bonus_q="Q2FY26", factor=2.0, with_pat=True, n_peers=30, seed=2):
    """PAT grows smoothly; the target's share count jumps by ``factor`` at ``bonus_q`` so its EPS drops."""
    rng = np.random.default_rng(seed)
    brk = P.index(bonus_q)
    rows = []
    for e in [TARGET] + [f"PEER{i:02d}" for i in range(n_peers)]:
        pat = (5.0 + rng.random() * 5) * (100 + 3 * np.arange(20)) * (1 + rng.normal(0, 0.02, 20))
        shares = np.full(20, 10.0 + rng.random() * 5)
        if e == TARGET:
            shares = np.where(np.array(IDX) >= brk, shares * factor, shares)
        if with_pat or e != TARGET:
            rows += _rows(e, "pat", pat)
        rows += _rows(e, "eps_basic", pat / shares)
    return _panel(rows)


def test_share_count_step_is_found_with_its_factor():
    p = bonus_panel(factor=2.0)
    steps = share_count_steps(p.series(TARGET, "pat"), p.series(TARGET, "eps_basic"), S["sensors"])
    assert steps == [(P.index("Q2FY26"), 2.0)]


def test_ordinary_noise_is_not_a_share_step():
    p = bonus_panel(factor=1.15)
    assert share_count_steps(p.series(TARGET, "pat"), p.series(TARGET, "eps_basic"), S["sensors"]) == []


def test_bonus_issue_makes_an_eps_signal_before_adjustment_and_none_after():
    raw = bonus_panel()
    before = scan_metric(raw, "eps_basic", S)
    assert before is not None and before.break_period == "Q2FY26"  # the trap: a bonus looks like a collapse
    adjusted, notes = adjust_per_share(raw, S)
    after = scan_metric(adjusted, "eps_basic", S)
    assert after is None or after.break_period != "Q2FY26"
    assert any("eps_basic" in n and "Q2FY26" in n for n in notes)


def test_adjustment_restates_earlier_eps_onto_the_new_share_base():
    raw = bonus_panel()
    adjusted, _ = adjust_per_share(raw, S)
    b = P.index("Q2FY26")
    r, a = raw.series(TARGET, "eps_basic"), adjusted.series(TARGET, "eps_basic")
    assert a[b - 1] == pytest.approx(r[b - 1] / 2.0)
    assert a[b] == pytest.approx(r[b])


def test_merger_sized_share_change_is_not_read_as_a_bonus():
    p = bonus_panel(factor=1.354)  # a merger issuing shares: close to 1.5 but not a standard step
    assert share_count_steps(p.series(TARGET, "pat"), p.series(TARGET, "eps_basic"), S["sensors"]) == []
    adjusted, notes = adjust_per_share(p, S)
    assert adjusted.series(TARGET, "eps_basic").empty
    assert any("eps_basic" in n and "without a bonus or split pattern" in n for n in notes)


def test_profit_that_does_not_match_eps_makes_the_share_count_unreadable():
    rows = []
    rng = np.random.default_rng(5)
    pat = 1000 + 30 * np.arange(20)
    eps = pat / 10 * rng.choice([0.6, 1.0, 1.7], 20)  # consolidated profit vs owners' EPS: no stable ratio
    rows += _rows(TARGET, "pat", pat) + _rows(TARGET, "eps_basic", eps)
    adjusted, notes = adjust_per_share(_panel(rows), S)
    assert adjusted.series(TARGET, "eps_basic").empty and notes


def test_per_share_metric_without_pat_is_dropped_with_a_note():
    adjusted, notes = adjust_per_share(bonus_panel(with_pat=False), S)
    assert adjusted.series(TARGET, "eps_basic").empty
    assert any("eps_basic" in n and "no profit series" in n for n in notes)


def test_sense_stage_does_not_report_a_bonus_issue(tmp_path):
    from conftest import make_ctx

    from signalcourt.pipeline import STAGE_ORDER
    from signalcourt.stages import sense_stage

    ctx = make_ctx(tmp_path, impl={s: "stub" for s in STAGE_ORDER})
    ctx.panel = bonus_panel()
    sense_stage.run(ctx)
    sigs = [ctx.ledger.get(r).payload for r in ctx.signals]
    assert not any(s["metrics"] == ["eps_basic"] and s["break_period"] == "Q2FY26" for s in sigs)
    ctx.ledger.close()
