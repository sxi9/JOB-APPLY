"""The subscriber bot's conversation engine (direct messages).

`handle(session, inbound, *, send=send_to, now=None)` takes one inbound record from the bridge webhook
(`{id, chat, from, phone?, pushName?, text, isGroup, ts}`), updates the `subscribers` row of that number and
sends the replies through `send(jid, text)` (the real `notify.send_to` by default; tests and `jobapply bot
simulate` pass a fake). It returns the replies it sent. Pure apart from the session and the send callable.

States (`Subscriber.state`): `new` -> `asked_name` -> `asked_fields` -> `asked_location` -> `active`; the
FIELDS / LOCATION commands of an active subscriber use `edit_fields` / `edit_location` and return to `active`.
Commands work in any state, case-insensitively: START / HI / HELLO (begin or resume onboarding), STOP (pause,
`active=False`), RESUME, FIELDS (re-pick), LOCATION, STATUS, HELP. A number the bot has never seen gets a reply
only for START / HI / HELLO: the bridge is a real phone, so other texts from strangers are left alone.

Guards: group messages go to `bot.groups` (`!` commands in groups with a feed; everything else ignored); more
than `RATE_LIMIT` inbound texts per number and hour are dropped silently; unknown text from an active
subscriber gets one short help line at most once per `HELP_THROTTLE` seconds. Both counters live in this
process (`reset_throttles()` clears them). Every message is short, plain WhatsApp text (`*bold*`), one emoji
at most, and the first one says that replies are automated and that STOP pauses.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime

from sqlmodel import Session, select

from jobapply.logs import get_logger
from jobapply.models import INTERN_FIELD, Subscriber, utcnow
from jobapply.notify import mask_target, send_to
from jobapply.pipeline.alerts import _aware
from jobapply.roles import all_fields

log = get_logger(__name__)

RATE_LIMIT = 20  # inbound texts per number ...
RATE_WINDOW = 3600.0  # ... per hour
HELP_THROTTLE = 600.0  # seconds between "not sure what you mean" lines per number
NAME_MAX = 60
LOCATION_MAX = 60
MAX_FIELDS = 16
INTERN_LABEL = "Internships (students)"
CHECK = "✅"  # ✅

START_WORDS = frozenset({"start", "hi", "hello", "hey", "join", "subscribe"})
COMMANDS = frozenset({"stop", "resume", "fields", "location", "status", "help", "unsubscribe", "pause"})
ANY_WORDS = frozenset({"any", "anywhere", "all", "everywhere", "no preference", "none", "skip"})
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|home)\b", re.I)

# extra words that pick a field by name, next to its label words and key parts
ALIASES: dict[str, tuple[str, ...]] = {
    "cybersecurity": ("cyber", "security", "infosec", "soc"),
    "software_engineering": ("software", "developer", "dev", "sde", "programming", "backend", "frontend", "fullstack"),
    "devops": ("devops", "sre", "infra", "infrastructure", "platform"),
    "data": ("data", "ai", "ml", "analytics", "analyst", "science"),
    "product": ("product", "pm"),
    "design": ("design", "designer", "ux", "ui"),
    "qa": ("qa", "test", "testing", "tester", "quality"),
    "it_ops": ("it", "sysadmin", "network", "helpdesk", "desktop"),
    "cloud": ("cloud", "aws", "azure", "gcp"),
    "program": ("project", "program", "pmo", "scrum"),
    "sales": ("sales", "bdr", "sdr", "account"),
    "marketing": ("marketing", "seo", "content", "growth"),
    "finance": ("finance", "accounting", "accounts", "ca"),
    "hr": ("hr", "people", "recruiter", "recruiting", "talent"),
    "customer": ("customer", "support", "success", "cs"),
    INTERN_FIELD: ("intern", "interns", "internship", "internships", "student", "students", "fresher", "trainee"),
}


# ----------------------------------------------------------------- fields


def field_options() -> list[tuple[str, str]]:
    """`[(key, label), ...]`: the taxonomy fields in display order plus the internships pseudo-field."""
    return [(f.key, f.label) for f in all_fields()] + [(INTERN_FIELD, INTERN_LABEL)]


def field_list_text() -> str:
    return "\n".join(f"{i}. {label}" for i, (_key, label) in enumerate(field_options(), 1))


def field_labels(keys: list[str] | None) -> list[str]:
    labels = dict(field_options())
    return [labels[k] for k in (keys or []) if k in labels]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _option_words(key: str, label: str) -> set[str]:
    return set(_words(label)) | set(key.split("_")) | set(ALIASES.get(key, ()))


def _match_name(part: str) -> list[str]:
    """Field keys whose label / key / alias words match this comma-separated part (word match; a prefix of at
    least 4 letters also counts: `cyber`, `engineer`), all of them when several fields match."""
    opts = field_options()
    labels = {label.lower(): key for key, label in opts}
    if part.lower().strip() in labels:
        return [labels[part.lower().strip()]]
    words = [w for w in _words(part) if len(w) >= 2]
    if not words:
        return []
    hits: list[str] = []
    for key, label in opts:
        own = _option_words(key, label)
        for w in words:
            if w in own or any(len(w) >= 4 and (o.startswith(w) or w.startswith(o)) for o in own if len(o) >= 4):
                hits.append(key)
                break
    return hits


def parse_fields(text: str) -> list[str]:
    """Field keys from a reply: numbers (`1, 4` / `1 4 7` / `3.`) from the numbered list and / or names
    (`cyber and data science`, `internships`), deduped in the order given; [] when nothing matched."""
    opts = field_options()
    keys: list[str] = []
    for part in re.split(r"[,\n;/]|\band\b|&|\+", str(text or "").lower()):
        part = part.strip(" .*-")
        if not part:
            continue
        if re.fullmatch(r"[\d\s.]+", part):
            for n in re.findall(r"\d+", part):
                i = int(n)
                if 1 <= i <= len(opts):
                    keys.append(opts[i - 1][0])
            continue
        keys.extend(_match_name(part))
    return list(dict.fromkeys(keys))[:MAX_FIELDS]


# ----------------------------------------------------------------- location


def parse_location(text: str) -> tuple[str, bool]:
    """`(location, remote_ok)`: `any` -> `("", False)`; `remote` -> `("", True)`; `Pune, remote` ->
    `("Pune", True)`; `Bengaluru` -> `("Bengaluru", False)`."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip(" .!")
    remote = bool(REMOTE_RE.search(raw))
    rest = REMOTE_RE.sub("", raw)
    rest = re.sub(r"\b(or|and|only|jobs?|in|at)\b", " ", rest, flags=re.I)
    rest = re.sub(r"[\s,/]+", " ", rest).strip(" ,/")
    if not rest or rest.lower() in ANY_WORDS:
        return "", remote
    return rest[:LOCATION_MAX], remote


