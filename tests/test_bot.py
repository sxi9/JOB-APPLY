"""The WhatsApp subscriber bot's conversation engine (bot/whatsapp.py) and the group commands (bot/groups.py).
No network: every test passes a fake `send` to `handle`; the conftest autouse fixture keeps the live bridge
out of `send_to` as well."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from jobapply.bot import groups as groups_mod
from jobapply.bot import whatsapp as bot
from jobapply.bot.whatsapp import (
    FIELDS_RETRY,
    HELP,
    STOPPED,
    UNKNOWN,
    WELCOME,
    handle,
    parse_fields,
    parse_location,
)
from jobapply.models import Job, Role, Subscriber, User
from jobapply.pipeline.feeds import add_feed, load_feeds

JID = "919876543210@s.whatsapp.net"
OTHER = "919876500000@s.whatsapp.net"
GROUP = "120363099999999999@g.us"
NOW = datetime(2026, 11, 5, 8, 30, tzinfo=UTC)


class FakeSend:
    def __init__(self, ok=True):
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    def __call__(self, to, text):
        self.sent.append((to, text))
        return self.ok


@pytest.fixture(autouse=True)
def _fresh_throttles():
    bot.reset_throttles()
    groups_mod.reset_throttles()
    yield
    bot.reset_throttles()
    groups_mod.reset_throttles()


def say(session, text, send, *, jid=JID, now=NOW, group=False, sender=None, push_name=None):
    inbound = {"id": "m1", "chat": jid, "from": sender or jid, "pushName": push_name, "text": text,
               "isGroup": group, "ts": now.isoformat()}
    return handle(session, inbound, send=send, now=now)


def sub_of(session, jid=JID) -> Subscriber | None:
    session.expire_all()
    return session.exec(select(Subscriber).where(Subscriber.phone_jid == jid)).first()


# ----------------------------------------------------------------- parsing


def test_parse_fields_numbers_and_names():
    assert parse_fields("1, 9") == ["cybersecurity", "cloud"]
    assert parse_fields("1 4 7") == ["cybersecurity", "data", "qa"]
    assert parse_fields("3.") == ["devops"]
    assert parse_fields("16") == ["intern"]
    assert parse_fields("cyber and data science") == ["cybersecurity", "data"]
    assert parse_fields("Cloud Engineering") == ["cloud"]
    assert parse_fields("internships") == ["intern"]
    assert parse_fields("student") == ["intern"]
    assert parse_fields("2, cloud, 2") == ["software_engineering", "cloud"]
    assert parse_fields("0, 99, astrology") == []
    assert parse_fields("") == []


def test_parse_location():
    assert parse_location("Pune") == ("Pune", False)
    assert parse_location("remote") == ("", True)
    assert parse_location("Bengaluru, remote") == ("Bengaluru", True)
    assert parse_location("remote or Pune") == ("Pune", True)
    assert parse_location("any") == ("", False)
    assert parse_location("Anywhere") == ("", False)
    assert parse_location("work from home") == ("", True)


# ----------------------------------------------------------------- the conversation


def test_full_happy_path(session):
    send = FakeSend()
    # a stranger's ordinary text gets nothing and creates nothing (the bridge is a real phone)
    assert say(session, "hello there, is this Ravi?", send) == []
    assert sub_of(session) is None and send.sent == []

    replies = say(session, "Start", send)
    assert replies == [WELCOME] and send.sent == [(JID, WELCOME)]
    assert "automated" in WELCOME and "*STOP*" in WELCOME and WELCOME.count("\U0001f44b") <= 1
    sub = sub_of(session)
    assert sub.state == "asked_name" and sub.active and sub.last_message_at == NOW

    (reply,) = say(session, "Priya", send)
    assert reply.startswith("Thanks, Priya! Which career fields")
    assert "1. Cybersecurity" in reply and "9. Cloud Engineering" in reply and "16. Internships (students)" in reply
    assert sub_of(session).state == "asked_fields" and sub_of(session).name == "Priya"

    (reply,) = say(session, "nope", send)
    assert reply == FIELDS_RETRY and sub_of(session).state == "asked_fields"

    (reply,) = say(session, "1, 9", send)
    assert reply.startswith("Got it: *Cybersecurity, Cloud Engineering*. Where do you want jobs?")
    assert sub_of(session).fields == ["cybersecurity", "cloud"] and sub_of(session).state == "asked_location"

    (reply,) = say(session, "Pune, remote", send)
    assert reply.startswith("You're all set, Priya!") and "Location: *Pune, remote*" in reply
    assert "STOP" in reply and reply.count("✅") == 1
    sub = sub_of(session)
    assert sub.state == "active" and sub.location == "Pune" and sub.remote_ok and sub.active
    assert len(send.sent) == 5


def _active(session, send, jid=JID):
    for text in ("start", "Priya", "1", "Pune"):
        say(session, text, send, jid=jid)
    send.sent.clear()
    return sub_of(session, jid)


def test_every_command(session):
    send = FakeSend()
    _active(session, send)
    (reply,) = say(session, "STATUS", send)
    assert "*Your subscription*" in reply and "Fields: Cybersecurity" in reply and "Location: Pune" in reply
    assert "Alerts: on" in reply
    assert say(session, "help", send) == [HELP]
    # FIELDS re-picks and returns to active
    (reply,) = say(session, "fields", send)
    assert "1. Cybersecurity" in reply and sub_of(session).state == "edit_fields"
    (reply,) = say(session, "data and 16", send)
    assert reply.startswith("Updated. Fields: *Data & AI, Internships (students)*")
    assert sub_of(session).state == "active" and sub_of(session).fields == ["data", "intern"]
    # LOCATION
    (reply,) = say(session, "Location", send)
    assert "Where do you want jobs?" in reply and sub_of(session).state == "edit_location"
    (reply,) = say(session, "remote", send)
    assert reply.startswith("Updated. Location: *remote*") and sub_of(session).location == ""
    assert sub_of(session).remote_ok and sub_of(session).state == "active"
    (reply,) = say(session, "location", send)
    (reply,) = say(session, "any", send)
    assert "Location: *anywhere*" in reply and not sub_of(session).remote_ok
    # STOP / RESUME
    assert say(session, "stop!", send) == [STOPPED] and sub_of(session).active is False
    (reply,) = say(session, "status", send)
    assert "paused (send RESUME)" in reply
    (reply,) = say(session, "RESUME", send)
    assert reply.startswith("Alerts resumed.") and sub_of(session).active is True
    assert say(session, "unsubscribe", send) == [STOPPED]
    # START on a paused, finished subscription switches it back on; on an active one it just restates
    (reply,) = say(session, "hi", send)
    assert reply.startswith("Welcome back!") and sub_of(session).active
    (reply,) = say(session, "hello", send)
    assert reply.startswith("You're already subscribed.")


def test_start_resumes_an_unfinished_signup(session):
    send = FakeSend()
    say(session, "start", send)
    say(session, "Ravi", send)
    (reply,) = say(session, "start", send)
    assert reply.startswith("Let's finish your subscription.") and "1. Cybersecurity" in reply
    assert sub_of(session).state == "asked_fields"
    say(session, "stop", send)
    (reply,) = say(session, "resume", send)
    assert reply.startswith("Alerts are on. Let's finish") and sub_of(session).active
    (reply,) = say(session, "status", send)
    assert "not complete yet" in reply


def test_hi_and_hello_begin_and_the_push_name_is_not_trusted(session):
    send = FakeSend()
    assert say(session, "hi", send, push_name="Spammer") == [WELCOME]
    assert sub_of(session).name == "" and sub_of(session).state == "asked_name"
    assert say(session, "Hello", send, jid=OTHER) == [WELCOME]
    assert say(session, "my name is Arjun Rao", send) and sub_of(session).name == "Arjun Rao"


def test_unknown_text_help_is_throttled(session):
    send = FakeSend()
    _active(session, send)
    assert say(session, "thanks!", send) == [UNKNOWN]
    assert say(session, "??", send, now=NOW + timedelta(minutes=5)) == []
    assert say(session, "??", send, now=NOW + timedelta(minutes=11)) == [UNKNOWN]
    # commands still work meanwhile, and a paused subscriber is told so
    say(session, "stop", send, now=NOW + timedelta(minutes=12))
    (reply,) = say(session, "hmm", send, now=NOW + timedelta(minutes=30))
    assert reply.startswith(UNKNOWN) and "RESUME" in reply
    # per number: another subscriber has their own budget
    _active(session, send, jid=OTHER)
    assert say(session, "thanks!", send, jid=OTHER, now=NOW + timedelta(minutes=31)) == [UNKNOWN]


def test_rate_limit_per_number_per_hour(session):
    send = FakeSend()
    _active(session, send)  # 4 texts used
    for i in range(16):
        assert say(session, "status", send, now=NOW + timedelta(seconds=i)) != []
    # the 21st text inside the hour is dropped silently, another number is not
    assert say(session, "status", send, now=NOW + timedelta(minutes=30)) == []
    assert say(session, "start", send, jid=OTHER, now=NOW + timedelta(minutes=30)) == [WELCOME]
    # an hour later the budget is back
    assert say(session, "status", send, now=NOW + timedelta(hours=1, seconds=1)) != []


def test_group_messages_and_empty_or_own_messages_are_ignored(session):
    send = FakeSend()
    assert say(session, "start", send, jid=GROUP, group=True, sender=JID) == []
    assert say(session, "!jobs", send, jid=GROUP, group=True, sender=JID) == []  # no feed for this group
    assert handle(session, {"chat": JID, "from": "me", "fromMe": True, "text": "start", "isGroup": False},
                  send=send) == []
    assert handle(session, {"chat": JID, "from": JID, "text": "", "isGroup": False}, send=send) == []
    assert handle(session, {"chat": "status@broadcast", "from": JID, "text": "start"}, send=send) == []
    assert handle(session, "garbage", send=send) == []
    assert send.sent == [] and session.exec(select(Subscriber)).all() == []


def test_failed_sends_are_not_counted_and_phone_wins_over_chat(session):
    send = FakeSend(ok=False)
    assert handle(session, {"chat": "123@lid", "phone": JID, "from": "123@lid", "text": "start"}, send=send) == []
    assert send.sent == [(JID, WELCOME)]
    assert sub_of(session).state == "asked_name"


# ----------------------------------------------------------------- group commands


def _job(session, title, *, hours_ago=1, location="Bengaluru", remote=False, key=None, description=""):
    when = NOW - timedelta(hours=hours_ago)
    job = Job(dedupe_key=key or f"g-{title}-{location}", title=title, company="Acme Inc", location=location,
              remote=remote, posted_at=when, first_seen_at=when, apply_url="https://apply.example/x",
              source="greenhouse", description=description)
    session.add(job)
    session.commit()
    return job


def _feed(session, **kw):
    filters = {"role_selection": ["cybersecurity:*"], "max_age_hours": 24, **kw.pop("filters", {})}
    feed = add_feed(session, name="Security jobs", group_jid=GROUP, filters=filters, **kw)
    session.commit()
    return feed


def test_group_without_a_feed_or_without_bang_is_ignored(session):
    send = FakeSend()
    _job(session, "SOC Analyst")
    assert say(session, "!jobs", send, jid=GROUP, group=True, sender=JID) == []
    _feed(session)
    assert say(session, "jobs please", send, jid=GROUP, group=True, sender=JID) == []
    assert say(session, "!weather", send, jid=GROUP, group=True, sender=JID) == []
    assert say(session, "!jobs", send, jid="120363000000000001@g.us", group=True, sender=JID) == []
    assert send.sent == []


def test_group_jobs_parsing_and_reply(session):
    send = FakeSend()
    _feed(session)
    soc = _job(session, "SOC Analyst", location="Bengaluru")
    _job(session, "SOC Analyst", location="Pune", key="soc-pune")
    _job(session, "Cloud Engineer", location="Bengaluru")
    _job(session, "Security Engineer", location="Bengaluru", hours_ago=30)
    parsed = groups_mod.parse_args(["cyber", "bengaluru", "24h"])
    assert parsed == {"fields": ["cybersecurity"], "location": "bengaluru", "hours": 24}
    assert groups_mod.parse_args(["3d", "Navi", "Mumbai", "9"]) == {"fields": ["cloud"], "location": "Navi Mumbai",
                                                                      "hours": 72}
    (reply,) = say(session, "!jobs cyber bengaluru 24h", send, jid=GROUP, group=True, sender=JID)
    assert send.sent == [(GROUP, reply)]
    assert reply.startswith("\U0001f4e2 Security jobs — 1 job in Cybersecurity · around bengaluru · last 24 h — ")
    assert "*SOC Analyst* — Acme Inc · Bengaluru" in reply and "Pune" not in reply and "Cloud" not in reply
    assert "https://apply.example/x" in reply and "\U0001f4cb" not in reply
    assert soc.id is not None
    # a wider window finds the older one; the feed's own filters are the default
    groups_mod.reset_throttles()
    (reply,) = say(session, "!jobs 48h", send, jid=GROUP, group=True, sender=JID)
    assert reply.startswith("\U0001f4e2 Security jobs — 3 jobs last 48 h — ") and "Security Engineer" in reply
    # !remote, !intern, !latest, !help
    _job(session, "Security Analyst", location="Remote", remote=True, key="sec-remote")
    _job(session, "SOC Analyst Intern", location="Bengaluru", key="sec-intern")
    groups_mod.reset_throttles()
    (reply,) = say(session, "!remote", send, jid=GROUP, group=True, sender=JID)
    assert "1 job remote" in reply and "Security Analyst" in reply
    groups_mod.reset_throttles()
    (reply,) = say(session, "!intern bengaluru", send, jid=GROUP, group=True, sender=JID)
    assert "1 job internships" in reply and "SOC Analyst Intern" in reply and "*SOC Analyst* —" not in reply
    groups_mod.reset_throttles()
    (reply,) = say(session, "!latest", send, jid=GROUP, group=True, sender=JID)
    assert "5 jobs newest" in reply
    groups_mod.reset_throttles()
    assert say(session, "!help", send, jid=GROUP, group=True, sender=JID) == [groups_mod.HELP]
    groups_mod.reset_throttles()
    (reply,) = say(session, "!jobs marketing", send, jid=GROUP, group=True, sender=JID)
    assert reply.startswith("\U0001f4e2 No jobs in Marketing")


def test_group_reply_never_exceeds_8_jobs_or_3500_chars(session):
    send = FakeSend()
    _feed(session)
    for i in range(12):
        _job(session, f"Security Operations Analyst {i} " + "with a very long descriptive title " * 3, key=f"big{i}")
    (reply,) = say(session, "!jobs", send, jid=GROUP, group=True, sender=JID)
    assert len(reply) <= groups_mod.MAX_CHARS and reply.count("https://apply.example/x") <= 8
    assert reply.count("https://apply.example/x") >= 1


def test_group_rate_limit_one_command_per_minute(session):
    send = FakeSend()
    _feed(session)
    assert say(session, "!help", send, jid=GROUP, group=True, sender=JID) == [groups_mod.HELP]
    assert say(session, "!help", send, jid=GROUP, group=True, sender=OTHER, now=NOW + timedelta(seconds=30)) == []
    assert say(session, "!help", send, jid=GROUP, group=True, sender=JID, now=NOW + timedelta(seconds=61)) != []
    assert len(send.sent) == 2


def test_group_feed_commands_are_admin_only(session, monkeypatch):
    send = FakeSend()
    _feed(session)
    monkeypatch.setattr(groups_mod, "bridge_me_digits", lambda: "919000000001")
    session.add(User(username="boss", password_hash="x", role=Role.admin,
                     preferences={"whatsapp_number": "+91 90000 00002"}))
    session.commit()
    owner, admin, stranger = "919000000001@s.whatsapp.net", "919000000002@s.whatsapp.net", JID
    # anyone may read the status
    (reply,) = say(session, "!feed status", send, jid=GROUP, group=True, sender=stranger)
    assert reply.startswith("*Feed Security jobs* (on)") and "fields: cybersecurity:*" in reply
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed hours 12", send, jid=GROUP, group=True, sender=stranger)
    assert reply == "Sorry, only an admin can change this feed."
    assert load_feeds(session)[0]["filters"]["max_age_hours"] == 24
    # the owner's number and an admin user's number may edit
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed hours 12", send, jid=GROUP, group=True, sender=owner)
    assert reply.startswith("Feed updated.") and load_feeds(session)[0]["filters"]["max_age_hours"] == 12
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed location Pune, Remote", send, jid=GROUP, group=True, sender=admin)
    assert load_feeds(session)[0]["filters"]["locations"] == ["Pune", "Remote"]
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed field cloud", send, jid=GROUP, group=True, sender=admin)
    assert load_feeds(session)[0]["filters"]["role_selection"] == ["cloud:*"] and "fields: cloud:*" in reply
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed field 16", send, jid=GROUP, group=True, sender=admin)
    assert "use !intern" in reply and load_feeds(session)[0]["filters"]["role_selection"] == ["cloud:*"]
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed pause", send, jid=GROUP, group=True, sender=owner)
    assert "(paused)" in reply and not load_feeds(session)[0]["enabled"]
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed resume", send, jid=GROUP, group=True, sender=owner)
    assert "(on)" in reply and load_feeds(session)[0]["enabled"]
    groups_mod.reset_throttles()
    (reply,) = say(session, "!feed bogus", send, jid=GROUP, group=True, sender=owner)
    assert reply.startswith("Usage: !feed")



def test_owner_group_commands_arrive_as_from_me_and_count_as_admin(session, monkeypatch):
    """The owner types in the group from the phone the bridge is linked to: the record is fromMe, and it must be
    answered (and allowed to edit the feed) instead of being ignored."""
    send = FakeSend()
    _feed(session)
    monkeypatch.setattr("jobapply.notify.bridge_me_digits", lambda: "919000000001")
    inbound = {"id": "m1", "chat": GROUP, "from": "me", "fromMe": True, "text": "!help", "isGroup": True,
               "ts": NOW.isoformat()}
    (reply,) = handle(session, inbound, send=send, now=NOW)
    assert send.sent == [(GROUP, reply)] and "!jobs" in reply
    later = NOW + timedelta(minutes=2)
    inbound = {**inbound, "id": "m2", "text": "!feed status", "ts": later.isoformat()}
    (reply,) = handle(session, inbound, send=send, now=later)
    assert "not an admin" not in reply.lower() and "Security jobs" in reply
