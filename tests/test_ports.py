"""The store and ledger satisfy the ports the data layer expects: DocStorePort and CalcSink."""

from datetime import date

from conftest import put_pdf

from signalcourt.ledger import LedgerCalcSink, RecordType, verify_run


def test_docstore_known_url_and_put_bytes_signature(store):
    assert store.known_url("https://example.test/a.pdf") is None
    m = put_pdf(
        store,
        ["Gross NPA ratio improved to 2.31% in the quarter."],
        url="https://example.test/first",
        final_url="https://example.test/final",
        period_end=date(2026, 6, 30),
        title="deck",
    )
    assert store.known_url("https://example.test/first") == m.sha256
    assert store.known_url("https://example.test/final") == m.sha256
    assert store.read_bytes(m.sha256).startswith(b"%PDF")
    store.manifest.record_fetch(url="https://example.test/x", final_url="https://example.test/x", sha256=m.sha256)
    assert store.known_url("https://example.test/x") == m.sha256


def test_ledger_calc_sink(ledger):
    sink = LedgerCalcSink(ledger)
    payload = {
        "formula_id": "yoy",
        "formula_version": 1,
        "formula_text": "change versus the same quarter a year earlier",
        "inputs": [],
        "params": {},
        "value": 0.142,
        "unit": "fraction",
        "period_end": "2026-06-30",
        "entity": "X",
        "recon_status": "pass",
    }
    rid = sink.append_calc(payload, [])
    env = ledger.get(rid)
    assert env.type is RecordType.CALC and env.author.component == "calculator" and env.payload["value"] == 0.142
    ledger.close()
    assert verify_run(ledger.run_dir).ok
