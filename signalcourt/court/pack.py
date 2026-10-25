"""Context pack: the only thing a court agent sees.

A pack holds the claim, the signal behind it, the CALC records computed for it and the passages of the run's
target documents that best match the claim's metric. It is entity-blind: every legal name, short name and
symbol of the target becomes "the company" in the text an agent reads, while each passage keeps its pid so the
quote gate can still check the original wording.

Agents never see ledger rids, which change from run to run. Each CALC gets a stable alias (C1, C2, ...), the
signal is S1, and evidence admitted later in the round becomes E1, E2, ...; the round maps aliases back to rids.
That keeps the prompt identical between a warm run and its replay, so the replay finds every reply in the cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..ledger import Ledger, RecordType
from ..ledger.canonical import sha256_hex
from ..prereg.template import BASELINES, metric_label
from ..settings import load_registry

BLIND = "the company"
PACK_DOC_TYPES = frozenset(
    {"deck", "transcript", "rating_letter", "press_release", "results_pdf", "annual_report", "scheme"}
)
DIRECTION_WORDS = {
    "up": ["increase", "increased", "rise", "rose", "growth", "grew", "higher", "improved"],
    "down": ["decrease", "decreased", "decline", "declined", "fall", "fell", "lower", "reduced"],
}
MAX_PASSAGE_WORDS = 160  # longer passages are skipped: they spend the token budget on one block of text
LEGAL_SUFFIXES = re.compile(r"\s+(limited|ltd\.?|ltd)$", re.I)
MIN_WORD_SHARE = 0.6  # a "prose" passage that is mostly figures is a table remnant: no sentence to quote
_WORD = re.compile(r"[a-z]{3,}")


@lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text))


@lru_cache(maxsize=1)
def _metric_rows() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in load_registry("metrics.yaml")}


def entity_names(ledger: Ledger, symbol: str | None) -> list[str]:
    """Every name the target goes by in its ENTITY record: legal names, the same without "Limited", the symbol."""
    names: set[str] = set()
    for env in ledger.query(RecordType.ENTITY):
        p = env.payload
        if symbol is not None and p["symbol"] != symbol:
            continue
        names.add(p["symbol"])
        for n in p.get("legal_names", []):
            n = n.strip()
            if n:
                names.add(n)
                names.add(LEGAL_SUFFIXES.sub("", n).strip())
    return sorted((n for n in names if len(n) >= 2), key=lambda n: (-len(n), n))


def blind_pattern(names: list[str]) -> re.Pattern | None:
    if not names:
        return None
    alts = "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names)
    return re.compile(rf"(?<![\w&])(?:{alts})(?![\w&])", re.I)


def blind(text: str, pattern: re.Pattern | None) -> str:
    return pattern.sub(BLIND, text) if pattern is not None else text


@dataclass
class Pack:
    claim_rid: str
    signal_rid: str
    claim: dict[str, Any]
    signal: dict[str, Any]
    calcs: list[dict[str, Any]]
    passages: list[dict[str, Any]]
    pack_hash: str
    names: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> rid (S1, C1, ...)

    def view(self) -> dict[str, Any]:
        """The rid-free content an agent reads."""
        return {
            "claim": self.claim,
            "signal": self.signal,
            "calcs": [{k: v for k, v in c.items() if k != "rid"} for c in self.calcs],
            "passages": self.passages,
        }

    def render(self) -> str:
        return json.dumps(self.view(), indent=1, ensure_ascii=False, sort_keys=False)


def _num(x: Any) -> Any:
    """Floats rounded to 4 significant digits for display; the unbacked-numbers gate allows +-1 in the last digit."""
    if isinstance(x, float):
        return float(f"{x:.4g}")
    return x


def _claim_view(claim: dict[str, Any], pattern: re.Pattern | None) -> dict[str, Any]:
    f = claim["falsification"]
    return {
        "statement": blind(claim["statement"], pattern),
        "mechanism": blind(claim["mechanism"], pattern),
        "kill_test": {
            "metric": f["metric"],
            "comparator": f["comparator"],
            "threshold": _num(float(f["threshold"])),
            "deadline_period": f["deadline_period"],
        },
        "alternatives_to_check": list(claim.get("alt_checklist", [])),
    }


def _signal_view(sig: dict[str, Any], pattern: re.Pattern | None) -> dict[str, Any]:
    placebo = sig.get("placebo") or {}
    status = (sig.get("surprise") or {}).get("coverage_status")
    return {
        "id": "S1",
        "metric": sig["metrics"][0],
        "metric_label": metric_label(sig["metrics"][0]),
        "direction": sig["direction"],
        "break_period": sig.get("break_period"),
        "baseline": BASELINES.get(sig.get("specificity_source"), BASELINES["na"]),
        "gap_z": _num(sig.get("z")),
        "gap_size": _num(sig.get("size")),
        "quarters_held": sig.get("persistence_q"),
        "placebo_p": _num(placebo.get("p_raw")),
        "placebo_n": placebo.get("n"),
        "note": blind(status, pattern) if status else None,
    }


def _calcs(ledger: Ledger, signal_rid: str) -> list[dict[str, Any]]:
    """CALC records computed on this claim's signal (the kill threshold and the code tests), in ledger order."""
    out = []
    for env in ledger.query(RecordType.CALC):
        p = env.payload
        if signal_rid not in p.get("inputs", []):
            continue
        out.append(
            {
                "id": f"C{len(out) + 1}",
                "rid": env.rid,
                "formula": p["formula_id"],
                "meaning": p["formula_text"],
                "value": _num(p["value"]) if p.get("value") is not None else None,
                "unit": p["unit"],
                # only the value is a citable figure (unbacked-numbers gate); numeric params would invite copying
                "detail": {k: v for k, v in sorted((p.get("params") or {}).items()) if isinstance(v, str)},
            }
        )
    return out


