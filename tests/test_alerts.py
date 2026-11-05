"""WhatsApp new-job / outcome alerts and the notify-only apply mode (pipeline.alerts). No network: the
notifier is a spy installed with set_notifier; the conftest autouse fixture already keeps the live bridge out."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from jobapply.collectors.ats import GreenhouseCollector
from jobapply.forms.runner import FillResult
from jobapply.models import Application, ApplicationEvent, AppStatus, Job, JobSource, User, UserSite, utcnow
from jobapply.notify import set_notifier
from jobapply.pipeline import alerts
from jobapply.pipeline import apply as apply_mod
from jobapply.pipeline.alerts import (
    BATCH_SIZE,
    MAX_MESSAGES_PER_RUN,
    apply_mode,
    build_messages,
    clean_title,
    fmt_ist,
    job_block,
    notify_new_jobs,
    notify_only,
    notify_outcome,
    outcome_text,
    relative_age,
)
from jobapply.pipeline.collect import run_all
from jobapply.users import create_user

from .helpers import fixture_client

NUMBER = "+919876543210"
GROUP = "120363012345678901@g.us"


class Spy:
    """Notifier stand-in with the direct `send_text` transport; can be told to fail some or all targets."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.legacy: list[tuple[str, str]] = []
        self.fail_all = False
        self.fail_for: set[str] = set()

    def send(self, user, kind, message):
        self.legacy.append((kind, message))
        return True

    def send_text(self, to, text):
        if self.fail_all or to in self.fail_for:
            return False
        self.sent.append((to, text))
        return True


@pytest.fixture
def spy():
    s = Spy()
    set_notifier(s)
    yield s
    set_notifier(None)


@pytest.fixture
def public_url(monkeypatch):
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_PUBLIC_URL", "http://dash.test:8010")
    get_settings.cache_clear()
    yield "http://dash.test:8010"
    get_settings.cache_clear()


def _user(session, name="alice", **prefs):
    base = {"whatsapp_number": NUMBER, "new_jobs_enabled": True}
    base.update(prefs)
    user = create_user(session, name, "pw12345678", preferences=base)
    session.commit()
    return user


def _job(session, user, title="Senior Backend Engineer", *, posted=None, score=None, status=AppStatus.new,
         key=None, **kw):
    fields = dict(dedupe_key=key or f"k-{title}-{posted}-{score}", title=title, company="Acme Inc", location="Pune",
                  posted_at=posted, url="https://jobs.example/1", apply_url="https://apply.example/1",
                  source="greenhouse", ats_type="greenhouse")
    fields.update(kw)
    job = Job(**fields)
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _notified(session, app):
    return [e.detail for e in session.exec(select(ApplicationEvent).where(
        ApplicationEvent.application_id == app.id, ApplicationEvent.kind == "notified")).all()]


# ----------------------------------------------------------------- formatting


def test_relative_age_and_ist_stamp():
    now = datetime(2026, 11, 5, 8, 35, tzinfo=UTC)  # 14:05 IST
    assert fmt_ist(now) == "14:05 IST, 05 Nov"
    assert fmt_ist(now.replace(tzinfo=None)) == "14:05 IST, 05 Nov"  # naive values are UTC
    assert relative_age(now - timedelta(seconds=20), now) == "just now"
    assert relative_age(now - timedelta(minutes=12), now) == "12 min ago"
    assert relative_age(now - timedelta(hours=3, minutes=10), now) == "3 h ago"
    assert relative_age(now - timedelta(days=2, hours=5), now) == "2 d ago"
    assert relative_age(None, now) == "unknown age"


def test_clean_title_bold_safe_and_capped():
    assert clean_title("  Staff *Engineer*   (Platform) ") == "Staff Engineer (Platform)"
    long = "Principal Engineer " * 10
    assert len(clean_title(long)) == 90 and clean_title(long).endswith("…")
    assert clean_title("") == "(untitled)"


