"""Deterministic, ledger-grounded research prose.

``write_finding`` deliberately appends reusable CALC projections of numerical
SIGNAL/CLAIM/VERDICT fields and source counts. The existing text gate accepts
only FACT/CALC values. Projections name the extraction/count operation and its
source records; they never strengthen reconciliation or turn prose into facts.
Use ``append_finding`` to gate the resulting FINDING before appending it.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Any

from ..calc import periods as P
from ..gates.common import write_reject
from ..gates.schema_gate import check_finding_text
from ..gates.unbacked import cited_values_from_ledger, find_unbacked, number_tokens
from ..judge.grade import admissible
from ..ledger import Author, Ledger, RecordType
from ..ledger.schema import Envelope, FindingElements, FindingPayload, TextItem
from ..settings import load_registry

METRIC_FAMILIES = {
    **dict.fromkeys(
        (
            "gnpa_amt",
            "nnpa_amt",
            "gnpa_pct",
            "nnpa_pct",
            "provisions",
            "impairment_fin_instr",
            "gross_slippages",
            "recoveries_upgrades",
            "write_offs",
            "pcr",
            "stage2_pct",
            "stage3_pct",
            "stage23_amt",
        ),
        "asset_quality",
    ),
    **dict.fromkeys(
        ("deposits", "casa_pct", "finance_costs", "interest_expended", "cost_of_funds", "borrowing_mix"), "funding"
    ),
    **dict.fromkeys(
        ("roa", "ppop", "pat", "pat_owners", "pbt", "eps_basic", "nim", "vnb_margin", "other_income"), "profitability"
    ),
    **dict.fromkeys(
        (
            "interest_earned",
            "total_income",
            "advances",
            "total_assets",
            "revenue",
            "interest_income",
            "loans",
            "premium_income",
            "net_premium_income",
            "aum",
            "ape",
            "persistency_13m",
            "persistency_25m",
            "persistency_61m",
            "segment_mix",
            "product_mix",
            "channel_mix",
        ),
        "growth",
    ),
    **dict.fromkeys(("cet1", "solvency"), "capital"),
    **dict.fromkeys(("opex", "total_expenses", "tax_expense"), "expenses"),
}
FAMILY_TEXT = {
    "asset_quality": (
        "Asset quality affects future credit costs; inspect provisions and recovery flows alongside the level."
    ),
    "funding": "Funding shifts can change margins and refinancing exposure; compare the funding mix and its cost.",
    "profitability": (
        "A profit break needs a bridge from revenue to costs and provisions before treating it as recurring."
    ),
    "growth": ("Read growth with its funding and business mix; a larger balance sheet alone does not explain quality."),
    "capital": (
        "Capital buffers constrain future growth and absorb losses; compare the ratio with its underlying capital base."
    ),
    "expenses": "An expense break can alter operating efficiency; distinguish recurring costs from exceptional items.",
}
LABELS = {
    "gnpa_amt": "gross NPA",
    "nnpa_amt": "net NPA",
    "gnpa_pct": "gross NPA ratio",
    "nnpa_pct": "net NPA ratio",
    "cet1": "common equity capital ratio",
    "roa": "return on assets",
    "eps_basic": "basic earnings per share",
    "pat": "profit after tax",
    "pat_owners": "profit attributable to owners",
    "pbt": "profit before tax",
    "ppop": "pre-provision operating profit",
    "opex": "operating expenses",
    "nim": "net interest margin",
    "casa_pct": "current and savings deposit share",
    "pcr": "provision coverage ratio",
    "aum": "assets under management",
    "ape": "annualised premium equivalent",
    "vnb_margin": "new business margin",
    "stage2_pct": "Stage II asset share",
    "stage3_pct": "Stage III asset share",
    "stage23_amt": "Stage II and III assets",
    "persistency_13m": "thirteenth-month persistency",
    "persistency_25m": "twenty-fifth-month persistency",
    "persistency_61m": "sixty-first-month persistency",
}
_QUARTER = re.compile(r"Q[1-4]\s*FY\s*\d{2,4}", re.I)


@lru_cache(maxsize=1)
def _metrics() -> dict[str, dict]:
    return {row["id"]: row for row in load_registry("metrics.yaml")}


def metric_label(metric: str) -> str:
    return LABELS.get(metric, metric.replace("_", " "))


def period_label(period: str | None) -> str:
    if not period:
        return "an unavailable quarter"
    try:
        return P.from_index(P.index(period)).replace("FY", " FY")
    except (ValueError, TypeError, AttributeError):
        return str(period)


def _period_index(period: str | None) -> int | None:
    if not period:
        return None
    try:
        return P.index(date.fromisoformat(period[:10])) if "-" in period else P.index(period)
    except (ValueError, TypeError, AttributeError):
        return None


def _period_date(period: str | None, fallback: str) -> str:
    if period and "-" in period:
        try:
            return date.fromisoformat(period[:10]).isoformat()
        except ValueError:
            return fallback[:10]
    idx = _period_index(period)
    return P.end(P.from_index(idx)).isoformat() if idx is not None else fallback[:10]


def format_value(value: float | None, unit: str = "same") -> str:
    """Stable Indian grouping and the units used in the metric registry."""
    if value is None or not math.isfinite(float(value)):
        return "unavailable"
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    if unit == "inr_per_share":
        return f"Rs {value:.2f}"
    if unit == "inr_cr":
        whole, decimal = f"{abs(value):.1f}".split(".")
        groups = [whole[-3:]]
        whole = whole[:-3]
        while whole:
            groups.insert(0, whole[-2:])
            whole = whole[:-2]
        return f"{'-' if value < 0 else ''}{','.join(groups)}.{decimal} cr"
    if unit == "count":
        return str(int(value))
    if unit == "z":
        return f"{value:.1f}"
    return format(Decimal(f"{value:.3g}"), "f")


def _get(ledger: Ledger, rid: str | None) -> Envelope | None:
    try:
        return ledger.get(rid) if rid else None
    except KeyError:
        return None


def _unique(rids) -> list[str]:
    return list(dict.fromkeys(rid for rid in rids if rid))


def _ground_text(ledger: Ledger, text: str, rids: list[str]) -> str:
    """Retain sourced prose, visibly omitting numbers citations cannot reproduce."""
    bad = find_unbacked(text, cited_values_from_ledger(ledger, rids))
    for token in reversed(bad):
        text = text[: token.position] + "[uncited number omitted]" + text[token.position + len(token.token) :]
    return text


class _Writer:
    def __init__(self, ledger: Ledger, claim_rid: str, panel: Any, settings: dict, impl: dict[str, str]):
        self.ledger, self.panel, self.settings, self.impl = ledger, panel, settings, impl
        self.claim = ledger.get(claim_rid)
        self.signal = _get(ledger, self.claim.payload.get("signal_rid"))
        self.s = self.signal.payload if self.signal else {}
        self.c = self.claim.payload
        verdicts = ledger.query("VERDICT", claim_rid=claim_rid)
        self.verdict = verdicts[-1] if verdicts else None
        self.v = self.verdict.payload if self.verdict else {}
        self.metric = (self.s.get("metrics") or [self.c["falsification"]["metric"]])[0]
        self.label = metric_label(self.metric)
        self.unit = _metrics().get(self.metric, {}).get("unit", "same")
        self.ancestors = ledger.closure(self.signal.rid) if self.signal else []
        # CALC has no top-level metric: only the measure stage's explicitly
        # associated YTD subtraction is a reported metric level. Growth, ratios
        # and other calculations may share ancestors without sharing its unit.
        self.facts = sorted(
            (
                env
                for env in self.ancestors
                if (
                    (env.type is RecordType.FACT and env.payload.get("metric") == self.metric)
                    or (
                        env.type is RecordType.CALC
                        and env.payload.get("formula_id") == "ytd_to_quarter"
                        and env.payload.get("params", {}).get("metric") == self.metric
                    )
                )
                and env.payload.get("entity") == self.s.get("entity")
                and env.payload.get("value") is not None
                and env.payload.get("period_end")
            ),
            key=lambda env: (env.payload["period_end"], env.seq),
        )
        if self.facts:
            self.unit = self.facts[-1].payload.get("unit", self.unit)
        self.docs = {env.payload["sha256"]: env for env in ledger.query("DOC")}
        self.projections = {
            self._key(env.payload): env.rid for env in ledger.query("CALC", formula_id="report_projection")
        }
        entities = ledger.query("ENTITY", symbol=self.s.get("entity"))
        self.entity = entities[-1] if entities else None
        name = (
            (self.entity.payload.get("legal_names") or [self.entity.payload["symbol"]])[0]
            if self.entity
            else "The company"
        )
        self.name = re.sub(r"\s+(?:limited|ltd\.?)$", "", name, flags=re.I)
        self.prereg_stub = (
            impl.get("prereg") == "stub"
            or self.c["statement"].strip().upper() == "STUB"
            or self.c["falsification"]["comparator"] == "none"
        )
        self.challenges = ledger.query("CHALLENGE", claim_rid=claim_rid)
        self.evidence = ledger.query("EVIDENCE", claim_rid=claim_rid)
        self.court_stub = impl.get("court") == "stub" or any(
            ch.payload["alt_type"] == "stub" or ch.payload["argument"].strip().upper() == "STUB"
            for ch in self.challenges
        )

    @staticmethod
    def _key(payload: dict) -> str:
        return json.dumps({key: payload.get(key) for key in ("inputs", "params", "value", "unit")}, sort_keys=True)

    def project(
        self, value: Any, sources: list[str], field: str, *, unit="same", operator="field", detail: dict | None = None
    ) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        sources = _unique(sources)
        if not sources:
            return None
        statuses = [
            env.payload["recon_status"]
            for rid in sources
            for env in self.ledger.closure(rid)
            if env.type in (RecordType.FACT, RecordType.CALC) and "recon_status" in env.payload
        ]
        recon = "fail" if "fail" in statuses else "single_source"
        params = {"field": field, "operator": operator, **(detail or {})}
        formula = (
            f"Extract {field} from source record(s) {', '.join(sources)}"
            if operator == "field"
            else f"{operator}: {field} over source record(s) {', '.join(sources)}; parameters={detail or {}}"
        )
        payload = {
            "formula_id": "report_projection",
            "formula_version": 1,
            "formula_text": formula,
            "inputs": sources,
            "params": params,
            "value": float(value),
            "unit": unit,
            "entity": self.s.get("entity"),
            "recon_status": recon,
        }
        key = self._key(payload)
        if key not in self.projections:
            env = self.ledger.append("CALC", payload, Author.code("calculator"), sources)
            self.projections[key] = env.rid
        return self.projections[key]

    def signal_cite(self, field: str, *, unit="same") -> list[str]:
        value: Any = self.s
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        rid = self.project(value, [self.signal.rid] if self.signal else [], field, unit=unit)
        return _unique([self.signal.rid if self.signal else None, rid])

    def item(self, text: str, rids=(), *, ground=False) -> TextItem:
        rids = _unique(rids)
        return TextItem(text=_ground_text(self.ledger, text, rids) if ground else text, rids=rids)

    def metadata_cites(
        self, source: Envelope, field: str, *, text: str | None = None, location: str = "payload"
    ) -> list[str]:
        """URL path digits are document metadata, explicitly extracted for the text gate."""
        return _unique(
            self.project(
                token[1],
                [source.rid],
                field,
                unit="metadata",
                operator="number_token",
                detail={"token_index": index, "token": token[0], "location": location},
            )
            for index, token in enumerate(number_tokens(text if text is not None else source.payload.get(field) or ""))
        )

    def peer_count(self) -> tuple[int | None, list[str]]:
        docs = self.ledger.query("DOC", doc_type="peer_panel")
        if self.panel is None or not docs:
            return None, [self.signal.rid] if self.signal else []
        df = getattr(self.panel, "df", None)
        if df is None or not {"entity", "metric"}.issubset(df.columns):
            return None, []
        peers = sorted(set(df.loc[(df.metric == self.metric) & (df.entity != self.s.get("entity")), "entity"]))
        rid = self.project(
            len(peers),
            [docs[-1].rid],
            "entity",
            unit="count",
            operator="count_distinct",
            detail={"metric": self.metric, "exclude_entity": self.s.get("entity"), "entities": peers},
        )
        return len(peers), _unique([docs[-1].rid, rid])

    def baseline(self) -> tuple[str, list[str]]:
        source = self.s.get("specificity_source", "na")
        if source == "na":
            return "its own history", [self.signal.rid] if self.signal else []
        coverage = (self.s.get("surprise") or {}).get("coverage_status") or ""
        pattern = rf"{re.escape(source)}:\s*(\d+)\s+donors?"
        donors = re.search(pattern, coverage)
        if donors and self.signal:
            n = int(donors.group(1))
            rid = self.project(
                n,
                [self.signal.rid],
                "surprise.coverage_status",
                unit="count",
                operator="regex_capture",
                detail={"pattern": pattern, "group": 1},
            )
            rids = [self.signal.rid, rid]
        else:
            n, rids = self.peer_count()
        label = "twin" if source == "twin" else "peer median"
        return (f"its {label} of {n} peers" if n is not None else f"its {label} (peer count unavailable)"), rids

    def discovery(self) -> TextItem:
        base, rids = self.baseline()
        direction = {"up": "rose", "down": "fell"}.get(self.s.get("direction"), "changed")
        gap = (
            format_value(abs(self.s["size"]), self.unit) if self.s.get("size") is not None else "an unavailable amount"
        )
        period = period_label(self.s.get("break_period"))
        z = self.s.get("z")
        text = f"From {period}, {self.name}'s {self.label} {direction} {gap} away from {base}" + (
            f", a break of {abs(z):.1f} robust standard deviations." if z is not None else "."
        )
        limit = max(1, self.settings.get("writer", {}).get("discovery_max_words", 35))
        if len(text.split()) > limit:
            text = f"{self.name}'s {self.label} {direction} {gap} from {period}, versus {base}."
        if len(text.split()) > limit:
            text = " ".join(text.split()[:limit]).rstrip(",.;") + "."
        return self.item(
            text,
            [
                self.claim.rid,
                self.entity.rid if self.entity else None,
                *(
                    self.metadata_cites(self.entity, "legal_names", text=self.name, location="payload_display_name")
                    if self.entity
                    else []
                ),
                *rids,
                *self.signal_cite("size", unit=self.unit),
                *self.signal_cite("z", unit="z"),
            ],
        )

    def why(self) -> list[TextItem]:
        placebo = self.s.get("placebo") or {}
        stat = (
            f"Placebo rank {placebo['rank']} of {placebo['n']}; raw placebo p = {format_value(placebo['p_raw'])}."
            if all(placebo.get(key) is not None for key in ("rank", "n", "p_raw"))
            else "Placebo rank or probability is unavailable; statistical rarity cannot be established from this run."
        )
        why = [
            self.item(
                stat,
                [
                    *self.signal_cite("placebo.rank", unit="count"),
                    *self.signal_cite("placebo.n", unit="count"),
                    *self.signal_cite("placebo.p_raw"),
                ],
            )
        ]
        persistence = self.s.get("persistence_q")
        why.append(
            self.item(
                f"The gap persisted for {persistence} quarters after the detected break."
                if persistence is not None
                else "The signal does not record how long the gap persisted.",
                self.signal_cite("persistence_q", unit="count"),
            )
        )
        baseline, rids = self.baseline()
        why.append(
            self.item(
                f"The signal was measured against {baseline}; this comparison is descriptive, not causal.",
                [*rids, self.signal.rid if self.signal else None],
            )
        )
        why.append(
            self.item(
                FAMILY_TEXT.get(
                    METRIC_FAMILIES.get(self.metric),
                    "Read the metric alongside related disclosures before assigning a mechanism.",
                ),
                [self.claim.rid],
            )
        )
        angle_id = self.c.get("angle_id") or self.s.get("angle_id")
        angle = next((a for a in load_registry("angles.yaml") if a["id"] == angle_id), None)
        if angle:
            why.append(self.item(f"Research angle: {angle['question']}.", [self.claim.rid], ground=True))
        maximum = min(5, max(3, self.settings.get("writer", {}).get("why_bullets", [3, 5])[-1]))
        return why[:maximum]

    def evidence_items(self) -> list[TextItem]:
        items: list[TextItem] = []
        trail: dict[str, tuple[Envelope, list[str]]] = {}
        for fact in self.facts:
            for env in self.ledger.closure(fact.rid):
                if env.type is RecordType.DOC and env.payload.get("entity_symbol") == self.s.get("entity"):
                    _, refs = trail.setdefault(env.rid, (env, []))
                    refs.append(fact.rid)
                elif env.type is RecordType.SPAN and env.payload.get("doc_sha256") in self.docs:
                    doc = self.docs[env.payload["doc_sha256"]]
                    if doc.payload.get("entity_symbol") == self.s.get("entity"):
                        _, refs = trail.setdefault(doc.rid, (doc, []))
                        refs.extend([fact.rid, env.rid])
        filings = sorted(trail.values(), key=lambda pair: (pair[0].payload.get("period_end") or "", pair[0].seq))
        if filings:
            xbrl = [(doc, refs) for doc, refs in filings if "xbrl" in doc.payload["doc_type"].lower()]
            counted = xbrl or filings
            rid = self.project(
                len(counted),
                [doc.rid for doc, _ in counted],
                "DOC",
                unit="count",
                operator="count",
                detail={"doc_type": "xbrl" if xbrl else "target_filings"},
            )
            first, last = counted[0][0].payload.get("period_end"), counted[-1][0].payload.get("period_end")
            first_label = period_label(P.from_index(_period_index(first))) if _period_index(first) else first
            last_label = period_label(P.from_index(_period_index(last))) if _period_index(last) else last
            items.append(
                self.item(
                    f"Exchange filings: {len(counted)} quarterly {'XBRL ' if xbrl else ''}filings, "
                    f"{first_label} to {last_label}.",
                    [rid, *[doc.rid for doc, _ in counted]],
                )
            )
            for doc, refs in filings:
                p = doc.payload
                idx = _period_index(p.get("period_end"))
                period = period_label(P.from_index(idx)) if idx is not None else "period unavailable"
                text = (
                    f"Exchange filings — {p['doc_type']}; {period}; "
                    f"published {p.get('publish_date') or 'date unavailable'}; source {p['final_url']}."
                )
                items.append(self.item(text, [doc.rid, *refs, *self.metadata_cites(doc, "final_url")], ground=True))
        else:
            items.append(
                self.item(
                    "Exchange filings: the signal has no linked target filing trail.",
                    [self.signal.rid] if self.signal else [],
                )
            )
        count, refs = self.peer_count()
        if count is not None:
            items.append(
                self.item(f"Peer filings: {count} peer companies' filings, hashed in the saved peer panel.", refs)
            )
        else:
            items.append(self.item("Peer filings: the peer count or saved peer-panel source is unavailable.", refs))
        for ev in sorted(self.evidence, key=lambda e: (e.payload["source_family"], e.seq)):
            refs = [env.rid for env in self.ledger.closure(ev.rid)]
            span = _get(self.ledger, ev.payload.get("span_rid"))
            if span:
                doc = self.docs.get(span.payload["doc_sha256"])
                refs.extend([doc.rid] if doc else [])
                refs.extend(f.rid for f in self.facts if f.payload.get("span_rid") == span.rid)
            if span and not find_unbacked(span.payload["text"], cited_values_from_ledger(self.ledger, refs)):
                quote = f" Quoted passage: “{span.payload['text']}”"
            elif span:
                quote = " The original passage is linked; its numerical detail is not reproduced here."
            else:
                quote = " Passage unavailable."
            items.append(
                self.item(
                    f"Court evidence / {ev.payload['source_family']}: {ev.payload['sentence']}{quote}",
                    refs,
                    ground=True,
                )
            )
        return items

    def corporate_note(self) -> str | None:
        coverage = (self.s.get("surprise") or {}).get("coverage_status") or ""
        notes = [
            part.strip()
            for part in coverage.split(";")
            if any(word in part.lower() for word in ("share-count", "share count", "merger", "bonus", "split"))
        ]
        if self.s.get("corp_action_rid"):
            env = _get(self.ledger, self.s["corp_action_rid"])
            if env and env.payload.get("summary"):
                notes.append(env.payload["summary"])
        return "; ".join(notes) if notes else None

    def timeline(self) -> list[TextItem]:
        items: list[TextItem] = []
        break_idx = _period_index(self.s.get("break_period"))
        pre = [f for f in self.facts if break_idx is not None and _period_index(f.payload["period_end"]) < break_idx]
        at_break = [f for f in self.facts if _period_index(f.payload["period_end"]) == break_idx]
        for label, fact in (
            ("Last pre-break quarter", pre[-1] if pre else None),
            ("Break quarter", at_break[-1] if at_break else None),
            ("Latest quarter", self.facts[-1] if self.facts else None),
        ):
            if fact:
                p = fact.payload
                docs = [e for e in self.ledger.closure(fact.rid) if e.type is RecordType.DOC]
                published = next(
                    (d.payload["publish_date"] for d in reversed(docs) if d.payload.get("publish_date")), None
                )
                text = (
                    f"{p['period_end'][:10]} — {label} ({period_label(P.from_index(_period_index(p['period_end'])))})"
                    f": reported {self.label} {format_value(p['value'], p.get('unit', self.unit))}"
                    f"; published {published or 'date unavailable'}."
                )
                items.append(self.item(text, [e.rid for e in self.ledger.closure(fact.rid)]))
        if not at_break and break_idx is not None:
            items.append(
                self.item(
                    f"{P.end(P.from_index(break_idx)).isoformat()} — Detected break quarter "
                    f"{period_label(self.s['break_period'])}; a linked level is unavailable.",
                    [self.signal.rid] if self.signal else [],
                )
            )
        note = self.corporate_note()
        if note:
            period = _QUARTER.search(note)
            when = _period_date(period.group() if period else self.s.get("break_period"), self.claim.created_at)
            note = _QUARTER.sub(lambda m: period_label(m.group()), note)
            refs = _unique([self.signal.rid if self.signal else None, self.s.get("corp_action_rid")])
            items.append(self.item(f"{when} — Corporate action: {note}.", refs, ground=True))
        items.append(
            self.item(
                f"{self.claim.created_at[:10]} — "
                + (
                    "Hypothesis and kill test were not pre-registered in this run."
                    if self.prereg_stub
                    else "Claim recorded; its pre-registration seal appears in the method and falsification sections."
                ),
                [self.claim.rid],
            )
        )
        fals = self.c["falsification"]
        deadline = _period_date(fals.get("deadline_period"), self.claim.created_at)
        items.append(
            self.item(
                f"{deadline} — "
                + (
                    "No pre-registered kill-test deadline in this run."
                    if self.prereg_stub
                    else f"Kill-test deadline: {period_label(fals['deadline_period'])}."
                ),
                [self.claim.rid],
            )
        )
        if self.signal and len(items) < max(3, self.settings.get("writer", {}).get("timeline_min_events", 3)):
            items.append(
                self.item(f"{self.signal.created_at[:10]} — Signal recorded by the sensor.", [self.signal.rid])
            )
        return sorted(items, key=lambda item: item.text[:10])

    def numbers(self) -> list[TextItem]:
        out: list[TextItem] = []
        pre = [
            f
            for f in self.facts
            if _period_index(self.s.get("break_period")) is not None
            and _period_index(f.payload["period_end"]) < _period_index(self.s["break_period"])
        ]
        for label, fact in (
            ("Reported pre-break level", pre[-1] if pre else None),
            ("Reported latest level", self.facts[-1] if self.facts else None),
        ):
            if fact:
                out.append(
                    self.item(
                        f"{label}: {format_value(fact.payload['value'], fact.payload.get('unit', self.unit))} "
                        f"({period_label(P.from_index(_period_index(fact.payload['period_end'])))}).",
                        [fact.rid],
                    )
                )
        out.append(
            self.item(
                f"Gap size: {format_value(self.s.get('size'), self.unit)}.", self.signal_cite("size", unit=self.unit)
            )
        )
        out.append(self.item(f"Robust z: {format_value(self.s.get('z'), 'z')}.", self.signal_cite("z", unit="z")))
        p = self.s.get("placebo") or {}
        text = (
            f"Raw placebo p: {format_value(p.get('p_raw'))}; {p['n']} comparisons."
            if p.get("n") is not None
            else "Raw placebo test unavailable."
        )
        out.append(self.item(text, [*self.signal_cite("placebo.p_raw"), *self.signal_cite("placebo.n", unit="count")]))
        n = self.s.get("persistence_q")
        out.append(
            self.item(
                f"Persistence: {n} quarters." if n is not None else "Persistence unavailable.",
                self.signal_cite("persistence_q", unit="count"),
            )
        )
        return out

    def counterargument(self) -> TextItem:
        note = self.corporate_note()
        prefix = f"The leading alternative explanation is a corporate action: {note}. " if note else ""
        refs = _unique([self.signal.rid if self.signal else None, self.claim.rid, self.s.get("corp_action_rid")])
        substantive = sorted(
            (
                c
                for c in self.challenges
                if c.payload["alt_type"] != "stub" and c.payload["argument"].strip().upper() != "STUB"
            ),
            key=lambda c: (c.payload["severity"] != "substantive", c.seq),
        )
        if self.court_stub:
            alternatives = self.c.get("alt_checklist") or [a["id"] for a in load_registry("alternatives.yaml")]
            text = (
                "The court ran as a stub in this run: no evidence was argued. "
                "Alternatives still to test: " + ", ".join(a.replace("_", " ") for a in alternatives) + "."
            )
        elif substantive:
            challenge = substantive[0]
            refs.extend(env.rid for env in self.ledger.closure(challenge.rid))
            text = f"Challenge ({challenge.payload['alt_type'].replace('_', ' ')}): {challenge.payload['argument']} "
            replies = self.ledger.query("REBUTTAL", challenge_rid=challenge.rid)
            if replies:
                results = [(reply, admissible(self.ledger, reply.payload)) for reply in replies]
                reply, (ok, reason) = next(((r, result) for r, result in results if result[0]), results[0])
                refs.extend(env.rid for env in self.ledger.closure(reply.rid))
                text += (
                    f"Rebuttal: {reply.payload['argument']} "
                    f"Under the judge's rules this is {'admissible' if ok else 'inadmissible'}: {reason}"
                )
            else:
                text += "No rebuttal was recorded; the challenge remains unanswered."
        else:
            text = "No substantive challenge or rebuttal was recorded; alternative explanations remain untested."
        return self.item(prefix + text, refs, ground=True)

    def falsification(self) -> TextItem:
        if self.prereg_stub:
            return self.item(
                "The hypothesis and kill test were not pre-registered in this run; no sealed "
                "falsification rule can be claimed for this signal.",
                [self.claim.rid],
            )
        fals = self.c["falsification"]
        metric = fals["metric"]
        unit = _metrics().get(metric, {}).get("unit", self.unit)
        refs = [self.claim.rid]
        existing = [
            env
            for env in self.ledger.closure(self.claim.rid)
            if env.type is RecordType.CALC
            and env.payload.get("value") == fals["threshold"]
            and env.payload.get("unit") == unit
        ]
        refs.append(
            existing[-1].rid
            if existing
            else self.project(fals["threshold"], [self.claim.rid], "falsification.threshold", unit=unit)
        )
        comparator = {">": "above", ">=": "at least", "<": "below", "<=": "at most", "==": "equal to"}.get(
            fals["comparator"], fals["comparator"]
        )
        text = (
            f"This finding is falsified if {metric_label(metric)} is {comparator} "
            f"{format_value(fals['threshold'], unit)} in the next two results filings "
            f"(by {period_label(fals['deadline_period'])})."
        )
        head = self.ledger.heads().get("prereg_head")
        sealed = next(
            (env for env in self.ledger.iter_records() if head and env.seq == head[0] and env.hash == head[1]),
            None,
        )
        if head and sealed and self.claim.seq <= head[0]:
            prefix = head[1][:12]
            refs.extend(self.metadata_cites(sealed, "hash", text=prefix, location="envelope_first_twelve"))
            text += f" Pre-registration seal {prefix}: sealed before any evidence was searched."
        else:
            text += " No pre-registration seal was found; the search ordering cannot be established."
        return self.item(text, refs)

    def strength(self) -> TextItem:
        if not self.verdict:
            return self.item("No code verdict was recorded. No model was asked how confident it is.", [self.claim.rid])
        refs = [self.verdict.rid]
        parts = [f"Evidence strength: {self.v['grade']}."]
        for name, check in self.v.get("checks", {}).items():
            for field in ("value", "need"):
                refs.append(self.project(check.get(field), [self.verdict.rid], f"checks.{name}.{field}"))

            def shown(value):
                if value is None:
                    return "unavailable"
                if isinstance(value, bool):
                    return "yes" if value else "no"
                return format_value(value) if isinstance(value, (float, int)) else str(value)

            parts.append(
                f"{name}: value {shown(check.get('value'))}; need {shown(check.get('need'))}; "
                f"{'pass' if check.get('pass') else 'fail'}."
            )
        for key, value in self.v.get("stat", {}).items():
            refs.append(self.project(value, [self.verdict.rid], f"stat.{key}"))
        count = sum(ch.payload["severity"] == "substantive" for ch in self.challenges)
        refs.append(
            self.project(
                count,
                [ch.rid for ch in self.challenges] or [self.claim.rid],
                "CHALLENGE.severity",
                operator="count",
                unit="count",
                detail={"equals": "substantive", "claim": self.claim.rid},
            )
        )
        parts.extend(
            reason if reason.strip().upper() != "STUB" else "The judge ran as a stub."
            for reason in self.v.get("reasons", [])
        )
        parts.append("No model was asked how confident it is.")
        return self.item(" ".join(parts), refs, ground=True)


def write_finding(ledger: Ledger, claim_rid: str, *, panel, settings: dict, impl: dict[str, str]) -> FindingPayload:
    """Build the eight elements, appending only explicitly sourced CALC projections.

    Use ``append_finding`` (or run the gate yourself) before adding the FINDING.
    A renderer reopening an old run should read its existing FINDINGs instead.
    """
    w = _Writer(ledger, claim_rid, panel, settings, impl)
    return FindingPayload(
        claim_rid=claim_rid,
        elements=FindingElements(
            discovery=w.discovery(),
            why=w.why(),
            evidence=w.evidence_items(),
            timeline=w.timeline(),
            numbers=w.numbers(),
            counterargument=w.counterargument(),
            falsification=w.falsification(),
            strength=w.strength(),
        ),
        footer={
            "run_id": ledger.run_id,
            "impl": dict(impl),
            "prereg_head": ledger.heads().get("prereg_head", (0, None))[1],
            "prereg_stub": w.prereg_stub,
            "court_stub": w.court_stub,
            "numeric_provenance": "SIGNAL/CLAIM/VERDICT fields and source counts use report_projection CALCs; "
            "projections retain source references and do not establish independent reconciliation.",
        },
        writer="template",
    )


def _simplify(ledger: Ledger, finding: FindingPayload) -> FindingPayload:
    payload = finding.model_dump()

    def walk(value):
        if isinstance(value, dict):
            if "text" in value and "rids" in value:
                value["text"] = _ground_text(ledger, value["text"], value["rids"])
            else:
                for child in value.values():
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload["elements"])
    return FindingPayload.model_validate(payload)


def append_finding(ledger: Ledger, claim_rid: str, *, panel, settings: dict, impl: dict[str, str]) -> str | None:
    """Gate, record rejection, simplify once, and append only a passing FINDING."""
    finding = write_finding(ledger, claim_rid, panel=panel, settings=settings, impl=impl)
    claim = ledger.get(claim_rid)
    verdicts = ledger.query("VERDICT", claim_rid=claim_rid)
    parents = _unique([claim_rid, verdicts[-1].rid if verdicts else None, claim.payload.get("signal_rid")])
    for attempt in range(2):
        payload = finding.model_dump(mode="json")
        rejection = check_finding_text(ledger, payload)
        if rejection is None:
            return ledger.append("FINDING", finding, Author.code("writer"), parents).rid
        write_reject(ledger, payload, "FINDING", rejection, parents=tuple(parents))
        if attempt == 0:
            finding = _simplify(ledger, finding)
    return None
