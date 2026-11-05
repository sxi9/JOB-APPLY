"""Posting new jobs to the WhatsApp subscribers (bot/dispatch.py): matching, dedupe, claim-before-send, the
caps, the collect hook, the migration and the CLI. No network: a spy notifier is installed with set_notifier
(the conftest autouse fixture keeps the live bridge out)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlmodel import select
from typer.testing import CliRunner

from jobapply.bot import dispatch as dispatch_mod
from jobapply.bot.dispatch import (
    MAX_MESSAGES_PER_RUN,
    build_subscriber_messages,
    candidate_jobs,
    notify_subscribers,
    subscriber_filters,
)
from jobapply.collectors.ats import GreenhouseCollector
from jobapply.db import make_engine, upgrade
from jobapply.models import Job, Subscriber, SubscriberPost, User, utcnow
from jobapply.notify import set_notifier
from jobapply.pipeline import collect as collect_mod
from jobapply.pipeline.alerts import BATCH_SIZE
from jobapply.pipeline.collect import run_all

from .helpers import fixture_client

JID = "919876543210@s.whatsapp.net"
OTHER = "919876500000@s.whatsapp.net"
NOW = datetime(2026, 11, 5, 8, 30, tzinfo=UTC)


class Spy:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_all = False
        self.claims_seen: list[int] = []
        self.session = None

    def send(self, user, kind, message):
        return True

    def send_text(self, to, text):
        if self.session is not None:  # the claim must already be committed while the send runs
            self.claims_seen.append(len(self.session.exec(select(SubscriberPost)).all()))
        if self.fail_all:
            return False
        self.sent.append((to, text))
        return True


@pytest.fixture
def spy():
    s = Spy()
    set_notifier(s)
    yield s
    set_notifier(None)


def _job(session, title, *, hours_ago=1, location="Pune", remote=False, key=None, first_seen=None):
    when = NOW - timedelta(hours=hours_ago)
    job = Job(dedupe_key=key or f"d-{title}-{location}", title=title, company="Acme Inc", location=location,
              remote=remote, posted_at=when, first_seen_at=first_seen or when, apply_url="https://apply.example/1",
              source="greenhouse", ats_type="greenhouse")
    session.add(job)
    session.commit()
    return job


def _sub(session, jid=JID, fields=("cybersecurity",), location="Pune", remote_ok=False, active=True, state="active",
         name="Priya"):
    sub = Subscriber(phone_jid=jid, name=name, fields=list(fields), location=location, remote_ok=remote_ok,
                     active=active, state=state)
    session.add(sub)
    session.commit()
    return sub


# ----------------------------------------------------------------- matching


def test_subscriber_filters_mirror_the_choices(session):
    f = subscriber_filters(_sub(session, fields=("cybersecurity", "cloud", "intern", "bogus"), location="Pune",
                                remote_ok=True))
    assert f.role_selection == ["cybersecurity:*", "cloud:*"] and f.seniority == ["intern"]
    assert f.locations == ["Pune", "remote"] and f.remote_only is False and f.max_age_hours == 48
    g = subscriber_filters(_sub(session, jid=OTHER, fields=("data",), location="", remote_ok=False))
    assert g.locations == [] and g.seniority == [] and g.role_selection == ["data:*"]


def test_candidates_by_field_intern_and_location(session):
    soc = _job(session, "SOC Analyst")
    _job(session, "SOC Analyst", location="Bengaluru", key="soc-blr")
    remote = _job(session, "Security Engineer", location="Remote", remote=True)
    intern = _job(session, "Cyber Security Analyst Intern", hours_ago=2)
    data = _job(session, "Data Scientist")
    _job(session, "SOC Analyst", hours_ago=30, key="old")  # outside the 24 h window
    sub = _sub(session)  # cybersecurity, Pune
    assert [j.id for j in candidate_jobs(session, sub, None, NOW)] == [soc.id, intern.id]
    sub.remote_ok = True
    assert {j.id for j in candidate_jobs(session, sub, None, NOW)} == {soc.id, intern.id, remote.id}
    sub.location = ""
    sub.remote_ok = False
    assert len(candidate_jobs(session, sub, None, NOW)) == 4  # anywhere, any age inside the window
    # internships only (no field): titles that read as an internship, in any field
    student = _sub(session, jid=OTHER, fields=("intern",), location="")
    assert [j.id for j in candidate_jobs(session, student, None, NOW)] == [intern.id]
    student.fields = ["data", "intern"]  # a field + internships: that field's jobs, interns welcome
    assert {j.id for j in candidate_jobs(session, student, None, NOW)} == {data.id}
    # explicit ids: only those are considered
    assert [j.id for j in candidate_jobs(session, sub, [data.id, soc.id], NOW)] == [soc.id]
    assert candidate_jobs(session, sub, [], NOW) == []


def test_messages_have_the_subscriber_header_no_dashboard_link_and_the_stop_footer(session):
    jobs = [_job(session, f"SOC Analyst {i}", key=f"m{i}") for i in range(BATCH_SIZE + 1)]
    msgs = build_subscriber_messages(jobs, NOW)
    assert len(msgs) == 2 and msgs[0].startswith(f"\U0001f195 {BATCH_SIZE} new jobs for you — 14:00 IST, 05 Nov\n\n")
    assert msgs[1].startswith("\U0001f195 1 new job for you — ")
    assert "*SOC Analyst 0* — Acme Inc · Pune" in msgs[0] and "https://apply.example/1" in msgs[0]
    assert "\U0001f4cb" not in msgs[0] and msgs[0].endswith("\n\nReply STOP to pause these alerts.")
    assert msgs[0].count("\U0001f195") == 1


# ----------------------------------------------------------------- dispatch


def test_dispatch_dedupes_across_runs_and_claims_before_send(session, spy):
    spy.session = session
    soc = _job(session, "SOC Analyst")
    sub = _sub(session)
    out = notify_subscribers(session, None, now=NOW)
    assert out["posted"] == 1 and out["messages"] == 1 and out["subscribers"][0]["sent"] == 1
    assert spy.sent == [(JID, out["subscribers"][0]["messages"][0])] and spy.claims_seen == [1]
    posts = session.exec(select(SubscriberPost)).all()
    assert len(posts) == 1 and posts[0].subscriber_id == sub.id and posts[0].job_id == soc.id
    # same jobs again: nothing
    assert notify_subscribers(session, None, now=NOW)["posted"] == 0 and len(spy.sent) == 1
    assert notify_subscribers(session, [soc.id], now=NOW)["posted"] == 0
    # a new job: one more message with that job only
    new = _job(session, "Security Engineer")
    out = notify_subscribers(session, [new.id, soc.id], now=NOW)
    assert out["posted"] == 1 and "Security Engineer" in spy.sent[-1][1] and "*SOC Analyst*" not in spy.sent[-1][1]
    assert len(session.exec(select(SubscriberPost)).all()) == 2


def test_failed_send_releases_the_claim_and_is_retried(session, spy):
    spy.session = session
    _job(session, "SOC Analyst")
    _sub(session)
    spy.fail_all = True
    out = notify_subscribers(session, None, now=NOW)
    r = out["subscribers"][0]
    assert r["error"] == "send failed" and r["sent"] == 0 and out["posted"] == 0
    assert spy.claims_seen == [1]  # claimed before the (failed) send ...
    assert session.exec(select(SubscriberPost)).all() == []  # ... and released after it
    spy.fail_all = False
    assert notify_subscribers(session, None, now=NOW)["posted"] == 1 and len(spy.sent) == 1


def test_dry_run_sends_and_records_nothing(session, spy):
    _job(session, "SOC Analyst")
    _sub(session)
    out = notify_subscribers(session, None, now=NOW, dry_run=True)
    assert len(out["subscribers"][0]["messages"]) == 1 and out["posted"] == 0 and out["messages"] == 0
    assert spy.sent == [] and session.exec(select(SubscriberPost)).all() == []


def test_three_message_cap_per_subscriber_and_run(session, spy):
    for i in range(BATCH_SIZE * MAX_MESSAGES_PER_RUN + 5):
        _job(session, f"SOC Analyst {i}", key=f"cap{i}", hours_ago=i / 10)
    _sub(session)
    out = notify_subscribers(session, None, now=NOW)
    assert out["messages"] == MAX_MESSAGES_PER_RUN and out["posted"] == BATCH_SIZE * MAX_MESSAGES_PER_RUN
    assert len(spy.sent) == 3 and all(m.count("https://apply.example/1") == BATCH_SIZE for _t, m in spy.sent)
    # newest first; the rest goes out next run
    assert "*SOC Analyst 0*" in spy.sent[0][1]
    out = notify_subscribers(session, None, now=NOW)
    assert out["messages"] == 1 and out["posted"] == 5


def test_inactive_unfinished_and_disabled_are_skipped(session, spy, monkeypatch):
    _job(session, "SOC Analyst")
    _sub(session, active=False)
    _sub(session, jid=OTHER, state="asked_location")
    out = notify_subscribers(session, None, now=NOW)
    assert [r["skipped"] for r in out["subscribers"]] == ["paused", "onboarding (asked_location)"]
    assert spy.sent == [] and out["posted"] == 0
    third = _sub(session, jid="919876511111@s.whatsapp.net")
    monkeypatch.setenv("JOBAPPLY_WHATSAPP_BOT_ENABLED", "false")
    from jobapply.config import get_settings

    get_settings.cache_clear()
    try:
        out = notify_subscribers(session, None, now=NOW)
        assert out["disabled"] and out["subscribers"] == [] and spy.sent == []
        # a dry run still shows what would go out
        out = notify_subscribers(session, None, now=NOW, dry_run=True)
        assert len(out["subscribers"][2]["messages"]) == 1 and out["subscribers"][2]["id"] == third.id
    finally:
        get_settings.cache_clear()


def test_one_subscriber_failing_never_stops_the_next(session, spy, monkeypatch):
    _job(session, "SOC Analyst")
    bad = _sub(session)
    _sub(session, jid=OTHER)
    real = dispatch_mod.candidate_jobs

    def boom(session_, sub, *a, **kw):
        if sub.id == bad.id:
            raise RuntimeError("kaput")
        return real(session_, sub, *a, **kw)

    monkeypatch.setattr(dispatch_mod, "candidate_jobs", boom)
    out = notify_subscribers(session, None, now=NOW)
    assert out["subscribers"][0]["error"].startswith("RuntimeError") and out["subscribers"][1]["sent"] == 1
    assert [t for t, _m in spy.sent] == [OTHER]


def test_collect_hook_posts_to_subscribers_and_survives_errors(engine, session, spy, monkeypatch):
    session.add(User(username="alice", password_hash="x", filters={"max_age_hours": 0}))
    _sub(session, fields=("cybersecurity",), location="")
    session.commit()
    # the fixture's "Abuse Investigator" postings are old and in no taxonomy field: the matching itself is
    # covered above, this test is about the hook, so the subscriber's filters become "abuse, any age"
    from jobapply.pipeline.filters import UserFilters

    monkeypatch.setattr(dispatch_mod, "subscriber_filters",
                        lambda sub: UserFilters(keywords_include=["abuse"], max_age_hours=0))
    good = GreenhouseCollector("stripe", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    result = run_all(collectors=[good], engine=engine)
    assert result["new_jobs"] == 2 and result["subscribers"] >= 1
    assert spy.sent and spy.sent[0][0] == JID and spy.sent[0][1].startswith("\U0001f195 ")
    assert len(session.exec(select(SubscriberPost)).all()) == result["subscribers"]
    again = GreenhouseCollector("stripe2", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    sent_before = len(spy.sent)
    assert run_all(collectors=[again], engine=engine)["subscribers"] == 0 and len(spy.sent) == sent_before

    def boom(session_, ids, **kw):
        raise RuntimeError("subscribers exploded")

    monkeypatch.setattr(collect_mod, "notify_subscribers", boom)
    result = run_all(collectors=[again], engine=engine)  # collect finished, nothing raised
    assert result["subscribers"] == 0 and "feeds" in result and "alerts" in result


# ----------------------------------------------------------------- migration


def test_migration_creates_subscribers_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    insp = inspect(make_engine(url))
    assert {"subscribers", "subscriber_posts"} <= set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("subscribers")}
    assert cols == {"id", "phone_jid", "name", "fields", "location", "remote_ok", "active", "state", "created_at",
                    "updated_at", "last_message_at", "source"}
    assert any(i["unique"] and i["column_names"] == ["phone_jid"] for i in insp.get_indexes("subscribers"))
    assert {c["name"] for c in insp.get_columns("subscriber_posts")} == {"id", "subscriber_id", "job_id", "posted_at"}
    assert any(set(u["column_names"]) == {"subscriber_id", "job_id"}
               for u in insp.get_unique_constraints("subscriber_posts"))


# ----------------------------------------------------------------- CLI


def test_cli_bot_help_subscribers_dispatch_and_simulate(tmp_path, monkeypatch, spy):
    from jobapply import db as db_mod
    from jobapply.cli import app as cli_app
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setattr("jobapply.cli.configure_logging", lambda *a, **k: None)
    get_settings.cache_clear()
    db_mod.get_engine.cache_clear()
    try:
        db_mod.create_all()
        runner = CliRunner()
        for args in (["bot", "--help"], ["bot", "dispatch", "--help"], ["bot", "simulate", "--help"]):
            res = runner.invoke(cli_app, args)
            assert res.exit_code == 0, res.output
        assert "--dry-run" in runner.invoke(cli_app, ["bot", "dispatch", "--help"]).output
        assert "--from" in runner.invoke(cli_app, ["bot", "simulate", "--help"]).output
        assert runner.invoke(cli_app, ["bot", "subscribers"]).output.startswith("no subscribers")
        # simulate: replies printed, nothing sent, the row changes like a real chat
        res = runner.invoke(cli_app, ["bot", "simulate", "--from", JID, "start"])
        assert res.exit_code == 0 and "JOB-APPLY" in res.output and "nothing was sent" in res.output, res.output
        assert JID not in res.output and "••••3210" in res.output and spy.sent == []
        for text in ("Priya", "1", "Pune"):
            assert runner.invoke(cli_app, ["bot", "simulate", "-f", JID, text]).exit_code == 0
        listed = runner.invoke(cli_app, ["bot", "subscribers"]).output
        assert "••••3210" in listed and "Priya" in listed and "Cybersecurity" in listed and JID not in listed
        with db_mod.session_scope() as s:
            s.add(Job(dedupe_key="cli-1", title="SOC Analyst", company="Acme", location="Pune",
                      posted_at=utcnow() - timedelta(minutes=5), apply_url="https://apply.example/cli"))
        res = runner.invoke(cli_app, ["bot", "dispatch", "--dry-run"])
        assert res.exit_code == 0 and "1 message(s) would be sent" in res.output and spy.sent == []
        res = runner.invoke(cli_app, ["bot", "dispatch"])
        assert res.exit_code == 0 and "1 message(s) sent, 1 job(s) posted" in res.output and len(spy.sent) == 1
    finally:
        get_settings.cache_clear()
        db_mod.get_engine.cache_clear()
