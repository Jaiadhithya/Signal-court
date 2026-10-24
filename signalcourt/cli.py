"""Console entry point: `signal`.

The app object and two housekeeping commands. Other commands register here
as their modules land (run, harvest, panel, verify, ...); keep each command a thin call into its module.
"""
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from signalcourt import __version__

app = typer.Typer(help="Signal Court: evidence-backed signals from Indian financial filings.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def config_digest() -> None:
    """Print the sha256 of every config, registry and prompt file (the hashes a RUN record carries)."""
    from signalcourt.settings import config_digest as digest

    for path, sha in digest().items():
        typer.echo(f"{sha[:16]}  {path}")


@app.command()
def run(
    company: str = typer.Option(..., help="Company name as typed; resolved by the harvest stage."),
    symbol: str | None = typer.Option(None, help="Exchange symbol, if known."),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; defaults to today."),
    mode: str = typer.Option("replay", help="live, warm or replay (replay makes no network calls)."),
    stub: str = typer.Option("", help="Comma-separated stages to force onto their stub, e.g. court,judge."),
) -> None:
    """Run all seven stages for one company; prints one line per stage, then the finding card path."""
    from signalcourt.runner import execute_run
    from signalcourt.settings import load_settings

    if mode not in ("live", "warm", "replay"):
        raise typer.BadParameter("mode must be live, warm or replay", param_hint="--mode")
    try:
        day = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as e:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--as-of") from e
    stubs = {s.strip() for s in stub.split(",") if s.strip()}
    try:
        ctx = execute_run(company, symbol, day, mode, stubs, load_settings().workspace.home)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--stub") from e
    for r in ctx.stage_runs:
        typer.echo(f"{r.stage:<8} {r.impl:<4} {r.records:>4} {r.seconds:7.3f}s")
    typer.echo(str(ctx.card_path))


@app.command()
def verify(run_dir: Annotated[Path, typer.Argument(help="A run directory holding ledger.sqlite.")]) -> None:
    """Recompute the hash chain of a run; prints OK or BROKEN (exit 1)."""
    from signalcourt.ledger import verify_run
    from signalcourt.ledger.store import LEDGER_DB

    if not (run_dir / LEDGER_DB).is_file():
        typer.echo(f"BROKEN: no ledger at {run_dir / LEDGER_DB}")
        raise typer.Exit(1)
    rep = verify_run(run_dir)
    if rep.ok:
        typer.echo(f"OK {rep.records} records, head {rep.head_hash[:16]}")
        return
    first = min(rep.problems, key=lambda p: p.seq)
    typer.echo(f"BROKEN at seq {first.seq}: {first.kind}")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
