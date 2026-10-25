"""Formatting, safe destinations and click-through provenance for ledger records."""

from __future__ import annotations

import math
import re
from datetime import date
from html import escape
from typing import Any
from urllib.parse import urlsplit

from markupsafe import Markup

from ..calc import periods as P

LABELS = {
    "gnpa_amt": "Gross non-performing assets",
    "nnpa_amt": "Net non-performing assets",
    "gnpa_pct": "Gross NPA ratio",
    "nnpa_pct": "Net NPA ratio",
    "pat": "Profit after tax",
    "pat_owners": "Profit attributable to owners",
    "opex": "Operating expenses",
    "eps_basic": "Basic earnings per share",
    "cet1": "Common equity tier one ratio",
    "roa": "Return on assets",
    "persistency_13m": "Policy persistency",
    "persistency_25m": "Policy persistency",
    "persistency_61m": "Policy persistency",
    "pbt": "Profit before tax",
}


def label(value: str) -> str:
    return LABELS.get(value, value.replace("_", " ").capitalize())


def period(value: Any) -> str:
    if value is None:
        return "Date unavailable"
    try:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            value = date.fromisoformat(value)
        value = P.from_index(int(value)) if isinstance(value, (int, float)) else P.from_index(P.index(value))
        return re.sub(r"(Q[1-4])FY", r"\1 FY", value)
    except (ValueError, TypeError, KeyError, AttributeError):
        return str(value or "Date unavailable")


def day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value or "Date unavailable")


def number(value: Any, unit: str = "", decimals: int | None = None) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    try:
        n = float(value)
    except (ValueError, TypeError):
        return str(value)
    if not math.isfinite(n):
        return "Unavailable"
    if unit == "fraction":
        return f"{n * 100:.1f}%"
    if unit == "inr_per_share":
        return f"Rs {n:,.2f}"
    dp = decimals if decimals is not None else (1 if unit == "inr_cr" else 3)
    if unit == "count":
        return str(int(n))
    if not unit and decimals is None and n.is_integer():
        return str(int(n))
    raw = f"{abs(n):.{dp}f}"
    whole, _, frac = raw.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while head:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(parts + [tail])
    out = ("-" if n < 0 else "") + whole + ("." + frac if frac else "")
    return out + (" cr" if unit == "inr_cr" else " pp" if unit == "pp" else "")


def safe_url(value: Any, *, local: bool = False) -> str | None:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return None
    return value if scheme in (("http", "https", "file") if local else ("http", "https")) else None


