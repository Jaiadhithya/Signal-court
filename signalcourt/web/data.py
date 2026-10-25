"""Read the runtime target list and saved run summaries without sharing ledger objects."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def run_summary(run_dir: Path) -> dict | None:
    try:
        with closing(connect_readonly(run_dir / "ledger.sqlite")) as con:
            row = con.execute(
                "SELECT payload, created_at FROM records WHERE type='RUN' ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run = json.loads(row[0])
            grades = [
                json.loads(r[0])["grade"] for r in con.execute("SELECT payload FROM records WHERE type='VERDICT'")
            ]
            last = con.execute("SELECT created_at FROM records ORDER BY seq DESC LIMIT 1").fetchone()[0]
        meta = read_json(run_dir / "report-meta.json")
        console = meta.get("console", {})
        elapsed = (
            datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.fromisoformat(row[1].replace("Z", "+00:00"))
        ).total_seconds()
        return {
            "id": run_dir.name,
            "company": run["company_input"],
            "symbol": run.get("symbol"),
            "mode": run["mode"],
            "grade": min(grades, key=lambda g: {"Strong": 0, "Moderate": 1, "Weak": 2, "Killed": 3}.get(g, 4))
            if grades
            else None,
            "total_seconds": console.get("total_seconds", max(0, elapsed)),
            "time": run.get("started_at", row[1]),
            "has_report": (run_dir / "report.html").is_file(),
            "status": console.get("status", "done" if (run_dir / "report.html").is_file() else "incomplete"),
            "impl": meta.get("impl", {}),
        }
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return None


def past_runs(home: Path) -> list[dict]:
    runs = home / "runs"
    if not runs.is_dir():
        return []
    summaries = [
        s
        for p in runs.iterdir()
        if p.is_dir() and p.resolve().parent == runs.resolve()
        if (s := run_summary(p)) is not None
    ]
    return sorted(summaries, key=lambda s: (s["time"], s["id"]), reverse=True)


def replay_targets(home: Path) -> dict[str, dict]:
    try:
        with (home / "prewarm_report.csv").open(encoding="utf-8-sig", newline="") as stream:
            return {
                r["symbol"].strip(): r
                for r in csv.DictReader(stream)
                if r.get("role") in {"track_a", "dev"} and r.get("symbol", "").strip()
            }
    except OSError:
        return {}


def company_mode(home: Path, symbol: str) -> str:
    return "replay" if symbol in replay_targets(home) else "live"


def companies(home: Path) -> list[dict]:
    targets = replay_targets(home)
    try:
        with closing(connect_readonly(home / "universe.sqlite")) as con:
            entities = {
                r[0]: (r[1], r[2])
                for r in con.execute(
                    "SELECT symbol,name,subtype FROM entities WHERE financial=1 "
                    "ORDER BY CASE WHEN subtype='bank' THEN 0 ELSE 1 END, name COLLATE NOCASE, symbol"
                )
            }
    except sqlite3.Error:
        return []
    result = []
    for symbol in dict.fromkeys([*targets, *entities]):
        if symbol not in entities:
            continue
        row = targets.get(symbol, {})
        name, subtype = entities[symbol]
        result.append(
            {
                "symbol": symbol,
                "name": name,
                "subtype": row.get("subtype") or subtype or "generic",
                "run_mode": "replay" if symbol in targets else "live",
            }
        )
        if len(result) == 50:
            break
    return result


def resolve_target(home: Path, query: str, symbol: str | None = None) -> dict:
    from signalcourt.stages.harvest_stage import AmbiguousCompany, UnknownCompany, resolve_company

    def candidate(match):
        return {"symbol": match.symbol, "name": match.name, "run_mode": company_mode(home, match.symbol)}

    try:
        with closing(connect_readonly(home / "universe.sqlite")) as con:
            match = resolve_company(con, query, symbol)
            row = con.execute("SELECT subtype FROM entities WHERE symbol=?", (match.symbol,)).fetchone()
            return {"status": "match", "match": {**candidate(match), "subtype": row[0] if row else None}}
    except AmbiguousCompany as exc:
        return {"status": "ambiguous", "candidates": [candidate(m) for m in exc.candidates]}
    except UnknownCompany:
        return {
            "status": "unknown",
            "candidates": [],
            "message": "No listed company matches. Try a full name or symbol.",
        }
    except (OSError, sqlite3.Error):
        return {"status": "unavailable", "candidates": [], "message": "The workspace universe is unavailable."}
