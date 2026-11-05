"""Group commands: a WhatsApp group that has a configured feed (pipeline/feeds.py, `group_jid`) answers texts
that start with `!`; every other group message - and every message in a group without a feed - is ignored.

    !jobs [field] [location] [Nh|Nd]   up to 8 matching jobs from the pool (default: the feed's own filters,
                                       last 24 h); the words resolve like the DM bot's (field label words or
                                       numbers, `24h` / `3d`, the rest is the location)
    !intern [location]                 internships only (seniority intern) inside the feed's filters
    !remote [field]                    remote jobs
    !latest                            the newest 5 jobs matching the feed (any age)
    !help                              this list
    !feed status | location <text> | field <name|number> | hours <N> | pause | resume
                                       admin only: edit that group's feed in place

Admin = the sender is the bridge's own number (`notify.bridge_me_digits`) or the `whatsapp_number` of an admin
dashboard user. Rate limit: one command per group per minute, extras dropped silently (per process,
`reset_throttles`). A reply never carries more than `MAX_JOBS` (8) jobs or ~`MAX_CHARS` (3500) characters and
goes back to the same group through `send(group_jid, text)`.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlmodel import Session, select

from jobapply.bot.dispatch import pool_jobs
from jobapply.bot.whatsapp import INTERN_LABEL, field_list_text, field_options, parse_fields
from jobapply.logs import get_logger
from jobapply.models import INTERN_FIELD, Job, Role, User, utcnow
from jobapply.notify import bridge_me_digits, mask_target, send_to
from jobapply.pipeline.alerts import IST, _aware, job_block
from jobapply.pipeline.feeds import (
    MEGAPHONE,
    NAME_MAX,
    clean_title,
    feed_filters,
    filters_summary,
    load_feeds,
    update_feed,
)
from jobapply.pipeline.filters import UserFilters

log = get_logger(__name__)

RATE_SECONDS = 60.0
MAX_JOBS = 8
LATEST_N = 5
MAX_CHARS = 3500
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 30
HOURS_RE = re.compile(r"^(\d{1,3})\s*(h|hr|hrs|hours?|d|days?)$", re.I)
ADMIN_SUBCOMMANDS = ("status", "location", "field", "hours", "pause", "resume")

HELP = (
    "*Group commands*\n"
    "!jobs [field] [city] [24h|3d] - matching jobs from the pool\n"
    "!intern [city] - internships only\n"
    "!remote [field] - remote jobs\n"
    "!latest - the newest 5 jobs for this group\n"
    "!help - this list\n"
    "Admin: !feed status | location <city> | field <name> | hours <N> | pause | resume"
)

_last_command: dict[str, float] = {}


def reset_throttles() -> None:
    _last_command.clear()


def rate_limited(group_jid: str, now: datetime) -> bool:
    stamp = now.timestamp()
    if stamp - _last_command.get(group_jid, -RATE_SECONDS) < RATE_SECONDS:
        return True
    _last_command[group_jid] = stamp
    return False


# ----------------------------------------------------------------- who may edit a feed


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "").partition("@")[0] if ch.isdigit())


def admin_digits(session: Session) -> set[str]:
    """Phone digits allowed to run `!feed ...`: the bridge's own number and every admin user's
    `whatsapp_number` preference."""
    out = {d for d in [_digits(bridge_me_digits())] if d}
    for user in session.exec(select(User).where(User.role == Role.admin, User.is_active)).all():
        d = _digits(str((user.preferences or {}).get("whatsapp_number") or ""))
        if d:
            out.add(d)
    return out


def is_admin_sender(session: Session, sender: str) -> bool:
    d = _digits(sender)
    return bool(d) and d in admin_digits(session)


# ----------------------------------------------------------------- parsing


def parse_args(args: list[str]) -> dict:
    """`["cyber", "bengaluru", "24h"]` -> `{"fields": ["cybersecurity"], "location": "bengaluru", "hours": 24}`
    (any order; `3d` = 72 h; a field number works too; `hours` None when not given)."""
    fields: list[str] = []
    location: list[str] = []
    hours: int | None = None
    for raw in args:
        tok = raw.strip(" ,.")
        if not tok:
            continue
        m = HOURS_RE.match(tok)
        if m:
            n = int(m.group(1))
            hours = min(MAX_HOURS, n * 24 if m.group(2).lower().startswith("d") else n)
            continue
        keys = parse_fields(tok)
        if keys and (tok.isdigit() or len(tok) >= 2):
            fields.extend(k for k in keys if k not in fields)
            continue
        location.append(tok)
    return {"fields": fields, "location": " ".join(location).strip(), "hours": hours}


def _apply_args(f: UserFilters, parsed: dict) -> tuple[UserFilters, bool]:
    """The feed's filters with the command's overrides; the bool says whether `intern` was asked for."""
    data = f.model_dump()
    data.pop("keywords_include_text", None)
    data["keywords_include"] = list(f.keywords_include)
    intern = INTERN_FIELD in parsed["fields"]
    roles = [f"{k}:*" for k in parsed["fields"] if k != INTERN_FIELD]
    if roles:
        data["role_selection"] = roles
        data["roles"] = []
    if parsed["location"]:
        data["locations"] = [parsed["location"]]
    if parsed["hours"] is not None:
        data["max_age_hours"] = parsed["hours"]
    if intern:
        data["seniority"] = ["intern"]
    return UserFilters.model_validate(data), intern


# ----------------------------------------------------------------- replies


def _header(feed: dict, count: int, what: str, now: datetime) -> str:
    noun = "job" if count == 1 else "jobs"
    stamp = _aware(now).astimezone(IST).strftime("%H:%M IST")
    return f"{MEGAPHONE} {clean_title(feed['name'], NAME_MAX)} — {count} {noun} {what} — {stamp}"


def jobs_reply(feed: dict, jobs: list[Job], what: str, now: datetime, *, limit: int = MAX_JOBS) -> str:
    """Header + feed-style job blocks (no dashboard link), at most `limit` jobs and `MAX_CHARS` characters."""
    jobs = jobs[:limit]
    if not jobs:
        return f"{MEGAPHONE} No jobs {what}. Try !jobs <field> <city> 48h or !help."
    while jobs:
        body = "\n\n".join(job_block(j, now, dashboard_link=False) for j in jobs)
        text = _header(feed, len(jobs), what, now) + "\n\n" + body
        if len(text) <= MAX_CHARS or len(jobs) == 1:
            return text
        jobs = jobs[:-1]
    return ""


def _what(parsed: dict, hours: int, *, intern: bool = False, remote: bool = False) -> str:
    bits = []
    if intern:
        bits.append("internships")
    if remote:
        bits.append("remote")
    labels = dict(field_options())
    names = [labels[k] for k in parsed["fields"] if k in labels and k != INTERN_FIELD]
    if names:
        bits.append("in " + ", ".join(names))
    if parsed["location"]:
        bits.append(f"around {parsed['location']}")
    bits.append(f"last {hours} h" if hours else "any age")
    return " · ".join(bits)


def feed_status_text(feed: dict) -> str:
    state = "on" if feed["enabled"] else "paused"
    return (f"*Feed {feed['name']}* ({state})\n{filters_summary(feed)}\n"
            f"max {feed['max_per_run']}/run · posted {feed.get('posted_count', 0)} · "
            f"last run {feed.get('last_run_at') or 'never'}")


# ----------------------------------------------------------------- the handler


def feed_for_group(session: Session, group_jid: str) -> dict | None:
    """The feed of this group (an enabled one first); None when the group has no feed."""
    feeds = [f for f in load_feeds(session) if f["group_jid"] == group_jid]
    feeds.sort(key=lambda f: not f["enabled"])
    return feeds[0] if feeds else None


def handle_group(session: Session, inbound: dict, *, send=send_to, now: datetime | None = None) -> list[str]:
    """Answer one group text (see the module docstring); [] when the group has no feed, the text is not a
    `!` command, or the group is rate limited."""
    now = _aware(now) or utcnow()
    text = str(inbound.get("text") or "").strip()
    group_jid = str(inbound.get("chat") or "").strip()
    if not text.startswith("!") or not group_jid.endswith("@g.us") or inbound.get("fromMe"):
        return []
    parts = text[1:].split()
    if not parts:
        return []
    cmd, args = parts[0].lower(), parts[1:]
    if cmd not in ("jobs", "intern", "interns", "internships", "remote", "latest", "help", "feed"):
        return []
    feed = feed_for_group(session, group_jid)
    if feed is None:
        return []
    if rate_limited(group_jid, now):
        log.info("group command rate limited", group=mask_target(group_jid))
        return []
    sender = str(inbound.get("from") or "")
    try:
        reply = _answer(session, feed, cmd, args, sender, now)
    except Exception as exc:  # noqa: BLE001 - a bad command must never raise into the webhook
        log.warning("group command failed", group=mask_target(group_jid), error=f"{type(exc).__name__}: {exc}"[:300])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        reply = ""
    if not reply:
        return []
    if send(group_jid, reply) is False:
        log.warning("group command reply not delivered", group=mask_target(group_jid))
        return []
    log.info("group command answered", group=mask_target(group_jid), command=cmd)
    return [reply]


def _answer(session: Session, feed: dict, cmd: str, args: list[str], sender: str, now: datetime) -> str:
    base = feed_filters(feed)
    if cmd == "help":
        return HELP
    if cmd == "latest":
        jobs = pool_jobs(session, base.model_copy(update={"max_age_hours": 0}), now, hours=0, limit=LATEST_N)
        return jobs_reply(feed, jobs, "newest", now, limit=LATEST_N)
    if cmd == "feed":
        return _feed_command(session, feed, args, sender)
    parsed = parse_args(args)
    if cmd in ("intern", "interns", "internships"):
        parsed["fields"] = [INTERN_FIELD] + [k for k in parsed["fields"] if k != INTERN_FIELD]
    f, intern = _apply_args(base, parsed)
    hours = parsed["hours"] if parsed["hours"] is not None else DEFAULT_HOURS
    remote = cmd == "remote"
    if remote:
        f = f.model_copy(update={"remote_only": True})
    jobs = pool_jobs(session, f, now, hours=hours, require_intern=intern)
    return jobs_reply(feed, jobs, _what(parsed, hours, intern=intern, remote=remote), now)


def _feed_command(session: Session, feed: dict, args: list[str], sender: str) -> str:
    sub = args[0].lower() if args else "status"
    rest = args[1:]
    if sub not in ADMIN_SUBCOMMANDS:
        return "Usage: !feed status | location <city> | field <name|number> | hours <N> | pause | resume"
    if sub == "status":
        return feed_status_text(feed)
    if not is_admin_sender(session, sender):
        log.info("group feed edit refused (not an admin)", feed=feed["name"], sender=mask_target(sender))
        return "Sorry, only an admin can change this feed."
    filters = dict(feed["filters"])
    if sub == "location":
        value = " ".join(rest).strip()
        if not value:
            return "Usage: !feed location <city>[, <city>] (or `any`)"
        filters["locations"] = [] if value.lower() in ("any", "anywhere", "all") else \
            [p.strip() for p in value.split(",") if p.strip()]
        updated = update_feed(session, feed["id"], filters=filters)
    elif sub == "field":
        keys = parse_fields(" ".join(rest))
        roles = [f"{k}:*" for k in keys if k != INTERN_FIELD]
        if not roles:
            if INTERN_FIELD in keys:
                return f"{INTERN_LABEL} is not a feed field; use !intern for internships."
            return "Which field? Reply !feed field <name|number>:\n" + field_list_text()
        filters["role_selection"] = roles
        updated = update_feed(session, feed["id"], filters=filters)
    elif sub == "hours":
        try:
            n = int(rest[0])
        except (IndexError, ValueError):
            return "Usage: !feed hours <N> (0 = any age)"
        filters["max_age_hours"] = max(0, min(MAX_HOURS, n))
        updated = update_feed(session, feed["id"], filters=filters)
    elif sub == "pause":
        updated = update_feed(session, feed["id"], enabled=False)
    else:  # resume
        updated = update_feed(session, feed["id"], enabled=True)
    session.commit()
    updated["posted_count"] = feed.get("posted_count", 0)
    updated["last_run_at"] = feed.get("last_run_at")
    return "Feed updated.\n" + feed_status_text(updated)


__all__ = ["RATE_SECONDS", "MAX_JOBS", "MAX_CHARS", "LATEST_N", "DEFAULT_HOURS", "HELP", "reset_throttles",
           "rate_limited", "admin_digits", "is_admin_sender", "parse_args", "jobs_reply", "feed_status_text",
           "feed_for_group", "handle_group"]
