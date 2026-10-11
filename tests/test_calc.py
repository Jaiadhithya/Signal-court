from datetime import date

import numpy as np
import pandas as pd
import pytest

from signalcourt.calc import calculator as C
from signalcourt.calc import formulas as F
from signalcourt.calc import periods as P
from signalcourt.calc.panel import PanelFact, build_panel


def test_periods_roundtrip_and_fy_boundaries():
    assert P.label(date(2026, 6, 30)) == "Q1FY27"
    assert P.label(date(2027, 3, 31)) == "Q4FY27"
    assert P.label(date(2026, 3, 31)) == "Q4FY26"
    assert P.end("Q1FY27") == date(2026, 6, 30)
    assert P.end("Q3FY25") == date(2024, 12, 31)
    assert P.end("Q4FY26") == date(2026, 3, 31)
    assert P.start("Q2FY24") == date(2023, 7, 1)
    assert P.shift("Q1FY27", -1) == "Q4FY26"
    assert P.index("Q1 FY 2027") == P.index("Q1FY27")
    assert P.last_n("Q2FY27", 3) == ["Q4FY26", "Q1FY27", "Q2FY27"]
    for i in range(8000, 8100):
        assert P.index(P.from_index(i)) == i and P.index(P.end(P.from_index(i))) == i


def _env(n=10):
    idx = list(range(8000, 8000 + n))
    return {"x": pd.Series([100.0 + 10 * i for i in range(n)], index=idx), "y": pd.Series([50.0] * n, index=idx)}


def test_evaluate_functions():
    e = _env()
    assert F.evaluate("x - y", e).iloc[0] == 50.0
    yoy = F.evaluate("yoy(x)", e)
    assert np.isnan(yoy.iloc[3]) and yoy.iloc[4] == pytest.approx(140 / 100 - 1)
    assert F.evaluate("lag(x, 1)", e).iloc[1] == 100.0
    assert F.evaluate("mean(x, lag(x, 1))", e).iloc[1] == pytest.approx(105.0)
    assert np.isnan(F.evaluate("mean(x, lag(x, 1))", e).iloc[0])  # half-missing mean refuses
    assert F.evaluate("(x / lag(x, 8)) ** 0.5 - 1", e).iloc[8] == pytest.approx((180 / 100) ** 0.5 - 1)
    assert F.evaluate("abs(y - x)", e).iloc[2] == 70.0
    assert F.evaluate("max(x, y)", e).iloc[0] == 100.0


def test_division_by_zero_is_nan_not_crash():
    e = {"x": pd.Series([1.0, 2.0], index=[1, 2]), "z": pd.Series([0.0, 4.0], index=[1, 2])}
    out = F.evaluate("x / z", e)
    assert np.isnan(out.iloc[0]) and out.iloc[1] == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os').system('x')",
        "x.__class__",
        "[1,2]",
        "x if y else z",
        "lambda: 1",
        "x[0]",
        "open('f')",
        "x < y",
        "'a' + x",
    ],
)
def test_evaluator_rejects_everything_outside_the_whitelist(bad):
    with pytest.raises(F.FormulaError):
        F.evaluate(bad, _env())


def _book(rec="pass"):
    fb = C.FactBook()
    for i, q in enumerate(P.span("Q1FY24", "Q4FY26")):
        fb.add("A", "advances", q, 1000.0 + 50 * i, f"FA-{i}", rec)
        fb.add("A", "provisions", q, 20.0, f"FP-{i}")
        fb.add("A", "pat", q, 100.0 + i, f"FT-{i}")
        fb.add("A", "ppop", q, 150.0 + i * 0.5, f"FO-{i}")
    return fb


def test_calc_value_inputs_and_dedup():
    sink = C.InMemorySink()
    calc = C.Calculator(_book(), sink)
    r = calc.compute("credit_cost", "A", "Q4FY26")
    a_last, a_prev = 1000 + 50 * 11, 1000 + 50 * 10
    assert r.value == pytest.approx(4 * 20 / ((a_last + a_prev) / 2))
    assert r.inputs == ["FA-10", "FA-11", "FP-10", "FP-11"]
    assert calc.compute("credit_cost", "A", "Q4FY26").rid == r.rid
    assert len(sink.records) == 1
    assert calc.compute("credit_cost", "A", "Q3FY26").rid != r.rid