class RecordLinks:
    """Resolve numeric links and source documents from a saved snapshot."""

    def __init__(self, records, store=None):
        from ..settings import load_registry

        self.records = list(records)
        self.by_rid = {r.rid: r for r in self.records}
        self.docs = {r.payload["sha256"]: r for r in self.records if r.type.value == "DOC"}
        self.store = store
        self.units = {m["id"]: m.get("unit", "") for m in load_registry("metrics.yaml")}
        self.as_of = next((r.payload["as_of"] for r in self.records if r.type.value == "RUN"), "")

    def eligible(self, doc) -> bool:
        if doc is None:
            return False
        p = doc.payload
        published = p.get("publish_date")
        return p.get("time_gate") == "ok" and bool(published and published <= self.as_of)

    def source(self, env, seen=None):
        if env is None:
            return None
        seen = set() if seen is None else seen
        if env.rid in seen:
            return None
        seen.add(env.rid)
        p = env.payload
        if env.type.value == "DOC":
            return env
        if env.type.value == "SPAN":
            return self.docs.get(p.get("doc_sha256"))
        for rid in [p.get("span_rid"), *p.get("inputs", []), *env.parents]:
            if rid in self.by_rid:
                doc = self.source(self.by_rid[rid], seen)
                if doc:
                    return doc
        return None

    def description(self, env) -> str:
        p, t = env.payload, env.type.value
        doc = self.source(env)
        if doc is not None and not self.eligible(doc):
            return "Excluded from research: source did not pass the report's as-of cutoff."
        if t == "FACT":
            return f"{label(p['metric'])} · {period(p['period_end'])} · {number(p['value'], p['unit'])}"
        if t == "CALC":
            return f"{p['formula_text']} = {number(p.get('value'), p.get('unit', ''))}"
        if t == "SIGNAL":
            metric = (p.get("metrics") or ["signal"])[0]
            placebo = p.get("placebo") or {}
            return (
                f"{label(metric)} · {period(p.get('break_period'))}. "
                f"Gap: {number(p.get('size'), self.units.get(metric, ''))}; "
                f"robust deviation: {number(p.get('z'), decimals=2)}; "
                f"placebo rank {placebo.get('rank', 'unavailable')} of {placebo.get('n', 'unavailable')}, "
                f"raw p {number(placebo.get('p_raw'))}; "
                f"persistence {number(p.get('persistence_q'), 'count')} quarters. "
                "Gap = mean post-break(target minus baseline) minus median pre-break(target minus baseline); "
                "robust deviation = gap divided by the pre-break robust spread."
            )
        if t == "VERDICT":
            checks = "; ".join(
                f"{name}: observed {check.get('value')}, required {check.get('need')}, "
                f"{'pass' if check.get('pass') else 'fail'}"
                for name, check in p.get("checks", {}).items()
            )
            return f"{p['grade']}. {checks}"
        if t == "CLAIM":
            fals = p["falsification"]
            if p["statement"].upper() == "STUB" or fals.get("comparator") == "none":
                return "Hypothesis and kill test were not pre-registered in this run."
            return (
                f"{p['statement']} Falsified if {label(fals['metric'])} {fals['comparator']} "
                f"{number(fals['threshold'], self.units.get(fals['metric'], ''))} by "
                f"{period(fals['deadline_period'])}. Source rule: {fals['source_rule']}."
            )
        return str(p.get("statement") or p.get("sentence") or p.get("reason") or p.get("doc_type") or t)

    def tooltip(self, rid: str) -> str:
        env = self.by_rid.get(rid)
        if env is None:
            return "Record unavailable"
        text = f"{rid} · {env.type.value}\n{self.description(env)}"
        if env.type.value == "CALC":
            text += "\nInputs: " + ", ".join(env.payload.get("inputs", []))
        doc = self.source(env)
        if doc and self.eligible(doc):
            text += "\nSource: " + str(doc.payload.get("final_url") or doc.payload.get("url"))
        return text.replace("STUB", "Not implemented in this run")

    def link(self, text: Any, rid: str | None, *, css: str = "") -> Markup:
        value = escape(str(text))
        if rid not in self.by_rid:
            return Markup(value)
        return Markup(
            f'<a class="record-link {escape(css)}" href="#{rid}" data-record="{rid}" '
            f'title="{escape(self.tooltip(rid), quote=True)}">{value}</a>'
        )

    def text(self, item: dict | None) -> Markup:
        from ..gates.unbacked import CitedValue, find_unbacked, number_tokens

        item = item or {}
        text = str(item.get("text", "")).replace("STUB", "Not implemented in this run")
        rids = [r for r in item.get("rids", []) if r in self.by_rid]
        pieces, cursor = [], 0
        for token, _shown, _unit, _dp, pos in number_tokens(text):
            end = pos + len(token)
            target = None
            for rid in rids:
                env = self.by_rid[rid]
                if env.type.value in ("FACT", "CALC") and env.payload.get("value") is not None:
                    cv = CitedValue(rid, env.payload["value"], env.payload.get("unit", "same"))
                    if not find_unbacked(token, [cv]):
                        target = rid
                        break
            target = target or next((r for r in rids if self.by_rid[r].type.value == "SIGNAL"), None)
            target = target or (rids[0] if rids else None)
            pieces += [escape(text[cursor:pos]), str(self.link(text[pos:end], target))]
            cursor = end
        pieces.append(escape(text[cursor:]))
        citations = " ".join(str(self.link(rid, rid, css="citation")) for rid in rids[:4])
        return Markup("".join(pieces) + (f'<span class="citations">{citations}</span>' if citations else ""))

    def doc_view(self, env) -> dict:
        p, local = env.payload, None
        eligible = self.eligible(env)
        if self.store is not None and eligible:
            try:
                path = self.store.blobs.find(p["sha256"])
                local = path.resolve().as_uri() if path else None
            except (OSError, KeyError, AttributeError):
                pass
        return {
            "rid": env.rid,
            "kind": label(p["doc_type"]),
            "family": label(p["source_family"]),
            "period": "Multiple reporting periods" if p["doc_type"] == "peer_panel" else period(p.get("period_end")),
            "published": day(p.get("publish_date")),
            "source": (safe_url(p.get("final_url")) or safe_url(p.get("url"))) if eligible else None,
            "local": local,
            "sha": p["sha256"],
        }
