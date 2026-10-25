from test_court_support import NAME, build

from signalcourt.court.alternatives import run_code_tests
from signalcourt.court.pack import build_pack, count_tokens, restore_quote
from signalcourt.gates import check_quote
from signalcourt.ledger import RecordType


def test_pack_is_entity_blind_but_pids_still_resolve(tmp_path):
    f = build(tmp_path)
    pack = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 2500)
    text = pack.render()
    assert "Testco" not in text and "TESTCO" not in text
    assert "the company" in text
    by_pid = {p["pid"]: p for p in pack.passages}
    assert f.pids["deck"] in by_pid and f.pids["rating"] in by_pid
    assert {p["source_family"] for p in pack.passages} == {"company_filing", "rating_agency"}
    # an agent copies blinded text; the original wording is restored and passes the quote gate
    blinded = "the company advances grew strongly after the second quarter"
    assert blinded in by_pid[f.pids["deck"]]["text"]
    original = f.store.passage_by_pid(f.pids["deck"]).text
    quote = restore_quote(blinded, original, pack.names)
    assert quote.startswith("Testco Finance advances")
    assert check_quote(f.store, f.pids["deck"], "2026-10-01", quote=quote).ok
    assert NAME in pack.names
    f.led.close()


def test_pack_has_no_rids_and_a_stable_hash(tmp_path):
    f = build(tmp_path)
    run_code_tests(f.led, f.panel, f.claim)
    a = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 2500)
    b = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 2500)
    assert a.pack_hash == b.pack_hash and len(a.pack_hash) == 64
    assert f.claim not in a.render() and f.signal not in a.render()
    assert a.aliases["S1"] == f.signal
    f.led.close()


def test_pack_respects_token_budget(tmp_path):
    f = build(tmp_path)
    full = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 5000)
    assert len(full.passages) >= 2
    head = count_tokens(full.render()) - sum(count_tokens(str(p)) for p in full.passages)
    tight = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, max(1, head + 60))
    assert len(tight.passages) < len(full.passages)
    none = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 10)
    assert none.passages == []
    f.led.close()


def test_sector_shock_calc_is_written_and_cited_by_the_pack(tmp_path):
    f = build(tmp_path)
    rids = run_code_tests(f.led, f.panel, f.claim)
    calcs = {f.led.get(r).payload["formula_id"]: f.led.get(r) for r in rids}
    assert set(calcs) == {"sector_shock", "corporate_action", "base_effect", "one_off"}
    ss = calcs["sector_shock"].payload
    assert f.signal in ss["inputs"]
    peer_doc = next(e.rid for e in f.led.query(RecordType.DOC, doc_type="peer_panel"))
    assert peer_doc in ss["inputs"]
    assert ss["params"]["n_peers"] == 5
    assert 0 <= ss["value"] < 0.3  # peers grew slowly; the target jumped
    assert calcs["one_off"].payload["value"] == 4.0
    assert calcs["corporate_action"].payload["value"] == 0.0
    pack = build_pack(f.led, f.store, f.claim, f.ctx.doc_shas, 2500)
    by_formula = {c["formula"]: c for c in pack.calcs}
    assert pack.aliases[by_formula["sector_shock"]["id"]] == rids[[*calcs].index("sector_shock")]
    # code tests are not repeated for the same signal
    assert run_code_tests(f.led, f.panel, f.claim) == []
    f.led.close()


def test_sector_shock_sees_peers_that_moved(tmp_path):
    f = build(tmp_path, peers_move=True)
    rids = run_code_tests(f.led, f.panel, f.claim)
    ss = next(f.led.get(r).payload for r in rids if f.led.get(r).payload["formula_id"] == "sector_shock")
    assert ss["value"] > 0.5
    f.led.close()


def test_table_remnants_are_not_offered_as_prose(tmp_path):
    from conftest import put_pdf

    from signalcourt.court.pack import _passages

    f = build(tmp_path)
    meta = put_pdf(f.store, ["Total\n456,322,818\n492,409,994\n312,148,251\n134,548.50\n124,345.75 SCHEDULE 15"],
                   url="https://example.test/t.pdf", final_url="https://example.test/t.pdf")
    doc = {"sha256": meta.sha256, "source_family": "company_filing", "doc_type": "annual_report"}
    assert _passages(f.store, [doc]) == []
    f.led.close()
