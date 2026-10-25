"""Baselines: synthetic twin, peer median and own history, and the rule that chooses between them."""
import numpy as np
import pandas as pd

from signalcourt.sensors.baseline import fit_baseline
from signalcourt.settings import load_config

S = load_config().model_dump()
IDX = list(range(80, 100))  # 20 quarters


def donors_frame(n_noise, rng, extra=None):
    cols = {f"N{i:02d}": rng.normal(0, 1, len(IDX)).cumsum() for i in range(n_noise)}
    cols.update(extra or {})
    return pd.DataFrame(cols, index=IDX)


def test_twin_recovers_known_weights():
    rng = np.random.default_rng(1)
    a, b = rng.normal(0, 1, 20).cumsum(), rng.normal(0, 1, 20).cumsum()
    donors = donors_frame(15, rng, {"A": a, "B": b})
    target = pd.Series(0.6 * a + 0.4 * b, index=IDX)
    base = fit_baseline(target, donors, IDX[-1], S)
    assert base.source == "twin"
    assert abs(base.weights["A"] - 0.6) < 0.05 and abs(base.weights["B"] - 0.4) < 0.05
    assert abs(sum(base.weights.values()) - 1.0) < 1e-9
    assert base.n_donors == 17 and base.pre_rmspe_sd is not None and base.pre_rmspe_sd < 0.1
    assert (base.series - target).abs().max() < 0.1


def test_twin_that_fails_its_quality_gate_falls_back_to_peer_median():
    rng = np.random.default_rng(2)
    donors = donors_frame(15, rng)
    target = pd.Series(rng.normal(0, 1, 20).cumsum() + 40, index=IDX)  # unrelated to every donor
    base = fit_baseline(target, donors, IDX[-1], S)
    assert base.source == "peer_median" and base.weights == {}
    assert base.pre_rmspe_sd is not None and base.pre_rmspe_sd > S["twin"]["max_pre_rmspe_sd"]
    assert "twin gate" in base.note


def test_peer_median_with_five_donors():
    rng = np.random.default_rng(3)
    donors = donors_frame(5, rng)
    target = pd.Series(rng.normal(0, 1, 20), index=IDX)
    base = fit_baseline(target, donors, IDX[10], S)
    assert base.source == "peer_median" and base.n_donors == 5
    assert np.allclose(base.series.values, donors.median(axis=1).values)
    assert "donors" in base.note


def test_own_history_with_two_donors():
    rng = np.random.default_rng(4)
    donors = donors_frame(2, rng)
    target = pd.Series(np.r_[np.full(10, 5.0) + rng.normal(0, 0.1, 10), np.full(10, 9.0)], index=IDX)
    base = fit_baseline(target, donors, IDX[9], S)
    assert base.source == "own_history" and base.n_donors == 2
    assert (base.series == target.iloc[:10].median()).all()  # flat at the pre-break median
    assert base.series.index.equals(target.index)


def test_no_donors_at_all_is_own_history():
    target = pd.Series(np.arange(20.0), index=IDX)
    base = fit_baseline(target, pd.DataFrame(index=IDX), IDX[9], S)
    assert base.source == "own_history" and base.n_donors == 0


def test_donor_with_too_few_pre_period_quarters_is_dropped_not_fatal():
    rng = np.random.default_rng(5)
    donors = donors_frame(6, rng)
    donors.loc[IDX[:6], "N00"] = np.nan  # reports only 4 of the 10 pre-period quarters
    donors.loc[IDX[2], "N01"] = np.nan  # one gap is tolerated
    base = fit_baseline(pd.Series(rng.normal(0, 1, 20), index=IDX), donors, IDX[9], S)
    assert base.source == "peer_median" and base.n_donors == 5


def test_twin_interpolates_an_interior_gap_in_a_donor():
    rng = np.random.default_rng(8)
    a = rng.normal(0, 1, 20).cumsum()
    donors = donors_frame(15, rng, {"A": a})
    donors.loc[IDX[4], "A"] = np.nan
    base = fit_baseline(pd.Series(a, index=IDX), donors, IDX[-1], S)
    assert base.source == "twin" and base.n_donors == 16


def test_short_pre_period_cannot_use_the_twin():
    rng = np.random.default_rng(6)
    a = rng.normal(0, 1, 20).cumsum()
    donors = donors_frame(15, rng, {"A": a})
    base = fit_baseline(pd.Series(a, index=IDX), donors, IDX[S["twin"]["pre_period_min_q"] - 2], S)
    assert base.source == "peer_median"


def test_donor_missing_one_quarter_of_a_short_pre_period_still_counts_for_the_peer_median():
    """Pre-period exactly ``pre_period_min_q`` long: one missing quarter must not drop the donor."""
    rng = np.random.default_rng(9)
    donors = donors_frame(6, rng)
    pre_q = S["twin"]["pre_period_min_q"]
    donors.loc[IDX[3], ["N00", "N01", "N02"]] = np.nan  # half the donors skipped one pre-period quarter
    base = fit_baseline(pd.Series(rng.normal(0, 1, 20), index=IDX), donors, IDX[pre_q - 1], S)
    assert base.source == "peer_median" and base.n_donors == 6
    assert np.allclose(base.series.values, donors.median(axis=1, skipna=True).values)


def test_donor_below_the_coverage_share_is_dropped():
    rng = np.random.default_rng(10)
    donors = donors_frame(6, rng)
    pre_q = S["twin"]["pre_period_min_q"]
    need = max(S["sensors"]["donor_min_quarters"], int(np.ceil(S["sensors"]["donor_min_coverage"] * pre_q)))
    donors.loc[IDX[: pre_q - need + 1], "N00"] = np.nan  # one quarter short of the share
    base = fit_baseline(pd.Series(rng.normal(0, 1, 20), index=IDX), donors, IDX[pre_q - 1], S)
    assert base.n_donors == 5


def test_donor_needs_a_minimum_number_of_observed_quarters():
    rng = np.random.default_rng(11)
    donors = donors_frame(6, rng)
    floor = S["sensors"]["donor_min_quarters"]
    donors.loc[IDX[floor - 1 : floor + 1], "N00"] = np.nan  # pre-period of floor + 1 quarters, N00 sees floor - 1
    base = fit_baseline(pd.Series(rng.normal(0, 1, 20), index=IDX), donors, IDX[floor], S)
    assert base.n_donors == 5


def test_twin_uses_donors_with_one_interior_gap_in_a_short_pre_period():
    rng = np.random.default_rng(12)
    a = rng.normal(0, 1, 20).cumsum()
    donors = donors_frame(15, rng, {"A": a})
    pre_q = S["twin"]["pre_period_min_q"]
    donors.loc[IDX[2], ["A"] + list(donors.columns[:9])] = np.nan  # ten donors skipped the same interior quarter
    base = fit_baseline(pd.Series(a, index=IDX), donors, IDX[pre_q - 1], S)
    assert base.source == "twin" and base.n_donors == 16
    assert base.weights["A"] > 0.9
