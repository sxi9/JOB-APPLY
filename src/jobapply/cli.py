"""`jobapply` command line."""

from __future__ import annotations

import typer

from jobapply.config import get_settings
from jobapply.logs import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = get_logger("jobapply.cli")


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_json)


@app.command()
def collect(source: list[str] = typer.Option(None, "--source", "-s", help="Only these sources")) -> None:
    """Run collectors once and store new jobs."""
    log.warning("collect not implemented yet", sources=source)


@app.command()
def score(user: str = typer.Option(None, "--user", "-u", help="Username, default all")) -> None:
    """Score unscored jobs for each user."""
    log.warning("score not implemented yet", user=user)


@app.command()
def apply(
    dry_run: bool = typer.Option(True, "--dry-run/--submit", help="Fill and screenshot only"),
    user: str = typer.Option(None, "--user", "-u"),
    limit: int = typer.Option(5, "--limit", "-n"),
) -> None:
    """Apply to queued jobs."""
    log.warning("apply not implemented yet", dry_run=dry_run, user=user, limit=limit)


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None)) -> None:
    """Run the dashboard."""
    log.warning("serve not implemented yet", host=host, port=port)
