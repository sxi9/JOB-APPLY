"""`jobapply mail ...`: test the saved mailbox and wait for a one-time code.

Exports `mail_app`; `cli.py` registers it with `app.add_typer(mail_app, name="mail")`. Nothing here sends
mail or changes the mailbox; `wait-code` only reads. The code itself is printed only with `--show`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer
from sqlmodel import select

from jobapply.logs import get_logger

mail_app = typer.Typer(
    no_args_is_help=True,
    help="Mailbox (Gmail app password etc.): test the saved IMAP/SMTP login, wait for a one-time code. "
    "Read-only; nothing is sent, marked or deleted.",
)
log = get_logger("jobapply.cli.mail")


def _user(session, username: str):
    from jobapply.models import User

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        typer.echo(f"no user named {username!r}", err=True)
        raise typer.Exit(code=2)
    return user


@mail_app.command()
def test(user: str = typer.Option(..., "--user", "-u", help="Username")) -> None:
    """Log in over IMAP (reads only the newest subject), then over SMTP (no send), and record the result on
    the user's profile, the same as the Settings page's "Test connection"."""
    from jobapply.db import session_scope
    from jobapply.mail.mailbox import test_mailbox

    with session_scope() as session:
        u = _user(session, user)
        result = test_mailbox(session, u)
    typer.echo(f"status={result.status} imap={'ok' if result.imap_ok else 'failed'} "
               f"smtp={'ok' if result.smtp_ok else 'failed'}")
    if result.newest_subject:
        typer.echo(f"newest subject: {result.newest_subject[:100]!r} from {result.newest_from}")
    if result.error:
        typer.echo(f"error: {result.error}", err=True)
    raise typer.Exit(code=0 if result.ok else 1)


@mail_app.command("wait-code")
def wait_code(
    user: str = typer.Option(..., "--user", "-u", help="Username"),
    sender: str = typer.Option("", "--sender", "-s",
                               help="Preset (workday, greenhouse, naukri, noreply, ...), domain, address or regex"),
    subject: list[str] = typer.Option(None, "--subject", help="Subject must contain any of these (repeatable)"),
    timeout: float = typer.Option(120.0, "--timeout", "-t", help="Seconds to wait"),
    poll: float = typer.Option(5.0, "--poll", help="Seconds between inbox polls"),
    since_minutes: float = typer.Option(0.0, "--since-minutes",
                                        help="Also accept mail up to this many minutes old (default: only mail that "
                                             "arrives after the command starts, with 2 minutes of clock slack)"),
    once: bool = typer.Option(True, "--once/--allow-reuse",
                              help="Remember the code so it is never consumed twice (app_settings registry)"),
    show: bool = typer.Option(False, "--show", help="Print the code itself (default: only that one was found)"),
) -> None:
    """Poll the user's mailbox for a one-time code and report whether one arrived and how long it is."""
    from jobapply.db import session_scope
    from jobapply.mail.imap import ImapReader
    from jobapply.mail.inbox import mailbox_configs
    from jobapply.mail.otp import CodeRegistry, wait_for_code

    started = datetime.now(UTC)
    after = started - timedelta(minutes=max(0.0, since_minutes))
    with session_scope() as session:
        u = _user(session, user)
        imap_cfg, _smtp = mailbox_configs(session, u)
        if imap_cfg is None:
            typer.echo("no mailbox saved for this user; run `jobapply mail test` after saving one", err=True)
            raise typer.Exit(code=2)
        registry = CodeRegistry(session, u.id) if once else None
        reader = ImapReader(imap_cfg)
        try:
            code = wait_for_code(reader, sender=sender, subject_words=tuple(subject or ()), timeout=timeout,
                                 poll=poll, after=after, registry=registry)
        finally:
            reader.close()
    if code is None:
        typer.echo(f"no code within {timeout:.0f}s (sender={sender or 'any'})")
        raise typer.Exit(code=1)
    typer.echo(f"code found: {len(code)} digits" + (f": {code}" if show else " (use --show to print it)"))


__all__ = ["mail_app"]
