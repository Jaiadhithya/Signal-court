"""Start a run: run dir, RUN record, run context, then the pipeline.

The RUN record carries the config digest and the git sha, so a run is tied to the exact code,
thresholds, registries and prompts it used. Every run gets a fresh directory.
"""

from __future__ import annotations

import secrets
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from signalcourt import stubs  # noqa: F401  (registers the stub stages)
from signalcourt.ledger import Ledger, RunPayload
from signalcourt.ledger.store import utc_now_ms
from signalcourt.pipeline import EventFn, RunContext, resolve_impl, run_pipeline
from signalcourt.settings import REPO_ROOT, Workspace, config_digest, load_config


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def new_run_dir(runs: Path) -> Path:
    """runs/YYYYMMDD-HHMMSS-<6 hex>; never an existing directory."""
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        d = runs / f"{stamp}-{secrets.token_hex(3)}"
        if not d.exists():
            return d


def run_payload(company: str, symbol: str | None, as_of: date, mode: str, settings: dict) -> RunPayload:
    digest = config_digest()
    return RunPayload(
        company_input=company,
        symbol=symbol,
        as_of=as_of.isoformat(),
        mode=mode,
        budgets=settings["budgets"],
        k=settings["rank"]["k"],
        code_git_sha=git_sha(),
        settings_hash=digest.get("config/settings.toml", "unknown"),
        registry_hashes={k: v for k, v in digest.items() if k.startswith("registry/")},
        prompt_hashes={k: v for k, v in digest.items() if k.startswith("prompts/")},
        model_pools_hash=digest.get("config/models.yaml", "unknown"),
        started_at=utc_now_ms(),
    )


def start_run(
    company: str,
    symbol: str | None,
    subtype: str | None,
    as_of: date,
    mode: str,
    stub: set[str],
    home: Path,
    on_event: EventFn | None = None,
) -> RunContext:
    """Create the run dir and ledger (RUN record) and return a context ready for ``run_pipeline``."""
    settings = load_config().model_dump()
    impl = resolve_impl(settings, stub)
    runs = Workspace(home=Path(home)).runs
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = new_run_dir(runs)
    ledger = Ledger.create(run_dir, run_payload(company, symbol, as_of, mode, settings))
    return RunContext(
        ledger=ledger, store=None, settings=settings, company=company, symbol=symbol, as_of=as_of, mode=mode,
        run_dir=run_dir, impl=impl, subtype=subtype,
        **({"on_event": on_event} if on_event else {}),
    )


def execute_run(
    company: str,
    symbol: str | None,
    subtype: str | None,
    as_of: date,
    mode: str,
    stub: set[str],
    home: Path,
    on_event: EventFn | None = None,
) -> RunContext:
    """start_run + run_pipeline; the ledger is closed on return, even when a stage fails."""
    ctx = start_run(company, symbol, subtype, as_of, mode, stub, home, on_event)
    try:
        return run_pipeline(ctx)
    finally:
        ctx.ledger.close()
