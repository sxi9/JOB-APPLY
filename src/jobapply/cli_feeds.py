"""`jobapply feeds ...`: WhatsApp group feeds (admin-configured, no account needed on the readers' side).

Exports `feeds_app`; `cli.py` registers it with `app.add_typer(feeds_app, name="feeds")`. `run --dry-run`
prints what each enabled feed would post without sending or recording anything; without it every enabled
feed posts its new matches to its group (each posted job is recorded, never posted twice). See
docs/NOTIFICATIONS.md, "Group feeds".
"""

from __future__ import annotations

import typer

from jobapply.logs import get_logger

feeds_app = typer.Typer(
    no_args_is_help=True,
    help="WhatsApp group feeds: list, run (post new matches to the groups), add, enable/disable, remove.",
)
log = get_logger("jobapply.cli.feeds")


def _feed_or_exit(session, feed_id: str) -> dict:
    from jobapply.pipeline.feeds import get_feed

    feed = get_feed(session, feed_id)
    if feed is None:
        typer.echo(f"no feed with id {feed_id!r} (see `jobapply feeds list`)", err=True)
        raise typer.Exit(code=2)
    return feed


def _check_fields(fields: list[str]) -> list[str]:
    """`cybersecurity` -> `cybersecurity:*`; `cybersecurity:soc` kept; unknown field keys exit 2."""
    from jobapply.roles import all_fields

    known = {f.key: f for f in all_fields()}
    out = []
    for raw in fields:
        key = str(raw).strip().lower()
        field_key, _, sub = key.partition(":")
        if field_key not in known:
            typer.echo(f"unknown career field {field_key!r}; known: {', '.join(sorted(known))}", err=True)
            raise typer.Exit(code=2)
        if sub and sub != "*" and sub not in {s.key for s in known[field_key].subroles}:
            typer.echo(f"unknown sub-role {sub!r} for {field_key}; known: "
                       f"{', '.join(s.key for s in known[field_key].subroles)}", err=True)
            raise typer.Exit(code=2)
        out.append(f"{field_key}:{sub or '*'}")
    return out


@feeds_app.command("list")
def list_feeds() -> None:
    """Every feed: id, enabled, name, group, filters, last run and how many jobs it posted."""
    from jobapply.db import session_scope
    from jobapply.notify import mask_target
    from jobapply.pipeline.feeds import filters_summary, load_feeds

    with session_scope() as session:
        feeds = load_feeds(session)
    if not feeds:
        typer.echo("no feeds (add one with `jobapply feeds add --name ... --group <jid>` or on /admin)")
        return
    for f in feeds:
        state = "on " if f["enabled"] else "off"
        typer.echo(f"{f['id']}  [{state}]  {f['name']}  ->  {mask_target(f['group_jid'])}")
        typer.echo(f"          {filters_summary(f)}  ·  max {f['max_per_run']}/run  ·  "
                   f"last run {f['last_run_at'] or 'never'}  ·  posted {f['posted_count']}")


@feeds_app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the messages; send nothing, record nothing"),
    feed: str = typer.Option(None, "--feed", "-f", help="Only this feed id"),
) -> None:
    """Post each enabled feed's new matches (jobs first seen inside its age window, not yet posted)."""
    from jobapply.db import session_scope
    from jobapply.pipeline.feeds import run_feeds

    with session_scope() as session:
        if feed:
            _feed_or_exit(session, feed)
        out = run_feeds(session, None, dry_run=dry_run, feed_ids=[feed] if feed else None)
    for r in out["feeds"]:
        head = f"--- {r['name']} ({r['id']}): "
        if r["skipped"]:
            typer.echo(head + r["skipped"])
            continue
        if dry_run:
            typer.echo(head + f"{r['candidates']} matching job(s), {len(r['messages'])} message(s) would be sent")
            for i, m in enumerate(r["messages"], 1):
                typer.echo(f"--- message {i}/{len(r['messages'])} ---")
                typer.echo(m)
        else:
            typer.echo(head + f"{r['sent']} message(s) sent, {r['posted']} job(s) posted"
                       + (f", error: {r['error']}" if r["error"] else ""))
    if dry_run:
        typer.echo(f"{out['messages']} message(s) would be sent (dry run: nothing sent, nothing recorded)")
    else:
        typer.echo(f"{out['messages']} message(s) sent, {out['posted']} job(s) posted")


