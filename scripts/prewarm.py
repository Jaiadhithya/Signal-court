"""Pre-warm the local workspace for live runs: universe, sub-types, peers and filings.

Run on the laptop, on home Wi-Fi (NSE blocks cloud IPs), from the repo root:
    uv run python scripts/prewarm.py --targets-file fixtures/targets.csv --tiers T1,T2

Steps:
  1. Refresh the universe (equity master + index CSVs) into <home>/universe.sqlite.
  2. Classify every financial symbol from its newest results XBRL (downloaded into the store) and
     mark its sub-type in the universe.
  3. Pick peers per target: same sub-type, the largest ``--max-peers`` by latest total income.
  4. Harvest T1 (results XBRL, 20 quarters) for targets, then for peers; then T2 for targets.
  5. Write <home>/prewarm_report.csv.

Company names come only from the targets file. Re-runs are incremental (the store skips known URLs)
and an interrupted run is safe to start again. ``--dry-run`` prints the plan and fetches no filings.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date
from pathlib import Path

from signalcourt.harvest import filings as Fl
from signalcourt.harvest import tiers as T
from signalcourt.net.http import PoliteClient
from signalcourt.net.nse import NSE
from signalcourt.resolve import universe as U
from signalcourt.resolve.entity import legal_names
from signalcourt.settings import Env, Workspace, load_settings
from signalcourt.stages import common as C
from signalcourt.store import DocStore

REPORT_COLUMNS = (
    "symbol", "role", "subtype", "subtype_rule", "xbrl_file", "xbrl_quarters", "decks", "transcripts",
    "peers_found", "errors",
)
T1_CAP_S = 900.0  # per-entity cap for one T1 harvest (20 quarters, two bases)
T2_CAP_S = 600.0  # per-target cap for T2 (announcements, decks, transcripts, annual reports)


def read_targets(path: Path) -> list[tuple[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return [(r["symbol"].strip().upper(), r["role"].strip()) for r in csv.DictReader(f) if r.get("symbol")]


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def legacy_rows(exch: NSE, symbol: str) -> tuple[str, list[dict]]:
    """Insurers list under the legacy "insurance" index; everyone else under "equities"."""
    for idx in ("equities", "insurance"):
        rows = exch.results_legacy(symbol, index=idx)
        if rows:
            return idx, rows
    return "equities", []


def insurance_type(rows: list[dict]) -> str:
    for r in rows[:3]:
        for k, v in r.items():
            if "insurance" in k.lower() and isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def issuer_candidates(name: str, rows: list[dict]) -> list[str]:
    names = [name] + [str(r.get("companyName", "")).strip() for r in rows[:3]]
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


class Prewarm:
    def __init__(self, ws: Workspace, as_of: date, max_peers: int, dry_run: bool) -> None:
        self.ws, self.as_of, self.max_peers, self.dry = ws, as_of, max_peers, dry_run
        self.con = U.connect(ws.universe_db)
        self.store = DocStore(ws.cache)
        self.client = PoliteClient(Env().http_user_agent, default_rps=1.0)
        self.exch = NSE(self.client)
        self.issuer: dict[str, str] = {}
        self.legacy_index: dict[str, str] = {}
        self.ins_type: dict[str, str] = {}
        self.subtype: dict[str, C.StoreSubtype] = {}
        self.size: dict[str, float] = {}
        self.errors: dict[str, list[str]] = {}

    def close(self) -> None:
        self.client.close()
        self.store.close()
        self.con.close()

    def err(self, symbol: str, msg: str) -> None:
        self.errors.setdefault(symbol, []).append(msg)

    def name_of(self, symbol: str) -> str | None:
        row = self.con.execute("SELECT name FROM entities WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    def isin_of(self, symbol: str) -> str | None:
        row = self.con.execute("SELECT isin FROM entities WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    # -- step 1 ------------------------------------------------------------------------
    def refresh(self) -> None:
        n = U.refresh_universe(self.exch, self.con)
        log(f"universe: {n} entities, {len(U.financial_symbols(self.con))} financial")

    # -- step 2 ------------------------------------------------------------------------
    def listing(self, symbol: str) -> list[Fl.ResultFiling]:
        """Legacy + integrated listing; finds the issuer string the integrated endpoint accepts."""
        idx, rows = legacy_rows(self.exch, symbol)
        self.legacy_index[symbol], self.ins_type[symbol] = idx, insurance_type(rows)
        legacy = [Fl.filing_from_row(symbol, r, "legacy") for r in rows]
        name = self.name_of(symbol) or symbol
        integrated: list[Fl.ResultFiling] = []
        for cand in issuer_candidates(name, rows):
            integrated = [Fl.filing_from_row(symbol, r, "integrated") for r in self.exch.results_integrated(symbol, cand)]
            if integrated:
                self.issuer[symbol] = cand
                break
        self.issuer.setdefault(symbol, name)
        allf = [f for f in legacy + integrated if f.xbrl_url and f.period_end]
        allf.sort(key=lambda f: (f.period_end, f.consolidated), reverse=True)  # newest, consolidated first
        return allf

    def classify(self, symbol: str) -> None:
        if not self.store.docs_for(symbol, C.RESULTS):
            if self.dry:
                return
            try:
                newest = self.listing(symbol)
            except Exception as e:
                self.err(symbol, f"listing: {type(e).__name__}: {e}")
                return
            if not newest:
                self.err(symbol, "no results XBRL listed")
                return
            f = newest[0]
            rep = T.HarvestReport()
            T._download(
                self.exch, self.store, rep, f.xbrl_url, doc_type=C.RESULTS, kind="results_xbrl", symbol=symbol,
                period_end=f.period_end, published=f.broadcast, mime="application/xml",
            )
            for e in rep.errors:
                self.err(symbol, e)
        st = C.subtype_from_store(self.store, symbol, self.ins_type.get(symbol, ""))
        if st is None:
            return
        self.subtype[symbol] = st
        U.mark_financial(self.con, symbol, st.decision.subtype)
        size = C.latest_size(self.store, symbol, st.decision.subtype)
        if size:
            self.size[symbol] = size

    # -- step 3 ------------------------------------------------------------------------
    def peers_of(self, target: str) -> list[str]:
        st = self.subtype.get(target)
        if st is None:
            return []
        same = [s for s, x in self.subtype.items() if x.decision.subtype == st.decision.subtype and s != target]
        same.sort(key=lambda s: (-self.size.get(s, 0.0), s))
        return same[: self.max_peers]

    # -- step 4 ------------------------------------------------------------------------
    def entity_ref(self, symbol: str) -> T.EntityRef:
        if symbol not in self.issuer:  # classified from the store on an earlier run: list again for the issuer
            self.listing(symbol)
        st = self.subtype[symbol].decision.subtype
        if self.legacy_index.get(symbol) == "insurance" and not st.startswith("insurer"):
            st = "insurer_life"  # only steers harvest to the insurance legacy index
        return T.EntityRef(symbol, self.issuer[symbol], st, self.isin_of(symbol), legal_names(self.con, symbol))

    def harvest(self, symbol: str, tiers: set[str], cap_s: float, pdfs: bool, label: str) -> None:
        t0 = time.monotonic()
        try:
            ent = self.entity_ref(symbol)
            rep = T.harvest(ent, self.as_of, tiers, cap_s, "warm", exch=self.exch, store=self.store, pdfs=pdfs)
        except Exception as e:
            self.err(symbol, f"harvest {','.join(sorted(tiers))}: {type(e).__name__}: {e}")
            log(f"{label} {symbol}: FAILED {type(e).__name__}: {e}")
            return
        for e in rep.errors:
            self.err(symbol, e)
        log(
            f"{label} {symbol} {','.join(sorted(tiers))}: new {rep.new_files}, known {rep.known_files}, "
            f"errors {len(rep.errors)}, xbrl quarters {C.xbrl_quarters(self.store, symbol, self.as_of)} "
            f"({time.monotonic() - t0:.0f}s)"
        )

    # -- step 5 ------------------------------------------------------------------------
    def report_rows(self, targets: list[tuple[str, str]], peers: dict[str, list[str]]) -> list[dict]:
        rows = []
        listed = {s for s, _ in targets}
        roles = list(targets) + sorted({(p, "peer") for ps in peers.values() for p in ps if p not in listed})
        for sym, role in roles:
            st = self.subtype.get(sym)
            rows.append(
                {
                    "symbol": sym,
                    "role": role,
                    "subtype": st.decision.subtype if st else "",
                    "subtype_rule": st.decision.rule if st else "",
                    "xbrl_file": st.xbrl_file if st else "",
                    "xbrl_quarters": C.xbrl_quarters(self.store, sym, self.as_of),
                    "decks": len(self.store.docs_for(sym, "deck")),
                    "transcripts": len(self.store.docs_for(sym, "transcript")),
                    "peers_found": (
                        sum(1 for p in peers.get(sym, []) if C.xbrl_quarters(self.store, p, self.as_of))
                        if role != "peer" else ""
                    ),
                    "errors": "; ".join(dict.fromkeys(self.errors.get(sym, [])))[:500],
                }
            )
        return rows

    def write_report(self, targets: list[tuple[str, str]], peers: dict[str, list[str]]) -> Path:
        path = self.ws.home / "prewarm_report.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
            w.writeheader()
            w.writerows(self.report_rows(targets, peers))
        return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets-file", type=Path, required=True)
    ap.add_argument("--tiers", default="T1,T2")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to today")
    ap.add_argument("--max-peers", type=int, default=None, help="defaults to [panel] max_peers")
    ap.add_argument("--home", type=Path, default=None, help="defaults to $SIGNAL_HOME")
    ap.add_argument("--skip-refresh", action="store_true", help="reuse the universe already on disk")
    ap.add_argument("--dry-run", action="store_true", help="print what would be fetched; download no filings")
    a = ap.parse_args(argv)

    settings = load_settings()
    ws = Workspace(home=(a.home or settings.workspace.home).resolve())
    ws.ensure()
    as_of = date.fromisoformat(a.as_of) if a.as_of else date.today()
    tiers = {t.strip().upper() for t in a.tiers.split(",") if t.strip()}
    max_peers = a.max_peers or settings.config.panel.max_peers
    targets = read_targets(a.targets_file)
    pw = Prewarm(ws, as_of, max_peers, a.dry_run)
    peers: dict[str, list[str]] = {}
    try:
        if not (a.skip_refresh or (a.dry_run and U.financial_symbols(pw.con))):
            pw.refresh()
        for sym, _ in targets:
            if pw.name_of(sym) is None:
                pw.err(sym, "not in the universe")
        fin = sorted(set(U.financial_symbols(pw.con)) | {s for s, _ in targets if pw.name_of(s)})
        log(f"classifying {len(fin)} symbols")
        for i, sym in enumerate(fin, 1):
            pw.classify(sym)
            st = pw.subtype.get(sym)
            log(f"[{i}/{len(fin)}] {sym}: {st.decision.subtype + ' ' + st.decision.rule if st else 'unclassified'}")
        for sym, _ in targets:
            peers[sym] = pw.peers_of(sym)
        all_peers = sorted({p for ps in peers.values() for p in ps} - {s for s, _ in targets})
        if a.dry_run:
            for sym, role in targets:
                st = pw.subtype.get(sym)
                log(f"{sym} ({role}) {st.decision.subtype if st else '?'}: {len(peers[sym])} peers: "
                    f"{' '.join(peers[sym])}")
            log(f"would fetch T1 for {len(targets)} targets and {len(all_peers)} peers, "
                f"{','.join(sorted(tiers - {'T1'})) or 'nothing'} for targets")
            return 0
        if "T1" in tiers:
            for i, sym in enumerate([s for s, _ in targets if s in pw.subtype], 1):
                pw.harvest(sym, {"T1"}, T1_CAP_S, True, f"[target {i}/{len(targets)}]")
                pw.write_report(targets, peers)
            for i, sym in enumerate(all_peers, 1):
                pw.harvest(sym, {"T1"}, T1_CAP_S, False, f"[peer {i}/{len(all_peers)}]")
                if i % 5 == 0:
                    pw.write_report(targets, peers)
        later = tiers - {"T1"}
        if later:
            for i, sym in enumerate([s for s, _ in targets if s in pw.subtype], 1):
                pw.harvest(sym, later, T2_CAP_S, True, f"[target {','.join(sorted(later))} {i}/{len(targets)}]")
                pw.write_report(targets, peers)
    except KeyboardInterrupt:
        log("interrupted; writing the report so far (re-run to continue)")
        return 130
    finally:
        if not a.dry_run:
            log(f"report: {pw.write_report(targets, peers)}")
        pw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
