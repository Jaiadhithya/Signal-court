"""Data layer against the real DocStore and CALC schema (the ports it was written against)."""

from datetime import date

from conftest import FIX

from signalcourt.calc import calculator as C
from signalcourt.calc import periods as P
from signalcourt.harvest import filings as Fl
from signalcourt.ledger.schema import RecordType, validate_payload


def test_docstore_satisfies_port_and_xbrl_survives_a_roundtrip(store):
    data = (FIX / "xbrl" / "integrated_nbfc_sample.xml").read_bytes()
    url = "https://example.test/xbrl/INTEGRATED_FILING_NBFC_INDAS_1_WEB.xml"
    meta = store.put_bytes(
        data,
        url=url,
        final_url=url,
        mime="application/xml",
        source_id="exchange_xbrl",
        source_family="exchange",
        doc_type="results_xbrl",
        entity_symbol="CHOLAFIN",
        period_end=date(2026, 6, 30),
        publish_date=date(2026, 7, 28),
        publish_date_source="exchange_broadcast",
    )
    assert store.known_url(url) == meta.sha256
    assert store.read_bytes(meta.sha256) == data
    row = {"qe_Date": "30-JUN-2026", "consolidated": "Consolidated", "xbrl": url}
    filing = Fl.filing_from_row("CHOLAFIN", row, "integrated")
    parsed = Fl.parse_filing(filing, store.read_bytes(meta.sha256), "nbfc", meta.sha256)
    assert {p.metric for p in parsed.quarter} >= {"interest_income", "pat", "pbt", "revenue"}
    assert all(p.span.doc_sha256 == meta.sha256 for p in parsed.quarter)


def test_calculator_payloads_validate_against_the_ledger_calc_schema():
    book = C.FactBook()
    for i, q in enumerate(P.span("Q1FY24", "Q4FY26")):
        book.add("A", "advances", q, 1000.0 + 50 * i, f"FA-{i}")
        book.add("A", "provisions", q, 20.0, f"FP-{i}")
    sink = C.InMemorySink()
    C.Calculator(book, sink).compute("credit_cost", "A", "Q4FY26")
    assert sink.records
    for payload in sink.records.values():
        validate_payload(RecordType.CALC, payload)
