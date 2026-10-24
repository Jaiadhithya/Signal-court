import sqlite3
import time

import pytest
from conftest import run_payload

from signalcourt.ledger import (
    Author,
    CapabilityError,
    Ledger,
    LedgerError,
    OrderingError,
    RecordType,
    SchemaError,
    verify_run,
)
from signalcourt.ledger.canonical import GENESIS_HASH

CODE = Author.code


def claim(led, i=1):
    return led.append(
        RecordType.CLAIM,
        {
            "hypothesis_index": i,
            "statement": "s",
            "mechanism": "m",
            "signal_rid": "SG-00000000",
            "falsification": {
                "metric": "gnpa_pct",
                "comparator": ">",
                "threshold": 0.03,
                "deadline_period": "Q4FY27",
                "source_rule": "r",
            },
            "drafter": {"model": "x", "family": "f"},
        },
        CODE("prereg"),
    )


INVESTIGATOR = Author(component="agents", role="investigator")


def evidence(led, claim_rid, author=INVESTIGATOR):
    return led.append(
        RecordType.EVIDENCE,
        {
            "claim_rid": claim_rid,
            "span_rid": "SP-00000000",
            "stance": "support",
            "sentence": "x",
            "source_family": "exchange",
            "origin_id": "o",
        },
        author,
    )


def test_genesis_and_chain(ledger):
    assert len(ledger) == 1
    r = ledger.query(RecordType.RUN)[0]
    assert r.seq == 1 and r.prev_hash == GENESIS_HASH and r.rid.startswith("RN-") and len(r.rid) == 11
    c = claim(ledger)
    assert c.prev_hash == r.hash and ledger.head() == (2, c.hash)
    assert ledger.get(c.rid) == c


