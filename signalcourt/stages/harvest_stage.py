"""Real harvest: resolve the typed name, refresh the target (warm/live), read the sub-type from the
filing format, pick peers, then record ENTITY and one DOC per target document.

Replay never builds an exchange client. A failed refresh is recorded as a failed ``harvest_refresh``
GATE and the run carries on with the pre-warmed store. Peers are same-sub-type entities of the universe
with results in the store, capped at the largest ``[panel] max_peers`` by latest total income.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..harvest import tiers as T
from ..ledger import Author, RecordType
from ..ledger.schema import DocPayload, EntityPayload, GatePayload, PeerRef, SubtypeEvidence
from ..pipeline import RunContext, register
from ..resolve import universe as U
from ..resolve.entity import Ambiguous, EntityMatch, resolve
from ..resolve.peers import select_peers
from ..settings import Env, Workspace
from ..store import DocMeta, DocStore
from . import common as C

RESOLVE = Author.code("resolve")
HARVEST = Author.code("harvest")
GATES = Author.code("gates")
REFRESH_TIERS = {"T1", "T2"}


class AmbiguousCompany(Exception):
    def __init__(self, candidates: list[EntityMatch]) -> None:
        super().__init__("ambiguous company name; candidates: " + ", ".join(c.symbol for c in candidates))
        self.candidates = candidates


class UnknownCompany(Exception):
    pass


def make_exchange():
    """The live exchange client (warm/live only). Tests replace this."""
    from ..net.http import PoliteClient
    from ..net.nse import NSE

    return NSE(PoliteClient(Env().http_user_agent))


def resolve_company(con: sqlite3.Connection, company: str, symbol: str | None = None) -> EntityMatch:
    """The financial universe first, then every listed entity; ambiguity and no match both raise."""
    m = resolve(con, symbol) if symbol else None
    if m is None:
        m = resolve(con, company, financial_only=True) or resolve(con, company)
    if isinstance(m, Ambiguous):
        raise AmbiguousCompany(m.candidates)
    if m is None:
        raise UnknownCompany(f"no listed company matches {company!r}")
    return m


def refresh(ctx: RunContext, con: sqlite3.Connection, m: EntityMatch) -> list[str]:
    """Fetch what is newer than the store for the target, under ``demo.harvest_cap_s``. Returns errors."""
    st = C.subtype_from_store(ctx.store, m.symbol)
    row = con.execute("SELECT subtype FROM entities WHERE symbol=?", (m.symbol,)).fetchone()
    subtype = st.decision.subtype if st else (row[0] if row and row[0] else "generic")
    ent = T.EntityRef(m.symbol, m.name, subtype, m.isin, m.legal_names)
    exch = None
    try:
        exch = make_exchange()
        rep = T.harvest(
            ent, ctx.as_of, REFRESH_TIERS, ctx.settings["demo"]["harvest_cap_s"], ctx.mode, exch=exch, store=ctx.store
        )
        return list(rep.errors)
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]
    finally:
        close = getattr(getattr(exch, "c", exch), "close", None)
        if callable(close):
            close()


def peer_universe(con: sqlite3.Connection, store: DocStore, target: str, subtype: str, cap: int) -> dict[str, float]:
    """Same-sub-type symbols with results in the store -> latest size, the largest ``cap`` only."""
    rows = con.execute("SELECT symbol FROM entities WHERE financial=1 AND subtype=? AND symbol<>?", (subtype, target))
    sizes = {}
    for (sym,) in rows:
        size = C.latest_size(store, sym, subtype)
        if size:
            sizes[sym] = size
    keep = sorted(sizes, key=lambda s: (-sizes[s], s))[:cap]
    return {s: sizes[s] for s in keep}


def time_gate(meta: DocMeta, as_of: date) -> str:
    if not meta.publish_date:
        return "undated"
    return "ok" if meta.publish_date <= as_of.isoformat() else "dropped"


def doc_payload(meta: DocMeta, as_of: date) -> DocPayload:
    return DocPayload(
        sha256=meta.sha256,
        url=meta.first_url,
        final_url=meta.final_url,
        doc_type=meta.doc_type or "other",
        source_id=meta.source_id,
        source_family=meta.source_family,
        origin_id=meta.first_url,
        entity_symbol=meta.entity_symbol,
        period_end=meta.period_end,
        publish_date=meta.publish_date,
        publish_date_source=meta.publish_date_source or "none",
        n_pages=meta.n_pages,
        ocr=meta.ocr,
        time_gate=time_gate(meta, as_of),
    )


@register("harvest", "real")
def harvest(ctx: RunContext) -> None:
    if ctx.store is None or ctx.home is None:
        raise RuntimeError("the real harvest stage needs a workspace (home) and an open DocStore")
    con = U.connect(Workspace(home=ctx.home).universe_db)
    try:
        m = resolve_company(con, ctx.company, ctx.symbol)
        errors = refresh(ctx, con, m) if ctx.mode in ("warm", "live") else []
        st = C.subtype_from_store(ctx.store, m.symbol)
        if ctx.subtype:  # explicit CLI override
            subtype = ctx.subtype
        elif st:
            subtype = st.decision.subtype
        else:
            row = con.execute("SELECT subtype FROM entities WHERE symbol=?", (m.symbol,)).fetchone()
            subtype = row[0] if row and row[0] else "generic"
        cap = ctx.settings["panel"]["max_peers"]
        sizes = peer_universe(con, ctx.store, m.symbol, subtype, cap)
        target_size = C.latest_size(ctx.store, m.symbol, subtype)
        if target_size:
            sizes[m.symbol] = target_size
        ps = select_peers(
            m.symbol,
            subtype,
            {s: subtype for s in sizes} | {m.symbol: subtype},
            sizes,
            has_deck=lambda s: bool(ctx.store.docs_for(s, "deck")),
            has_metric=lambda s: s in sizes,
            n_tier2=ctx.settings["panel"]["tier2_peers"],
        )
    finally:
        con.close()

    entity = EntityPayload(
        symbol=m.symbol,
        isin=m.isin,
        legal_names=m.legal_names,
        subtype=subtype,
        subtype_evidence=SubtypeEvidence(
            entry_point=st.entry_point if st else None,
            taxonomy=st.xbrl_file if st else None,
            segments=st.segments if st else [],
        ),
        peers=[PeerRef(symbol=p, subtype=subtype, tier2=p in ps.tier2) for p in ps.placebo],
        peer_rule=f"{ps.rule}; universe: same sub-type with results, largest {cap} by latest total income",
    )
    ent_rid = ctx.ledger.append(RecordType.ENTITY, entity, RESOLVE).rid
    for err in errors:
        gate = GatePayload(gate="harvest_refresh", subject_rid=ent_rid, result="fail", reason=err[:500])
        ctx.ledger.append(RecordType.GATE, gate, GATES, [ent_rid])
        ctx.on_event("stage_note", {"stage": "harvest", "note": f"refresh failed, using the store: {err[:200]}"})

    ctx.doc_shas = []
    for meta in ctx.store.docs_for(m.symbol):
        doc = doc_payload(meta, ctx.as_of)
        ctx.ledger.append(RecordType.DOC, doc, HARVEST, [ent_rid])
        if doc.time_gate == "ok":
            ctx.doc_shas.append(meta.sha256)
    ctx.symbol, ctx.subtype, ctx.peers = m.symbol, subtype, list(ps.placebo)
