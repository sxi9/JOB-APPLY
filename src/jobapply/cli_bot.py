"""`jobapply bot ...`: the WhatsApp subscriber bot (bot/whatsapp.py, bot/dispatch.py).

Exports `bot_app`; `cli.py` registers it with `app.add_typer(bot_app, name="bot")`. `subscribers` lists the
rows (numbers masked), `dispatch [--dry-run]` posts (or prints) the new matching jobs per subscriber and
`simulate --from <jid> "<text>"` runs one inbound text through the conversation engine with a fake sender
that prints the replies: nothing is ever sent, but the subscriber row changes exactly as in a real chat.
See docs/NOTIFICATIONS.md, "Subscriber bot".
"""

from __future__ import annotations

import typer

from jobapply.logs import get_logger

bot_app = typer.Typer(
    no_args_is_help=True,
    help="WhatsApp subscriber bot: list subscribers, dispatch new jobs to them, simulate a conversation.",
)
log = get_logger("jobapply.cli.bot")


@bot_app.command()
def subscribers() -> None:
    """Every subscriber: id, on/off, masked number, name, state, fields, location, last message."""
    from sqlmodel import select

    from jobapply.bot.whatsapp import field_labels, location_label
    from jobapply.db import session_scope
    from jobapply.models import Subscriber
    from jobapply.notify import mask_target

    with session_scope() as session:
        rows = session.exec(select(Subscriber).order_by(Subscriber.id)).all()
        if not rows:
            typer.echo("no subscribers yet (people join by sending START to the bridge's number)")
            return
        for s in rows:
            state = "on " if s.active else "off"
            last = s.last_message_at.strftime("%Y-%m-%d %H:%M") if s.last_message_at else "never"
            typer.echo(f"{s.id:>4}  [{state}]  {mask_target(s.phone_jid)}  {s.name or '-'}  ({s.state})")
            typer.echo(f"            fields: {', '.join(field_labels(s.fields)) or '-'}  ·  "
                       f"location: {location_label(s)}  ·  last message {last}")


@bot_app.command()
def dispatch(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the messages; send nothing, record nothing"),
) -> None:
    """Post each active subscriber's new matches (jobs first seen in the last 24 h, not yet posted)."""
    from jobapply.bot.dispatch import notify_subscribers
    from jobapply.db import session_scope

    with session_scope() as session:
        out = notify_subscribers(session, None, dry_run=dry_run)
    if out.get("disabled"):
        typer.echo("subscriber bot is disabled (JOBAPPLY_WHATSAPP_BOT_ENABLED=false); nothing sent")
        return
    for r in out["subscribers"]:
        head = f"--- {r['target']} {r['name'] or ''} (#{r['id']}): "
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
        typer.echo(f"{sum(len(r['messages']) for r in out['subscribers'])} message(s) would be sent "
                   "(dry run: nothing sent, nothing recorded)")
    else:
        typer.echo(f"{out['messages']} message(s) sent, {out['posted']} job(s) posted")


@bot_app.command()
def simulate(
    text: str = typer.Argument(..., help="The inbound text, e.g. \"start\" or \"1, 9\""),
    sender: str = typer.Option(..., "--from", "-f", help="Sender jid (919876543210@s.whatsapp.net) or group jid"),
    push_name: str = typer.Option("", "--name", help="The sender's WhatsApp push name"),
) -> None:
    """Run one inbound text through the bot with a fake sender: replies are printed, never sent. The
    subscriber row changes as in a real conversation (use a test number)."""
    from jobapply.bot.whatsapp import handle
    from jobapply.db import session_scope
    from jobapply.notify import mask_target

    jid = sender.strip()
    if "@" not in jid:
        jid = "".join(ch for ch in jid if ch.isdigit()) + "@s.whatsapp.net"
    inbound = {"id": "simulated", "chat": jid, "from": jid, "pushName": push_name or None, "text": text,
               "isGroup": jid.endswith("@g.us"), "ts": None}

    def fake_send(to: str, body: str) -> bool:
        typer.echo(f"--- reply to {mask_target(to)} ---")
        typer.echo(body)
        return True

    with session_scope() as session:
        replies = handle(session, inbound, send=fake_send)
    typer.echo(f"{len(replies)} reply(ies); nothing was sent (simulation)")


__all__ = ["bot_app"]
