"""Offline re-verification of a run: chain, quotes, CALC recompute.

No network and no LLM. Quote re-checking needs the document store; CALC recompute needs the
calculator, so it is injected as a callable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import GENESIS_HASH, compute_hash, make_rid
from .schema import PAYLOAD_MODELS, POST_PREREG_TYPES, PREFIX, Author, RecordType, may_write
from .store import LEDGER_DB, LEDGER_JSONL

# (doc_sha256, page, kind, norm_text) -> None if the span still matches the cache, else a reason
QuoteChecker = Callable[[dict[str, Any]], str | None]
# CALC payload -> recomputed value (or None if not recomputable here)
CalcRecomputer = Callable[[dict[str, Any], dict[str, dict[str, Any]]], float | None]


@dataclass
class Problem:
    seq: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"seq {self.seq}: {self.kind}: {self.detail}"


@dataclass
class VerifyReport:
    ok: bool = True
    records: int = 0
    problems: list[Problem] = field(default_factory=list)
    quotes_checked: int = 0
    calcs_checked: int = 0
    head_hash: str = GENESIS_HASH

    def add(self, seq: int, kind: str, detail: str) -> None:
        self.ok = False
        self.problems.append(Problem(seq, kind, detail))

    def first_bad_seq(self) -> int | None:
        return min((p.seq for p in self.problems), default=None)


def verify_run(
    run_dir: Path,
    store: Any = None,
    *,
    quote_checker: QuoteChecker | None = None,
    calc_recomputer: CalcRecomputer | None = None,
    calc_tolerance: float = 1e-9,
) -> VerifyReport:
    """Recompute the hash chain and (optionally) every quote and CALC.

    ``store`` is a DocStore-like object; if given without ``quote_checker`` the default checker
    re-reads the page text and applies the quote-gate normaliser to the stored span.
    """
    run_dir = Path(run_dir)
    rep = VerifyReport()
    conn = sqlite3.connect(str(run_dir / LEDGER_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM records ORDER BY seq").fetchall()
        heads = {r["name"]: (r["seq"], r["hash"]) for r in conn.execute("SELECT * FROM heads")}
    finally:
        conn.close()
    rep.records = len(rows)

    prev = GENESIS_HASH
    by_rid: dict[str, dict[str, Any]] = {}
    envs: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        seq = r["seq"]
        if seq != i:
            rep.add(seq, "seq_gap", f"expected {i}")
        env = {
            "seq": seq,
            "type": r["type"],
            "run_id": r["run_id"],
            "author": json.loads(r["author"]),
            "created_at": r["created_at"],
            "parents": json.loads(r["parents"]),
            "payload": json.loads(r["payload"]),
            "prev_hash": r["prev_hash"],
        }
        if r["prev_hash"] != prev:
            rep.add(seq, "prev_hash_mismatch", "chain broken before this record")
        h = compute_hash(r["prev_hash"], env)
        if h != r["hash"]:
            rep.add(seq, "hash_mismatch", "record content was changed")
        try:
            rtype = RecordType(r["type"])
            if r["rid"] != make_rid(PREFIX[rtype], r["hash"][: len(r["rid"]) - 3]):
                rep.add(seq, "rid_mismatch", r["rid"])
            PAYLOAD_MODELS[rtype].model_validate(env["payload"])
            if not may_write(Author(**env["author"]), rtype):
                rep.add(seq, "capability", f"{env['author']} wrote {rtype.value}")
        except Exception as e:  # unknown type or invalid payload
            rep.add(seq, "schema", str(e)[:200])
        # use the stored hash as the next link so one bad record is reported once
        prev = r["hash"]
        env["rid"] = r["rid"]
        env["hash"] = r["hash"]
        by_rid[r["rid"]] = env
        envs.append(env)
    rep.head_hash = prev

    # heads must point at real records
    for name, (hseq, hhash) in heads.items():
        if hseq < 1 or hseq > len(envs) or envs[hseq - 1]["hash"] != hhash:
            rep.add(hseq, "head_mismatch", name)

    # ordering rule: post-prereg types must reference CLAIMs sealed before prereg_head
    ph = heads.get("prereg_head")
    for env in envs:
        t = env["type"]
        if t in {x.value for x in POST_PREREG_TYPES}:
            if ph is None or env["seq"] <= ph[0]:
                rep.add(env["seq"], "ordering", f"{t} before prereg_head")

    # JSONL mirror agrees with SQLite
    jl = run_dir / LEDGER_JSONL
    if jl.exists():
        lines = [ln for ln in jl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) != len(envs):
            rep.add(len(lines), "jsonl_mismatch", f"{len(lines)} lines vs {len(envs)} records")
        else:
            for line, env in zip(lines, envs, strict=False):
                try:
                    if json.loads(line).get("hash") != env["hash"]:
                        rep.add(env["seq"], "jsonl_mismatch", "hash differs from SQLite")
                except ValueError:
                    rep.add(env["seq"], "jsonl_mismatch", "unparseable line")

    # quotes
    if quote_checker is None and store is not None:
        quote_checker = default_quote_checker(store)
    if quote_checker is not None:
        for env in envs:
            if env["type"] == "SPAN" and env["payload"].get("kind") in ("prose", "table_cell"):
                rep.quotes_checked += 1
                why = quote_checker(env["payload"])
                if why:
                    rep.add(env["seq"], "quote_not_found", why)

    # calcs
    if calc_recomputer is not None:
        for env in envs:
            if env["type"] == "CALC":
                rep.calcs_checked += 1
                p = env["payload"]
                try:
                    v = calc_recomputer(p, by_rid)
                except Exception as e:
                    rep.add(env["seq"], "calc_error", str(e)[:200])
                    continue
                if v is None and p.get("value") is None:
                    continue
                if v is None or p.get("value") is None or abs(v - p["value"]) > calc_tolerance * max(1.0, abs(v)):
                    rep.add(env["seq"], "calc_mismatch", f"recomputed {v} vs stored {p.get('value')}")
    return rep


def default_quote_checker(store: Any) -> QuoteChecker:
    from ..gates.quote_gate import recheck_span

    def check(payload: dict[str, Any]) -> str | None:
        return recheck_span(store, payload)

    return check