def test_job_block_lines(public_url):
    now = datetime(2026, 11, 5, 8, 35, tzinfo=UTC)
    job = Job(id=42, dedupe_key="x", title="Senior Backend Engineer", company="Acme Inc", location="Pune",
              posted_at=now - timedelta(minutes=12), url="https://jobs.example/1",
              apply_url="https://apply.example/1", source="greenhouse")
    lines = job_block(job, now).split("\n")
    assert lines[0] == "*Senior Backend Engineer* — Acme Inc · Pune"
    assert lines[1] == "\U0001f552 posted 12 min ago (13:53 IST, 05 Nov) · via greenhouse"
    assert lines[2] == "https://apply.example/1"  # the apply URL alone on its line, nothing after it
    assert lines[3] == "\U0001f4cb http://dash.test:8010/jobs/42"
    # no apply URL: the job URL; no posted_at: first_seen_at; remote with no location
    job2 = Job(id=7, dedupe_key="y", title="SRE", company="Beta", remote=True, apply_url="", url="https://j/2",
               first_seen_at=now - timedelta(hours=3), source="lever")
    lines2 = job_block(job2, now).split("\n")
    assert lines2[0].endswith("· Remote") and lines2[1].startswith("\U0001f552 posted 3 h ago (")
    assert lines2[2] == "https://j/2"


def test_build_messages_batches_with_headers():
    now = datetime(2026, 11, 5, 8, 35, tzinfo=UTC)
    jobs = [Job(id=i, dedupe_key=str(i), title=f"Job {i}", company="C", posted_at=now, apply_url=f"https://a/{i}")
            for i in range(10)]
    msgs = build_messages(jobs, now)
    assert len(msgs) == 2
    assert msgs[0].startswith("\U0001f195 8 new jobs — 14:05 IST, 05 Nov\n\n*Job 0* ")
    assert msgs[1].startswith("\U0001f195 2 new jobs — 14:05 IST, 05 Nov\n\n*Job 8* ")
    assert msgs[0].count("\U0001f4cb ") == 8 and msgs[1].count("\U0001f4cb ") == 2
    blocks = msgs[0].split("\n\n")
    assert len(blocks) == 9  # header + 8 jobs separated by blank lines
    assert build_messages([jobs[0]], now)[0].startswith("\U0001f195 1 new job —")


# ----------------------------------------------------------------- notify_new_jobs


def test_new_job_alerts_newest_first_batched_capped_and_never_reposted(session, spy):
    user = _user(session)
    now = utcnow()
    total = BATCH_SIZE * MAX_MESSAGES_PER_RUN + 5
    apps = [_job(session, user, f"Job {i:02d}", posted=now - timedelta(minutes=i), key=f"j{i}")
            for i in range(total)]
    sent = notify_new_jobs(session, user, now=now)
    assert len(sent) == MAX_MESSAGES_PER_RUN and len(spy.sent) == MAX_MESSAGES_PER_RUN
    assert all(to == NUMBER for to, _ in spy.sent)
    # newest posted first: the first message starts with Job 00, the fifth ends with Job 39
    assert "\n\n*Job 00* " in sent[0] and "*Job 39* " in sent[-1] and "*Job 40* " not in "".join(sent)
    assert sent[0].startswith(f"\U0001f195 {BATCH_SIZE} new jobs — {fmt_ist(now)}")
    notified = [i for i, (app, _) in enumerate(apps) if _notified(session, app)]
    assert notified == list(range(BATCH_SIZE * MAX_MESSAGES_PER_RUN))
    assert _notified(session, apps[0][0]) == ["whatsapp:number"]  # the kind, never the number itself
    # next run: only the 5 dropped ones, in one message; then nothing is ever posted again
    second = notify_new_jobs(session, user, now=now)
    assert len(second) == 1 and second[0].startswith("\U0001f195 5 new jobs") and "*Job 40* " in second[0]
    assert notify_new_jobs(session, user, now=now) == [] and len(spy.sent) == MAX_MESSAGES_PER_RUN + 1
    # a status change does not make a job "new" again
    apps[3][0].status = AppStatus.queued
    session.add(apps[3][0])
    session.commit()
    assert notify_new_jobs(session, user, now=now) == []


