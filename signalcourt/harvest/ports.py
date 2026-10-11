"""Ports to B1's document store. B2 depends on these shapes only, never on store internals."""

from __future__ import annotations

from datetime import date
from typing import Protocol


class DocMetaLike(Protocol):
    sha256: str


class DocStorePort(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        url: str,
        final_url: str,
        mime: str,
        source_id: str,
        source_family: str,
        doc_type: str,
        entity_symbol: str | None = None,
        period_end: date | None = None,
        publish_date: date | None = None,
        publish_date_source: str = "none",
        publish_ts: str | None = None,
        title: str | None = None,
    ) -> DocMetaLike: ...

    def known_url(self, url: str) -> str | None:
        """sha256 of an already-stored file fetched from ``url``, else None (idempotent harvest)."""
        ...

    def read_bytes(self, sha: str) -> bytes: ...
