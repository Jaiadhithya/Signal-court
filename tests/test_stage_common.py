"""DocStore.docs_for and the store-backed helpers shared by the real stages and the pre-warm."""
from datetime import date

import pytest
from conftest import put_pdf, put_xbrl, quarter_ends

from signalcourt.resolve import universe as U
from signalcourt.resolve.entity import Ambiguous, EntityMatch, resolve
from signalcourt.stages import common as C


def test_docs_for_filters_by_symbol_and_type_newest_first(store):
    for end in quarter_ends(date(2026, 6, 30), 3):
        put_xbrl(store, "AAA", "bank", end)
    put_xbrl(store, "BBB", "bank", date(2026, 6, 30))
    put_pdf(store, ["Investor presentation"], entity_symbol="AAA")
    docs = store.docs_for("AAA", "results_xbrl")
    assert [d.period_end for d in docs] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    assert {d.entity_symbol for d in docs} == {"AAA"}
    assert len(store.docs_for("AAA")) == 4
    assert store.docs_for("ZZZ") == []


@pytest.mark.parametrize("subtype", ["bank", "nbfc", "insurer_life", "generic"])
def test_subtype_comes_from_the_stored_filing_format(store, subtype):
    put_xbrl(store, "AAA", subtype, date(2026, 6, 30))
    st = C.subtype_from_store(store, "AAA")
    assert st.decision.subtype == subtype and st.decision.rule == "format"
    assert st.xbrl_file.endswith(".xml") and st.entry_point.endswith(".xsd")


def test_subtype_from_store_is_none_without_filings(store):
    assert C.subtype_from_store(store, "AAA") is None


def test_latest_size_and_quarter_count(store):
    for end in quarter_ends(date(2026, 6, 30), 4):
        put_xbrl(store, "AAA", "nbfc", end, scale=2.0)
    assert C.latest_size(store, "AAA", "nbfc") > 0
    assert C.xbrl_quarters(store, "AAA") == 4
    assert C.xbrl_quarters(store, "AAA", as_of=date(2026, 5, 1)) == 3  # the June quarter is published in July


def test_resolver_can_prefer_the_financial_universe(tmp_path):
    con = U.connect(tmp_path / "u.sqlite")
    master = [
        {"SYMBOL": "ACMEFIN", "NAME OF COMPANY": "Acme Finance Limited"},
        {"SYMBOL": "ACMEFSV", "NAME OF COMPANY": "Acme Finserv Limited"},
        {"SYMBOL": "ACMEAUTO", "NAME OF COMPANY": "Acme Auto Limited"},
        {"SYMBOL": "ACMEELEC", "NAME OF COMPANY": "Acme Electricals Limited"},
    ]
    idx = {"fin": [{"Symbol": s, "Industry": "Financial Services"} for s in ("ACMEFIN", "ACMEFSV")]}
    U.ingest(con, master, idx)
    every = resolve(con, "Acme")
    fin = resolve(con, "Acme", financial_only=True)
    assert isinstance(every, Ambiguous) and isinstance(fin, Ambiguous)
    assert {c.symbol for c in fin.candidates} == {"ACMEFIN", "ACMEFSV"}
    assert isinstance(resolve(con, "acme auto ltd", financial_only=True), type(None))
    assert isinstance(resolve(con, "acme finance ltd", financial_only=True), EntityMatch)
