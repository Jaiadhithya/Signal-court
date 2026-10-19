"""Page text extraction (PyMuPDF), passages with pids, per-page cache.

Agents only ever see passages with pids, never whole documents.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .normalise import normalise

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9₹(\"'])")
_NUM = r"\(?-?[₹`]?\d[\d,]*(?:\.\d+)?%?\)?"
_ROW = re.compile(rf"^\s*[A-Za-z][^\d\n]{{2,}}?(?:\s+{_NUM}(?:\s*(?:cr|bn|mn|bps|x))?){{1,}}\s*$", re.IGNORECASE)
_CHUNK_MAX_SENT = 3


@dataclass(frozen=True)
class Passage:
    pid: str
    text: str
    norm: str
    start: int
    end: int
    kind: str  # prose | row


@dataclass(frozen=True)
class PageText:
    page: int
    raw: str
    norm: str


def pid_for(sha: str, page: int, i: int) -> str:
    return f"{sha[:10]}-p{page:04d}-{i:03d}"


def parse_pid(pid: str):
    m = re.match(r"^([0-9a-f]{10})-p(\d{4})-(\d{3})$", pid)
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


def extract_pages(data: bytes, mime: str) -> list[str]:
    mime = mime.split(";")[0].strip().lower()
    if mime == "application/pdf":
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            return [p.get_text("text") for p in doc]
    text = data.decode("utf-8", errors="replace")
    if mime in ("text/html", "application/xhtml+xml"):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        text = html.unescape(text)
    return [text]


def pdf_metadata(data: bytes) -> dict:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        return dict(doc.metadata or {})


def split_passages(sha: str, page: int, raw: str) -> list[Passage]:
    """Table-like lines become `row` passages; other text is split into 1-3 sentence chunks."""
    out: list[Passage] = []
    i = 0
    pos = 0
    buf: list[tuple] = []  # (start, end) of prose lines awaiting a flush

    def flush() -> None:
        nonlocal i
        if not buf:
            return
        s0, e0 = buf[0][0], buf[-1][1]
        block = raw[s0:e0]
        sents, cur = [], 0
        for m in _SENT_SPLIT.finditer(block):
            sents.append((cur, m.start()))
            cur = m.end()
        sents.append((cur, len(block)))
        for k in range(0, len(sents), _CHUNK_MAX_SENT):
            a, b = sents[k][0], sents[min(k + _CHUNK_MAX_SENT, len(sents)) - 1][1]
            txt = block[a:b]
            n = normalise(txt)
            if n:
                out.append(Passage(pid_for(sha, page, i), txt, n, s0 + a, s0 + b, "prose"))
                i += 1
        buf.clear()

    for line in raw.splitlines(keepends=True):
        s, e = pos, pos + len(line.rstrip("\r\n"))
        pos += len(line)
        if not line.strip():
            flush()
        elif _ROW.match(line):
            flush()
            n = normalise(line)
            out.append(Passage(pid_for(sha, page, i), line.strip(), n, s, e, "row"))
            i += 1
        else:
            buf.append((s, e))
    flush()
    return out


class TextCache:
    """cache/text/<sha256>/p0001.json = {raw, norm, passages:[...]}; extracted once."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha: str, page: int) -> Path:
        return self.root / sha / f"p{page:04d}.json"

    def build(self, sha: str, data: bytes, mime: str) -> int:
        pages = extract_pages(data, mime)
        d = self.root / sha
        d.mkdir(parents=True, exist_ok=True)
        for n, raw in enumerate(pages, start=1):
            ps = split_passages(sha, n, raw)
            rec = {"raw": raw, "norm": normalise(raw), "passages": [asdict(p) for p in ps]}
            tmp = self._path(sha, n).with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(sha, n))
        (d / "_n_pages").write_text(str(len(pages)), encoding="utf-8")
        return len(pages)

    def n_pages(self, sha: str) -> int | None:
        p = self.root / sha / "_n_pages"
        return int(p.read_text()) if p.exists() else None

    def page(self, sha: str, page: int) -> PageText:
        rec = json.loads(self._path(sha, page).read_text(encoding="utf-8"))
        return PageText(page, rec["raw"], rec["norm"])

    def passages(self, sha: str, page: int) -> list[Passage]:
        rec = json.loads(self._path(sha, page).read_text(encoding="utf-8"))
        return [Passage(**p) for p in rec["passages"]]