def location_label(sub: Subscriber) -> str:
    parts = [sub.location] if sub.location else []
    if sub.remote_ok:
        parts.append("remote")
    return ", ".join(parts) or "anywhere"


# ----------------------------------------------------------------- messages

WELCOME = (
    "Hi! I'm the *JOB-APPLY* job alerts bot. I'll message you here when new jobs matching your career field "
    "and location show up.\n\n"
    "Replies here are automated. Send *STOP* at any time to pause the alerts, *HELP* for all commands.\n\n"
    "First, what's your name?"
)
NAME_PROMPT = "What's your name?"
FIELDS_RETRY = "I didn't catch a field. Reply with numbers from the list, e.g. *1, 9*, or a name like *cloud*."
LOCATION_PROMPT = ("Where do you want jobs? Reply with a city (e.g. *Pune*), *remote*, or *any*. "
                   "You can combine them: *Bengaluru, remote*.")
STOPPED = "Alerts paused. Send *RESUME* whenever you want them again."
UNKNOWN = "Not sure what you mean. Commands: STATUS, FIELDS, LOCATION, STOP, HELP."
HELP = (
    "*Commands*\n"
    "STATUS - what you subscribed to\n"
    "FIELDS - pick career fields again\n"
    "LOCATION - change the location\n"
    "STOP - pause the alerts\n"
    "RESUME - continue the alerts\n"
    "HELP - this list\n\n"
    "Replies are automated."
)