def test_nested_formulas_and_request_path():
    fb = C.FactBook()
    for i, q in enumerate(P.span("Q1FY25", "Q4FY26")):
        fb.add("A", "interest_earned", q, 200.0 + i, f"I{i}")
        fb.add("A", "interest_expended", q, 100.0, f"E{i}")
        fb.add("A", "other_income", q, 30.0, f"O{i}")
        fb.add("A", "opex", q, 60.0, f"X{i}")
    calc = C.Calculator(fb, C.InMemorySink())
    assert calc.compute("nii", "A", "Q4FY26").value == 107.0
    assert calc.request(C.CalcRequest("cost_to_income", "A", "Q4FY26")).value == pytest.approx(60 / (107 + 30))


def test_missing_input_raises_instead_of_inventing():
    calc = C.Calculator(_book(), C.InMemorySink())
    with pytest.raises(F.FormulaError):
        calc.compute("yoy_advances", "A", "Q2FY24")  # no prior-year quarter
    with pytest.raises(F.FormulaError):
        calc.compute("ldr", "A", "Q4FY26")  # no deposits series


def test_recon_fail_propagates_to_calc():
    calc = C.Calculator(_book(rec="fail"), C.InMemorySink())
    assert calc.compute("credit_cost", "A", "Q4FY26").recon_status == "fail"
    calc2 = C.Calculator(_book(rec="single_source"), C.InMemorySink())
    assert calc2.compute("credit_cost", "A", "Q4FY26").recon_status == "single_source"


def test_series_helper_and_pat_minus_ppop():
    calc = C.Calculator(_book(), C.InMemorySink())
    s = calc.series("A", "yoy(pat) - yoy(ppop)", ["Q4FY26"])
    assert s.iloc[0] == pytest.approx((111 / 107 - 1) - (155.5 / 153.5 - 1))


class _Src:
    def __init__(self, rows):
        self.rows = rows

    def panel_facts(self, ents):
        return [r for r in self.rows if r.entity in ents]


def _pf(e, q, v, pub, basis="standalone", rid=None):
    return PanelFact(e, "pat", q, basis, v, rid or f"{e}-{q}-{pub}", 1, pub)


def test_panel_time_gate_restatement_and_window():
    rows = [
        _pf("A", "Q1FY27", 10, date(2026, 8, 1)),
        _pf("A", "Q4FY26", 9, date(2026, 5, 10)),
        _pf("A", "Q4FY26", 9.5, date(2026, 8, 1)),  # restated in the Q1 filing
        _pf("B", "Q4FY26", 4, date(2026, 5, 15)),
        _pf("B", "Q4FY26", 4, date(2026, 5, 15), basis="consolidated"),
    ]
    p = build_panel("A", ["B"], date(2026, 7, 1), _Src(rows))
    assert p.periods("A") == ["Q4FY26"]  # Q1FY27 not public yet
    assert p.series("A", "pat").iloc[0] == 9  # as-of view uses the original number
    p2 = build_panel("A", ["B"], date(2026, 9, 1), _Src(rows))
    assert p2.series("A", "pat").iloc[-1] == 10 and p2.series("A", "pat").iloc[0] == 9.5
    assert len(p2.df[(p2.df.entity == "B")]) == 1  # one basis per entity
    p3 = build_panel("A", [], date(2026, 9, 1), _Src(rows), quarters=1)
    assert p3.periods("A") == ["Q1FY27"]


def test_panel_parquet_roundtrip(tmp_path):
    rows = [_pf("A", "Q4FY26", 9, date(2026, 5, 10))]
    p = build_panel("A", [], date(2026, 9, 1), _Src(rows))
    p.save(tmp_path / "p.parquet")
    assert pd.read_parquet(tmp_path / "p.parquet").value.iloc[0] == 9
    fb = p.to_factbook()
    assert fb.ref("A", "pat", P.index("Q4FY26")).rid == rows[0].rid