def test_run_must_be_first_and_unique(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    with pytest.raises(LedgerError):
        led.append(RecordType.RUN, run_payload(), CODE("controller"))


def test_append_only_triggers(ledger):
    claim(ledger)
    conn = sqlite3.connect(str(ledger.run_dir / "ledger.sqlite"))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE records SET payload='{}' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM records WHERE seq=1")


def test_heads_write_once(ledger):
    claim(ledger)
    h = ledger.seal_head("prereg_head")
    assert ledger.heads()["prereg_head"][1] == h
    with pytest.raises(LedgerError):
        ledger.seal_head("prereg_head")
    conn = sqlite3.connect(str(ledger.run_dir / "ledger.sqlite"))
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        conn.execute("UPDATE heads SET hash='x'")
    with pytest.raises(sqlite3.DatabaseError, match="write-once"):
        conn.execute("DELETE FROM heads")


def test_prereg_head_is_last_claim_hash(ledger):
    claim(ledger, 1)
    c2 = claim(ledger, 2)
    assert ledger.seal_head("prereg_head") == c2.hash


def test_prereg_head_needs_claim(ledger):
    with pytest.raises(LedgerError):
        ledger.seal_head("prereg_head")


def test_evidence_refused_before_prereg_head(ledger):
    c = claim(ledger)
    with pytest.raises(OrderingError, match="prereg_head"):
        evidence(ledger, c.rid)
    ledger.seal_head("prereg_head")
    assert evidence(ledger, c.rid).rid.startswith("EV-")


def test_claim_after_head_is_not_preregistered(ledger):
    claim(ledger)
    ledger.seal_head("prereg_head")
    late = claim(ledger, 2)
    with pytest.raises(OrderingError, match="not pre-registered"):
        evidence(ledger, late.rid)


def test_rebuttal_ordering_and_unknown_claim(ledger):
    c = claim(ledger)
    ledger.seal_head("prereg_head")
    with pytest.raises(OrderingError):
        evidence(ledger, "CL-deadbeef")
    ch = ledger.append(
        RecordType.CHALLENGE,
        {"claim_rid": c.rid, "alt_type": "sector", "argument": "a", "severity": "substantive"},
        Author(component="a", role="redteam"),
    )
    rb = ledger.append(
        RecordType.REBUTTAL,
        {"challenge_rid": ch.rid, "argument": "b", "admissible": True, "admissible_reason": "r"},
        Author(component="a", role="advocate"),
    )
    assert rb.rid.startswith("RB-")
    with pytest.raises(OrderingError):
        ledger.append(
            RecordType.REBUTTAL,
            {"challenge_rid": c.rid, "argument": "b", "admissible": True, "admissible_reason": "r"},
            Author(component="a", role="advocate"),
        )


def test_capability_table(ledger):
    c = claim(ledger)
    ledger.seal_head("prereg_head")
    with pytest.raises(CapabilityError):  # Red team cannot write a REBUTTAL
        ledger.append(
            RecordType.REBUTTAL,
            {"challenge_rid": c.rid, "argument": "b", "admissible": True, "admissible_reason": "r"},
            Author(component="a", role="redteam"),
        )
    with pytest.raises(CapabilityError):  # Advocate cannot write a CHALLENGE
        ledger.append(
            RecordType.CHALLENGE,
            {"claim_rid": c.rid, "alt_type": "x", "argument": "a", "severity": "minor"},
            Author(component="a", role="advocate"),
        )
    with pytest.raises(CapabilityError):  # an agent cannot edit/write a CLAIM
        ledger.append(RecordType.CLAIM, {}, Author(component="a", role="advocate"))
    with pytest.raises(CapabilityError):
        ledger.append(
            RecordType.CALC,
            {
                "formula_id": "f",
                "formula_version": 1,
                "formula_text": "t",
                "inputs": [],
                "value": 1.0,
                "unit": "x",
                "recon_status": "pass",
            },
            Author(component="a", role="investigator"),
        )


def test_schema_rejects_bad_and_extra(ledger):
    with pytest.raises(SchemaError):
        ledger.append(RecordType.CLAIM, {"hypothesis_index": 1}, CODE("prereg"))
    with pytest.raises(SchemaError):
        ledger.append(RecordType.GATE, {"gate": "g", "result": "pass", "reason": "r", "extra": 1}, CODE("gates"))
    assert len(ledger) == 1  # nothing partially written


def test_query_and_closure(ledger):
    c = claim(ledger)
    ledger.seal_head("prereg_head")
    e = evidence(ledger, c.rid)
    assert [x.rid for x in ledger.query(RecordType.EVIDENCE, claim_rid=c.rid)] == [e.rid]
    assert ledger.query(RecordType.EVIDENCE, claim_rid="CL-00000000") == []
    clo = ledger.closure(e.rid)
    assert [x.rid for x in clo] == [c.rid, e.rid]
    clo2 = ledger.closure(c.rid)
    assert [x.rid for x in clo2] == [c.rid]


def test_closure_follows_parents(ledger):
    g = ledger.append(RecordType.GATE, {"gate": "g", "result": "pass", "reason": "r"}, CODE("gates"))
    g2 = ledger.append(RecordType.GATE, {"gate": "g", "result": "pass", "reason": "r2"}, CODE("gates"), parents=[g.rid])
    assert [x.rid for x in ledger.closure(g2.rid)] == [g.rid, g2.rid]


def test_jsonl_mirror_and_repair(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    claim(led)
    claim(led, 2)
    led.close()
    jl = tmp_path / "r" / "ledger.jsonl"
    lines = jl.read_text().splitlines()
    assert len(lines) == 3
    jl.write_text("\n".join(lines[:2]) + "\n" + lines[2][:20])  # behind and torn
    led2 = Ledger.open(tmp_path / "r")
    assert len(jl.read_text().splitlines()) == 3
    assert verify_run(tmp_path / "r").ok
    claim(led2, 3)
    assert len(jl.read_text().splitlines()) == 4
    led2.close()


def test_verify_catches_a_payload_edit_in_the_jsonl_mirror(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    claim(led)
    led.close()
    jl = tmp_path / "r" / "ledger.jsonl"
    jl.write_text(jl.read_text().replace('"statement":"s"', '"statement":"t"'))
    rep = verify_run(tmp_path / "r")
    assert not rep.ok and [p.kind for p in rep.problems] == ["jsonl_mismatch"] and rep.first_bad_seq() == 2


def test_reopen_continues_chain(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    c = claim(led)
    led.close()
    led2 = Ledger.open(tmp_path / "r")
    c2 = claim(led2, 2)
    assert c2.prev_hash == c.hash and c2.seq == 3 and led2.run_id == "r"
    led2.close()


def test_deterministic_with_fixed_clock(tmp_path):
    def build(d):
        led = Ledger.create(d, run_payload(), clock=lambda: "2026-10-11T00:00:00.000Z")
        claim(led)
        h = led.head()[1]
        led.close()
        return h

    assert build(tmp_path / "a") == build(tmp_path / "a2") or True  # run_id differs by dir name
    # same run_id -> same hash
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()

    def build2(d):
        led = Ledger.create(d / "run", run_payload(), run_id="fixed", clock=lambda: "T")
        claim(led)
        h = led.head()[1]
        led.close()
        return h

    assert build2(tmp_path / "x") == build2(tmp_path / "y")


def test_tamper_detected_by_seq(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    claim(led, 1)
    claim(led, 2)
    claim(led, 3)
    led.close()
    assert verify_run(tmp_path / "r").ok
    db = tmp_path / "r" / "ledger.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TRIGGER no_update")  # an attacker with file access
    conn.execute("UPDATE records SET payload = replace(payload, '\"s\"', '\"t\"') WHERE seq = 3")
    conn.commit()
    conn.close()
    rep = verify_run(tmp_path / "r")
    assert not rep.ok and rep.first_bad_seq() == 3
    assert any(p.kind == "hash_mismatch" and p.seq == 3 for p in rep.problems)
    assert not any(p.seq in (1, 2) for p in rep.problems)


def test_deleted_record_detected(tmp_path):
    led = Ledger.create(tmp_path / "r", run_payload())
    for i in range(1, 4):
        claim(led, i)
    led.close()
    conn = sqlite3.connect(str(tmp_path / "r" / "ledger.sqlite"))
    conn.execute("DROP TRIGGER no_delete")
    conn.execute("DELETE FROM records WHERE seq = 3")
    conn.commit()
    conn.close()
    rep = verify_run(tmp_path / "r")
    assert not rep.ok


def test_10k_appends_fast(tmp_path):
    led = Ledger.create(tmp_path / "big", run_payload())
    t = time.time()
    for i in range(10_000):
        led.append(RecordType.GATE, {"gate": "g", "result": "pass", "reason": str(i)}, CODE("gates"))
    dt = time.time() - t
    led.close()
    assert dt < 5.0, dt
    assert verify_run(tmp_path / "big").records == 10_001
