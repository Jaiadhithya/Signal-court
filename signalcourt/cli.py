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
    subtype: str | None = typer.Option(None, help="Sub-type override; normally read from the filing format."),
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD; defaults to today."),
    mode: str = typer.Option("replay", help="live, warm or replay (replay makes no network calls)."),
    stub: str = typer.Option("", help="Comma-separated stages to force onto their stub, e.g. court,judge."),
    open_card: bool = typer.Option(False, "--open", help="Open the finding card in the browser when done."),
) -> None:
    """Run all seven stages for one company; prints each stage as it finishes, then the total and the card path."""
    import time
    import webbrowser

    from signalcourt.pipeline import STAGE_ORDER, run_pipeline
    from signalcourt.runner import close_run, start_run
    from signalcourt.settings import load_settings
    from signalcourt.stages.harvest_stage import AmbiguousCompany, UnknownCompany

    if mode not in ("live", "warm", "replay"):
        raise typer.BadParameter("mode must be live, warm or replay", param_hint="--mode")
    try:
        day = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as e:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--as-of") from e
    stubs = {s.strip() for s in stub.split(",") if s.strip()}
    unknown = stubs - set(STAGE_ORDER)
    if unknown:
        raise typer.BadParameter(f"unknown stage(s): {sorted(unknown)}", param_hint="--stub")

    def show(kind: str, d: dict) -> None:
        if kind == "stage_end":
            typer.echo(f"{d['stage']:<8} {d['impl']:<4} {d['records']:>4} {d['seconds']:7.3f}s")
        elif kind == "stage_error":
            typer.echo(f"FAILED at {d['stage']}: {d['error']}")
        elif kind == "stage_note":
            typer.echo(f"{d['stage']:<8} note {d['note']}")

    t0 = time.perf_counter()
    ctx = start_run(company, symbol, subtype, day, mode, stubs, load_settings().workspace.home, show)
    try:
        run_pipeline(ctx)
    except AmbiguousCompany as e:  # the operator retypes; exit 2, never a traceback
        typer.echo("Did you mean:")
        for c in e.candidates:
            typer.echo(f"  {c.symbol:<12} {c.name}")
        raise typer.Exit(2) from e
    except UnknownCompany as e:
        typer.echo(f"{e}. Try the exchange symbol or the full legal name.")
        raise typer.Exit(2) from e
    except Exception as e:  # the stage, its error and a STOP record are already out; point at the evidence
        typer.echo(f"run {ctx.run_dir}")
        raise typer.Exit(1) from e
    finally:
        close_run(ctx)
    typer.echo(f"total    {time.perf_counter() - t0:.3f}s")
    typer.echo(str(ctx.card_path))
    if open_card and ctx.card_path:
        webbrowser.open(ctx.card_path.resolve().as_uri())


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


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="An existing run directory holding ledger.sqlite.")],
    pdf: bool = typer.Option(False, "--pdf", help="Also export report.pdf when Chrome is available."),
    open_report: bool = typer.Option(False, "--open", help="Open the HTML research report in the browser."),
) -> None:
    """Re-render a saved research report without appending ledger records."""
    import webbrowser

    from signalcourt.ledger.store import LEDGER_DB

    if not (run_dir / LEDGER_DB).is_file():
        typer.echo(f"No ledger at {run_dir / LEDGER_DB}")
        raise typer.Exit(1)

    from signalcourt.render import render_report
    from signalcourt.settings import Workspace, load_settings
    from signalcourt.store import DocStore

    configured = load_settings().workspace
    nearby = Workspace(home=run_dir.resolve().parent.parent)
    workspace = next((ws for ws in (configured, nearby) if ws.manifest_db.is_file()), None)
    store = DocStore(workspace.cache) if workspace else None
    try:
        html = render_report(run_dir, store=store)
    finally:
        if store is not None:
            store.close()
    html_path = run_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    (run_dir / "finding.html").write_text(html, encoding="utf-8")
    typer.echo(str(html_path))
    if pdf:
        from signalcourt.render.pdf import export_pdf

        pdf_path = export_pdf(html_path)
        if pdf_path is None:
            typer.echo("PDF unavailable: Chrome was not found or could not export; the HTML report is ready.")
        else:
            typer.echo(str(pdf_path))
    if open_report:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    app()
