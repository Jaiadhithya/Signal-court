"""Corporate-action markers: scheme filings and unexplained share-count moves discount a coinciding break."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import make_ctx, put_pdf
from test_gap_sensor import make_panel

from signalcourt.calc import periods as P
from signalcourt.calc.panel import Panel
from signalcourt.ledger import Author, RecordType, verify_run
from signalcourt.ledger.schema import DocPayload, PlaceboStat, SignalPayload, Surprise
from signalcourt.pipeline import STAGES
from signalcourt.sensors.corp_actions import Marker, apply_markers, scheme_markers, share_count_markers
from signalcourt.settings import load_config

S = load_config().model_dump()
SENS = S["sensors"]
TARGET = "TARGETCO"


@pytest.fixture
def real_sense():
    """The real sense impl; the registry is left as it was found, so other modules still see what they expect."""
    had = STAGES["sense"].get("real")
    from signalcourt.stages import sense_stage

    STAGES["sense"]["real"] = sense_stage.sense
    yield sense_stage.sense
    if had is None:
        STAGES["sense"].pop("real", None)
    else:
        STAGES["sense"]["real"] = had


def add_scheme_doc(ledger, store, publish, title, *, symbol=TARGET, gate="ok", n=0):
    meta = put_pdf(
        store, [f"scheme filing {publish.isoformat()} {n}"], publish=publish, doc_type="scheme", title=title,
        entity_symbol=symbol, url=f"https://example.test/s{publish.isoformat()}-{n}.pdf",
        final_url=f"https://example.test/s{publish.isoformat()}-{n}.pdf",
    )
    doc = DocPayload(
        sha256=meta.sha256, url=meta.first_url, final_url=meta.final_url, doc_type="scheme", source_id="exchange",
        source_family="exchange", origin_id=meta.first_url, entity_symbol=symbol,
        publish_date=publish.isoformat(), publish_date_source="exchange_broadcast", time_gate=gate,
    )
    return ledger.append(RecordType.DOC, doc, Author.code("harvest")).rid


def test_merger_filings_cluster_into_one_marker_at_the_latest_filing(tmp_path, store):
    ctx = make_ctx(tmp_path, {}, store=store, symbol=TARGET)
    dates = [date(2022, 4, 4), date(2022, 7, 3), date(2022, 9, 20), date(2022, 12, 8), date(2023, 3, 17),
             date(2023, 5, 15), date(2023, 7, 1)]
    rids = [add_scheme_doc(ctx.ledger, store, d, "Amalgamation/Merger", n=i) for i, d in enumerate(dates)]
    marks = scheme_markers(ctx.ledger, store, TARGET, SENS)
    assert len(marks) == 1
    m = marks[0]
    assert m.source == "scheme" and m.pidx == P.index(date(2023, 7, 1)) and m.rid == rids[-1]
    assert "7 scheme filings" in m.detail


def test_routine_scheme_categories_and_gated_docs_are_not_markers(tmp_path, store):
    ctx = make_ctx(tmp_path, {}, store=store, symbol=TARGET)
    for i, d in enumerate([date(2024, 3, 1), date(2024, 3, 15), date(2024, 5, 14)]):
        add_scheme_doc(ctx.ledger, store, d, "Acquisition", n=i)  # stake purchases are routine
    add_scheme_doc(ctx.ledger, store, date(2024, 8, 1), "Disclosure under SEBI Takeover Regulations")
    add_scheme_doc(ctx.ledger, store, date(2024, 9, 1), "Amalgamation/Merger", gate="dropped")  # after as-of
    add_scheme_doc(ctx.ledger, store, date(2024, 9, 2), "Amalgamation/Merger", symbol="OTHERCO")
    assert scheme_markers(ctx.ledger, store, TARGET, SENS) == []
    assert scheme_markers(ctx.ledger, None, TARGET, SENS) == []  # no store, no titles: nothing to read


def test_filings_far_apart_give_separate_markers(tmp_path, store):
    ctx = make_ctx(tmp_path, {}, store=store, symbol=TARGET)
    add_scheme_doc(ctx.ledger, store, date(2022, 12, 1), "Scheme of Arrangement", n=0)
    add_scheme_doc(ctx.ledger, store, date(2025, 6, 1), "Amalgamation/Merger", n=1)
    marks = scheme_markers(ctx.ledger, store, TARGET, SENS)
    assert [m.pidx for m in marks] == [P.index(date(2022, 12, 1)), P.index(date(2025, 6, 1))]


def share_panel(jump_q="Q2FY24", ratio=1.35):
    idx = list(range(P.index("Q1FY23"), P.index("Q4FY26") + 1))
    rng = np.random.default_rng(0)
    pat = 100 + np.arange(len(idx)) + rng.normal(0, 1, len(idx))
    shares = np.where(np.array(idx) >= P.index(jump_q), 10.0 * ratio, 10.0)
    rows = []
    for metric, vals in (("pat", pat), ("eps_basic", pat / shares)):
        for q, v in zip(idx, vals, strict=True):
            rows.append(dict(entity=TARGET, metric=metric, period=P.from_index(q), pidx=q, basis="standalone",
                             value=float(v), rid=f"FA-{q:08x}", tier=1, published=date(2026, 9, 1),
                             recon_status="pass"))
    return Panel(pd.DataFrame(rows), TARGET, [], date(2026, 10, 1))


def test_unexplained_share_count_move_is_a_marker():
    marks = share_count_markers(share_panel(), SENS)
    assert len(marks) == 1
    m = marks[0]
    assert m.source == "share_count" and m.pidx == P.index("Q2FY24") and m.rid is None
    assert m.detail == "share-count move x1.35 at Q2FY24"


def test_bonus_issue_step_is_not_a_marker():
    assert share_count_markers(share_panel(ratio=2.0), SENS) == []


def signal(break_period, score=4.0):
    return SignalPayload(
        sensor="gap", entity=TARGET, metrics=["m1"], direction="up", break_period=break_period, size=1.0, z=score,
        persistence_q=4, specificity=1.0, specificity_source="twin", placebo=PlaceboStat(p_raw=0.0, n=30, rank=1),
        surprise=Surprise(score=score, z=score, persistence=1.0, specificity=1.0, coverage_status="twin: 20 donors"),
    )


def test_a_break_next_to_a_marker_is_discounted_and_says_why():
    doc = Marker(P.index("Q1FY24"), "scheme", "DC-0000abcd", "3 scheme filings (Amalgamation/Merger) to 2023-07-01")
    share = Marker(P.index("Q2FY24"), "share_count", None, "share-count move x1.35 at Q2FY24")
    s = signal("Q2FY24")
    assert apply_markers(s, [doc, share], SENS)
    assert s.surprise.score == pytest.approx(4.0 * SENS["corp_action_factor"])
    assert s.corp_action_rid == "DC-0000abcd"  # a document beats a data-only marker
    assert "corporate action" in s.surprise.coverage_status
    assert "Amalgamation/Merger" in s.surprise.coverage_status and "x1.35 at Q2FY24" in s.surprise.coverage_status


def test_share_count_marker_alone_leaves_rid_empty_and_names_the_move():
    s = signal("Q3FY24")
    assert apply_markers(s, [Marker(P.index("Q2FY24"), "share_count", None, "share-count move x1.35 at Q2FY24")],
                         SENS)
    assert s.corp_action_rid is None and "share-count move x1.35 at Q2FY24" in s.surprise.coverage_status


def test_a_break_outside_the_window_is_untouched():
    far = P.index("Q2FY24") + SENS["corp_action_window_q"] + 1
    s = signal(P.from_index(far))
    assert not apply_markers(s, [Marker(P.index("Q2FY24"), "share_count", None, "x")], SENS)
    assert s.surprise.score == 4.0 and s.corp_action_rid is None and s.surprise.coverage_status == "twin: 20 donors"


def test_sense_stage_ranks_a_merger_break_below_a_clean_one(tmp_path, store, real_sense):
    """The bigger break coincides with a merger filing: it is kept, discounted, and ranked below the smaller one."""
    big = make_panel(metrics=["m1"], n_peers=20, break_q="Q2FY25", step=8.0, seed=1).df
    small = make_panel(metrics=["m2"], n_peers=20, break_q="Q4FY24", step=4.0, seed=2).df
    df = pd.concat([big, small], ignore_index=True)
    panel = Panel(df, TARGET, sorted(set(df.entity) - {TARGET}), date(2026, 10, 1))
    ctx = make_ctx(tmp_path, {"sense": "real"}, store=store, symbol=TARGET)
    ctx.panel = panel
    rid = add_scheme_doc(ctx.ledger, store, date(2024, 9, 25), "Amalgamation/Merger")  # Q2FY25
    real_sense(ctx)
    sigs = [ctx.ledger.get(r).payload for r in ctx.signals]
    assert [s["metrics"][0] for s in sigs] == ["m2", "m1"]
    m1 = sigs[1]
    assert m1["corp_action_rid"] == rid and "corporate action" in m1["surprise"]["coverage_status"]
    assert sigs[0]["corp_action_rid"] is None
    assert verify_run(ctx.run_dir).ok
