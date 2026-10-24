"""Gap sensor: break, baseline gap, persistence, placebo p-value and surprise score, on synthetic panels."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from signalcourt.calc import periods as P
from signalcourt.calc.panel import Panel
from signalcourt.sensors.gap import scan_metric
from signalcourt.settings import load_config

S = load_config().model_dump()
LAST = P.index("Q4FY26")
TARGET = "TARGETCO"
DOC_RID = "DC-00000001"


def make_panel(
    metrics=("m1",), n_peers=30, quarters=20, break_q="Q2FY25", step=4.0, noise=0.3, seed=0, flat=False,
    peer_quarters=None,
):
    """Target and peers share a trend and a strong quarter effect; the target gets a step at ``break_q``."""
    rng = np.random.default_rng(seed)
    idx = list(range(LAST - quarters + 1, LAST + 1))
    brk = P.index(break_q) if break_q else None
    rows = []
    for mi, m in enumerate(metrics):
        common = 0.05 * np.arange(quarters) + rng.normal(0, 1.0, quarters)
        for e in [TARGET] + [f"PEER{i:02d}" for i in range(n_peers)]:
            qs = idx if (e == TARGET or peer_quarters is None) else idx[-peer_quarters:]
            vals = np.full(len(qs), 10.0 + mi) if flat else (
                10.0 + mi + common[-len(qs):] + rng.normal(0, noise, len(qs))
            )
            if e == TARGET and brk is not None and not flat:
                vals = vals + np.where(np.array(qs) >= brk, step, 0.0)
            for q, v in zip(qs, vals, strict=True):
                rid = f"FA-{mi:02d}{q:06x}" if e == TARGET else DOC_RID
                rows.append(
                    dict(entity=e, metric=m, period=P.from_index(q), pidx=q, basis="standalone", value=float(v),
                         rid=rid, tier=1, published=date(2026, 9, 1), recon_status="pass")
                )
    df = pd.DataFrame(rows)
    return Panel(df, TARGET, sorted(set(df.entity) - {TARGET}), date(2026, 10, 1))


def test_planted_break_is_found_with_low_p():
    sig = scan_metric(make_panel(), "m1", S)
    assert sig is not None
    assert sig.break_period == "Q2FY25" and sig.direction == "up"
    assert sig.placebo.p_raw < 0.1 and sig.placebo.n >= 30
    assert sig.specificity_source == "twin" and sig.sensor == "gap"
    assert sig.z > S["sensors"]["robust_z_threshold"] and sig.persistence_q >= S["sensors"]["persistence_min_quarters"]
    assert sig.specificity == pytest.approx(1 - sig.placebo.p_raw)
    sp = sig.surprise
    assert sp.score == pytest.approx(abs(sig.z) * min(1, sig.persistence_q / 4) * sig.specificity)
    assert sp.coverage_status.startswith("twin")
    assert sig.entity == TARGET and sig.metrics == ["m1"]


def test_downward_break_has_down_direction():
    sig = scan_metric(make_panel(step=-4.0), "m1", S)
    assert sig is not None and sig.direction == "down" and sig.z < 0


def test_signal_is_peer_specific_not_a_sector_move():
    # every entity steps at the same quarter: a sector move, so the gap to the peers stays flat
    p = make_panel(step=0.0, seed=3)
    brk = P.index("Q2FY25")
    p.df.loc[p.df.pidx >= brk, "value"] += 4.0
    assert scan_metric(p, "m1", S) is None


def test_flat_series_gives_no_signal():
    assert scan_metric(make_panel(flat=True, break_q=None), "m1", S) is None


def test_noise_only_rarely_gives_a_confident_signal():
    # a placebo p-value below 0.1 is expected about one time in ten on pure noise, never routinely
    sigs = [scan_metric(make_panel(break_q=None, seed=s), "m1", S) for s in range(12)]
    confident = [g for g in sigs if g is not None and g.placebo.p_raw < 0.1]
    assert len(confident) <= 3


def test_short_history_gives_no_signal():
    n = S["panel"]["min_quarters_to_run"] - 1
    assert scan_metric(make_panel(quarters=n, break_q=P.from_index(LAST - 2)), "m1", S) is None


def test_missing_metric_gives_no_signal():
    assert scan_metric(make_panel(), "not_a_metric", S) is None


@pytest.mark.parametrize("blip", [False, True])
def test_mad_zero_does_not_crash(blip):
    p = make_panel(n_peers=2, flat=True, break_q=None)
    brk = P.index("Q2FY25")
    p.df.loc[(p.df.entity == TARGET) & (p.df.pidx >= brk), "value"] += 5.0
    if blip:  # constant except one quarter, so MAD is zero but the spread is not
        p.df.loc[(p.df.entity == TARGET) & (p.df.pidx == brk - 5), "value"] += 0.5
    sig = scan_metric(p, "m1", S)  # must not raise; a zero pre-break spread cannot give an honest z
    assert sig is None or np.isfinite(sig.z)


def test_few_peer_subtype_uses_fallback_and_records_it():
    own = scan_metric(make_panel(n_peers=1), "m1", S)
    assert own is not None and own.specificity_source == "na"
    assert own.surprise.coverage_status.startswith("own_history: 1 donor")
    assert own.placebo.n <= 1 + len(S["placebo"]["in_time_shifts"])  # one peer plus placebos in time

    med = scan_metric(make_panel(n_peers=S["demo"]["peer_median_min_donors"] + 1), "m1", S)
    assert med is not None and med.specificity_source == "peer_median"
    assert med.surprise.coverage_status.startswith("peer_median")

    zero = scan_metric(make_panel(n_peers=0), "m1", S)
    assert zero is not None and zero.specificity_source == "na"
    assert zero.surprise.coverage_status.startswith("own_history: 0 donors")
    assert zero.placebo.p_raw >= 1 / (1 + len(S["placebo"]["in_time_shifts"]))


def test_surprise_is_floored():
    sig = scan_metric(make_panel(n_peers=0), "m1", S)
    assert sig.surprise.score >= S["rank"]["surprise_floor"]


def test_peers_with_short_history_are_skipped_not_fatal():
    sig = scan_metric(make_panel(peer_quarters=5), "m1", S)
    assert sig is not None and sig.specificity_source == "na"


def test_inputs_hold_target_rids_and_the_peer_doc_once():
    sig = scan_metric(make_panel(), "m1", S)
    assert sig.inputs.count(DOC_RID) == 1
    assert len([r for r in sig.inputs if r.startswith("FA-")]) == 20
    assert len(set(sig.inputs)) == len(sig.inputs)
    solo = scan_metric(make_panel(n_peers=0), "m1", S)
    assert DOC_RID not in solo.inputs
