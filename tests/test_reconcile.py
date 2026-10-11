from datetime import date

from signalcourt.calc import reconcile as R
from signalcourt.extract.types import FactProposal, SpanProposal


def fp(metric="pat", value=100.0, text="100.0", extractor="xbrl", unit="inr_cr", pe=date(2026, 6, 30)):
    return FactProposal("A", metric, value, unit, pe, "standalone", extractor, text, SpanProposal(kind="xbrl"))


def test_sanity_pass_fail_and_unregistered():
    assert R.sanity(fp("pat", 5000)).status == "pass"
    assert R.sanity(fp("gnpa_pct", 3.2, unit="fraction")).status == "fail"  # 320% is a unit slip
    assert R.sanity(fp("no_such_metric", 1)).status == "single_source"


def test_cross_source_tolerance_and_rounding():
    assert R.cross_source(fp(value=10261.0), fp(value=10262.0, extractor="table")).status == "pass"  # 0.01%
    assert R.cross_source(fp(value=10261.0), fp(value=10900.0, extractor="table")).status == "fail"
    a = fp("gnpa_pct", 0.0255, "0.0255", unit="fraction")
    b = fp("gnpa_pct", 0.0256, "2.56%", "table", unit="fraction")
    assert R.cross_source(a, b).status == "pass"  # within 0.5% relative


def test_composition_and_segment_sum():
    assert R.composition({"a": 0.5, "b": 0.3, "c": 0.2}).status == "pass"
    assert R.composition({"a": 0.5, "b": 0.3, "c": 0.15}).status == "fail"
    assert R.segment_sum([100.0, 50.0], 150.4).status == "pass"
    assert R.segment_sum([100.0, 50.0], 160.0).status == "fail"


def test_silent_restatement_keeps_both_uses_latest_writes_event():
    old = R.DatedFact(fp(value=100.0), date(2026, 8, 1), "FA-old")
    new = R.DatedFact(fp(value=112.0, text="112.0"), date(2026, 11, 3), "FA-new")
    out = R.detect_restatement([new, old])
    assert out.used.rid == "FA-new" and [s.rid for s in out.superseded] == ["FA-old"]
    assert out.regevent["event_type"] == "restatement" and out.regevent["old_rid"] == "FA-old"
    assert out.results[0].restatement["old"] == 100.0 and out.results[0].restatement["new"] == 112.0


def test_identical_republication_is_not_a_restatement():
    a = R.DatedFact(fp(value=100.0), date(2026, 8, 1), "FA1")
    b = R.DatedFact(fp(value=100.0), date(2026, 11, 3), "FA2")
    out = R.detect_restatement([a, b])
    assert out.regevent is None and out.results[0].status == "pass"
