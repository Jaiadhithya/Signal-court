"""Append-only, hash-chained run ledger.

One SQLite file per run plus a JSONL mirror with the same envelopes. ``append`` is
the only write path; UPDATE and DELETE are blocked by triggers, heads are write-once.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .canonical import GENESIS_HASH, canonical_json, compute_hash, make_rid
from .schema import (
    POST_PREREG_TYPES,
    PREFIX,
    RID_PATTERN,
    Author,
    Envelope,
    RecordType,
    RunPayload,
    may_write,
    payload_dict,
    validate_payload,
)

LEDGER_DB = "ledger.sqlite"
LEDGER_JSONL = "ledger.jsonl"

DDL = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS records (
  seq INTEGER PRIMARY KEY,
  rid TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL,
  run_id TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  parents TEXT NOT NULL,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE INDEX IF NOT EXISTS records_type ON records(type);
CREATE TABLE IF NOT EXISTS heads (name TEXT PRIMARY KEY, seq INTEGER, hash TEXT);
CREATE TRIGGER IF NOT EXISTS heads_once BEFORE UPDATE ON heads BEGIN SELECT RAISE(ABORT, 'heads are write-once'); END;
CREATE TRIGGER IF NOT EXISTS heads_no_delete BEFORE DELETE ON heads
BEGIN SELECT RAISE(ABORT, 'heads are write-once'); END;
"""

_RID_RE = re.compile(RID_PATTERN)
_RID_FIELD_SUFFIXES = ("_rid", "_rids")
_RID_LIST_FIELDS = {"inputs", "evidence", "calc", "rids", "signal_rids", "parents"}

HeadName = Literal["prereg_head", "run_head"]
Clock = Callable[[], str]


class LedgerError(Exception):
    """Base class for refused ledger writes."""


class CapabilityError(LedgerError):
    pass


class SchemaError(LedgerError):
    pass


class OrderingError(LedgerError):
    pass


def utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row_to_env(row: sqlite3.Row) -> Envelope:
    return Envelope(
        seq=row["seq"],
        rid=row["rid"],
        type=RecordType(row["type"]),
        run_id=row["run_id"],
        author=Author(**json.loads(row["author"])),
        created_at=row["created_at"],
        parents=json.loads(row["parents"]),
        payload=json.loads(row["payload"]),
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )


