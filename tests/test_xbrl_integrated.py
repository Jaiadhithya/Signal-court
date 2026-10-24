"""Integrated-format goldens (entry point in-capmkt-ent-*.xsd), plus NBFC and life-insurer value checks.

NBFC and life fixtures hold values copied from real June-2026-quarter filings pulled on 11 Oct; the
bank fixture is synthetic (real tag names and units, round numbers). Integrated facts are plain
integers in full rupees; the LevelOfRounding text is only a label.
"""

from datetime import date

import pytest
from conftest import FIX

from signalcourt.extract import xbrl as X
from signalcourt.extract import xbrl_map as XM

END = date(2026, 6, 30)


def _doc(name):
    return X.parse((FIX / "xbrl" / name).read_bytes())


def _props(name, subtype):
    doc = _doc(name)
    return doc, {p.metric: p for p in XM.to_fact_proposals(doc, "X", subtype)}


def test_integrated_period_uses_oned_and_onei():
    doc = _doc("integrated_nbfc_sample.xml")
    assert doc.entry_point == "in-capmkt-ent-2026-01-31.xsd"
    assert doc.result_type is None  # integrated files carry no ResultType fact
    assert X.period_end(doc) == END
    assert X.quarter_context(doc, END).id == "OneD"
    assert X.instant_context(doc, END).id == "OneI"


def test_nbfc_metrics_map_and_convert_to_crore():
    doc, p = _props("integrated_nbfc_sample.xml", "nbfc")
    assert doc.nature == "Consolidated" and doc.symbol == "CHOLAFIN" and doc.isin == "INE121A01024"
    assert p["interest_income"].value == pytest.approx(8048.11)
    assert p["revenue"].value == pytest.approx(8856.29)
    assert p["impairment_fin_instr"].value == pytest.approx(922.06)
    assert p["pat"].value == pytest.approx(1656.22) and p["pat_owners"].value == pytest.approx(1656.22)
    assert p["pbt"].value == pytest.approx(2222.07)
    assert p["eps_basic"].value == pytest.approx(19.43)
    assert "loans" not in p  # no balance sheet in quarterly NBFC XBRL
    assert all(x.basis == "consolidated" for x in p.values())


def test_life_insurer_metrics():
    doc, p = _props("integrated_life_sample.xml", "insurer_life")
    assert doc.nature == "Standalone" and doc.text_fact("TypeOfInsurance")
    assert p["premium_income"].value == pytest.approx(10250.63)
    assert p["net_premium_income"].value == pytest.approx(9749.33)
    assert p["pbt"].value == pytest.approx(431.22) and p["pat"].value == pytest.approx(386.18)
    assert p["solvency"].value == pytest.approx(2.254)
    assert p["persistency_13m"].value == pytest.approx(0.815)
    assert p["persistency_61m"].value == pytest.approx(0.632)
    assert p["total_assets"].value == pytest.approx(331662.46)  # instant context
    assert p["eps_basic"].value == pytest.approx(2.66)


def test_bank_integrated_tags_match_legacy_names():
    _, p = _props("integrated_bank_synthetic.xml", "bank")
    assert p["interest_earned"].value == pytest.approx(10000.0)
    assert p["gnpa_pct"].value == pytest.approx(0.0128) and p["nnpa_pct"].value == pytest.approx(0.0039)
    assert p["cet1"].value == pytest.approx(0.147) and p["roa"].value == pytest.approx(0.0151)
    assert p["pat"].value == pytest.approx(2000.0)


def _segment_xml(ctx_ids):
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:link="http://www.xbrl.org/2003/linkbase"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt">'
        '<link:schemaRef xlink:type="simple" xlink:href="in-capmkt-ent-2026-01-31.xsd"/>'
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
    )
    period = "<xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>"
    ent = '<xbrli:entity><xbrli:identifier scheme="x">SEGCO</xbrli:identifier></xbrli:entity>'
    body = f'<xbrli:context id="OneD">{ent}{period}</xbrli:context>'
    body += '<in-capmkt:DateOfStartOfReportingPeriod contextRef="OneD">2026-04-01</in-capmkt:DateOfStartOfReportingPeriod>'
    body += '<in-capmkt:DateOfEndOfReportingPeriod contextRef="OneD">2026-06-30</in-capmkt:DateOfEndOfReportingPeriod>'
    for cid, label in ctx_ids:
        body += f'<xbrli:context id="{cid}">{ent}{period}</xbrli:context>'
        body += f'<in-capmkt:DescriptionOfReportableSegment contextRef="{cid}">{label}</in-capmkt:DescriptionOfReportableSegment>'
        body += f'<in-capmkt:SegmentRevenue contextRef="{cid}" unitRef="INR" decimals="-5">1000000000</in-capmkt:SegmentRevenue>'
    return (head + body + "</xbrli:xbrl>").encode()


def test_segment_contexts_with_one_or_two_digit_member_index():
    from datetime import date

    from signalcourt.extract import xbrl as X
    from signalcourt.extract.segments import segment_labels

    doc = X.parse(_segment_xml([("OneReportable1D", "Insurance"), ("OneReportable12D", "Retail financing")]))
    assert segment_labels(doc, date(2026, 6, 30)) == ["Insurance", "Retail financing"]
