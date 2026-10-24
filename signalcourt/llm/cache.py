"""SQLite response cache: one row per (model, messages, max_tokens), written only after a reply validated."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..ledger.canonical import canonical_json, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses(
    key TEXT PRIMARY KEY, model TEXT NOT NULL, family TEXT NOT NULL, provider TEXT NOT NULL,
    text TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, created_at REAL NOT NULL
)
"""


def cache_key(model: str, messages: list[dict], max_tokens: int | None) -> str:
    return sha256_hex(canonical_json({"model": model, "messages": messages, "max_tokens": max_tokens}))


@dataclass(frozen=True)
class CachedResponse:
    key: str
    model: str
    family: str
    provider: str
    text: str
    tokens_in: int
    tokens_out: int


class ResponseCache:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, model, family, provider, text, tokens_in, tokens_out FROM responses WHERE key=?", (key,)
            ).fetchone()
        return CachedResponse(*row) if row else None

    def put(self, key: str, model: str, family: str, provider: str, text: str, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?,?,?)",
                (key, model, family, provider, text, tokens_in, tokens_out, time.time()),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
