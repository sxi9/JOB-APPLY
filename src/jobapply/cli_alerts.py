"""`jobapply alerts ...`: WhatsApp new-job alerts, the bridge's groups and a test message.

Exports `alerts_app`; `cli.py` registers it with `app.add_typer(alerts_app, name="alerts")`. `send-new --dry-run`
prints the messages without sending or recording anything; without it the messages go to the user's saved
target(s) and each posted job gets a `notified` event (never posted twice). See docs/NOTIFICATIONS.md.
"""

from __future__ import annotations

import typer
from sqlmodel import select

from jobapply.logs import get_logger

alerts_app = typer.Typer(
    no_args_is_help=True,
    help="WhatsApp alerts: post new matches (send-new), list the bridge's groups, send a test message.",
)
log = get_logger("jobapply.cli.alerts")


def _user(session, username: str):
    from jobapply.models import User

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        typer.echo(f"no user named {username!r}", err=True)
        raise typer.Exit(code=2)
    return user


@alerts_app.command("send-new")
def send_new(
    user: str = typer.Option(..., "--user", "-u", help="Username"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the messages; send nothing, record nothing"),
    force: bool = typer.Option(False, "--force", help="Build the messages even when the user's alerts are off"),
) -> None:
    """Post the user's not-yet-notified matches (batches of 8, newest first, at most 5 messages)."""
    from jobapply.db import session_scope
    from jobapply.pipeline.alerts import new_jobs_enabled, notify_new_jobs

    with session_scope() as session:
        u = _user(session, user)
        if not new_jobs_enabled(u):
            if not (force and dry_run):
                typer.echo(f"new job alerts are off for {user}; enable them in Settings -> Notifications "
                           "(or use --dry-run --force to preview)")
                raise typer.Exit(code=1)
            u.preferences = {**(u.preferences or {}), "new_jobs_enabled": True}  # in-memory only: not saved
            session.expunge(u)
        messages = notify_new_jobs(session, u, dry_run=dry_run)
    if dry_run:
        for i, m in enumerate(messages, 1):
            typer.echo(f"--- message {i}/{len(messages)} ---")
            typer.echo(m)
        typer.echo(f"{len(messages)} message(s) would be sent (dry run: nothing sent, nothing recorded)")
    else:
        typer.echo(f"{len(messages)} message(s) sent")


@alerts_app.command()
def groups() -> None:
    """List the groups the bridge's WhatsApp account is in (name and jid), for Settings -> WhatsApp group."""
    from jobapply.notify import bridge_health, list_groups

    health = bridge_health()
    typer.echo(f"bridge: {'open' if health.get('ok') else health.get('status')}"
               + (f" ({health['me_masked']})" if health.get("me_masked") else ""))
    rows = list_groups(refresh=True)
    if not rows:
        typer.echo("no groups (bridge unreachable, not configured, or the account is in no group)")
        raise typer.Exit(code=1)
    width = max(len(g["name"]) for g in rows)
    typer.echo(f"{'name'.ljust(width)}  jid")
    for g in rows:
        typer.echo(f"{g['name'].ljust(width)}  {g['jid']}")


@alerts_app.command()
def test(user: str = typer.Option(..., "--user", "-u", help="Username")) -> None:
    """Send one short test message to the user's saved WhatsApp target(s)."""
    from jobapply.db import session_scope
    from jobapply.models import utcnow
    from jobapply.notify import alert_targets, mask_target, send_to
    from jobapply.pipeline.alerts import apply_mode, fmt_ist

    with session_scope() as session:
        u = _user(session, user)
        targets = alert_targets(u)
        mode = apply_mode(u)
    if not targets:
        typer.echo("no WhatsApp target saved for this user (number / group in Settings -> Notifications)", err=True)
        raise typer.Exit(code=2)
    text = f"✅ JOB-APPLY test for {user} — {fmt_ist(utcnow())}. Mode: {mode}. Alerts will arrive here."
    failed = 0
    for t in targets:
        ok = send_to(t, text)
        typer.echo(f"{mask_target(t)}: {'sent' if ok else 'FAILED'}")
        failed += 0 if ok else 1
    raise typer.Exit(code=1 if failed else 0)


__all__ = ["alerts_app"]
