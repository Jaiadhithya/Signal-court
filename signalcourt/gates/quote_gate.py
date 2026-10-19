"""Gate 3: quote gate.

The cited pid must exist in a dated DOC of this run; the quote (if narrower than the passage)
must be a substring of the passage after normalisation; prose spans are 4-60 words; table-cell
spans need the row label and the value in the same row passage. OCR pages only: fuzzy >= 0.97.
A pass yields a SPAN payload; a fail yields a Rejection with the closest passage as explanation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from ..ledger import Author, Ledger, RecordType
from ..store.normalise import normalise
from ..store.text import parse_pid
from ..store.timegate import allowed
from .common import Rejection, write_reject

QUOTE_AUTHOR = Author.code("quote_gate")

DEFAULTS = {"min_words": 4, "max_words": 60, "table_window_chars": 300, "fuzzy_for_ocr_only": 0.97}


@dataclass
class QuoteResult:
    ok: bool
    span: dict[str, Any] | None = None  # SpanPayload dict when ok
    rejection: Rejection | None = None


def _opts(settings: dict[str, Any] | None) -> dict[str, Any]:
    o = dict(DEFAULTS)
    if settings:
        o.update(settings)
    return o


def _closest(store, sha: str, page: int, nquote: str) -> dict[str, Any]:
    best, best_p = 0.0, None
    try:
        for p in store.passages(sha, page):
            s = fuzz.partial_ratio(nquote, p.norm) / 100.0
            if s > best:
                best, best_p = s, p
    except FileNotFoundError:
        pass
    return {
        "closest_pid": best_p.pid if best_p else None,
        "closest_text": best_p.text[:300] if best_p else None,
        "similarity": round(best, 4),
    }


def _fail(reason: str, **detail: Any) -> QuoteResult:
    return QuoteResult(False, None, Rejection("quote", reason, detail))


def check_quote(
    store,
    pid: str,
    as_of: str | date,
    *,
    quote: str | None = None,
    kind: str = "prose",
    row_label: str | None = None,
    value_text: str | None = None,
    run_docs: Iterable[str] | None = None,
    settings: dict[str, Any] | None = None,
) -> QuoteResult:
    """Check one proposed quote. `quote` defaults to the whole passage.

    `run_docs` is the set of DOC shas harvested in this run; if given, the cited document must
    be in it.
    """
    o = _opts(settings)
    parsed = parse_pid(pid)
    if parsed is None:
        return _fail("quote_not_found", why="bad_pid", pid=pid)
    sha = store.sha_for_pid(pid)
    if sha is None:
        return _fail("quote_not_found", why="unknown_document", pid=pid)
    if run_docs is not None and sha not in set(run_docs):
        return _fail("quote_not_found", why="document_not_in_run", pid=pid, sha256=sha)
    meta = store.meta(sha)
    gate = allowed(meta, as_of)
    if gate == "undated":
        return _fail("undated_doc", pid=pid, sha256=sha)
    if gate == "dropped":
        return QuoteResult(
            False,
            None,
            Rejection("time", "time_leak", {"sha256": sha, "publish_date": meta.publish_date, "as_of": str(as_of)}),
        )
    passage = store.passage_by_pid(pid)
    if passage is None:
        return _fail("quote_not_found", why="no_such_passage", pid=pid)
    page = parsed[1]

    q = quote if quote is not None else passage.text
    nq = normalise(q)
    if not nq:
        return _fail("quote_not_found", why="empty_quote", pid=pid)

    if kind == "prose":
        n_words = len(nq.split())
        if n_words < o["min_words"] or n_words > o["max_words"]:
            return _fail("quote_length", words=n_words, min=o["min_words"], max=o["max_words"], pid=pid)
    elif kind in ("table_cell",):
        if not row_label or not value_text:
            return _fail("quote_not_found", why="table_cell_needs_row_label_and_value", pid=pid)
    else:
        return _fail("quote_not_found", why=f"unsupported_kind:{kind}", pid=pid)

    exact = nq in passage.norm
    fuzzy_sim: float | None = None
    if not exact:
        if meta.ocr:
            fuzzy_sim = fuzz.partial_ratio(nq, passage.norm) / 100.0
            if fuzzy_sim < o["fuzzy_for_ocr_only"]:
                return _fail("quote_not_found", **_closest(store, sha, page, nq), pid=pid)
        else:
            return _fail("quote_not_found", **_closest(store, sha, page, nq), pid=pid)

    nlabel = nvalue = None
    if kind == "table_cell":
        nlabel, nvalue = normalise(row_label), normalise(value_text)
        if not _cell_ok(store, sha, page, passage, nlabel, nvalue, o["table_window_chars"]) or nvalue not in nq:
            return _fail(
                "quote_not_found", why="row_label_and_value_not_together", **_closest(store, sha, page, nq), pid=pid
            )

    whole = nq == passage.norm
    span = {
        "doc_sha256": sha,
        "page": page,
        "kind": kind,
        "text": q,
        "norm_text": nq,
        "start": passage.start if whole else None,
        "end": passage.end if whole else None,
        "pid": pid,
        "row_label": row_label,
        "value_text": value_text,
        "match": "exact" if exact else "fuzzy",
        "similarity": None if exact else round(fuzzy_sim or 0, 4),
    }
    return QuoteResult(True, span, None)


def _cell_ok(store, sha: str, page: int, passage, nlabel: str, nvalue: str, window: int) -> bool:
    """Label and value in the same passage. A row passage must hold both itself; the window
    fallback exists only for prose passages, where a PDF may have broken a row across lines."""
    if _label_value_together(passage.norm, nlabel, nvalue):
        return True
    if passage.kind == "row":
        return False
    return _label_value_together(store.pages(sha)[page - 1].norm, nlabel, nvalue, window=window)


def _label_value_together(norm_text: str, nlabel: str, nvalue: str, window: int | None = None) -> bool:
    """Label and value in the same row; with `window`, the value must follow the label within it."""
    if window is None:
        return nlabel in norm_text and nvalue in norm_text
    start = 0
    while True:
        i = norm_text.find(nlabel, start)
        if i < 0:
            return False
        seg = norm_text[i + len(nlabel) : i + len(nlabel) + window]
        if nvalue in seg:
            return True
        start = i + 1


def admit_quote(
    ledger: Ledger,
    store,
    pid: str,
    as_of: str | date,
    *,
    quote: str | None = None,
    kind: str = "prose",
    row_label: str | None = None,
    value_text: str | None = None,
    proposal: dict[str, Any] | None = None,
    run_docs: Iterable[str] | None = None,
    settings: dict[str, Any] | None = None,
):
    """Run the gate and write the result: (SPAN rid, None) on pass, (None, REJECT rid) on fail.

    An identical span (same document, page and normalised text) is not duplicated.
    """
    res = check_quote(
        store,
        pid,
        as_of,
        quote=quote,
        kind=kind,
        row_label=row_label,
        value_text=value_text,
        run_docs=run_docs,
        settings=settings,
    )
    if not res.ok:
        prop = proposal or {"pid": pid, "quote": quote, "kind": kind}
        return None, write_reject(ledger, prop, "SPAN", res.rejection)
    s = res.span
    for e in ledger.query(
        RecordType.SPAN,
        doc_sha256=s["doc_sha256"],
        page=s["page"],
        norm_text=s["norm_text"],
        kind=s["kind"],
        row_label=s["row_label"],
        value_text=s["value_text"],
    ):
        return e.rid, None
    return ledger.append(RecordType.SPAN, s, QUOTE_AUTHOR).rid, None


def recheck_span(store, span: dict[str, Any], window: int = DEFAULTS["table_window_chars"]) -> str | None:
    """For `signal verify`: does the stored span still match the cached document? None = yes."""
    sha, page = span["doc_sha256"], span["page"]
    if not store.has(sha):
        return f"document {sha[:10]} not in cache"
    try:
        pg = store.pages(sha)[page - 1]
    except IndexError:
        return f"page {page} missing"
    nq = span["norm_text"]
    if normalise(span["text"]) != nq:
        return "stored text and norm_text disagree"
    if span.get("match") == "fuzzy":
        sim = fuzz.partial_ratio(nq, pg.norm) / 100.0
        return None if sim >= DEFAULTS["fuzzy_for_ocr_only"] else f"fuzzy similarity {sim:.3f} below 0.97"
    if nq not in pg.norm:
        return "quote no longer found on page"
    if span["kind"] == "table_cell":
        nl, nv = normalise(span.get("row_label") or ""), normalise(span.get("value_text") or "")
        passage = store.passage_by_pid(span["pid"]) if span.get("pid") else None
        if not (nl and nv) or passage is None or not _cell_ok(store, sha, page, passage, nl, nv, window):
            return "row label and value not together"
    return None