def test_new_job_alerts_off_by_default_and_min_score(session, spy):
    quiet = _user(session, "quiet", new_jobs_enabled=False)
    _job(session, quiet, "Silent", key="s1")
    assert notify_new_jobs(session, quiet) == [] and spy.sent == []
    plain = create_user(session, "plain", "pw12345678", preferences={"whatsapp_number": NUMBER})
    session.commit()
    _job(session, plain, "Also silent", key="s2")
    assert notify_new_jobs(session, plain) == [] and spy.sent == []

    picky = _user(session, "picky", new_jobs_min_score=50)
    a_none, _ = _job(session, picky, "Unscored", score=None, key="p1")
    a_low, _ = _job(session, picky, "Low", score=30, key="p2")
    a_high, _ = _job(session, picky, "High", score=70, key="p3")
    sent = notify_new_jobs(session, picky)
    assert len(sent) == 1 and "*High* " in sent[0] and "*Low* " not in sent[0] and "*Unscored* " not in sent[0]
    assert _notified(session, a_high) and not _notified(session, a_low) and not _notified(session, a_none)
    # min 0: an unscored job passes too
    picky.preferences = {**picky.preferences, "new_jobs_min_score": 0}
    session.add(picky)
    session.commit()
    sent = notify_new_jobs(session, picky)
    assert len(sent) == 1 and "*Unscored* " in sent[0] and "*Low* " in sent[0] and "*High* " not in sent[0]


def test_new_job_alerts_dry_run_sends_and_writes_nothing(session, spy):
    user = _user(session)
    app, _ = _job(session, user, "Preview me", key="d1")
    msgs = notify_new_jobs(session, user, dry_run=True)
    assert len(msgs) == 1 and "*Preview me* " in msgs[0]
    assert spy.sent == [] and _notified(session, app) == []
    # a real run afterwards still posts it
    assert len(notify_new_jobs(session, user)) == 1 and _notified(session, app)


def test_new_job_alerts_failed_send_writes_no_event_and_retries(session, spy):
    user = _user(session)
    app, _ = _job(session, user, "Flaky", key="f1")
    spy.fail_all = True
    assert notify_new_jobs(session, user) == [] and _notified(session, app) == []
    spy.fail_all = False
    assert len(notify_new_jobs(session, user)) == 1 and _notified(session, app) == ["whatsapp:number"]


def test_new_job_alerts_targets_number_group_both(session, spy):
    both = _user(session, "both", whatsapp_group_jid=GROUP, alerts_target="both")
    app, _ = _job(session, both, "Twice", key="t1")
    assert len(notify_new_jobs(session, both)) == 1
    assert [to for to, _ in spy.sent] == [NUMBER, GROUP]
    assert sorted(_notified(session, app)) == ["whatsapp:group", "whatsapp:number"]
    assert NUMBER not in "".join(_notified(session, app)) and GROUP not in "".join(_notified(session, app))
    spy.sent.clear()
    grp = _user(session, "grp", whatsapp_group_jid=GROUP, alerts_target="group")
    app2, _ = _job(session, grp, "Group only", key="t2")
    notify_new_jobs(session, grp)
    assert [to for to, _ in spy.sent] == [GROUP] and _notified(session, app2) == ["whatsapp:group"]
    spy.sent.clear()
    # one failing target: the event is written for the delivered one only, the failed one is retried
    spy.fail_for = {GROUP}
    app3, _ = _job(session, both, "Half", key="t3")
    notify_new_jobs(session, both)
    assert [to for to, _ in spy.sent] == [NUMBER] and _notified(session, app3) == ["whatsapp:number"]
    # ... next run, to the group only (the number never sees the job twice); then it is done for both
    spy.sent.clear()
    spy.fail_for = set()
    sent = notify_new_jobs(session, both)
    assert len(sent) == 1 and "*Half* " in sent[0] and [to for to, _ in spy.sent] == [GROUP]
    assert sorted(_notified(session, app3)) == ["whatsapp:group", "whatsapp:number"]
    assert notify_new_jobs(session, both) == [] and len(spy.sent) == 1
    # a dry run previews what any target would still get
    app5, _ = _job(session, both, "Preview half", key="t5")
    spy.fail_for = {GROUP}
    notify_new_jobs(session, both)
    preview = notify_new_jobs(session, both, dry_run=True)
    assert len(preview) == 1 and "*Preview half* " in preview[0]
    spy.fail_for = set()
    spy.sent.clear()
    # no target at all: nothing sent, nothing written, no crash
    none = _user(session, "none", whatsapp_number="")
    app4, _ = _job(session, none, "Nowhere", key="t4")
    assert notify_new_jobs(session, none) == [] and _notified(session, app4) == []


