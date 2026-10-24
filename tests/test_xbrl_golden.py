"""Golden tests against real public NSE filings (legacy formats). Values hand-checked against the files.

Integrated-format, NBFC and insurer goldens live in test_xbrl_integrated.py.
"""

from datetime import date

import pytest
from conftest import FIX

from signalcourt.extract import xbrl as X
from signalcourt.extract import xbrl_map as XM


@pytest.fixture(scope="module")
def bank():
    return X.parse((FIX / "xbrl" / "legacy_banking_sample.xml").read_bytes())


@pytest.fixture(scope="module")
def indas():
    return X.parse((FIX / "xbrl" / "legacy_indas_sample.xml").read_bytes())


def test_entry_point_and_identity(bank, indas):
    assert bank.entry_point == "banking_entry_point_2019-09-30.xsd"
    assert bank.result_type == "Banking Format"
    assert bank.nature == "Standalone" and bank.audited is False
    assert indas.entry_point.lower().startswith("ind-as_entry_point")
    assert indas.nature == "Consolidated"


def test_period_and_contexts_use_dates_not_ids(bank):
    end = X.period_end(bank)
    assert end == date(2023, 9, 30)
    q = X.quarter_context(bank, end)
    y = X.ytd_context(bank, end)
    assert (q.eff_start, q.eff_end) == (date(2023, 7, 1), date(2023, 9, 30))
    # FourD's <period> says Jul-Sep but its reporting-period facts say Apr-Sep: the facts win.
    assert (y.eff_start, y.eff_end) == (date(2023, 4, 1), date(2023, 9, 30))
    assert X.instant_context(bank, end).id == "OneI"


def test_bank_quarter_values_in_crore(bank):
    props = {p.metric: p for p in XM.to_fact_proposals(bank, "TESTBANK", "bank")}
    assert props["interest_earned"].value == pytest.approx(34920.39, abs=0.01)
    assert props["pat"].value == pytest.approx(10261.0, abs=0.01)
    assert props["gnpa_amt"].value == pytest.approx(29836.94, abs=0.01)
    assert props["gnpa_pct"].value == pytest.approx(0.0255)
    assert props["nnpa_pct"].value == pytest.approx(0.0051)  # filed without a unitRef
    assert props["advances"].value == pytest.approx(1110542.14, abs=0.01)  # instant context
    assert props["cet1"].unit == "fraction"
    assert props["pat"].basis == "standalone"
    assert props["pat"].span.xbrl["element"] == "ProfitLossForThePeriod"
    assert props["pat"].span.xbrl["context_id"] == "OneD"


def test_nii_identity_in_file(bank):
    p = {x.metric: x.value for x in XM.to_fact_proposals(bank, "T", "bank")}
    # PPOP = interest earned - interest expended + other income - operating expenses (file's own arithmetic)
    assert p["interest_earned"] - p["interest_expended"] + p["other_income"] - p["opex"] == pytest.approx(
        p["ppop"], abs=0.02
    )


def test_ytd_values_for_derivation(bank):
    y = XM.ytd_values(bank, "bank")
    assert y["interest_earned"][0] == pytest.approx(68248.0, abs=0.01)
    assert y["pat"][0] == pytest.approx(19909.2, abs=0.01)
    assert X.ytd_to_quarter(68248.0, None) == 68248.0
    assert X.ytd_to_quarter(68248.0, 33327.61) == pytest.approx(34920.39)


def test_segments(bank):
    segs = X.segment_facts(bank, date(2023, 9, 30))
    rev = {s.segment: s.value for s in segs if s.metric == "SegmentRevenue"}
    assert set(rev) == {"Other Banking", "Retail", "Treasury", "Wholesale"}
    assert rev["Retail"] / 1e7 == pytest.approx(33080.02, abs=0.01)
    liab = [s for s in segs if s.metric == "SegmentLiabilities"]
    assert len(liab) == 4 and {s.segment for s in liab} == set(rev)  # label found through the matching D context


def test_indas_generic_filer(indas):
    end = X.period_end(indas)
    assert end == date(2023, 9, 30)
    p = {x.metric: x for x in XM.to_fact_proposals(indas, "TESTCO", "generic")}
    assert p["revenue"].value == pytest.approx(8478.57, abs=0.01)
    assert p["pat"].value == pytest.approx(1232.39, abs=0.01)
    assert p["pat_owners"].value == pytest.approx(1205.42, abs=0.01)
    assert p["pat"].basis == "consolidated"
    assert "gnpa_amt" not in p  # bank-only metric never leaks into a generic filer


def test_unmapped_report_lists_unclaimed_numeric(bank):
    names = {n for n, _, _ in XM.unmapped_numeric(bank)}
    assert "InterestOrDiscountOnAdvancesOrBills" in names and "GrossNonPerformingAssets" not in names


def test_amount_text_is_as_written(bank):
    pat = [p for p in XM.to_fact_proposals(bank, "T", "bank") if p.metric == "pat"][0]
    assert pat.value_as_written == "102610000000.00"


def test_main_contexts_rebuilt_when_a_legacy_file_omits_them():
    """Some exchange-hosted legacy files reference OneD/OneI without defining them."""
    import re
    from datetime import date

    from signalcourt.extract import xbrl as X
    from signalcourt.extract import xbrl_map as XM

    raw = (FIX / "xbrl" / "legacy_banking_sample.xml").read_text(encoding="utf-8")
    for cid in ("OneD", "OneI", "FourD", "FourI"):  # like the real files: only OneD/OneI are referenced
        raw = re.sub(rf'<xbrli:context id="{cid}">.*?</xbrli:context>', "", raw, flags=re.S)
    raw = re.sub(r'<([\w:-]+) contextRef="Four[DI]"[^>]*>[^<]*</>', "", raw)
    assert 'id="OneD"' not in raw and 'contextRef="OneD"' in raw
    doc = X.parse(raw.encode())
    assert X.period_end(doc) == date(2023, 9, 30)
    assert X.quarter_context(doc, date(2023, 9, 30)).id == "OneD"
    props = {p.metric: p for p in XM.to_fact_proposals(doc, "AAA", "bank")}
    good_doc = X.parse((FIX / "xbrl" / "legacy_banking_sample.xml").read_bytes())
    good = {p.metric: p for p in XM.to_fact_proposals(good_doc, "AAA", "bank")}
    assert props["pat"].value == good["pat"].value and props["advances"].value == good["advances"].value
