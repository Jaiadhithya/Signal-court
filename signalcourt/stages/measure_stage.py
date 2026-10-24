"""Real measure: parse every results XBRL of the target and its peers from the store.

Target: one SPAN (kind ``xbrl``) and one FACT per fact, so every target number has full lineage; a
quarter that exists only as year-to-date becomes a CALC over the two YTD spans. Peers get no FACT
records: their rows go to ``run_dir/peer_panel.parquet``, which is stored in the DocStore and recorded
by one DOC whose rid every peer panel fact carries. The panel applies the time gate on publish date and
is saved to ``run_dir/panel.parquet``; registry formulas are then computed for the latest quarter.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..calc import formulas as F
from ..calc import periods as P
from ..calc import reconcile
from ..calc.calculator import Calculator, FactBook
from ..calc.panel import PanelFact, build_panel
from ..extract.types import FactProposal
from ..harvest import filings as Fl
from ..ledger import Author, LedgerCalcSink, RecordType
from ..ledger.schema import DocPayload, FactPayload, SpanPayload, XbrlRef
from ..pipeline import RunContext, register
from ..store import DocMeta, DocStore
from . import common as C

QUOTE_GATE = Author.code("quote_gate")
EXTRACT = Author.code("extract")
HARVEST = Author.code("harvest")
PEER_PANEL = "peer_panel.parquet"
PANEL = "panel.parquet"
YTD_FORMULA = {
    "formula_id": "ytd_to_quarter",
    "formula_version": 1,
    "formula_text": "year-to-date value less the year-to-date value at the previous quarter end",
}


@dataclass
class ListSource:
    """FactSource over rows already built in memory."""

    rows: list[PanelFact]

    def panel_facts(self, entities: list[str]) -> Iterable[PanelFact]:
        want = set(entities)
        return [r for r in self.rows if r.entity in want]


def visible_docs(store: DocStore, symbol: str, as_of: date) -> list[DocMeta]:
    """Results filings published on or before ``as_of``; undated and later ones are never parsed."""
    return [m for m in store.docs_for(symbol, C.RESULTS) if m.publish_date and m.publish_date <= as_of.isoformat()]


def parse_all(store: DocStore, symbol: str, subtype: str, docs: list[DocMeta]) -> list[tuple[DocMeta, Fl.ParsedFiling]]:
    out = []
    for m in docs:
        try:
            out.append((m, C.parse_doc(store, m, symbol, subtype)))
        except Exception:
            continue  # one unreadable filing never stops the panel
    return out


def published_of(meta: DocMeta) -> date:
    return date.fromisoformat(meta.publish_date)


def xbrl_ref(p: FactProposal) -> XbrlRef:
    x = p.span.xbrl or {}
    dec = x.get("decimals")
    return XbrlRef(
        element=x.get("element", ""),
        context_id=x.get("context_id", ""),
        unit=x.get("unit"),
        decimals=None if dec is None else str(dec),
    )


def write_span(ctx: RunContext, sha: str, text: str, xbrl: XbrlRef, doc_rids: dict[str, str]) -> str:
    span = SpanPayload(
        doc_sha256=sha, page=0, kind="xbrl", text=text, norm_text=text, value_text=text, xbrl=xbrl
    )
    parents = [doc_rids[sha]] if sha in doc_rids else []
    return ctx.ledger.append(RecordType.SPAN, span, QUOTE_GATE, parents).rid


def measure_target(
    ctx: RunContext, symbol: str, parsed: list[tuple[DocMeta, Fl.ParsedFiling]]
) -> list[PanelFact]:
    doc_rids = {e.payload["sha256"]: e.rid for e in ctx.ledger.query(RecordType.DOC)}
    rows: list[PanelFact] = []
    for meta, pf in parsed:
        for p in pf.quarter:
            span_rid = write_span(ctx, meta.sha256, p.value_as_written, xbrl_ref(p), doc_rids)
            status = reconcile.sanity(p).status
            fact = FactPayload(
                entity=symbol, metric=p.metric, value=p.value, unit=p.unit, period_end=p.period_end.isoformat(),
                basis=p.basis, span_rid=span_rid, extractor="xbrl", value_as_written=p.value_as_written,
                recon_status=status,
            )
            rid = ctx.ledger.append(RecordType.FACT, fact, EXTRACT, [span_rid]).rid
            rows.append(PanelFact(symbol, p.metric, P.label(p.period_end), p.basis, p.value, rid, 1,
                                  published_of(meta), status))
    rows += derived_target(ctx, symbol, parsed, doc_rids)
    return rows


def derived_target(
    ctx: RunContext, symbol: str, parsed: list[tuple[DocMeta, Fl.ParsedFiling]], doc_rids: dict[str, str]
) -> list[PanelFact]:
    """Quarters a filing has only as year-to-date: CALC(ytd now - ytd at the previous quarter end)."""
    by_sha = {m.sha256: (m, pf) for m, pf in parsed}
    by_key = {(pf.filing.consolidated, P.index(pf.period_end)): pf for _, pf in parsed if pf.period_end}
    sink = LedgerCalcSink(ctx.ledger)
    rows: list[PanelFact] = []
    for d in Fl.derive_missing_quarters(pf for _, pf in parsed):
        meta, _ = by_sha[d.span.doc_sha256]
        inputs = [write_span(ctx, meta.sha256, d.value_as_written, xbrl_ref(d), doc_rids)]
        if d.notes.get("ytd_prev"):
            prev = by_key.get((d.basis == "consolidated", P.index(d.period_end) - 1))
            if prev is not None and d.metric in prev.ytd:
                _, text, element, cid = prev.ytd[d.metric]
                ref = XbrlRef(element=element, context_id=cid, unit="INR")
                inputs.append(write_span(ctx, prev.sha256, text, ref, doc_rids))
        status = reconcile.sanity(d).status
        payload = {
            **YTD_FORMULA,
            "inputs": inputs,
            "params": {"metric": d.metric, "basis": d.basis, **d.notes},
            "value": d.value,
            "unit": d.unit,
            "period_end": d.period_end.isoformat(),
            "entity": symbol,
            "recon_status": status,
        }
        rid = sink.append_calc(payload, inputs)
        rows.append(PanelFact(symbol, d.metric, P.label(d.period_end), d.basis, d.value, rid, 1,
                              published_of(meta), status))
    return rows


def peer_rows(symbol: str, parsed: list[tuple[DocMeta, Fl.ParsedFiling]]) -> list[dict]:
    pub = {m.sha256: m.publish_date for m, _ in parsed}
    props = [p for _, pf in parsed for p in pf.quarter] + Fl.derive_missing_quarters(pf for _, pf in parsed)
    out = []
    for p in props:
        x = p.span.xbrl or {}
        out.append(
            {
                "entity": symbol, "metric": p.metric, "period": P.label(p.period_end),
                "period_end": p.period_end.isoformat(), "basis": p.basis, "value": p.value, "unit": p.unit,
                "published": pub[p.span.doc_sha256], "recon_status": reconcile.sanity(p).status,
                "doc_sha256": p.span.doc_sha256, "element": x.get("element"), "context_id": x.get("context_id"),
                "value_as_written": p.value_as_written, "derived_from": p.derived_from,
            }
        )
    return out


def record_peer_panel(ctx: RunContext, rows: list[dict]) -> str | None:
    """Write the peers' rows to parquet, store it, and append one DOC for it; returns the DOC rid."""
    if not rows:
        return None
    df = pd.DataFrame.from_records(rows).sort_values(["entity", "metric", "period_end", "basis", "published"])
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    data = buf.getvalue()
    (ctx.run_dir / PEER_PANEL).write_bytes(data)
    url = f"signalcourt://runs/{ctx.ledger.run_id}/{PEER_PANEL}"
    published = date.fromisoformat(max(r["published"] for r in rows))
    meta = ctx.store.put_bytes(
        data, url=url, final_url=url, mime="application/vnd.apache.parquet", source_id="peer_panel",
        source_family="peer_filing", doc_type="peer_panel", publish_date=published,
        publish_date_source="latest_input",
    )
    doc = DocPayload(
        sha256=meta.sha256, url=url, final_url=url, doc_type="peer_panel", source_id="peer_panel",
        source_family="peer_filing", origin_id=url, publish_date=published.isoformat(),
        publish_date_source="latest_input", n_pages=meta.n_pages, time_gate="ok",
    )
    return ctx.ledger.append(RecordType.DOC, doc, HARVEST).rid


