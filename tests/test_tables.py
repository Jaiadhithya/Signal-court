import pytest

from signalcourt.extract import tables as T


@pytest.mark.parametrize(
    "text,val,kind",
    [
        ("1,23,456.78", 123456.78, "plain"),
        ("(1,234)", -1234.0, "plain"),
        ("−45.5", -45.5, "plain"),
        ("2.55%", 2.55, "percent"),
        ("12,345 cr", 12345.0, "crore"),
        ("₹ 3.2 bn", 3.2, "billion"),
        ("15 bps", 15.0, "bps"),
        ("`1,05,000", 105000.0, "plain"),
        ("4.5x", 4.5, "multiple"),
    ],
)
def test_parse_number(text, val, kind):
    p = T.parse_number(text)
    assert p.value == pytest.approx(val) and p.unit == kind


def test_parse_number_none():
    assert T.parse_number("n.a.") is None


@pytest.mark.parametrize(
    "hdr,lbl",
    [
        ("Q1FY27", "Q1FY27"),
        ("Q1 FY 27", "Q1FY27"),
        ("Q1 FY2027", "Q1FY27"),
        ("1QFY27", "Q1FY27"),
        ("Jun-26", "Q1FY27"),
        ("Jun 2026", "Q1FY27"),
        ("30.06.2026", "Q1FY27"),
        ("31-Mar-25", "Q4FY25"),
        ("31/03/2025", "Q4FY25"),
        ("Dec-25", "Q3FY26"),
        ("YoY", None),
        ("Apr-26", None),
    ],
)
def test_period_labels(hdr, lbl):
    assert T.parse_period_label(hdr) == lbl


DECK = [
    "Asset quality  (₹ crore)",
    "Particulars  Q2FY27  Q1FY27  Q2FY26  YoY",
    "Gross NPA  29,836.94  30,100.10  31,000.00  (3.8%)",
    "Gross NPA %  2.55%  2.60%  2.80%  -25 bps",
    "Net NPA %  0.51%  0.52%  0.60%  -9 bps",
    "Provision coverage ratio  82.0%  81.5%  79.0%  300 bps",
    "Random Line  1  2  3  4",
]


def test_table_extraction_maps_columns_and_skips_comparisons():
    facts = T.extract_from_lines(DECK, subtype="bank")
    got = {(f.metric, f.period_label): f.parsed.text for f in facts}
    assert got[("gnpa_amt", "Q2FY27")] == "29,836.94" and got[("gnpa_amt", "Q2FY26")] == "31,000.00"
    assert got[("gnpa_pct", "Q1FY27")] == "2.60%" and got[("pcr", "Q2FY27")] == "82.0%"
    assert not any(k[0] == "random_line" for k in got)
    assert all(f.period_label in ("Q2FY27", "Q1FY27", "Q2FY26") for f in facts)  # YoY column dropped


def test_proposals_convert_units_and_keep_row_text_for_quote_gate():
    hint = T.detect_unit_hint(DECK[0])
    assert hint == "crore"
    props = T.to_fact_proposals(T.extract_from_lines(DECK, subtype="bank"), "A", "standalone", "sha1", hint)
    by = {(p.metric, p.period_end.isoformat()): p for p in props}
    assert by[("gnpa_amt", "2026-09-30")].value == pytest.approx(29836.94)
    assert by[("gnpa_pct", "2026-09-30")].value == pytest.approx(0.0255)
    p = by[("gnpa_pct", "2026-09-30")]
    assert p.span.kind == "table_cell" and "Gross NPA %" in p.span.text and p.span.value_text == "2.55%"


def test_unit_hint_conversions_and_ragged_rows():
    bn = T.extract_from_lines(["Q2FY27 Q1FY27", "Advances  1,500.5  1,400.0"], subtype="bank")
    props = T.to_fact_proposals(bn, "A", "standalone", None, "billion")
    assert props[0].value == pytest.approx(150050.0)  # 1,500.5 bn = 150,050 cr
    assert T.extract_from_lines(["Q2FY27 Q1FY27 Q2FY26", "Advances  1,500.5  1,400.0"], subtype="bank") == []


def test_label_matching_threshold():
    assert T.match_label("Gross NPA ratio", "bank")[0] == "gnpa_pct"
    assert T.match_label("Employee welfare", "bank") is None
