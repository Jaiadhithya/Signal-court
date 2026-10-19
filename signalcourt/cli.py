"""Console entry point: `signal`.

The app object and two housekeeping commands. Other commands register here
as their modules land (run, harvest, panel, verify, ...); keep each command a thin call into its module.
"""
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


if __name__ == "__main__":
    app()
