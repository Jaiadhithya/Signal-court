"""DocStore: the facade over blobs, manifest and text cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .blobs import BlobStore
from .dates import resolve_publish_date
from .manifest import Manifest
from .text import PageText, Passage, TextCache, pdf_metadata


@dataclass(frozen=True)
class DocMeta:
    sha256: str
    mime: str
    size: int
    n_pages: int | None
    first_url: str
    final_url: str
    doc_type: str | None
    source_id: str
    source_family: str
    entity_symbol: str | None
    period_end: str | None
    publish_date: str | None
    publish_date_source: str | None
    publish_ts: str | None
    ocr: bool
    title: str | None


def _iso(d: None | str | date) -> str | None:
    return d.isoformat() if isinstance(d, date) else d


class DocStore:
    """`root` is the workspace `cache/` directory (raw/, text/, manifest.sqlite)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs = BlobStore(self.root / "raw")
        self.text = TextCache(self.root / "text")
        self.manifest = Manifest(self.root / "manifest.sqlite")

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
        ocr: bool = False,
    ) -> DocMeta:
        sha = self.blobs.put(data, mime)
        n_pages = self.text.n_pages(sha)
        if n_pages is None:
            n_pages = self.text.build(sha, data, mime)
        pd_, pd_src = publish_date, publish_date_source
        if pd_ is None:
            pdf_c = None
            if mime.split(";")[0].strip().lower() == "application/pdf":
                try:
                    pdf_c = pdf_metadata(data).get("creationDate")
                except Exception:
                    pdf_c = None
            page1 = self.text.page(sha, 1).raw if n_pages else None
            r = resolve_publish_date(
                broadcast_ts=publish_ts,
                pdf_creation=pdf_c,
                period_end=period_end,
                page1_text=page1,
                url=final_url or url,
            )
            pd_, pd_src = r.date, r.source
        self.manifest.upsert_document(
            {
                "sha256": sha,
                "mime": mime,
                "size": len(data),
                "n_pages": n_pages,
                "first_url": url,
                "final_url": final_url,
                "doc_type": doc_type,
                "source_id": source_id,
                "source_family": source_family,
                "entity_symbol": entity_symbol,
                "period_end": _iso(period_end),
                "publish_date": _iso(pd_),
                "publish_date_source": pd_src,
                "publish_ts": publish_ts,
                "ocr": int(ocr),
                "title": title,
            }
        )
        return self.meta(sha)

    def meta(self, sha: str) -> DocMeta:
        r = self.manifest.get_document(sha)
        if r is None:
            raise KeyError(sha)
        d = dict(r)
        d["ocr"] = bool(d["ocr"])
        return DocMeta(**d)

    def known_url(self, url: str) -> str | None:
        """sha256 of an already-stored file fetched from ``url`` (first or final), else None.

        Lets the harvester skip a download it already has (DocStorePort).
        """
        row = self.manifest.conn.execute(
            "SELECT sha256 FROM documents WHERE first_url=? OR final_url=? LIMIT 1", (url, url)
        ).fetchone()
        if row is None:
            row = self.manifest.conn.execute(
                "SELECT sha256 FROM fetches WHERE (url=? OR final_url=?) AND sha256 IS NOT NULL LIMIT 1", (url, url)
            ).fetchone()
        return row["sha256"] if row else None

    def has(self, sha: str) -> bool:
        return self.manifest.get_document(sha) is not None

    def read_bytes(self, sha: str) -> bytes:
        return self.blobs.read(sha)

    def pages(self, sha: str) -> list[PageText]:
        n = self.text.n_pages(sha) or 0
        return [self.text.page(sha, i) for i in range(1, n + 1)]

    def passages(self, sha: str, page: int) -> list[Passage]:
        return self.text.passages(sha, page)

    def passage_by_pid(self, pid: str) -> Passage | None:
        from .text import parse_pid

        parsed = parse_pid(pid)
        if parsed is None:
            return None
        prefix, page, _ = parsed
        row = self.manifest.conn.execute("SELECT sha256 FROM documents WHERE sha256 LIKE ?", (prefix + "%",)).fetchone()
        if row is None:
            return None
        try:
            for p in self.passages(row["sha256"], page):
                if p.pid == pid:
                    return p
        except FileNotFoundError:
            return None
        return None

    def sha_for_pid(self, pid: str) -> str | None:
        from .text import parse_pid

        parsed = parse_pid(pid)
        if parsed is None:
            return None
        row = self.manifest.conn.execute(
            "SELECT sha256 FROM documents WHERE sha256 LIKE ?", (parsed[0] + "%",)
        ).fetchone()
        return row["sha256"] if row else None

    def close(self) -> None:
        self.manifest.close()
