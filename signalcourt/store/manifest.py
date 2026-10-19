"""cache/manifest.sqlite: documents, fetches, listings."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS documents (
  sha256 TEXT PRIMARY KEY, mime TEXT NOT NULL, size INTEGER NOT NULL, n_pages INTEGER,
  first_url TEXT NOT NULL, final_url TEXT NOT NULL, doc_type TEXT, source_id TEXT NOT NULL,
  source_family TEXT NOT NULL, entity_symbol TEXT, period_end TEXT, publish_date TEXT,
  publish_date_source TEXT, publish_ts TEXT, ocr INTEGER DEFAULT 0, title TEXT
);
CREATE TABLE IF NOT EXISTS fetches (
  id INTEGER PRIMARY KEY, url TEXT, final_url TEXT, endpoint TEXT, params TEXT,
  http_status INTEGER, fetched_at TEXT, sha256 TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS listings (
  key TEXT PRIMARY KEY, endpoint TEXT, params TEXT, fetched_at TEXT, ttl_s INTEGER, sha256 TEXT
);
CREATE INDEX IF NOT EXISTS documents_entity ON documents(entity_symbol, doc_type, period_end);
"""

DOC_COLUMNS = (
    "sha256",
    "mime",
    "size",
    "n_pages",
    "first_url",
    "final_url",
    "doc_type",
    "source_id",
    "source_family",
    "entity_symbol",
    "period_end",
    "publish_date",
    "publish_date_source",
    "publish_ts",
    "ocr",
    "title",
)


class Manifest:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)

    def upsert_document(self, row: dict[str, Any]) -> None:
        """First sight wins for url and source; a later call may fill a missing publish date."""
        cur = self.get_document(row["sha256"])
        if cur is None:
            cols = ",".join(DOC_COLUMNS)
            self.conn.execute(
                f"INSERT INTO documents ({cols}) VALUES ({','.join('?' * len(DOC_COLUMNS))})",
                tuple(row.get(c) for c in DOC_COLUMNS),
            )
            return
        if cur["publish_date"] is None and row.get("publish_date"):
            self.conn.execute(
                "UPDATE documents SET publish_date=?, publish_date_source=?, publish_ts=? WHERE sha256=?",
                (row["publish_date"], row.get("publish_date_source"), row.get("publish_ts"), row["sha256"]),
            )
        for col in ("n_pages", "period_end", "entity_symbol", "title", "doc_type"):
            if cur[col] is None and row.get(col) is not None:
                self.conn.execute(f"UPDATE documents SET {col}=? WHERE sha256=?", (row[col], row["sha256"]))

    def get_document(self, sha: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()

    def record_fetch(self, **kw: Any) -> int:
        cols = ("url", "final_url", "endpoint", "params", "http_status", "fetched_at", "sha256", "error")
        cur = self.conn.execute(
            f"INSERT INTO fetches ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(kw.get(c) for c in cols),
        )
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()
