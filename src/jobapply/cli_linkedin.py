"""`jobapply linkedin ...`: login, session check, search and Easy Apply for the assisted LinkedIn flow."""

from __future__ import annotations

from datetime import UTC, datetime

import typer
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.logs import get_logger

linkedin_app = typer.Typer(no_args_is_help=True, help="LinkedIn: persistent login, search, Easy Apply (assisted)")
log = get_logger("jobapply.cli.linkedin")


def _load_user(session, username: str):
    from jobapply.models import User

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        typer.echo(f"no user named {username!r}", err=True)
        raise typer.Exit(code=2)
    return user


def _with_session(username: str, headless: bool | None, fn):
    """Open the DB session and the persistent LinkedIn profile, run `fn(linkedin_session, page)`."""
    from playwright.sync_api import sync_playwright

    from jobapply.db import session_scope
    from jobapply.sites.browser import launch_persistent
    from jobapply.sites.linkedin.session import LinkedInSession

    with session_scope() as session:
        user = _load_user(session, username)
        with sync_playwright() as pw:
            context = launch_persistent(pw, user.id, "linkedin", headless=headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                return fn(LinkedInSession(context, user, session), page)
            finally:
                try:
                    context.close()
                except Exception as exc:  # noqa: BLE001 - the user may have closed the window already
                    log.debug("browser context already closed", error=str(exc)[:120])


@linkedin_app.command()
def login(user: str = typer.Option(..., "--user", "-u", help="Username"),
          timeout: int = typer.Option(600, "--timeout", help="Seconds to wait for the manual login")) -> None:
    """Open a visible browser on the LinkedIn login page and wait for you to sign in. Credentials are never typed."""
    ok = _with_session(user, False, lambda s, page: s.login_interactive(page, timeout_s=timeout))
    typer.echo("login: ok, session saved" if ok else "login: not completed")
    raise typer.Exit(code=0 if ok else 1)


@linkedin_app.command()
def check(user: str = typer.Option(..., "--user", "-u", help="Username"),
          headless: bool = typer.Option(None, "--headless/--headful",
                                        help="Default from JOBAPPLY_LINKEDIN_HEADLESS")) -> None:
    """Open the feed with the saved profile and report ok | expired | challenge | unknown."""
    status = _with_session(user, headless, lambda s, page: s.check(page))
    typer.echo(f"linkedin session: {status}")
    raise typer.Exit(code=0 if status == "ok" else 1)


def _minutes_ago(posted_at: datetime | None) -> str:
    if posted_at is None:
        return "?"
    now = datetime.now(UTC)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return str(max(0, int((now - posted_at).total_seconds() // 60)))


@linkedin_app.command()
def search(
    query: list[str] = typer.Argument(..., help="One or more search phrases"),
    location: str = typer.Option("", "--location", "-l"),
    minutes: int = typer.Option(60, "--minutes", help="Only postings this many minutes old or newer"),
    remote: bool = typer.Option(False, "--remote", help="Remote jobs only"),
    easy_apply: bool = typer.Option(False, "--easy-apply", help="Easy Apply jobs only"),
    limit: int = typer.Option(25, "--limit", "-n"),
    details: bool = typer.Option(True, "--details/--no-details", help="Open each job for the description"),
    store: bool = typer.Option(False, "--store", help="Store results through the collect pipeline"),
) -> None:
    """Search recent LinkedIn postings and print a table."""
    try:
        from jobapply.sites.linkedin import search as search_mod
    except ImportError as exc:
        typer.echo(f"linkedin search is not available yet ({exc})", err=True)
        raise typer.Exit(code=2) from exc

    jobs = search_mod.search_once(list(query), location=location, minutes_old=minutes, remote_only=remote,
                                  easy_apply_only=easy_apply, max_results=limit, with_details=details)
    header = f"{'title':<40} {'company':<24} {'location':<22} {'min':>5} {'easy':<4} url"
    typer.echo(header)
    typer.echo("-" * len(header))
    for j in jobs:
        easy = "yes" if (j.raw or {}).get("easy_apply") else "no"
        typer.echo(f"{j.title[:40]:<40} {j.company[:24]:<24} {j.location[:22]:<22} "
                   f"{_minutes_ago(j.posted_at):>5} {easy:<4} {j.url or j.apply_url}")
    typer.echo(f"{len(jobs)} job(s)")
    if not store:
        return
    from jobapply.collectors.base import BaseCollector
    from jobapply.pipeline.collect import run_all

    class _Prefetched(BaseCollector):
        """Feeds the jobs already fetched above through dedupe/store/assign without a second search."""

        source = "linkedin"

        @property
        def key(self) -> str:
            return "linkedin:cli"

        def collect(self):
            return jobs

    result = run_all(collectors=[_Prefetched()])
    typer.echo(f"stored: {result}")

@linkedin_app.command()
def apply(
    user: str = typer.Option(None, "--user", "-u", help="Username, default all active users"),
    limit: int = typer.Option(3, "--limit", "-n"),
    dry_run: bool = typer.Option(True, "--dry-run/--submit", help="Fill and screenshot only"),
) -> None:
    """Run Easy Apply on queued LinkedIn jobs, honouring caps, active hours, switches and challenge cooldowns."""
    from jobapply.sites.browser import is_active_hours

    settings = get_settings()
    if not is_active_hours(spec=settings.linkedin_active_hours):
        # apply_linkedin() checks this too (for the scheduler); here it is only a friendlier message.
        typer.echo(f"outside LinkedIn active hours ({settings.linkedin_active_hours}), nothing done")
        raise typer.Exit(code=0)
    try:
        from jobapply.sites.linkedin.easy_apply import apply_linkedin
    except ImportError as exc:
        typer.echo(f"linkedin easy apply is not available yet ({exc})", err=True)
        raise typer.Exit(code=2) from exc

    outcomes = apply_linkedin(username=user, limit=limit, dry_run=dry_run)
    for o in outcomes:
        typer.echo(" ".join(f"{k}={v}" for k, v in o.items()))
    typer.echo(f"{len(outcomes)} outcome(s), dry_run={dry_run}")


__all__ = ["linkedin_app"]
