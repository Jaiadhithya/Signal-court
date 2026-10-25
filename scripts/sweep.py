"""Run selected pre-warmed targets sequentially and save each target's latest acceptance row.

Each target gets a fresh Python process. total_s includes interpreter startup, imports, pipeline,
verification and shutdown; startup_s separately reports worker imports. Replay blocks HTTP and sockets.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import patch

STAGES = ("harvest", "measure", "sense", "prereg", "court", "judge", "report")
COLUMNS = (
    "symbol", "company", "role", "mode", "as_of", "subtype", "baseline_source", "n_peers", "signals", "claims",
    "claims_attempted", "claims_argued", "evidence_admitted", "challenges", "rejects", "grade", "stop_reason",
    *(f"{stage}_s" for stage in STAGES), "startup_s", "setup_s", "verify_s", "worker_s", "process_overhead_s",
    "total_s", "verify_ok", "verify_problems", "verify_report_path", "report_path", "run_dir", "network_attempts",
    "pass", "error",
)


class NetworkBlocked(OSError):
    """A replay attempted external I/O; the attempted address is never logged."""


@contextmanager
def block_network():
    import httpx

    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(True)
        raise NetworkBlocked("network disabled for replay")

    async def async_blocked(*args, **kwargs):
        return blocked()

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.Client, "send", blocked))
        stack.enter_context(patch.object(httpx.AsyncClient, "send", async_blocked))
        for name in ("create_connection", "getaddrinfo"):
            stack.enter_context(patch.object(socket, name, blocked))
        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                stack.enter_context(patch.object(socket.socket, name, blocked))
        yield attempts


def read_targets(path: Path) -> list[dict[str, str]]:
    """Prewarm role columns select track_a/dev; minimal symbol/company CSVs select every row."""
    targets = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not {"symbol", "company"}.intersection(reader.fieldnames or []):
            raise ValueError("targets CSV needs a symbol or company column")
        has_roles = "role" in (reader.fieldnames or [])
        for row in reader:
            role = (row.get("role") or "").strip().lower()
            if has_roles and role not in {"track_a", "dev"}:
                continue
            symbol = (row.get("symbol") or "").strip().upper()
            company = (row.get("company") or symbol).strip()
            if company:
                targets.append({"symbol": symbol, "company": company, "role": role,
                                "subtype": (row.get("subtype") or "").strip()})
    return targets


def worker(company: str, symbol: str, mode: str, home: Path, as_of: date, result_file: Path | None = None) -> dict:
    """Use the normal runner/context APIs; retain metrics even when a stage fails."""
    t0 = time.perf_counter()
    row = {"error": "", "network_attempts": 0}
    ctx = None
    progress = {"stage": "startup", "status": "running"}

    def checkpoint():
        row["worker_s"] = time.perf_counter() - t0
        if ctx is not None:
            row.update(symbol=ctx.symbol or symbol, subtype=ctx.subtype or "", n_peers=len(ctx.peers))
            (ctx.run_dir / "sweep-progress.json").write_text(
                json.dumps({**row, **progress}, indent=2), encoding="utf-8",
            )
        if result_file is not None:
            result_file.write_text(json.dumps(row), encoding="utf-8")

    guard = block_network() if mode == "replay" else nullcontext([])
    with guard as attempts:
        try:
            from signalcourt.ledger import RecordType, verify_run
            from signalcourt.pipeline import run_pipeline
            from signalcourt.runner import close_run, start_run

            row["startup_s"] = time.perf_counter() - t0
            stage_started = {}
            court_stop_offset = 0
            court_argued = set()

            def on_event(kind, data):
                nonlocal court_stop_offset
                stage = data.get("stage")
                if kind == "stage_start":
                    stage_started[stage] = time.perf_counter()
                    progress.update(stage=stage, status="running")
                    if stage == "court":
                        court_stop_offset = len(ctx.ledger.query(RecordType.STOP))
                elif kind == "stage_end":
                    row[f"{stage}_s"] = data["seconds"]
                    progress.update(stage=stage, status="finished")
                elif kind == "stage_error":
                    row[f"{stage}_s"] = time.perf_counter() - stage_started[stage]
                    progress.update(stage=stage, status="error")
                if stage == "court" and kind in {"stage_end", "stage_error"}:
                    court_stops = ctx.ledger.query(RecordType.STOP)[court_stop_offset:]
                    open_claims = {rid for stop in court_stops for rid in stop.payload["open_items"]}
                    # A later report STOP concerns writing, not completed court rounds.
                    # An unhandled stage error cannot establish which attempted round finished.
                    if kind == "stage_end":
                        court_argued.update(set(ctx.court) - open_claims)
                if kind in {"stage_start", "stage_end", "stage_error", "stage_note"}:
                    # Stage notes may contain provider errors: persist only safe stage state and timings.
                    if progress["status"] == "running" and stage in stage_started:
                        row[f"{stage}_s"] = time.perf_counter() - stage_started[stage]
                    checkpoint()

            setup = time.perf_counter()
            ctx = start_run(company, symbol or None, None, as_of, mode, set(), home, on_event)
            row["setup_s"] = time.perf_counter() - setup
            row["run_dir"] = str(ctx.run_dir)
            checkpoint()
            try:
                run_pipeline(ctx)
            except Exception as e:  # a failed target still gets a durable CSV row and verification
                row["error"] = type(e).__name__
            led = ctx.ledger
            signals = led.query(RecordType.SIGNAL)
            stops = led.query(RecordType.STOP)
            row.update(
                symbol=ctx.symbol or symbol, subtype=ctx.subtype or "", n_peers=len(ctx.peers),
                baseline_source=";".join(sorted({
                    "own_history" if r.payload["specificity_source"] == "na" else r.payload["specificity_source"]
                    for r in signals
                })),
                signals=len(signals), claims=len(led.query(RecordType.CLAIM)),
                claims_attempted=len(ctx.court), claims_argued=len(court_argued),
                evidence_admitted=len(led.query(RecordType.EVIDENCE)),
                challenges=len(led.query(RecordType.CHALLENGE)), rejects=len(led.query(RecordType.REJECT)),
                grade=";".join(sorted({r.payload["grade"] for r in led.query(RecordType.VERDICT)})),
                stop_reason=";".join(dict.fromkeys(s.payload["reason"] for s in stops)),
                report_path=str(ctx.run_dir / "report.html"),
            )
            verify_started = time.perf_counter()
            rep = verify_run(ctx.run_dir, store=ctx.store)
            row.update(verify_ok=rep.ok, verify_problems=len(rep.problems))
            # Keep detailed verifier evidence without provider messages or free-text errors.
            verify_path = ctx.run_dir / "sweep-verify.json"
            verify_path.write_text(json.dumps({
                "ok": rep.ok, "records": rep.records, "quotes_checked": rep.quotes_checked,
                "calcs_checked": rep.calcs_checked, "head_hash": rep.head_hash,
                "problems": [{"seq": p.seq, "kind": p.kind} for p in rep.problems],
            }, indent=2), encoding="utf-8")
            row["verify_report_path"] = str(verify_path)
            row["verify_s"] = time.perf_counter() - verify_started
        except Exception as e:
            row["error"] = type(e).__name__
        finally:
            if ctx is not None:
                close_run(ctx)
            row["network_attempts"] = len(attempts)
    progress.update(status="error" if row["error"] else "finished")
    checkpoint()
    return row


def target_key(row: dict) -> tuple[str, str]:
    symbol = (row.get("symbol") or "").strip().upper()
    return ("symbol", symbol) if symbol else ("company", (row.get("company") or "").strip().casefold())


def atomic_csv(path: Path, rows) -> None:
    """Publish a complete CSV only after its contents reach the filesystem."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as f:
            temp_path = Path(f.name)
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def run_sweep(targets_file: Path, mode: str, home: Path, as_of: date | None = None) -> Path:
    as_of = as_of or date.today()
    home.mkdir(parents=True, exist_ok=True)
    output = home / f"sweep_{date.today().isoformat()}_{mode}.csv"
    targets = read_targets(targets_file)
    existing = []
    if output.is_file():
        with output.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    history = output.with_suffix(".history.csv")
    if not history.exists() or history.stat().st_size == 0:
        # Preserve legacy duplicate attempts before collapsing the canonical summary.
        atomic_csv(history, existing)
    latest = {target_key(row): row for row in existing}
    atomic_csv(output, latest.values())
    with history.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        for target in targets:
            row = {**dict.fromkeys(COLUMNS, ""), **target, "mode": mode, "as_of": as_of.isoformat()}
            with tempfile.TemporaryDirectory(prefix="signal-sweep-") as temp:
                result = Path(temp) / "result.json"
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--company", target["company"],
                           "--symbol", target["symbol"], "--mode", mode, "--home", str(home.resolve()),
                           "--as-of", as_of.isoformat(), "--result-file", str(result)]
                started = time.perf_counter()
                process_error = ""
                try:
                    process = subprocess.run(command, capture_output=True, timeout=240 if mode == "warm" else 120)
                    if process.returncode:
                        process_error = "worker_exit"
                except Exception as e:
                    # Never copy exception text, stderr, response bodies or provider keys to output.
                    process_error = type(e).__name__
                if result.is_file():
                    try:
                        row.update(json.loads(result.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        process_error = process_error or "invalid_worker_result"
                if process_error:
                    row["error"] = process_error
                row["total_s"] = time.perf_counter() - started
            row["process_overhead_s"] = max(0.0, row["total_s"] - float(row.get("worker_s") or 0))
            report = row.get("report_path")
            row["pass"] = bool(
                not row["error"] and row.get("verify_ok") is True and report and Path(report).is_file()
                and row["total_s"] <= (180 if mode == "warm" else 60)
                and not row.get("network_attempts")
            )
            writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())
            latest[target_key(row)] = row
            atomic_csv(output, latest.values())
            print(f"{row['symbol'] or row['company']}: {'PASS' if row['pass'] else 'FAIL'} ({row['total_s']:.2f}s)", flush=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets-file", type=Path)
    parser.add_argument("--mode", choices=("warm", "replay"), required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--company", default="", help=argparse.SUPPRESS)
    parser.add_argument("--symbol", default="", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Direct execution from scripts/ must also work without an editable install.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.worker:
        args.result_file.write_text(json.dumps(worker(
            args.company, args.symbol, args.mode, args.home, args.as_of or date.today(), args.result_file,
        )), encoding="utf-8")
        return 0
    if args.targets_file is None:
        parser.error("--targets-file is required")
    if args.home is None:
        from signalcourt.settings import load_settings

        args.home = load_settings().workspace.home
    try:
        output = run_sweep(args.targets_file, args.mode, args.home, args.as_of)
        print(f"Sweep CSV: {output}")
        return 0
    except Exception as e:
        print(f"Sweep failed: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