def fields_prompt() -> str:
    return ("Which career fields do you want jobs for? Reply with the numbers (e.g. *1, 9*) or type the "
            "names.\n\n" + field_list_text())


def done_text(sub: Subscriber) -> str:
    who = f", {sub.name}" if sub.name else ""
    return (f"You're all set{who}! {CHECK}\n\n"
            f"Fields: *{', '.join(field_labels(sub.fields)) or 'none'}*\n"
            f"Location: *{location_label(sub)}*\n\n"
            "I'll send you new matching jobs as they come in (a few times a day, newest first). "
            "Commands: STATUS, FIELDS, LOCATION, STOP, HELP.")


def status_text(sub: Subscriber) -> str:
    if sub.state != "active":
        return ("*Your subscription* is not complete yet. " + _prompt_for(sub))
    return (f"*Your subscription*\n"
            f"Name: {sub.name or '-'}\n"
            f"Fields: {', '.join(field_labels(sub.fields)) or 'none'}\n"
            f"Location: {location_label(sub)}\n"
            f"Alerts: {'on' if sub.active else 'paused (send RESUME)'}\n\n"
            "Change with FIELDS or LOCATION; STOP pauses.")


def _prompt_for(sub: Subscriber) -> str:
    if sub.state in ("new", "asked_name"):
        return NAME_PROMPT
    if sub.state in ("asked_fields", "edit_fields"):
        return fields_prompt()
    if sub.state in ("asked_location", "edit_location"):
        return LOCATION_PROMPT
    return status_text(sub)


# ----------------------------------------------------------------- throttles (per process)

_inbound: dict[str, deque] = {}
_help_sent: dict[str, float] = {}


def reset_throttles() -> None:
    _inbound.clear()
    _help_sent.clear()


def rate_limited(jid: str, now: datetime) -> bool:
    """True when this number already sent `RATE_LIMIT` texts inside the last hour (this one is dropped)."""
    stamp = now.timestamp()
    q = _inbound.setdefault(jid, deque())
    while q and q[0] <= stamp - RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(stamp)
    return False


def _help_allowed(jid: str, now: datetime) -> bool:
    stamp = now.timestamp()
    if stamp - _help_sent.get(jid, -HELP_THROTTLE) < HELP_THROTTLE:
        return False
    _help_sent[jid] = stamp
    return True


# ----------------------------------------------------------------- the engine


def command_word(text: str) -> str:
    """`Stop!` -> `stop`; `` for anything that is not a single command word."""
    word = re.sub(r"[^a-z]", "", str(text or "").strip().lower()[:24])
    if word in START_WORDS or word in COMMANDS:
        # a longer text that merely starts with a command word is not a command ("hello, my name is ...")
        if len(str(text or "").strip()) <= len(word) + 3:
            return word
    return ""


def inbound_jid(inbound: dict) -> str:
    """The number to answer: the phone jid when the bridge knows it, else the chat jid."""
    for key in ("phone", "chat", "from"):
        value = str(inbound.get(key) or "").strip()
        if value and value != "me":
            return value
    return ""


def clean_name(text: str) -> str:
    name = re.sub(r"\s+", " ", str(text or "")).replace("*", "").replace("_", "").strip(" .!")
    name = re.sub(r"^(i am|i'm|my name is|this is|it's|its)\s+", "", name, flags=re.I)
    return name[:NAME_MAX].strip()


