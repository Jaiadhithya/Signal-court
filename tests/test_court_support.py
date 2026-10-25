"""Shared fixture for the court tests: a sealed claim on a small panel, two target documents, a fake router."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from conftest import fixed_clock, make_ctx, put_pdf
from pydantic import BaseModel

from signalcourt.calc import periods as P
from signalcourt.calc.panel import COLUMNS, Panel
from signalcourt.ledger import Author, Ledger, RecordType
from signalcourt.llm.router import RouterResult
from signalcourt.store import DocStore

NAME = "Testco Finance Limited"
SYMBOL = "TESTCO"
DECK = (
    "Quarterly update\n\n"
    "Testco Finance advances grew strongly after the second quarter, driven by retail loans and a wider branch "
    "network across the northern region.\n\n"
    "The board reviewed the dividend policy and noted the progress of the digital lending platform this year."
)
RATING = (
    "Rating rationale\n\n"
    "The rating reflects the sustained growth in advances of Testco Finance Limited and its stable deposit "
    "franchise, supported by healthy capital buffers.\n\n"
    "The outlook remains stable given adequate liquidity and a diversified funding profile over the period."
)
FIRST = "Q1FY24"
N_Q = 12
BREAK_AT = 8  # index into the quarters: the break quarter


def quarters() -> list[str]:
    return [P.shift(FIRST, i) for i in range(N_Q)]


@dataclass
class Fix:
    led: Ledger
    store: DocStore
    panel: Panel
    claim: str
    signal: str
    docs: dict[str, str]  # doc_type -> sha
    doc_rids: dict[str, str]
    ctx: object = None
    pids: dict[str, str] = field(default_factory=dict)


def _doc(led, meta, family, doc_type, url):
    return led.append(
        RecordType.DOC,
        {
            "sha256": meta.sha256,
            "url": url,
            "final_url": url,
            "doc_type": doc_type,
            "source_id": "exchange",
            "source_family": family,
            "origin_id": url,
            "entity_symbol": SYMBOL,
            "publish_date": meta.publish_date,
            "publish_date_source": "exchange_broadcast",
            "time_gate": "ok",
        },
        Author.code("harvest"),
    ).rid


def build(tmp_path, *, seal=True, p_raw=0.05, peers_move=False, impl=None) -> Fix:
    store = DocStore(tmp_path / "cache")
    deck = put_pdf(store, [DECK], url="https://example.test/deck.pdf", final_url="https://example.test/deck.pdf",
                   doc_type="deck", source_family="company_filing", publish=date(2026, 8, 1))
    rating = put_pdf(store, [RATING], url="https://example.test/rating.pdf",
                     final_url="https://example.test/rating.pdf", doc_type="rating_letter",
                     source_family="rating_agency", publish=date(2026, 7, 1))
    ctx = make_ctx(tmp_path, impl or {"court": "real", "judge": "real"}, store=store, home=tmp_path / "home",
                   mode="warm")
    led = ctx.ledger
    led.append(
        RecordType.ENTITY,
        {"symbol": SYMBOL, "legal_names": [NAME], "subtype": "bank", "subtype_evidence": {}, "peer_rule": "test"},
        Author.code("resolve"),
    )
    doc_rids = {
        "deck": _doc(led, deck, "company_filing", "deck", "https://example.test/deck.pdf"),
        "rating_letter": _doc(led, rating, "rating_agency", "rating_letter", "https://example.test/rating.pdf"),
    }
    peer_doc = led.append(
        RecordType.DOC,
        {"sha256": "f" * 64, "url": "peer://panel", "final_url": "peer://panel", "doc_type": "peer_panel",
         "source_id": "peer_panel", "source_family": "peer_filing", "origin_id": "peer://panel",
         "publish_date": "2026-08-01", "time_gate": "ok"},
        Author.code("harvest"),
    ).rid

    qs = quarters()
    rows = []
    for i, q in enumerate(qs):
        v = 100.0 + i + (30.0 if i >= BREAK_AT else 0.0)
        fact = led.append(
            RecordType.FACT,
            {"entity": SYMBOL, "metric": "advances", "value": v, "unit": "inr_cr", "period_end": P.end(q).isoformat(),
             "basis": "standalone", "span_rid": "na", "extractor": "xbrl", "value_as_written": str(v),
             "recon_status": "pass"},
            Author.code("extract"),
        ).rid
        rows.append(dict(entity=SYMBOL, metric="advances", period=q, pidx=P.index(q), basis="standalone", value=v,
                         rid=fact, tier=1, published=date(2026, 8, 1), recon_status="pass"))
        for k in range(5):
            pv = 50.0 + k + 0.5 * i + (15.0 if peers_move and i >= BREAK_AT else 0.0)
            rows.append(dict(entity=f"PEER{k}", metric="advances", period=q, pidx=P.index(q), basis="standalone",
                             value=pv, rid=peer_doc, tier=1, published=date(2026, 8, 1), recon_status="pass"))
    panel = Panel(pd.DataFrame.from_records(rows, columns=COLUMNS), SYMBOL, [f"PEER{k}" for k in range(5)],
                  date(2026, 10, 1))
    ctx.panel = panel

    sig = led.append(
        RecordType.SIGNAL,
        {"sensor": "gap", "entity": SYMBOL, "metrics": ["advances"], "direction": "up",
         "break_period": qs[BREAK_AT], "size": 30.0, "z": 6.2, "persistence_q": 4, "specificity": 0.95,
         "specificity_source": "peer_median", "placebo": {"p_raw": p_raw, "n": 20, "rank": 1},
         "inputs": [r["rid"] for r in rows if r["entity"] == SYMBOL] + [peer_doc],
         "surprise": {"score": 5.0, "z": 6.2, "persistence": 1.0, "specificity": 0.95,
                      "coverage_status": "peer_median: 5 donors"}},
        Author.code("sensors"),
    ).rid
    claim = led.append(
        RecordType.CLAIM,
        {"hypothesis_index": 0, "statement": "Advances moved up against its peer median from Q1FY26 and stayed there.",
         "mechanism": "A lasting upward shift in advances that its peer median did not share.",
         "signal_rid": sig,
         "falsification": {"metric": "advances", "comparator": "<", "threshold": 126.0, "deadline_period": "Q2FY27",
                           "source_rule": "next two exchange results filings"},
         "alt_checklist": ["sector_shock", "base_effect"], "drafter": {"model": "template", "family": "code"}},
        Author.code("prereg"),
    ).rid
    if seal:
        led.seal_head("prereg_head")
    ctx.claims = [claim]
    ctx.signals = [sig]
    ctx.doc_shas = [deck.sha256, rating.sha256]
    pids = {}
    for key, meta, needle in (("deck", deck, "advances grew strongly"), ("rating", rating, "sustained growth")):
        for page in range(1, 2):
            for p in store.passages(meta.sha256, page):
                if needle in p.norm:
                    pids[key] = p.pid
    return Fix(led, store, panel, claim, sig, {"deck": deck.sha256, "rating_letter": rating.sha256}, doc_rids,
               ctx, pids)


class FakeRouter:
    """Scripted replies per role. A reply is a dict, a callable(messages) -> dict, or an exception to raise."""

    def __init__(self, replies: dict, families: dict | None = None):
        self.replies = {k: list(v) if isinstance(v, list) else [v] for k, v in replies.items()}
        self.families = families or {"investigate": "nvidia", "advocate": "nvidia", "redteam": "openai",
                                     "entail": "google"}
        self.calls_used = 0
        self.calls: list[dict] = []
        self._used: dict[str, set[str]] = {}

    def families_used(self, role: str) -> set[str]:
        return set(self._used.get(role, ()))

    def call(self, role, template, messages, schema: type[BaseModel], *, exclude_families=frozenset(),
             max_tokens=None) -> RouterResult:
        self.calls_used += 1
        fam = self.families[role]
        self.calls.append({"role": role, "exclude": set(exclude_families), "family": fam, "messages": messages})
        queue = self.replies.get(role) or [{}]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages)
        self._used.setdefault(role, set()).add(fam)
        return RouterResult(schema.model_validate(reply), None, f"model-{fam}", fam, f"prov-{fam}", False)

    def close(self) -> None:
        pass


def test_fixture_builds_a_sealed_claim(tmp_path):
    f = build(tmp_path)
    assert f.led.heads().get("prereg_head")
    assert set(f.pids) == {"deck", "rating"}
    f.led.close()
    f.store.close()


__all__ = ["NAME", "SYMBOL", "Fix", "FakeRouter", "build", "fixed_clock"]