@feeds_app.command()
def add(
    name: str = typer.Option(..., "--name", "-n", help="Feed name (shown in the message header)"),
    group: str = typer.Option(..., "--group", "-g", help="WhatsApp group jid (120363...@g.us), see `alerts groups`"),
    field: list[str] = typer.Option(None, "--field", help="Career field key (cybersecurity) or field:subrole"),
    role: list[str] = typer.Option(None, "--role", help="Free-text role; all its words must be in the title"),
    include: list[str] = typer.Option(None, "--include", help="Keyword that must appear (title or description)"),
    exclude: list[str] = typer.Option(None, "--exclude", help="Keyword that must not appear in the title"),
    location: list[str] = typer.Option(None, "--location", help="Location substring; `remote` for remote jobs"),
    source: list[str] = typer.Option(None, "--source", help="Only these sources (greenhouse, linkedin, ...)"),
    hours: int = typer.Option(24, "--hours", help="Only jobs posted within this many hours (0 = any age)"),
    remote_only: bool = typer.Option(False, "--remote-only", help="Remote jobs only"),
    min_salary: float = typer.Option(None, "--min-salary", help="Reject when the posted maximum is below this"),
    max_per_run: int = typer.Option(10, "--max-per-run", help="At most this many jobs per run"),
    disabled: bool = typer.Option(False, "--disabled", help="Create the feed switched off"),
) -> None:
    """Add a feed. Example: feeds add -n "Security jobs" -g 1203...@g.us --field cybersecurity --location remote"""
    from jobapply.db import session_scope
    from jobapply.pipeline.feeds import add_feed, filters_summary

    filters = {"role_selection": _check_fields(field or []), "roles": role or [], "keywords_include": include or [],
               "keywords_exclude": exclude or [], "locations": location or [], "sources": source or [],
               "max_age_hours": hours, "remote_only": remote_only, "min_salary": min_salary}
    try:
        with session_scope() as session:
            feed = add_feed(session, name=name, group_jid=group, filters=filters, max_per_run=max_per_run,
                            enabled=not disabled)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"added feed {feed['id']}: {feed['name']} ({'on' if feed['enabled'] else 'off'})")
    typer.echo(f"  {filters_summary(feed)}")


def _toggle(feed_id: str, enabled: bool) -> None:
    from jobapply.db import session_scope
    from jobapply.pipeline.feeds import set_enabled

    with session_scope() as session:
        _feed_or_exit(session, feed_id)
        feed = set_enabled(session, feed_id, enabled)
    typer.echo(f"feed {feed['id']} ({feed['name']}) is now {'on' if feed['enabled'] else 'off'}")


@feeds_app.command()
def enable(feed_id: str = typer.Argument(..., help="Feed id from `feeds list`")) -> None:
    """Switch a feed on."""
    _toggle(feed_id, True)


@feeds_app.command()
def disable(feed_id: str = typer.Argument(..., help="Feed id from `feeds list`")) -> None:
    """Switch a feed off (kept, nothing posted until enabled again)."""
    _toggle(feed_id, False)


@feeds_app.command()
def remove(feed_id: str = typer.Argument(..., help="Feed id from `feeds list`")) -> None:
    """Delete a feed and its posted-job records."""
    from jobapply.db import session_scope
    from jobapply.pipeline.feeds import remove_feed

    with session_scope() as session:
        feed = _feed_or_exit(session, feed_id)
        remove_feed(session, feed_id)
    typer.echo(f"removed feed {feed['id']} ({feed['name']})")


__all__ = ["feeds_app"]