def _query_terms(metric: str, direction: str) -> str:
    row = _metric_rows().get(metric, {})
    terms = [metric_label(metric), metric.replace("_", " "), *row.get("synonyms", [])]
    return " ".join([*terms, *DIRECTION_WORDS.get(direction, [])])


def _candidate_docs(ledger: Ledger, doc_shas: list[str]) -> list[dict[str, Any]]:
    wanted = set(doc_shas)
    docs, seen = [], set()
    for env in ledger.query(RecordType.DOC):
        p = env.payload
        if p["time_gate"] != "ok" or p["doc_type"] not in PACK_DOC_TYPES or p["sha256"] not in wanted:
            continue
        if p["sha256"] in seen:
            continue
        seen.add(p["sha256"])
        docs.append({"rid": env.rid, **p})
    return docs


def _passages(store, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for d in docs:
        try:
            pages = store.text.n_pages(d["sha256"]) or 0
        except FileNotFoundError:
            continue
        for page in range(1, pages + 1):
            try:
                ps = store.passages(d["sha256"], page)
            except FileNotFoundError:
                continue
            for p in ps:
                if p.kind != "prose" or p.norm in seen:
                    continue
                n_words = len(p.norm.split())
                if n_words < 6 or n_words > MAX_PASSAGE_WORDS:
                    continue
                if sum(1 for w in p.norm.split() if _WORD.search(w)) < MIN_WORD_SHARE * n_words:
                    continue
                seen.add(p.norm)
                out.append(
                    {
                        "pid": p.pid,
                        "raw": p.text,
                        "norm": p.norm,
                        "source_family": d["source_family"],
                        "doc_type": d["doc_type"],
                        "publish_date": d.get("publish_date"),
                    }
                )
    return out


def _rank(passages: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    if not passages:
        return []
    import bm25s

    corpus = bm25s.tokenize([p["norm"] for p in passages], stopwords="en", show_progress=False)
    retriever = bm25s.BM25()
    retriever.index(corpus, show_progress=False)
    q = bm25s.tokenize([query.lower()], stopwords="en", show_progress=False)
    k = min(len(passages), 200)
    idx, scores = retriever.retrieve(q, k=k, show_progress=False)
    ranked = [(float(s), int(i)) for i, s in zip(idx[0], scores[0], strict=True) if float(s) > 0]
    ranked.sort(key=lambda t: (-t[0], passages[t[1]]["pid"]))  # ties broken by pid: the same order every run
    return [passages[i] for _, i in ranked]


def build_pack(ledger: Ledger, store, claim_rid: str, doc_shas: list[str], max_tokens: int) -> Pack:
    claim = ledger.get(claim_rid).payload
    signal_rid = claim["signal_rid"]
    sig = ledger.get(signal_rid).payload
    names = entity_names(ledger, sig.get("entity"))
    pattern = blind_pattern(names)

    claim_view = _claim_view(claim, pattern)
    signal_view = _signal_view(sig, pattern)
    calcs = _calcs(ledger, signal_rid)
    head = json.dumps({"claim": claim_view, "signal": signal_view, "calcs": calcs}, ensure_ascii=False)
    budget = max(0, max_tokens - count_tokens(head))

    chosen: list[dict[str, Any]] = []
    if store is not None:
        ranked = _rank(_passages(store, _candidate_docs(ledger, doc_shas)), _query_terms(
            sig["metrics"][0], sig["direction"]))
        used = 0
        for p in ranked:
            text = blind(p["raw"], pattern)
            entry = {
                "pid": p["pid"],
                "text": text,
                "source_family": p["source_family"],
                "doc_type": p["doc_type"],
                "publish_date": p["publish_date"],
            }
            cost = count_tokens(json.dumps(entry, ensure_ascii=False))
            if used + cost > budget:
                continue
            chosen.append(entry)
            used += cost

    aliases = {"S1": signal_rid, **{c["id"]: c["rid"] for c in calcs}}
    pack = Pack(claim_rid, signal_rid, claim_view, signal_view, calcs, chosen, "", names, aliases)
    pack.pack_hash = sha256_hex(json.dumps(pack.view(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return pack


def restore_quote(quote: str, original: str, names: list[str]) -> str:
    """The original wording of a quote the agent copied from blinded text.

    Each "the company" in the quote may stand for any of the target's names (or be literal), so the quote is
    matched against the original passage with those slots open. Returns the matched original text, or the quote
    unchanged when nothing matches (the quote gate then rejects it).
    """
    parts = re.split(r"the\s+company", quote, flags=re.I)
    if len(parts) == 1:
        return quote
    slot = "(?:" + "|".join([*(re.escape(n).replace(r"\ ", r"\s+") for n in names), r"the\s+company"]) + ")"
    pieces = [r"\s+".join(re.escape(w) for w in part.split()) for part in parts]
    m = re.search((r"\s*" + slot + r"\s*").join(pieces), original, re.I)
    return m.group(0).strip() if m else quote
