"""`jobapply sites ...` (Naukri, Instahyre, Cutshort, Wellfound: session check, cookie import, public search,
gated apply) and `jobapply naukri ...` (profile refresh)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlmodel import select

from jobapply.logs import get_logger

sites_app = typer.Typer(no_args_is_help=True,
                        help="Assisted sites (naukri, instahyre, cutshort, wellfound): check, import-cookies, "
                             "search, apply. Login is a cookie export; nothing here types a credential.")
naukri_app = typer.Typer(no_args_is_help=True, help="Naukri extras: daily profile refresh")
log = get_logger("jobapply.cli.sites")

SITES = ("naukri", "instahyre", "cutshort", "wellfound")


def _site(value: str) -> str:
    site = (value or "").strip().lower()
    if site not in SITES:
        typer.echo(f"unknown site {value!r}; one of {', '.join(SITES)}", err=True)
        raise typer.Exit(code=2)
    return site


def _user_id(username: str) -> int:
    from jobapply.db import session_scope
    from jobapply.models import User

    with session_scope() as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if user is None:
            typer.echo(f"no user named {username!r}", err=True)
            raise typer.Exit(code=2)
        return int(user.id)


@sites_app.command()
def check(user: str = typer.Option(..., "--user", "-u", help="Username"),
          site: str = typer.Option(..., "--site", "-s", help=", ".join(SITES))) -> None:
    """Open the site with the saved (headless) profile and report ok | login | challenge | unknown."""
    from jobapply.sites.connect import check_session_for

    site = _site(site)
    status = check_session_for(site, _user_id(user))
    typer.echo(f"{site} session: {status}")
    raise typer.Exit(code=0 if status == "ok" else 1)


@sites_app.command("import-cookies")
def import_cookies(user: str = typer.Option(..., "--user", "-u", help="Username"),
                   site: str = typer.Option(..., "--site", "-s", help=", ".join(SITES)),
                   path: str = typer.Option(..., "--file", "-f", help="Cookie export JSON (the site's domain only)"),
                   check_after: bool = typer.Option(True, "--check/--no-check",
                                                    help="Open the site afterwards and report the status")) -> None:
    """Load a cookie export into this user's profile for the site, so no interactive login is needed.
    Export the file yourself (a cookie-export extension), save it locally, and pass its path; only the
    site's own cookies are used and their values are never printed."""
    from jobapply.sites.connect import import_cookies_for

    site = _site(site)
    result = import_cookies_for(site, _user_id(user), Path(path), verify=check_after)
    typer.echo(f"imported {result.count} {site} cookie(s) into the profile for {user!r}")
    if check_after:
        typer.echo(f"{site} session: {result.status}")
        raise typer.Exit(code=0 if result.status == "ok" else 1)


def _age(posted_at: datetime | None) -> str:
    if posted_at is None:
        return "?"
    now = datetime.now(UTC)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    hours = max(0, int((now - posted_at).total_seconds() // 3600))
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


@sites_app.command()
def search(
    site: str = typer.Option(..., "--site", "-s", help=", ".join(SITES)),
    query: list[str] = typer.Option(..., "--query", "-q", help="Search phrase (repeatable)"),
    location: str = typer.Option("", "--location", "-l"),
    hours: int = typer.Option(24, "--hours", help="Only postings this many hours old or newer"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results per query"),
    max_requests: int = typer.Option(40, "--max-requests", help="Polite per-run request budget"),
    store: bool = typer.Option(False, "--store", help="Store results through the collect pipeline"),
) -> None:
    """Public (logged-out) search on one site and print a table."""
    import importlib

    site = _site(site)
    search_mod = importlib.import_module(f"jobapply.sites.{site}.search")
    jobs = search_mod.search_once(list(query), location, hours, max_results=limit, max_requests=max_requests)
    header = f"{'title':<40} {'company':<24} {'location':<22} {'age':>4} url"
    typer.echo(header)
    typer.echo("-" * len(header))
    for j in jobs:
        typer.echo(f"{j.title[:40]:<40} {j.company[:24]:<24} {(j.location or '')[:22]:<22} "
                   f"{_age(j.posted_at):>4} {j.url or j.apply_url}")
    typer.echo(f"{len(jobs)} job(s)")
    if not store:
        return
    from jobapply.collectors.base import BaseCollector
    from jobapply.pipeline.collect import run_all

    class _Prefetched(BaseCollector):
        """Feeds the jobs already fetched above through dedupe/store/assign without a second search."""

        source = site

        @property
        def key(self) -> str:
            return f"{site}:cli"

        def collect(self):
            return jobs

    typer.echo(f"stored: {run_all(collectors=[_Prefetched()])}")


@sites_app.command()
def apply(
    site: str = typer.Option(..., "--site", "-s", help=", ".join(SITES)),
    user: str = typer.Option(None, "--user", "-u", help="Username, default all active users"),
    limit: int = typer.Option(3, "--limit", "-n"),
    dry_run: bool = typer.Option(True, "--dry-run/--submit", help="Stop before the final click and screenshot"),
) -> None:
    """Apply to queued jobs on one site inside each user's logged-in profile, honouring the kill switch, the
    global dry-run switch, the site's enabled / auto-submit flags, caps, active hours and challenge cooldowns.
    A real submit needs --submit AND the global dry-run switch off AND the site's auto-submit on."""
    from jobapply.pipeline.apply import apply_assisted, site_active_hours
    from jobapply.sites.browser import is_active_hours

    site = _site(site)
    hours = site_active_hours(site)
    if not is_active_hours(spec=hours):
        typer.echo(f"outside {site} active hours ({hours}), nothing done")
        raise typer.Exit(code=0)
    outcomes = apply_assisted(site, username=user, limit=limit, dry_run=dry_run)
    for o in outcomes:
        typer.echo(" ".join(f"{k}={v}" for k, v in o.items()))
    typer.echo(f"{len(outcomes)} outcome(s), dry_run={dry_run}")


@naukri_app.command("refresh-profile")
def refresh_profile(
    user: str = typer.Option(..., "--user", "-u", help="Username"),
    dry_run: bool = typer.Option(True, "--dry-run/--refresh", help="Only open the profile page and screenshot"),
    resume: str = typer.Option(None, "--resume", help="Re-upload this resume file too (real run only)"),
) -> None:
    """Re-save the Naukri resume headline (and optionally re-upload the resume) so the profile's 'last updated'
    moves. The default dry run only navigates and screenshots."""
    from jobapply.sites.naukri.apply import refresh_profile as _refresh

    out = _refresh(_user_id(user), dry_run=dry_run, resume_path=resume)
    typer.echo(" ".join(f"{k}={v}" for k, v in out.items()))
    raise typer.Exit(code=0 if out.get("status") in ("dry_run", "refreshed") else 1)


__all__ = ["sites_app", "naukri_app", "SITES"]
