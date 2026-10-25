"""Corporate actions: quarters where a merger or a large share issue explains a break better than a discovery would.

Two independent markers, neither read from a company's name:

1. **Scheme filings.** Harvest records one DOC per target document. A DOC of ``doc_type`` "scheme" counts when the
   exchange category of the filing (the manifest ``title``) names a combination of businesses
   (``sensors.corp_action_titles``: amalgamation, merger, arrangement). Stake acquisitions and takeover-code
   disclosures share the scheme doc type but are routine, so they are not markers. Only time-gated (``ok``) DOCs of
   the target count. A merger is filed many times over a year or more (board approval, regulator approvals, court
   order, effective date), so filings closer than ``sensors.corp_action_cluster_gap_days`` form one cluster, and the
   cluster marks the quarter of its **latest** filing: the effective-date intimation comes last, and the combined
   numbers first appear in the quarter it falls in or the next one.
2. **Share-count moves.** ``per_share.read_share_count`` reads the share count as profit / EPS. A move that is
   neither flat nor a bonus or split step is direct data evidence of a merger or a large issue, even when no scheme
   document was harvested. It marks the quarter of the move. A series with more such moves than
   ``sensors.corp_action_max_share_moves`` (a share count that jumps and falls back) does not read as a share count
   at all, so it gives no marker.

A signal whose break is within ``sensors.corp_action_window_q`` quarters of a marker keeps its place in the ledger
but its surprise is multiplied by ``sensors.corp_action_factor``, its ``corp_action_rid`` names the scheme DOC (a
document beats a data-only marker), and its coverage status says which markers applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from ..calc import periods as P
from ..calc.panel import Panel
from ..ledger import RecordType
from ..ledger.schema import SignalPayload
from .per_share import PROFIT_METRICS, per_share_metrics, read_share_count


@dataclass(frozen=True)
class Marker:
    pidx: int  # quarter index of the corporate action
    source: Literal["scheme", "share_count"]
    rid: str | None  # the scheme DOC that marks it; None for a share-count move
    detail: str  # plain words for the coverage status


def _title(store: Any, sha: str) -> str:
    try:
        return store.meta(sha).title or ""
    except KeyError:
        return ""


def scheme_markers(ledger: Any, store: Any, symbol: str | None, sens: dict[str, Any]) -> list[Marker]:
    """One marker per cluster of the target's combination filings, at the quarter of the cluster's latest filing."""
    if store is None or symbol is None:
        return []
    words = [w.lower() for w in sens["corp_action_titles"]]
    hits: list[tuple[date, str, str]] = []
    for env in ledger.query(RecordType.DOC, doc_type="scheme"):
        p = env.payload
        if p.get("time_gate") != "ok" or p.get("entity_symbol") != symbol or not p.get("publish_date"):
            continue
        title = _title(store, p["sha256"])
        if any(w in title.lower() for w in words):
            hits.append((date.fromisoformat(p["publish_date"]), env.rid, title))
    hits.sort()
    clusters: list[list[tuple[date, str, str]]] = []
    for h in hits:
        if clusters and (h[0] - clusters[-1][-1][0]).days <= sens["corp_action_cluster_gap_days"]:
            clusters[-1].append(h)
        else:
            clusters.append([h])
    out = []
    for c in clusters:
        last, rid, title = c[-1]
        n = len(c)
        span = f"{c[0][0].isoformat()} to {last.isoformat()}" if n > 1 else last.isoformat()
        what = f"{n} scheme filing{'' if n == 1 else 's'} ({title}) {span}, latest {rid}"
        out.append(Marker(P.index(last), "scheme", rid, what))
    return out


def share_count_markers(panel: Panel, sens: dict[str, Any]) -> list[Marker]:
    """Unexplained share-count moves of the target, read from its profit and each per-share metric it reports."""
    profit = next((s for s in (panel.series(panel.target, m) for m in PROFIT_METRICS) if not s.dropna().empty), None)
    if profit is None:
        return []
    seen: dict[int, float] = {}
    for metric in sorted(per_share_metrics() & set(panel.metrics(panel.target))):
        _, unexplained = read_share_count(profit, panel.series(panel.target, metric), sens)
        if len(unexplained) > sens["corp_action_max_share_moves"]:
            continue
        for q, r in unexplained:
            seen.setdefault(q, r)
    return [
        Marker(q, "share_count", None, f"share-count move x{r:.2f} at {P.from_index(q)}")
        for q, r in sorted(seen.items())
    ]


def apply_markers(sig: SignalPayload, markers: list[Marker], sens: dict[str, Any]) -> bool:
    """Discount ``sig`` in place when its break is near a marker; True when it was."""
    brk = P.index(sig.break_period)
    near = [m for m in markers if abs(m.pidx - brk) <= sens["corp_action_window_q"]]
    if not near:
        return False
    near.sort(key=lambda m: (m.source != "scheme", abs(m.pidx - brk)))
    sig.corp_action_rid = near[0].rid
    factor = sens["corp_action_factor"]
    sig.surprise.score *= factor
    why = "; ".join(m.detail for m in near)
    note = f"corporate action within {sens['corp_action_window_q']}q of the break ({why}); surprise x{factor:g}"
    sig.surprise.coverage_status = f"{sig.surprise.coverage_status}; {note}" if sig.surprise.coverage_status else note
    return True
