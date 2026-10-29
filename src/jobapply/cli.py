"""`jobapply` command line."""

from __future__ import annotations

import typer

from jobapply.cli_linkedin import linkedin_app
from jobapply.config import get_settings
from jobapply.logs import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True, help="Database commands")
app.add_typer(db_app, name="db")
app.add_typer(linkedin_app, name="linkedin")
log = get_logger("jobapply.cli")


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_json)


@app.command()
def collect(source: list[str] = typer.Option(None, "--source", "-s", help="Only these sources")) -> None:
    """Run collectors once, store new jobs and assign them to users."""
    from jobapply.pipeline.collect import run_all

    log.info("collect finished", **run_all(only=source or None))


@app.command()
def score(user: str = typer.Option(None, "--user", "-u", help="Username, default all"),
          limit: int = typer.Option(None, "--limit", "-n")) -> None:
    """Score new jobs for each user, tailor resume and cover letter for the ones above the threshold."""
    from jobapply.pipeline.score import score_all

    log.info("score finished", results=score_all(user, limit=limit))


@app.command()
def apply(
    dry_run: bool = typer.Option(True, "--dry-run/--submit", help="Fill and screenshot only"),
    user: str = typer.Option(None, "--user", "-u"),
    limit: int = typer.Option(5, "--limit", "-n"),
) -> None:
    """Fill application forms for approved and queued jobs. Dry run never submits."""
    from jobapply.pipeline.apply import apply_all

    results = apply_all(user, limit=limit, dry_run=dry_run)
    for name, outcomes in results.items():
        for o in outcomes:
            log.info("applied", user=name, application=o["app_id"], status=o["status"], detail=o["detail"])
    log.info("apply finished", dry_run=dry_run, users=len(results), applications=sum(map(len, results.values())))


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None)) -> None:
    """Run the dashboard."""
    import uvicorn

    settings = get_settings()
    uvicorn.run("jobapply.web.app:create_app", factory=True, host=host or settings.host, port=port or settings.port,
                log_level=settings.log_level.lower())


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply Alembic migrations."""
    from jobapply.db import upgrade

    upgrade(revision=revision)
    log.info("database upgraded", revision=revision)


@app.command()
def seed() -> None:
    """Create demo admin/user and demo jobs so the dashboard can be reviewed without API keys."""
    from jobapply.db import get_engine, upgrade
    from jobapply.seed import seed as _seed

    upgrade()
    log.info("seeded", **_seed(get_engine()))


@app.command("create-user")
def create_user_cmd(username: str, password: str = typer.Option(..., prompt=True, hide_input=True),
                    email: str = "", admin: bool = False) -> None:
    """Add a user from the command line."""
    from jobapply.db import session_scope
    from jobapply.models import Role
    from jobapply.users import create_user

    with session_scope() as session:
        create_user(session, username, password, email=email, role=Role.admin if admin else Role.user)
    log.info("user created", username=username, admin=admin)


@app.command("vault-key")
def vault_key() -> None:
    """Print a new credential vault key for JOBAPPLY_VAULT_KEY."""
    from jobapply.vault import generate_key

    typer.echo(generate_key())


@app.command()
def inbox(user: str = typer.Option(None, "--user", "-u"), limit: int = typer.Option(100, "--limit", "-n")) -> None:
    """Read each user's mailbox: classify replies, update applications, draft answers, forward invites."""
    from jobapply.pipeline.mail import sync_all

    log.info("inbox finished", results=sync_all(user, limit=limit))


@app.command("follow-ups")
def follow_ups(user: str = typer.Option(None, "--user", "-u")) -> None:
    """Send due follow-ups to recruiters who replied; remind the user about the rest."""
    from jobapply.pipeline.mail import follow_ups_all

    log.info("follow-ups finished", results=follow_ups_all(user))


@app.command()
def summary(user: str = typer.Option(None, "--user", "-u")) -> None:
    """Send each user their daily summary through the notifier."""
    from jobapply.pipeline.summary import send_summaries

    log.info("summaries sent", results=send_summaries(user))


@app.command("detect-companies")
def detect_companies_cmd() -> None:
    """Fill in ats and slug in config/companies.yaml from each careers_url."""
    from jobapply.collectors.registry import detect_companies, load_companies, save_companies

    path = get_settings().companies_file
    companies = load_companies(path)
    if detect_companies(companies):
        save_companies(path, companies)


@app.command()
def refilter() -> None:
    """Re-run every user's filters over recent jobs (after filters change)."""
    from jobapply.db import session_scope
    from jobapply.pipeline.filters import assign_jobs

    with session_scope() as session:
        log.info("refilter finished", assigned=assign_jobs(session, None))


@app.command()
def schedule(group: list[str] = typer.Option(None, "--group", "-g", help="collector and/or worker")) -> None:
    """Run the scheduler in the foreground."""
    from jobapply.scheduler import build_scheduler

    sched = build_scheduler(group or None)
    log.info("scheduler starting", jobs=[j.id for j in sched.get_jobs()])
    sched.start()