def _iter_rids(obj: Any, key: str = "") -> Iterable[str]:
    """Every rid referenced inside a payload (fields named *_rid, *_rids and known rid lists)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_rids(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_rids(v, key)
    elif (
        isinstance(obj, str)
        and (key.endswith(_RID_FIELD_SUFFIXES) or key in _RID_LIST_FIELDS or key == "supersedes")
        and _RID_RE.match(obj)
    ):
        yield obj


def referenced_rids(env: Envelope) -> list[str]:
    out = list(env.parents)
    out.extend(_iter_rids(env.payload))
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


class Ledger:
    def __init__(self, run_dir: Path, conn: sqlite3.Connection, clock: Clock | None = None):
        self.run_dir = Path(run_dir)
        self._conn = conn
        self._clock = clock or utc_now_ms
        self._jsonl = open(self.run_dir / LEDGER_JSONL, "a", encoding="utf-8", newline="\n")
        row = conn.execute("SELECT run_id FROM records ORDER BY seq LIMIT 1").fetchone()
        self.run_id: str | None = row["run_id"] if row else None

    # ------------------------------------------------------------------ lifecycle

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL)
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @classmethod
    def create(
        cls,
        run_dir: Path,
        run: RunPayload,
        *,
        run_id: str | None = None,
        author: Author | None = None,
        clock: Clock | None = None,
    ) -> Ledger:
        """Create the run ledger and write the RUN record (seq 1, prev_hash = genesis).

        ``run_id`` defaults to the directory name (``YYYYMMDD-HHMMSS-<symbol>-<6 hex>``).
        ``clock`` makes ``created_at`` deterministic for replay and offline e2e tests.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        db = run_dir / LEDGER_DB
        if db.exists():
            raise LedgerError(f"ledger already exists: {db}")
        (run_dir / LEDGER_JSONL).write_text("", encoding="utf-8")
        led = cls(run_dir, cls._connect(db), clock)
        led.run_id = run_id or run_dir.name
        led.append(RecordType.RUN, run, author or Author.code("controller"))
        return led

    @classmethod
    def open(cls, run_dir: Path, *, clock: Clock | None = None) -> Ledger:
        """Open an existing ledger; rebuilds ledger.jsonl from SQLite if it is behind or torn."""
        run_dir = Path(run_dir)
        db = run_dir / LEDGER_DB
        if not db.exists():
            raise LedgerError(f"no ledger at {db}")
        conn = cls._connect(db)
        cls._repair_jsonl(run_dir, conn)
        return cls(run_dir, conn, clock)

    @staticmethod
    def _repair_jsonl(run_dir: Path, conn: sqlite3.Connection) -> int:
        path = run_dir / LEDGER_JSONL
        good: list[str] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    json.loads(line)
                except ValueError:
                    break  # torn final line from a crash; everything after it is rebuilt
                good.append(line)
        n_db = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if len(good) == n_db and path.exists():
            return 0
        rows = conn.execute("SELECT * FROM records ORDER BY seq").fetchall()
        lines = [canonical_json(_row_to_env(r).as_dict()).decode("utf-8") for r in rows]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8", newline="\n")
        tmp.replace(path)
        return n_db - len(good)

    def close(self) -> None:
        try:
            self._jsonl.close()
        finally:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ write path

    def append(
        self, type: RecordType | str, payload: BaseModel | dict[str, Any], author: Author, parents: Sequence[str] = ()
    ) -> Envelope:
        rtype = RecordType(type)
        if not may_write(author, rtype):
            raise CapabilityError(f"{author.writer!r} may not write {rtype.value}")
        try:
            model = validate_payload(rtype, payload)
        except ValidationError as e:
            raise SchemaError(f"{rtype.value}: {e}") from e
        if rtype is RecordType.RUN and self.head()[0] != 0:
            raise LedgerError("RUN must be the first record")
        if rtype is not RecordType.RUN and self.head()[0] == 0:
            raise LedgerError("first record must be RUN")
        pdict = payload_dict(model)
        if rtype in POST_PREREG_TYPES:
            self._check_ordering(rtype, pdict)
        for p in parents:
            if not _RID_RE.match(p):
                raise SchemaError(f"bad parent rid {p!r}")

        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            last = c.execute("SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS_HASH)
            env = {
                "seq": seq,
                "type": rtype.value,
                "run_id": self.run_id,
                "author": {k: v for k, v in author.model_dump().items() if v is not None},
                "created_at": self._clock(),
                "parents": list(parents),
                "payload": pdict,
                "prev_hash": prev,
            }
            h = compute_hash(prev, env)
            rid = self._free_rid(PREFIX[rtype], h)
            c.execute(
                "INSERT INTO records (seq, rid, type, run_id, author, created_at, parents, payload,"
                " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    rid,
                    rtype.value,
                    self.run_id,
                    canonical_json(env["author"]).decode(),
                    env["created_at"],
                    canonical_json(env["parents"]).decode(),
                    canonical_json(pdict).decode(),
                    prev,
                    h,
                ),
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        envelope = Envelope(
            rid=rid, hash=h, author=author, type=rtype, **{k: v for k, v in env.items() if k not in ("author", "type")}
        )
        self._jsonl.write(canonical_json(envelope.as_dict()).decode("utf-8") + "\n")
        self._jsonl.flush()
        return envelope

    def _free_rid(self, prefix: str, h: str) -> str:
        # first 8 hex; on the rare collision take more of the hash (deterministic)
        for n in range(8, 65):
            rid = make_rid(prefix, h[:n])
            if not self._conn.execute("SELECT 1 FROM records WHERE rid=?", (rid,)).fetchone():
                return rid
        raise LedgerError("rid space exhausted")  # pragma: no cover

    def _check_ordering(self, rtype: RecordType, pdict: dict[str, Any]) -> None:
        """EVIDENCE, CHALLENGE and REBUTTAL only after prereg_head, about a pre-registered CLAIM."""
        ph = self._head_row("prereg_head")
        if ph is None:
            raise OrderingError(f"{rtype.value} refused: prereg_head not sealed")
        if rtype is RecordType.REBUTTAL:
            ch = self._get_or_none(pdict["challenge_rid"])
            if ch is None or ch.type is not RecordType.CHALLENGE:
                raise OrderingError(f"REBUTTAL refused: {pdict['challenge_rid']} is not a CHALLENGE")
            claim_rid = ch.payload["claim_rid"]
        else:
            claim_rid = pdict["claim_rid"]
        claim = self._get_or_none(claim_rid)
        if claim is None or claim.type is not RecordType.CLAIM:
            raise OrderingError(f"{rtype.value} refused: {claim_rid} is not a CLAIM")
        if claim.seq > ph[0]:
            raise OrderingError(f"{rtype.value} refused: {claim_rid} was not pre-registered")

    # ------------------------------------------------------------------ heads

    def _head_row(self, name: str) -> tuple[int, str] | None:
        r = self._conn.execute("SELECT seq, hash FROM heads WHERE name=?", (name,)).fetchone()
        return (r["seq"], r["hash"]) if r else None

    def seal_head(self, name: HeadName) -> str:
        """Write a head once. prereg_head = hash of the last CLAIM; run_head = hash of the last record."""
        if name not in ("prereg_head", "run_head"):
            raise ValueError(name)
        if self._head_row(name) is not None:
            raise LedgerError(f"{name} already sealed")
        if name == "prereg_head":
            r = self._conn.execute(
                "SELECT seq, hash FROM records WHERE type='CLAIM' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if r is None:
                raise LedgerError("prereg_head needs at least one CLAIM")
        else:
            r = self._conn.execute("SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        try:
            self._conn.execute("INSERT INTO heads (name, seq, hash) VALUES (?,?,?)", (name, r["seq"], r["hash"]))
        except sqlite3.IntegrityError as e:  # concurrent sealer
            raise LedgerError(f"{name} already sealed") from e
        return r["hash"]

    def heads(self) -> dict[str, tuple[int, str]]:
        return {r["name"]: (r["seq"], r["hash"]) for r in self._conn.execute("SELECT * FROM heads")}

    def head(self) -> tuple[int, str]:
        """(seq, hash) of the last record; (0, genesis) for an empty ledger."""
        r = self._conn.execute("SELECT seq, hash FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        return (r["seq"], r["hash"]) if r else (0, GENESIS_HASH)

    # ------------------------------------------------------------------ reads

    def _get_or_none(self, rid: str) -> Envelope | None:
        r = self._conn.execute("SELECT * FROM records WHERE rid=?", (rid,)).fetchone()
        return _row_to_env(r) if r else None

    def get(self, rid: str) -> Envelope:
        env = self._get_or_none(rid)
        if env is None:
            raise KeyError(rid)
        return env

    def query(self, type: RecordType | str | None = None, **eq: Any) -> list[Envelope]:
        """Records in seq order, filtered by type and by equality on payload fields.

        Envelope fields (``run_id``, ``rid``, ``seq``) can be filtered too; ``author_component``
        and ``author_role`` filter on the author.
        """
        if type is not None:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE type=? ORDER BY seq", (RecordType(type).value,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM records ORDER BY seq").fetchall()
        out = []
        for row in rows:
            env = _row_to_env(row)
            if all(self._matches(env, k, v) for k, v in eq.items()):
                out.append(env)
        return out

    @staticmethod
    def _matches(env: Envelope, key: str, value: Any) -> bool:
        if key in ("run_id", "rid", "seq"):
            return getattr(env, key) == value
        if key == "author_component":
            return env.author.component == value
        if key == "author_role":
            return env.author.role == value
        return env.payload.get(key, _MISSING) == value

    def closure(self, rid: str) -> list[Envelope]:
        """``rid`` and all its ancestors via parents and rid-valued payload fields, in seq order."""
        seen: dict[str, Envelope] = {}
        stack = [rid]
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            env = self._get_or_none(r)
            if env is None:
                continue
            seen[r] = env
            stack.extend(x for x in referenced_rids(env) if x not in seen)
        return sorted(seen.values(), key=lambda e: e.seq)

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def iter_records(self) -> Iterable[Envelope]:
        for row in self._conn.execute("SELECT * FROM records ORDER BY seq"):
            yield _row_to_env(row)

    def raw_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM records ORDER BY seq").fetchall()


class _Missing:
    pass


_MISSING = _Missing()