def handle(session: Session, inbound: dict, *, send=send_to, now: datetime | None = None) -> list[str]:
    """Answer one inbound record (see the module docstring). Returns the replies sent; [] when ignored."""
    now = _aware(now) or utcnow()
    if not isinstance(inbound, dict):
        return []
    if inbound.get("isGroup") or str(inbound.get("chat") or "").endswith("@g.us"):
        from jobapply.bot.groups import handle_group

        return handle_group(session, inbound, send=send, now=now)
    if inbound.get("fromMe"):
        return []
    jid = inbound_jid(inbound)
    text = str(inbound.get("text") or "").strip()
    if not jid or not text or jid == "status@broadcast":
        return []
    if rate_limited(jid, now):
        log.info("subscriber bot: rate limited", to=mask_target(jid))
        return []
    cmd = command_word(text)
    sub = session.exec(select(Subscriber).where(Subscriber.phone_jid == jid)).first()
    if sub is None:
        if cmd not in START_WORDS:
            return []  # a stranger's ordinary text: this is a real phone, leave it alone
        sub = Subscriber(phone_jid=jid, state="asked_name", created_at=now, updated_at=now)
        session.add(sub)
        replies = [WELCOME]
        log.info("subscriber bot: new conversation", to=mask_target(jid))
    else:
        replies = _step(sub, text, cmd, now)
    sub.last_message_at = now
    sub.updated_at = now
    session.add(sub)
    session.commit()
    sent: list[str] = []
    for reply in replies:
        if send(jid, reply) is False:
            log.warning("subscriber bot: reply not delivered", to=mask_target(jid))
            continue
        sent.append(reply)
    return sent


def _step(sub: Subscriber, text: str, cmd: str, now: datetime) -> list[str]:
    """The replies for one text of a known subscriber, with the state and field changes applied to `sub`."""
    if cmd in START_WORDS:
        if sub.state == "active":
            if not sub.active:
                sub.active = True
                return ["Welcome back! Alerts are on again.\n\n" + status_text(sub)]
            return ["You're already subscribed.\n\n" + status_text(sub)]
        sub.active = True
        return ["Let's finish your subscription. " + _prompt_for(sub)]
    if cmd in ("stop", "unsubscribe", "pause"):
        sub.active = False
        return [STOPPED]
    if cmd == "resume":
        sub.active = True
        if sub.state == "active":
            return ["Alerts resumed.\n\n" + status_text(sub)]
        return ["Alerts are on. Let's finish your subscription. " + _prompt_for(sub)]
    if cmd == "fields":
        sub.state = "edit_fields" if sub.state in ("active", "edit_location") else "asked_fields"
        return [fields_prompt()]
    if cmd == "location":
        sub.state = "edit_location" if sub.state in ("active", "edit_fields") else "asked_location"
        return [LOCATION_PROMPT]
    if cmd == "status":
        return [status_text(sub)]
    if cmd == "help":
        return [HELP]

    state = sub.state
    if state in ("new", "asked_name"):
        name = clean_name(text)
        if not name:
            return [NAME_PROMPT]
        sub.name = name
        sub.state = "asked_fields"
        return [f"Thanks, {name}! " + fields_prompt()]
    if state in ("asked_fields", "edit_fields"):
        keys = parse_fields(text)
        if not keys:
            return [FIELDS_RETRY]
        sub.fields = keys
        labels = ", ".join(field_labels(keys))
        if state == "edit_fields":
            sub.state = "active"
            return [f"Updated. Fields: *{labels}*. Location: *{location_label(sub)}*."]
        sub.state = "asked_location"
        return [f"Got it: *{labels}*. " + LOCATION_PROMPT]
    if state in ("asked_location", "edit_location"):
        sub.location, sub.remote_ok = parse_location(text)
        sub.state = "active"
        if state == "edit_location":
            return [f"Updated. Location: *{location_label(sub)}*. Fields: "
                    f"*{', '.join(field_labels(sub.fields)) or 'none'}*."]
        return [done_text(sub)]
    # active (or paused) subscriber, free text
    if _help_allowed(sub.phone_jid, now):
        return [UNKNOWN + ("" if sub.active else " Alerts are paused; send RESUME to continue.")]
    return []


__all__ = ["RATE_LIMIT", "HELP_THROTTLE", "WELCOME", "HELP", "STOPPED", "UNKNOWN", "FIELDS_RETRY", "LOCATION_PROMPT",
           "INTERN_LABEL", "field_options", "field_list_text", "field_labels", "fields_prompt", "parse_fields",
           "parse_location", "location_label", "status_text", "done_text", "command_word", "inbound_jid",
           "reset_throttles", "rate_limited", "handle"]