def test_overlapping_runs_post_a_batch_once(engine, session, spy, monkeypatch):
    """collect and collect_hot (or a CLI run) in the send window at the same time: both read the same pending
    row, but the batch is claimed with its `notified` events before the send, so exactly one posts it."""
    import threading

    from sqlmodel import Session

    user = _user(session)
    app, _ = _job(session, user, "Race", key="race")
    gate = threading.Barrier(2, timeout=15)
    real = alerts.pending_new_jobs

    def pending(session_, user_, now=None, target=None):
        rows = real(session_, user_, now, target)
        if target is not None:
            gate.wait()  # both runs have read the row before either one claims it
        return rows

    monkeypatch.setattr(alerts, "pending_new_jobs", pending)
    results: list[list[str]] = []
    errors: list[Exception] = []

    def run():
        with Session(engine, expire_on_commit=False) as s:
            u = s.exec(select(User).where(User.id == user.id)).one()
            try:
                results.append(notify_new_jobs(s, u))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and sorted(len(r) for r in results) == [0, 1]
    assert len(spy.sent) == 1 and _notified(session, app) == ["whatsapp:number"]
    # the DB guard on its own: a row another process claimed between our read and our claim is never sent
    app2, job2 = _job(session, user, "Claimed elsewhere", key="race2")
    session.add(ApplicationEvent(application_id=app2.id, user_id=user.id, kind="notified", detail="whatsapp:number"))
    session.commit()
    monkeypatch.setattr(alerts, "pending_new_jobs", lambda s, u, now=None, target=None: [(app2, job2)])
    assert notify_new_jobs(session, user) == [] and len(spy.sent) == 1
    assert _notified(session, app2) == ["whatsapp:number"]


