"""Worker-owned pipeline execution, startup warm-up and reconnectable event history."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .data import read_json


class BusyError(Exception):
    pass


class NotReadyError(Exception):
    pass


@dataclass
class RunSession:
    id: str
    company: str
    symbol: str | None
    mode: str
    started: float = field(default_factory=time.perf_counter)
    stage: str | None = None
    finished: bool = False
    events: list[dict] = field(default_factory=list)
    changed: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def emit(self, kind: str, data: dict) -> None:
        with self.changed:
            self.events.append({"id": len(self.events) + 1, "event": kind, "data": data})
            self.changed.notify_all()

    def snapshot(self) -> dict:
        with self.changed:
            return {
                "id": self.id,
                "company": self.company,
                "symbol": self.symbol,
                "mode": self.mode,
                "elapsed_seconds": time.perf_counter() - self.started,
                "events": list(self.events),
            }

    def next_events(self, after: int) -> tuple[list[dict], bool]:
        with self.changed:
            if len(self.events) <= after and not self.finished:
                self.changed.wait(timeout=1)
            return list(self.events[after:]), self.finished


class RunManager:
    def __init__(self, home: Path):
        self.home = home
        self.ready = threading.Event()
        self.warmup_error: str | None = None
        self.warmup_seconds: float | None = None
        self.import_seconds: float | None = None
        self.active: RunSession | None = None
        self.sessions: dict[str, RunSession] = {}
        self.lock = threading.Lock()
        self._secrets: list[str] = []

    def public_text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)[^\s,;]+", r"\1[redacted]", value)
        return value[:3000]

    def warmup(self) -> None:
        started = time.perf_counter()
        try:
            from signalcourt.settings import Env

            env = Env()
            self._secrets = [
                key.strip()
                for field in type(env).model_fields
                if field.endswith(("_keys", "_tokens", "_key"))
                for key in (getattr(env, field) or "").split(",")
                if key.strip()
            ]
            import signalcourt.runner  # noqa: F401
            from signalcourt.render import render_report  # noqa: F401
            from signalcourt.render.pdf import export_pdf  # noqa: F401
            from signalcourt.store import DocStore

            self.import_seconds = time.perf_counter() - started
            store = DocStore(self.home / "cache")
            store.close()
        except Exception as exc:
            self.warmup_error = self.public_text(f"{type(exc).__name__}: {exc}")
        finally:
            self.warmup_seconds = time.perf_counter() - started
            self.ready.set()

    def status(self) -> dict:
        active = self.active
        return {
            "service": "signal-court-console",
            "state": "warming" if not self.ready.is_set() else "error" if self.warmup_error else "ready",
            "message": self.warmup_error,
            "warmup_seconds": self.warmup_seconds,
            "import_seconds": self.import_seconds,
            "active_run": active.snapshot() if active else None,
        }

    def start(self, request: dict) -> RunSession:
        if not self.ready.is_set() or self.warmup_error:
            raise NotReadyError("Warming up. Please wait for Ready." if not self.warmup_error else self.warmup_error)
        if not self.lock.acquire(blocking=False):
            raise BusyError("A run is in progress. Wait for it to finish.")
        allocated: Future = Future()
        try:
            threading.Thread(target=self._execute, args=(request, allocated), daemon=True, name="console-run").start()
        except Exception:
            self.lock.release()
            raise
        return allocated.result()

    def _execute(self, request: dict, allocated: Future) -> None:
        ctx, session = None, None
        started = time.perf_counter()
        outcome, terminal = "error", None
        try:
            from signalcourt.pipeline import run_pipeline
            from signalcourt.runner import close_run, start_run

            ctx = start_run(
                request["company"],
                request.get("symbol"),
                None,
                date.today(),
                request["mode"],
                set(request.get("stub", [])),
                self.home,
            )
            setup_seconds = time.perf_counter() - started
            session = RunSession(ctx.run_dir.name, request["company"], request.get("symbol"), request["mode"], started)
            self.sessions[session.id] = session
            self.active = session

            def on_event(kind, data):
                if kind == "stage_start":
                    session.stage = data["stage"]
                safe = {
                    key: self.public_text(value) if isinstance(value, str) else value for key, value in data.items()
                }
                safe["elapsed_seconds"] = time.perf_counter() - started
                if kind == "stage_start":
                    safe.update({"records": 0, "seconds": 0})
                session.emit(kind, safe)

            ctx.on_event = on_event
            allocated.set_result(session)
            run_pipeline(ctx)
            # Stubs write only a small card; render the same full saved-ledger report used by the CLI.
            if not (ctx.run_dir / "report.html").is_file():
                from signalcourt.render import render_report

                (ctx.run_dir / "report.html").write_text(render_report(ctx.run_dir, store=ctx.store), encoding="utf-8")
            for record in ctx.ledger.query("STOP"):
                session.emit("stop", record.payload)
            outcome = "done"
            terminal = {"report": f"/runs/{session.id}/report", "total_seconds": time.perf_counter() - started}
        except Exception as exc:
            message = self.public_text(f"{type(exc).__name__}: {exc}")
            if session:
                if ctx:
                    for record in ctx.ledger.query("STOP"):
                        session.emit("stop", record.payload)
                terminal = {
                    "stage": session.stage or "setup",
                    "error": message,
                    "total_seconds": time.perf_counter() - started,
                }
            else:
                allocated.set_exception(NotReadyError(message))
        finally:
            try:
                if ctx:
                    try:
                        close_run(ctx)
                    except Exception as exc:
                        outcome = "error"
                        terminal = {
                            "stage": "cleanup",
                            "error": self.public_text(f"{type(exc).__name__}: {exc}"),
                            "total_seconds": time.perf_counter() - started,
                        }
                    meta_path = ctx.run_dir / "report-meta.json"
                    meta = read_json(meta_path)
                    meta.update({"impl": dict(ctx.impl), "stage_runs": [asdict(s) for s in ctx.stage_runs]})
                    meta["console"] = {
                        "total_seconds": time.perf_counter() - started,
                        "status": outcome,
                        "setup_seconds": setup_seconds if session else None,
                    }
                    try:
                        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    except OSError as exc:
                        outcome = "error"
                        terminal = {
                            "stage": "report",
                            "error": self.public_text(f"Could not save run metadata: {exc}"),
                            "total_seconds": time.perf_counter() - started,
                        }
            finally:
                self.active = None
                self.lock.release()
                if session:
                    with session.changed:
                        if terminal:
                            session.emit(outcome, terminal)
                        session.finished = True
                        session.changed.notify_all()