def compute_formulas(ctx: RunContext, panel) -> int:
    """Every registry formula that is defined for the target's latest quarter, as CALC records."""
    latest = panel.latest_period()
    if latest is None:
        return 0
    book = FactBook()
    for r in panel.df[panel.df.entity == panel.target].itertuples(index=False):
        book.add(r.entity, r.metric, r.period, r.value, r.rid, r.recon_status)
    calc = Calculator(book, LedgerCalcSink(ctx.ledger))
    n = 0
    for fid, fm in calc.formulas.items():
        if fm.get("expr") == "builtin":
            continue
        try:
            calc.compute(fid, panel.target, latest)
            n += 1
        except (F.FormulaError, KeyError, ValueError, TypeError, ZeroDivisionError, SyntaxError):
            continue  # an input this sub-type does not report
    return n


@register("measure", "real")
def measure(ctx: RunContext) -> None:
    if ctx.store is None or not ctx.symbol:
        raise RuntimeError("the real measure stage needs the real harvest stage first (symbol and DocStore)")
    t0 = time.monotonic()
    cap = ctx.settings["demo"]["measure_cap_s"]
    cfg = ctx.settings["panel"]
    subtype = ctx.subtype or "generic"
    allowed = set(ctx.doc_shas)
    target_docs = [m for m in visible_docs(ctx.store, ctx.symbol, ctx.as_of) if m.sha256 in allowed]
    rows = measure_target(ctx, ctx.symbol, parse_all(ctx.store, ctx.symbol, subtype, target_docs))

    peers_raw: list[dict] = []
    used: list[str] = []
    for i, peer in enumerate(ctx.peers):
        if time.monotonic() - t0 > cap:
            ctx.on_event("stage_note", {"stage": "measure", "note": f"time cap: {len(ctx.peers) - i} peers skipped"})
            break
        got = peer_rows(peer, parse_all(ctx.store, peer, subtype, visible_docs(ctx.store, peer, ctx.as_of)))
        if got:
            peers_raw += got
            used.append(peer)
    doc_rid = record_peer_panel(ctx, peers_raw)
    for r in peers_raw:
        rows.append(PanelFact(r["entity"], r["metric"], r["period"], r["basis"], r["value"], doc_rid, 1,
                              date.fromisoformat(r["published"]), r["recon_status"]))

    panel = build_panel(
        ctx.symbol, used, ctx.as_of, ListSource(rows), quarters=cfg["quarters"], basis_default=cfg["basis_default"]
    )
    panel.save(ctx.run_dir / PANEL)
    ctx.panel = panel
    compute_formulas(ctx, panel)