def test_migration_0006_anonymises_dedupes_and_guards_notified_events(tmp_path):
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from jobapply.db import make_engine, upgrade

    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url, "0005")
    eng = make_engine(url)
    with Session(eng) as s:
        user = create_user(s, "mig", "pw12345678")
        s.commit()
        a1, _ = _job(s, user, "One", key="mig1")
        a2, _ = _job(s, user, "Two", key="mig2")
        for detail in (f"whatsapp:{NUMBER}", f"whatsapp:{NUMBER}", f"whatsapp:{GROUP}"):  # old rows, one duplicate
            s.add(ApplicationEvent(application_id=a1.id, user_id=user.id, kind="notified", detail=detail))
        s.add(ApplicationEvent(application_id=a2.id, user_id=user.id, kind="notified", detail="whatsapp:number"))
        for _ in range(2):  # other kinds may repeat a detail
            s.add(ApplicationEvent(application_id=a2.id, user_id=user.id, kind="note", detail="same"))
        s.commit()
        a1_id, a2_id, uid = a1.id, a2.id, user.id
    upgrade(url)
    assert any(i["name"] == "ux_application_events_notified" and i["unique"]
               for i in inspect(eng).get_indexes("application_events"))
    with Session(eng) as s:
        rows = s.exec(select(ApplicationEvent).where(ApplicationEvent.kind == "notified")).all()
        assert sorted((e.application_id, e.detail) for e in rows) == [
            (a1_id, "whatsapp:group"), (a1_id, "whatsapp:number"), (a2_id, "whatsapp:number")]
        assert NUMBER not in "".join(e.detail for e in rows)
        assert len(s.exec(select(ApplicationEvent).where(ApplicationEvent.kind == "note")).all()) == 2
        s.add(ApplicationEvent(application_id=a2_id, user_id=uid, kind="note", detail="same"))
        s.commit()
        s.add(ApplicationEvent(application_id=a2_id, user_id=uid, kind="notified", detail="whatsapp:number"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


def test_new_job_alerts_ignore_old_applications(session, spy):
    user = _user(session)
    old, _ = _job(session, user, "Ancient", key="o1")
    old.created_at = utcnow() - alerts.NEW_JOB_WINDOW - timedelta(hours=1)
    session.add(old)
    session.commit()
    assert notify_new_jobs(session, user) == [] and spy.sent == []


# ----------------------------------------------------------------- collect hook


def test_collect_hook_posts_new_matches_and_survives_a_notifier_exception(engine, session, spy, monkeypatch):
    session.add(User(username="alice", password_hash="x", filters={"max_age_hours": 0},
                     preferences={"whatsapp_number": NUMBER, "new_jobs_enabled": True}))
    session.commit()
    good = GreenhouseCollector("stripe", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    result = run_all(collectors=[good], engine=engine)
    assert result["assigned"] == {"alice": 2} and result["alerts"] == {"alice": 1}
    assert len(spy.sent) == 1 and spy.sent[0][0] == NUMBER and "2 new jobs" in spy.sent[0][1]
    assert len(session.exec(select(ApplicationEvent).where(ApplicationEvent.kind == "notified")).all()) == 2

    def boom(session_, user, **kw):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(alerts, "notify_new_jobs", boom)
    more = GreenhouseCollector("stripe2", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    result = run_all(collectors=[more], engine=engine)  # same postings: nothing new, but the hook still runs
    assert result["alerts"] == {"alice": 0} and "assigned" in result  # collect finished, nothing raised


# ----------------------------------------------------------------- mode


def test_apply_mode_defaults_to_notify():
    assert apply_mode(User(username="n", password_hash="x")) == "notify"
    assert notify_only(User(username="n", password_hash="x", preferences={}))
    assert notify_only(User(username="n", password_hash="x", preferences={"apply_mode": "bogus"}))
    assert not notify_only(User(username="n", password_hash="x", preferences={"apply_mode": "auto"}))


def _enable(session, user, site, **kw):
    row = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == site)).one()
    row.enabled = True
    for k, v in kw.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()


def test_notify_mode_blocks_every_picker_and_run(session, spy, monkeypatch):
    from jobapply.sites.cutshort.apply import apply_cutshort_user, pick_cutshort_applications
    from jobapply.sites.instahyre.apply import apply_instahyre_user, pick_instahyre_applications
    from jobapply.sites.linkedin import easy_apply as li_mod
    from jobapply.sites.linkedin.easy_apply import pick_linkedin_applications
    from jobapply.sites.naukri.apply import apply_naukri_user, pick_naukri_applications
    from jobapply.sites.wellfound.apply import pick_wellfound_applications
    from jobapply.sites.workday import apply as wd_mod
    from jobapply.sites.workday.apply import apply_workday_user, pick_workday_applications

    user = _user(session, "watcher")  # apply_mode unset -> notify
    for site in ("linkedin", "naukri", "workday", "instahyre", "cutshort", "wellfound"):
        _enable(session, user, site, auto_submit=True)
    gh, _ = _job(session, user, "GH", status=AppStatus.approved, score=90, key="m1")
    li, li_job = _job(session, user, "LI easy", status=AppStatus.queued, score=80, key="m2", source="linkedin",
                      ats_type=None, apply_url="https://www.linkedin.com/jobs/view/1")
    session.add(JobSource(job_id=li_job.id, source="linkedin", source_id="1", raw={"easy_apply": True}))
    _job(session, user, "LI offsite", status=AppStatus.queued, score=75, key="m3", source="linkedin", ats_type=None,
         apply_url="https://www.linkedin.com/jobs/view/2")
    _job(session, user, "WD", status=AppStatus.queued, score=70, key="m4", source="workday", ats_type="workday",
         apply_url="https://acme.wd5.myworkdayjobs.com/en-US/x/job/1")
    _job(session, user, "NK", status=AppStatus.queued, score=60, key="m5", source="naukri", ats_type="naukri",
         apply_url="https://www.naukri.com/job-listings-1")
    session.commit()

    assert apply_mod.pick_applications(session, user, 10) == []
    assert apply_mod.pick_external_linkedin_applications(session, user, 10) == []
    assert pick_linkedin_applications(session, user, 10) == []
    assert pick_workday_applications(session, user, 10) == []
    for pick in (pick_naukri_applications, pick_instahyre_applications, pick_cutshort_applications,
                 pick_wellfound_applications):
        assert pick(session, user, 10) == []

    def never(*a, **k):
        raise AssertionError("a browser / filler must not be touched in notify mode")

    class Browser:
        def new_context(self, **kw):
            never()

    monkeypatch.setattr(apply_mod, "run_one", never)
    monkeypatch.setattr(apply_mod, "_launch_persistent_site", never)
    monkeypatch.setattr(apply_mod, "_launch_persistent_linkedin", never)
    monkeypatch.setattr(apply_mod, "_site_throttled", lambda site, u, s: False)
    assert apply_mod.apply_user(session, user, None, Browser(), dry_run=False) == []
    assert apply_mod.apply_assisted_user(None, session, user, "naukri", limit=5, dry_run=False) == []
    assert apply_mod.apply_assisted_user(None, session, user, "workday", limit=5, dry_run=False) == []
    assert apply_mod.apply_external_linkedin_user(None, session, user, limit=5, dry_run=False) == []
    # the site runners themselves (documented entry points): no profile launched, no session check, no alert
    monkeypatch.setattr(li_mod, "_launch_persistent", never)
    monkeypatch.setattr(li_mod, "_is_throttled", lambda u, s: False)
    assert li_mod.apply_linkedin_user(None, session, user, limit=5, dry_run=False) == []
    assert apply_naukri_user(None, session, user, limit=5, dry_run=False) == []
    assert apply_instahyre_user(None, session, user, limit=5, dry_run=False) == []
    assert apply_cutshort_user(None, session, user, limit=5, dry_run=False) == []
    import jobapply.sites.browser as browser_mod

    monkeypatch.setattr(browser_mod, "launch_persistent", never)
    monkeypatch.setattr(wd_mod, "throttled", lambda u, s: False)
    monkeypatch.setattr(wd_mod, "pick_workday_applications", never)  # the runner's own gate, not the picker's
    assert apply_workday_user(None, session, user, limit=5, dry_run=False) == []
    assert spy.sent == [] and spy.legacy == []

    # automatic mode: the pickers see the rows again (every other gate untouched)
    user.preferences = {**user.preferences, "apply_mode": "auto"}
    session.add(user)
    session.commit()
    assert [a.id for a, _, _ in apply_mod.pick_applications(session, user, 10)] == [gh.id]
    assert [a.id for a, _ in pick_linkedin_applications(session, user, 10)] == [li.id]
    assert len(pick_workday_applications(session, user, 10)) == 1
    assert len(pick_naukri_applications(session, user, 10)) == 1
    assert len(apply_mod.pick_external_linkedin_applications(session, user, 10)) == 1


# ----------------------------------------------------------------- outcome alerts


def test_outcome_text(public_url):
    job = Job(id=5, dedupe_key="o", title="Data Engineer", company="Acme")
    assert outcome_text(job, AppStatus.applied) == (
        "✅ applied\n*Data Engineer* — Acme\n\U0001f4cb http://dash.test:8010/jobs/5")
    assert outcome_text(job, "dry_run").startswith("\U0001f4dd queued for your approval\n")
    assert outcome_text(job, AppStatus.queued).startswith("\U0001f4dd queued for your approval\n")
    assert outcome_text(job, AppStatus.paused, "captcha on page").startswith("⏸ paused: captcha on page\n")
    assert outcome_text(job, AppStatus.failed, "no form").startswith("❌ failed: no form\n")
    assert outcome_text(job, AppStatus.skipped) is None and outcome_text(job, "nonsense") is None


def test_auto_mode_sends_outcome_alerts_through_record_outcome(session, spy, public_url):
    auto = _user(session, "auto", apply_mode="auto")
    app, job = _job(session, auto, "Backend Dev", status=AppStatus.approved, score=90, key="r1")
    out = apply_mod.record_outcome(session, auto, app, job, FillResult(url=job.apply_url, profile_origin="builtin",
                                                                       submitted=True))
    assert out.status == AppStatus.applied
    assert spy.sent == [(NUMBER, f"✅ applied\n*Backend Dev* — Acme Inc\n\U0001f4cb {public_url}/jobs/{job.id}")]
    kinds = [e.kind for e in session.exec(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id))]
    assert "outcome" in kinds
    # paused: the outcome alert replaces the older "[JOB-APPLY] paused" number alert (no duplicate)
    app2, job2 = _job(session, auto, "Paused Dev", status=AppStatus.queued, score=80, key="r2")
    out = apply_mod.record_outcome(session, auto, app2, job2, FillResult(url=job2.apply_url, profile_origin="builtin",
                                                                         paused_reason="captcha on page"))
    assert out.status == AppStatus.paused
    assert spy.sent[-1][1].startswith("⏸ paused: captcha on page\n*Paused Dev* ") and spy.legacy == []
    # failed (crash) and dry run
    app3, job3 = _job(session, auto, "Crash Dev", status=AppStatus.queued, score=80, key="r3")
    apply_mod.record_outcome(session, auto, app3, job3, None, error="TimeoutError: page")
    assert spy.sent[-1][1].startswith("❌ failed: TimeoutError: page\n")
    app4, job4 = _job(session, auto, "Dry Dev", status=AppStatus.queued, score=80, key="r4")
    apply_mod.record_outcome(session, auto, app4, job4, FillResult(url=job4.apply_url, profile_origin="builtin"))
    assert spy.sent[-1][1].startswith("\U0001f4dd queued for your approval\n*Dry Dev* ")

    # notify mode: no outcome alert; the legacy paused alert to the number still goes out
    watcher = _user(session, "watcher")
    app5, job5 = _job(session, watcher, "Watch Dev", status=AppStatus.queued, score=80, key="r5")
    before = len(spy.sent)
    apply_mod.record_outcome(session, watcher, app5, job5, FillResult(url=job5.apply_url, profile_origin="builtin",
                                                                      paused_reason="captcha"))
    assert len(spy.sent) == before and spy.legacy == [("paused", "Watch Dev at Acme Inc: captcha")]


def test_notify_outcome_never_raises(session, spy, monkeypatch):
    auto = _user(session, "auto", apply_mode="auto")
    app, job = _job(session, auto, "X", status=AppStatus.approved, key="n1")

    def boom(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr(alerts, "send_to", boom)
    assert notify_outcome(session, auto, app, job, AppStatus.applied, "") is False
    monkeypatch.setattr(alerts, "send_to", lambda t, text: False)
    assert notify_outcome(session, auto, app, job, AppStatus.applied, "") is False
    assert not session.exec(select(ApplicationEvent).where(ApplicationEvent.kind == "outcome")).all()


# ----------------------------------------------------------------- CLI


def test_cli_alerts_dry_run_groups_and_test(engine, session, spy, monkeypatch):
    from jobapply import db as db_mod
    from jobapply.cli import app as cli_app
    from jobapply.cli_alerts import alerts_app

    monkeypatch.setattr(db_mod, "get_engine", lambda: engine)
    user = _user(session, "cli")
    app, _ = _job(session, user, "CLI Job", key="c1")
    runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})
    assert "alerts" in runner.invoke(cli_app, ["--help"]).output
    out = runner.invoke(alerts_app, ["send-new", "-u", "cli", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "\U0001f195 1 new job" in out.output and "*CLI Job* " in out.output and "nothing sent" in out.output
    assert spy.sent == [] and _notified(session, app) == []
    assert runner.invoke(alerts_app, ["send-new", "-u", "nobody", "--dry-run"]).exit_code == 2
    out = runner.invoke(alerts_app, ["send-new", "-u", "cli"])
    assert out.exit_code == 0 and "1 message(s) sent" in out.output and len(spy.sent) == 1
    assert _notified(session, app) == ["whatsapp:number"]
    # groups: the spy is not a bridge -> no groups, exit 1
    out = runner.invoke(alerts_app, ["groups"])
    assert out.exit_code == 1 and "no groups" in out.output
    # test message to the saved target
    out = runner.invoke(alerts_app, ["test", "-u", "cli"])
    assert out.exit_code == 0 and "sent" in out.output and NUMBER not in out.output
    assert spy.sent[-1][0] == NUMBER and "JOB-APPLY test for cli" in spy.sent[-1][1]
    none = _user(session, "silent", whatsapp_number="")
    assert none.id and runner.invoke(alerts_app, ["test", "-u", "silent"]).exit_code == 2
