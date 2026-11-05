"""WhatsApp group feeds (pipeline.feeds): admin-configured feeds in app_settings, the shared matcher, the
feed_posts dedupe table, batching / caps, the collect hook and the CLI. No network: the notifier is a spy
installed with set_notifier; the conftest autouse fixture already keeps the live bridge out."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from typer.testing import CliRunner

from jobapply.collectors.ats import GreenhouseCollector
from jobapply.db import make_engine, upgrade
from jobapply.models import FeedPost, Job, User, utcnow
from jobapply.notify import set_notifier
from jobapply.pipeline import collect as collect_mod
from jobapply.pipeline import feeds as feeds_mod
from jobapply.pipeline.alerts import BATCH_SIZE
from jobapply.pipeline.collect import run_all
from jobapply.pipeline.feeds import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MAX_PER_RUN,
    FEEDS_KEY,
    add_feed,
    build_feed_messages,
    candidate_jobs,
    feed_filters,
    feed_header,
    filters_summary,
    get_feed,
    load_feeds,
    posted_job_ids,
    remove_feed,
    run_feed,
    run_feeds,
    set_enabled,
    update_feed,
)
from jobapply.pipeline.filters import UserFilters
from jobapply.switches import get_value, set_value

from .helpers import fixture_client

GROUP = "120363099999999999@g.us"
OTHER_GROUP = "120363088888888888@g.us"


class Spy:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_all = False

    def send(self, user, kind, message):
        return True

    def send_text(self, to, text):
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


def _job(session, title, *, posted=None, first_seen=None, key=None, **kw):
    fields = dict(dedupe_key=key or f"k-{title}", title=title, company="Acme Inc", location="Pune",
                  posted_at=posted, url="https://jobs.example/1", apply_url="https://apply.example/1",
                  source="greenhouse", ats_type="greenhouse")
    if first_seen is not None:
        fields["first_seen_at"] = first_seen
    fields.update(kw)
    job = Job(**fields)
    session.add(job)
    session.commit()
    return job


def _feed(session, name="Security jobs", group=GROUP, **kw):
    feed = add_feed(session, name=name, group_jid=group, **kw)
    session.commit()
    return feed


# ----------------------------------------------------------------- feed rows


def test_feed_crud_in_app_settings(session):
    assert load_feeds(session) == []
    feed = _feed(session, filters={"role_selection": ["cybersecurity:*"], "keywords_exclude": "intern, junior",
                                   "max_age_hours": "12", "min_salary": "", "bogus": 1})
    assert feed["id"] and feed["enabled"] and feed["max_per_run"] == DEFAULT_MAX_PER_RUN
    assert feed["filters"]["keywords_exclude"] == ["intern", "junior"] and feed["filters"]["max_age_hours"] == 12
    assert feed["filters"]["min_salary"] is None and "bogus" not in feed["filters"]
    stored = json.loads(get_value(session, FEEDS_KEY))
    assert [f["id"] for f in stored] == [feed["id"]]
    assert get_feed(session, feed["id"])["name"] == "Security jobs" and get_feed(session, "nope") is None

    updated = update_feed(session, feed["id"], name="Sec feed", max_per_run=500, filters={"locations": ["remote"]})
    assert updated["name"] == "Sec feed" and updated["max_per_run"] == 50  # capped at the hard ceiling
    assert updated["filters"]["locations"] == ["remote"] and updated["filters"]["role_selection"] == []
    assert updated["filters"]["max_age_hours"] == DEFAULT_MAX_AGE_HOURS
    assert not set_enabled(session, feed["id"], False)["enabled"]
    assert set_enabled(session, feed["id"], True)["enabled"]
    with pytest.raises(ValueError):
        update_feed(session, feed["id"], name="  ")
    with pytest.raises(ValueError):
        update_feed(session, "nope", name="x")
    with pytest.raises(ValueError):
        add_feed(session, name="", group_jid=GROUP)
    with pytest.raises(ValueError):
        add_feed(session, name="x", group_jid=" ")

    second = _feed(session, "Other", OTHER_GROUP)
    job = _job(session, "SOC Analyst")
    session.add(FeedPost(feed_id=feed["id"], job_id=job.id))
    session.add(FeedPost(feed_id=second["id"], job_id=job.id))
    session.commit()
    assert remove_feed(session, feed["id"]) and not remove_feed(session, feed["id"])
    session.commit()
    assert [f["id"] for f in load_feeds(session)] == [second["id"]]
    assert posted_job_ids(session, feed["id"]) == set() and posted_job_ids(session, second["id"]) == {job.id}

    # garbage in the setting is ignored, never raised
    set_value(session, FEEDS_KEY, "{not json")
    session.commit()
    assert load_feeds(session) == []
    set_value(session, FEEDS_KEY, json.dumps({"a": 1}))
    session.commit()
    assert load_feeds(session) == [] and run_feeds(session, None) == {"feeds": [], "posted": 0, "messages": 0}


def test_feed_filters_are_the_users_filters_and_candidates_use_the_shared_matcher(session):
    now = utcnow()
    feed = _feed(session, filters={"role_selection": ["cybersecurity:*"], "keywords_exclude": ["junior"],
                                   "locations": ["remote", "Pune"], "max_age_hours": 24, "min_salary": 50000,
                                   "sources": ["greenhouse", "lever"]})
    f = feed_filters(feed)
    assert isinstance(f, UserFilters)
    assert f.role_selection == ["cybersecurity:*"] and f.keywords_exclude == ["junior"]
    assert f.locations == ["remote", "Pune"] and f.max_age_hours == 24 and f.salary_floor == 50000
    assert f.sources == ["greenhouse", "lever"] and not f.remote_only
    assert filters_summary(feed) == ("fields: cybersecurity:* · exclude: junior · in: remote, Pune · "
                                     "sources: greenhouse, lever · salary ≥ 50000 · 24 h")

    soc = _job(session, "SOC Analyst", posted=now - timedelta(hours=1))
    remote = _job(session, "Security Engineer", posted=now - timedelta(minutes=5), location="", remote=True)
    _job(session, "Backend Engineer", posted=now - timedelta(hours=1))  # wrong field
    _job(session, "Junior Security Engineer", posted=now - timedelta(hours=1))  # excluded keyword
    _job(session, "Penetration Tester", posted=now - timedelta(days=3))  # too old
    _job(session, "Cloud Security Architect", posted=now - timedelta(hours=1), location="Bangalore")
    _job(session, "Security Analyst", posted=now - timedelta(hours=1), salary_max=20000)  # below floor
    _job(session, "Threat Hunter", posted=now - timedelta(hours=1), source="indeed")  # source not selected
    jobs = candidate_jobs(session, feed, None, now)
    assert [j.title for j in jobs] == ["Security Engineer", "SOC Analyst"]  # newest posted first
    # explicit ids restrict the candidates; a job already posted by this feed is skipped
    assert [j.id for j in candidate_jobs(session, feed, [soc.id], now)] == [soc.id]
    assert candidate_jobs(session, feed, [], now) == []
    session.add(FeedPost(feed_id=feed["id"], job_id=soc.id))
    session.commit()
    assert [j.id for j in candidate_jobs(session, feed, None, now)] == [remote.id]


# ----------------------------------------------------------------- messages


def test_feed_messages_have_the_feed_header_and_no_dashboard_link(monkeypatch):
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_PUBLIC_URL", "http://dash.test:8010")
    get_settings.cache_clear()
    try:
        now = datetime(2026, 11, 5, 8, 35, tzinfo=UTC)  # 14:05 IST
        assert feed_header("Security *jobs*", 1, now) == "\U0001f4e2 Security jobs — 1 new job — 14:05 IST"
        feed = {"id": "f1", "name": "Security jobs", "group_jid": GROUP}
        jobs = [Job(id=i, dedupe_key=str(i), title=f"Job {i}", company="Acme", location="Pune",
                    posted_at=now - timedelta(minutes=i), apply_url=f"https://a/{i}", source="greenhouse")
                for i in range(10)]
        msgs = build_feed_messages(feed, jobs, now)
        assert len(msgs) == 2
        assert msgs[0].startswith("\U0001f4e2 Security jobs — 8 new jobs — 14:05 IST\n\n*Job 0* — Acme · Pune\n")
        assert msgs[1].startswith("\U0001f4e2 Security jobs — 2 new jobs — 14:05 IST\n\n*Job 8* ")
        assert "\U0001f4cb" not in msgs[0] and "dash.test" not in msgs[0]  # no dashboard link for group readers
        assert "\n\U0001f552 posted just now (14:05 IST, 05 Nov) · via greenhouse\nhttps://a/0\n\n" in msgs[0]
        assert len(msgs[0].split("\n\n")) == 9
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------- run_feeds


def test_run_feeds_batches_caps_dedupes_and_records_only_after_a_successful_send(session, spy):
    now = utcnow()
    feed = _feed(session, filters={"role_selection": ["cybersecurity:*"]})
    jobs = [_job(session, f"Security Engineer {i:02d}", posted=now - timedelta(minutes=i), key=f"s{i}")
            for i in range(12)]
    _job(session, "Backend Engineer", posted=now)

    # a failed send: nothing recorded, nothing counted, run stats untouched
    spy.fail_all = True
    out = run_feeds(session, None, now=now)
    assert out["posted"] == 0 and out["messages"] == 0 and out["feeds"][0]["error"] == "send failed"
    assert out["feeds"][0]["candidates"] == 12 and posted_job_ids(session, feed["id"]) == set()
    assert get_feed(session, feed["id"])["posted_count"] == 0
    spy.fail_all = False

    out = run_feeds(session, None, now=now)
    assert out["posted"] == DEFAULT_MAX_PER_RUN and out["messages"] == 2
    assert [to for to, _ in spy.sent] == [GROUP, GROUP]
    assert spy.sent[0][1].startswith(f"\U0001f4e2 Security jobs — {BATCH_SIZE} new jobs — ")
    assert "*Security Engineer 00* " in spy.sent[0][1] and "*Security Engineer 09* " in spy.sent[1][1]
    assert "*Security Engineer 10* " not in spy.sent[0][1] + spy.sent[1][1] and "*Backend" not in spy.sent[0][1]
    assert posted_job_ids(session, feed["id"]) == {j.id for j in jobs[:DEFAULT_MAX_PER_RUN]}
    saved = get_feed(session, feed["id"])
    assert saved["posted_count"] == DEFAULT_MAX_PER_RUN and saved["last_run_at"] == now.isoformat()

    # next run: only the two dropped ones; then nothing is ever posted again
    out = run_feeds(session, None, now=now)
    assert out["posted"] == 2 and len(spy.sent) == 3 and "*Security Engineer 10* " in spy.sent[2][1]
    assert run_feeds(session, None, now=now)["posted"] == 0 and len(spy.sent) == 3
    assert get_feed(session, feed["id"])["posted_count"] == 12
    # the dedupe table refuses a duplicate row
    session.add(FeedPost(feed_id=feed["id"], job_id=jobs[0].id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_run_feeds_skips_disabled_feeds_and_dry_run_writes_nothing(session, spy):
    now = utcnow()
    on = _feed(session, "On", filters={"role_selection": ["cybersecurity:*"]})
    off = _feed(session, "Off", OTHER_GROUP, filters={"role_selection": ["cybersecurity:*"]}, enabled=False)
    job = _job(session, "SOC Analyst", posted=now)
    out = run_feeds(session, [job.id], now=now, dry_run=True)
    by_name = {r["name"]: r for r in out["feeds"]}
    assert by_name["Off"]["skipped"] == "disabled" and by_name["Off"]["messages"] == []
    assert len(by_name["On"]["messages"]) == 1 and "*SOC Analyst* " in by_name["On"]["messages"][0]
    assert spy.sent == [] and posted_job_ids(session, on["id"]) == set() and out["posted"] == 0
    assert get_feed(session, on["id"])["last_run_at"] is None  # a dry run leaves the stats alone
    # a preview of a disabled feed is possible on request; a real run never posts it
    assert len(run_feed(session, off, [job.id], now=now, dry_run=True, ignore_disabled=True)["messages"]) == 1
    out = run_feeds(session, [job.id], now=now)
    assert [to for to, _ in spy.sent] == [GROUP] and posted_job_ids(session, off["id"]) == set()
    assert posted_job_ids(session, on["id"]) == {job.id}
    assert get_feed(session, off["id"])["last_run_at"] is None and get_feed(session, on["id"])["last_run_at"]
    # a feed without a group: messages built, nothing sent, nothing recorded
    set_value(session, FEEDS_KEY, json.dumps([{"id": "nogroup", "name": "No group", "group_jid": ""}]))
    session.commit()
    job2 = _job(session, "Security Engineer", posted=now, key="ng")
    out = run_feeds(session, [job2.id], now=now)
    assert out["feeds"][0]["skipped"] == "no group" and len(spy.sent) == 1


def test_run_feeds_never_rewrites_the_feed_definitions(session, spy, monkeypatch):
    """An admin edit made while a run is posting (Disable here) must survive the run: the stats live outside
    `group_feeds` (feed_posts + the runs setting), so run_feeds never writes the definitions back."""
    now = utcnow()
    feed = _feed(session, filters={"role_selection": ["cybersecurity:*"]})
    job = _job(session, "Security Engineer", posted=now)

    def send_while_admin_disables(target, text):
        set_enabled(session, feed["id"], False)  # the admin clicks Disable while the message is going out
        session.commit()
        return True

    monkeypatch.setattr(feeds_mod, "send_to", send_while_admin_disables)
    out = run_feeds(session, [job.id], now=now)
    assert out["posted"] == 1
    saved = get_feed(session, feed["id"])
    assert saved["enabled"] is False  # the admin's edit stands
    assert saved["posted_count"] == 1 and saved["last_run_at"] == now.isoformat()  # the stats are still there
    stored = json.loads(get_value(session, FEEDS_KEY))[0]
    assert "posted_count" not in stored and "last_run_at" not in stored and stored["enabled"] is False
    # a removed feed takes its run stamp with it
    assert remove_feed(session, feed["id"])
    session.commit()
    assert feeds_mod.feed_stats(session) == {}


def test_run_feeds_never_raises_and_the_other_feed_still_runs(session, spy, monkeypatch):
    now = utcnow()
    _feed(session, "Broken", OTHER_GROUP, filters={"role_selection": ["cybersecurity:*"]})
    good = _feed(session, "Good", filters={"role_selection": ["cybersecurity:*"]})
    job = _job(session, "SOC Analyst", posted=now)
    real = feeds_mod.run_feed

    def flaky(session_, feed, *a, **kw):
        if feed["name"] == "Broken":
            raise RuntimeError("bridge exploded")
        return real(session_, feed, *a, **kw)

    monkeypatch.setattr(feeds_mod, "run_feed", flaky)
    out = run_feeds(session, [job.id], now=now)
    by_name = {r["name"]: r for r in out["feeds"]}
    assert by_name["Broken"]["error"].startswith("RuntimeError: bridge exploded") and by_name["Broken"]["posted"] == 0
    assert by_name["Good"]["posted"] == 1 and posted_job_ids(session, good["id"]) == {job.id}


# ----------------------------------------------------------------- collect hook


def test_collect_hook_posts_new_jobs_to_feeds_and_survives_errors(engine, session, spy, monkeypatch):
    session.add(User(username="alice", password_hash="x", filters={"max_age_hours": 0}))
    _feed(session, "Stripe feed", filters={"keywords_include": ["abuse"], "max_age_hours": 0})  # 0: any age
    session.commit()
    good = GreenhouseCollector("stripe", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    result = run_all(collectors=[good], engine=engine)
    assert result["new_jobs"] == 2 and result["feeds"] == {"Stripe feed": 2}
    assert len(spy.sent) == 1 and spy.sent[0][0] == GROUP
    assert spy.sent[0][1].startswith("\U0001f4e2 Stripe feed — 2 new jobs — ") and "\U0001f4cb" not in spy.sent[0][1]
    assert len(session.exec(select(FeedPost)).all()) == 2
    # same postings again: nothing new, nothing posted
    again = GreenhouseCollector("stripe2", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    assert run_all(collectors=[again], engine=engine)["feeds"] == {"Stripe feed": 0} and len(spy.sent) == 1

    def boom(session_, ids, **kw):
        raise RuntimeError("feeds exploded")

    monkeypatch.setattr(collect_mod, "run_feeds", boom)
    result = run_all(collectors=[again], engine=engine)  # collect finished, nothing raised
    assert result["feeds"] == {} and "assigned" in result and "alerts" in result


# ----------------------------------------------------------------- migration


def test_migration_creates_feed_posts(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    insp = inspect(make_engine(url))
    assert "feed_posts" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("feed_posts")}
    assert cols == {"id", "feed_id", "job_id", "posted_at"}
    assert any(set(u["column_names"]) == {"feed_id", "job_id"} for u in insp.get_unique_constraints("feed_posts"))


# ----------------------------------------------------------------- CLI


def test_cli_feeds_help_and_add_list_toggle_remove(tmp_path, monkeypatch, spy):
    from jobapply import db as db_mod
    from jobapply.cli import app as cli_app
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    # the CLI callback reconfigures logging process-wide; keep the test session's logging as it is
    monkeypatch.setattr("jobapply.cli.configure_logging", lambda *a, **k: None)
    get_settings.cache_clear()
    db_mod.get_engine.cache_clear()
    try:
        db_mod.create_all()
        runner = CliRunner()
        for args in (["feeds", "--help"], ["feeds", "run", "--help"], ["feeds", "add", "--help"]):
            res = runner.invoke(cli_app, args)
            assert res.exit_code == 0, res.output
        assert "--dry-run" in runner.invoke(cli_app, ["feeds", "run", "--help"]).output
        out = runner.invoke(cli_app, ["feeds", "add", "--help"]).output
        for opt in ("--name", "--group", "--field", "--location", "--hours"):
            assert opt in out
        assert runner.invoke(cli_app, ["feeds", "list"]).output.startswith("no feeds")

        res = runner.invoke(cli_app, ["feeds", "add", "--name", "Sec", "--group", GROUP, "--field", "cybersecurity",
                                      "--field", "cloud:*", "--location", "remote", "--hours", "12"])
        assert res.exit_code == 0, res.output
        with db_mod.session_scope() as s:
            feeds = load_feeds(s)
        assert len(feeds) == 1 and feeds[0]["filters"]["role_selection"] == ["cybersecurity:*", "cloud:*"]
        assert feeds[0]["filters"]["locations"] == ["remote"] and feeds[0]["filters"]["max_age_hours"] == 12
        fid = feeds[0]["id"]
        assert runner.invoke(cli_app, ["feeds", "add", "--name", "Bad", "--group", GROUP,
                                       "--field", "astrology"]).exit_code == 2
        listed = runner.invoke(cli_app, ["feeds", "list"]).output
        assert fid in listed and "fields: cybersecurity:*, cloud:*" in listed and "[on ]" in listed

        res = runner.invoke(cli_app, ["feeds", "run", "--dry-run"])
        assert res.exit_code == 0 and "0 message(s) would be sent" in res.output and spy.sent == []
        assert runner.invoke(cli_app, ["feeds", "disable", fid]).exit_code == 0
        assert "[off]" in runner.invoke(cli_app, ["feeds", "list"]).output
        assert runner.invoke(cli_app, ["feeds", "enable", fid]).exit_code == 0
        assert runner.invoke(cli_app, ["feeds", "enable", "nope"]).exit_code == 2
        assert runner.invoke(cli_app, ["feeds", "remove", fid]).exit_code == 0
        assert runner.invoke(cli_app, ["feeds", "list"]).output.startswith("no feeds")
    finally:
        get_settings.cache_clear()
        db_mod.get_engine.cache_clear()

