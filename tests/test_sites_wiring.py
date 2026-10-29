"""Wiring of the assisted sites (naukri, instahyre, cutshort, wellfound): registry, apply routing, CLI, web.

Everything runs with fakes: no browser is launched and no network request is made.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from typer.testing import CliRunner

from jobapply.cli import app as cli_app
from jobapply.collectors.registry import (
    ASSISTED_COLLECTORS,
    ASSISTED_COOLDOWN_KEYS,
    SearchConfig,
    assisted_collectors,
    assisted_cooldown_active,
    assisted_cooldown_until,
    assisted_options,
    build_collectors,
    set_assisted_cooldown,
)
from jobapply.config import Settings, get_settings
from jobapply.models import Application, AppStatus, Job, User, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.pipeline.apply import (
    ACCOUNT_SITES,
    ASSISTED_SITES,
    COOKIE_SITES,
    SITE_APPLIERS,
    apply_assisted,
    apply_assisted_user,
    fill_result_from,
    pick_applications,
    route_external_result,
    stop_reason_for,
)
from jobapply.seed import seed
from jobapply.sites import connect
from jobapply.web.app import create_app

SITE_URLS = {
    "naukri": "https://www.naukri.com/job-listings-python-developer-acme-bengaluru-3-to-6-years-011025501234",
    "instahyre": "https://www.instahyre.com/job-12345-python-developer-at-acme/",
    "cutshort": "https://cutshort.io/job/Python-Developer-Acme-koBBDTQ1",
    "wellfound": "https://wellfound.com/jobs/1053169-python-developer",
    "workday": "https://acme.wd5.myworkdayjobs.com/Careers/job/Bengaluru/Python-Developer_JR1",
}


# ------------------------------------------------------------------ registry


def test_assisted_sites_are_registered_everywhere():
    assert set(COOKIE_SITES) == set(ASSISTED_COLLECTORS) == set(ASSISTED_COOLDOWN_KEYS)
    assert set(SITE_APPLIERS) == set(COOKIE_SITES) | set(ACCOUNT_SITES) == set(SITE_URLS)
    assert set(SITE_APPLIERS) <= ASSISTED_SITES and "linkedin" in ASSISTED_SITES
    assert set(COOKIE_SITES) <= set(connect.VERIFIED_SITES)
    assert ACCOUNT_SITES == ("workday",) and not any(connect.is_site(s) for s in ACCOUNT_SITES)


def test_registry_builds_only_enabled_assisted_sites():
    s = Settings(_env_file=None)
    searches = SearchConfig(queries=["python developer"], feeds=[], hours_old=48, assisted={
        "naukri": {"enabled": False},
        "instahyre": {"enabled": True, "location": "Bangalore", "max_results": 10, "max_details": 3},
        "cutshort": {"enabled": True, "hub_slugs": ["python-jobs"], "remote_only": True},
        "wellfound": {"enabled": True, "hours_old": 12, "max_requests": 7, "max_pages": 2},
    })
    cols = build_collectors(s, [], searches, extra_queries=["backend engineer"])
    by_source = {c.source: c for c in cols}
    assert set(by_source) == {"instahyre", "cutshort", "wellfound"}
    for c in by_source.values():
        assert c.queries[:2] == ["python developer", "backend engineer"] or "python developer" in c.queries
        assert c.skip_if is not None and c.on_rate_limited is not None
        assert c.ats_type == c.source
    ih, cs, wf = by_source["instahyre"], by_source["cutshort"], by_source["wellfound"]
    assert ih.location == "Bangalore" and ih.max_results == 10 and ih.max_details == 3 and ih.hours_old == 48
    assert cs.remote_only is True and "python-jobs" in cs.queries and cs.hours_old == 48
    assert wf.hours_old == 12 and wf.max_requests == 7 and wf.max_pages == 2
    # `only` filters by source like every other collector
    assert [c.source for c in build_collectors(s, [], searches, only=["cutshort"])] == ["cutshort"]


def test_registry_default_yaml_block_is_sane():
    from jobapply.collectors.registry import load_searches

    searches = load_searches(get_settings().companies_file.with_name("searches.yaml"))
    assert set(searches.assisted) == set(ASSISTED_COLLECTORS)
    assert searches.assisted["naukri"]["enabled"] is False  # recaptcha wall on the logged-out API
    enabled = {site for site, block in searches.assisted.items() if block.get("enabled")}
    assert enabled == {"instahyre", "cutshort", "wellfound"}
    cols = assisted_collectors(searches, ["python"], skip_if=lambda s: (lambda: True),
                               on_rate_limited=lambda s: (lambda h: None))
    assert sorted(c.source for c in cols) == sorted(enabled)


def test_registry_bad_per_site_extra_disables_only_that_site():
    """An extra the collector itself rejects (unknown job_type, non-int years) must not abort build_collectors."""
    s = Settings(_env_file=None)
    searches = SearchConfig(queries=["python"], feeds=[], assisted={
        "cutshort": {"enabled": True},
        "instahyre": {"enabled": True, "job_type": "fulltime"},
        "wellfound": {"enabled": True, "max_pages": "all"},
    })
    cols = build_collectors(s, [], searches)
    assert [c.source for c in cols] == ["cutshort"]
    # the shared registry never hands Naukri a user profile: the pool search must not run as one user
    from jobapply.collectors.registry import ASSISTED_EXTRAS

    assert "user_id" not in ASSISTED_EXTRAS["naukri"]
    opts = assisted_options("naukri", {"enabled": True, "api": True, "browser_fallback": True, "user_id": 3})
    assert "user_id" not in opts and opts["browser_fallback"] is True


def test_assisted_options_validation():
    assert assisted_options("naukri", None) is None
    assert assisted_options("naukri", {"enabled": False}) is None
    assert assisted_options("nope", {"enabled": True}) is None
    assert assisted_options("naukri", {"enabled": True, "max_results": "lots"}) is None  # bad int: site off
    opts = assisted_options("cutshort", {"enabled": True, "max_results": "5", "hub_slugs": ["x-jobs"],
                                         "job_type": "both"}, default_hours_old=36)
    assert opts == {"location": "", "hours_old": 36, "max_results": 5, "max_requests": 40, "hub_slugs": ["x-jobs"]}


def test_assisted_cooldown_helpers(session):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    assert assisted_cooldown_until(session, "naukri") is None
    assert not assisted_cooldown_active(session, "naukri", now)
    until = set_assisted_cooldown(session, "naukri", 2, now=now)
    assert until == now + timedelta(hours=2)
    assert assisted_cooldown_active(session, "naukri", now)
    assert not assisted_cooldown_active(session, "instahyre", now)  # keys are per site
    # a repeat block while the first cooldown is active doubles the remaining window, capped at 48 h
    later = now + timedelta(hours=1)
    assert set_assisted_cooldown(session, "naukri", 2, now=later) == later + timedelta(hours=2)
    assert set_assisted_cooldown(session, "naukri", 100, now=later) == later + timedelta(hours=48)
    assert not assisted_cooldown_active(session, "naukri", later + timedelta(hours=49))
    # cutshort / wellfound share the key their own module reads
    from jobapply.sites.cutshort.search import public_cooldown_active
    from jobapply.sites.wellfound.search import guest_cooldown_active

    set_assisted_cooldown(session, "cutshort", 2, now=now)
    set_assisted_cooldown(session, "wellfound", 2, now=now)
    assert public_cooldown_active(session, now) and guest_cooldown_active(session, now)


def test_assisted_cooldown_honours_user_challenge_cooldown(session, demo):
    from jobapply.sites.instahyre.session import COOLDOWN_KEY

    now = datetime.now(UTC)
    assert not assisted_cooldown_active(session, "instahyre", now)
    demo.preferences = {**(demo.preferences or {}), COOLDOWN_KEY: (now + timedelta(hours=3)).isoformat()}
    session.add(demo)
    session.commit()
    assert assisted_cooldown_active(session, "instahyre", now)
    assert not assisted_cooldown_active(session, "naukri", now)


# ------------------------------------------------------------------ apply routing


class _FakePage:
    url = "https://example.test/"

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self.closed = False
        self.pages = []

    def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeSiteSession:
    def __init__(self, status="ok"):
        self.status = status
        self.challenges = 0

    def check(self, page):
        return self.status

    def record_challenge(self):
        self.challenges += 1


def _job(session, user, site, *, status=AppStatus.queued, score=80, suffix=""):
    job = Job(dedupe_key=f"{site}-{suffix or score}", title="Python Developer", company="Acme", source=site,
              ats_type=site, apply_url=SITE_URLS[site] + suffix, url=SITE_URLS[site] + suffix)
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _enable(session, user, site, **kw):
    row = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == site)).one()
    row.enabled = True
    for k, v in kw.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    return row


def _wire(monkeypatch, results, *, status="ok", throttled=False):
    """Replace the browser / site hooks so apply_assisted_user never opens a browser. `results` is a list of
    apply_job dicts handed out in order (the last one repeats)."""
    calls: list[dict] = []
    notified: list[tuple[str, str]] = []
    contexts: list[_FakeContext] = []
    sessions: list[_FakeSiteSession] = []

    def launch(pw, user_id, site):
        ctx = _FakeContext()
        contexts.append(ctx)
        return ctx

    def make_session(site, context, user, db, llm=None):
        s = _FakeSiteSession(status)
        sessions.append(s)
        return s

    def fake_apply(site, site_session, application, *, dry_run, auto_submit, llm=None):
        calls.append({"site": site, "app": application.id, "dry_run": dry_run, "auto_submit": auto_submit})
        return dict(results[min(len(calls) - 1, len(results) - 1)])

    monkeypatch.setattr(apply_mod, "_launch_persistent_site", launch)
    monkeypatch.setattr(apply_mod, "_site_session", make_session)
    monkeypatch.setattr(apply_mod, "_site_throttled", lambda site, u, s: throttled)
    monkeypatch.setattr(apply_mod, "_site_apply_job", fake_apply)
    monkeypatch.setattr(apply_mod, "_between_applications", lambda page: None)
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: notified.append((kind, msg)))
    return calls, notified, contexts, sessions


@pytest.mark.parametrize("site", SITE_APPLIERS)
def test_apply_routing_dispatches_per_site_under_dry_run(session, demo, monkeypatch, site):
    _enable(session, demo, site, auto_submit=True)  # even with auto-submit on, a dry run never submits
    app, job = _job(session, demo, site)
    calls, notified, contexts, _ = _wire(monkeypatch, [{"status": "dry_run", "reason": "stopped before the click",
                                                        "screenshot": "shot.png", "answers": {"Notice": "30"}}])
    # the generic ATS filler leaves assisted-site jobs alone
    assert pick_applications(session, demo, 10) == []
    out = apply_assisted_user(None, session, demo, site, limit=5, dry_run=True)
    assert [o.status for o in out] == [AppStatus.dry_run]
    assert calls == [{"site": site, "app": app.id, "dry_run": True, "auto_submit": True}]
    assert contexts and contexts[0].closed
    session.refresh(app)
    assert app.status == AppStatus.dry_run and app.screenshot_path == "shot.png" and app.submitted_at is None
    assert app.answers.get("Notice") == "30"
    assert notified == []


@pytest.mark.parametrize("site", SITE_APPLIERS)
def test_apply_routing_global_dry_run_switch_wins(session, demo, monkeypatch, site):
    """`dry_run=False` from a caller still reaches apply_job as dry_run=True while the global switch is on."""
    _enable(session, demo, site, auto_submit=True)
    _job(session, demo, site)
    calls, *_ = _wire(monkeypatch, [{"status": "applied", "reason": "would be a submit"}])
    out = apply_assisted_user(None, session, demo, site, limit=5, dry_run=False)
    assert calls and calls[0]["dry_run"] is True and calls[0]["auto_submit"] is True
    assert out  # the (fake) applied result is still booked, as the site reported it
    assert out[0].status == AppStatus.applied


def test_apply_routing_passes_auto_submit_off(session, demo, monkeypatch):
    _enable(session, demo, "cutshort", auto_submit=False)
    _job(session, demo, "cutshort")
    calls, *_ = _wire(monkeypatch, [{"status": "dry_run", "reason": "auto-submit off"}])
    apply_assisted_user(None, session, demo, "cutshort", limit=5, dry_run=False)
    assert calls[0]["auto_submit"] is False


def test_apply_routing_real_submit_needs_every_gate(session, demo, monkeypatch):
    from jobapply.switches import set_value

    _enable(session, demo, "wellfound", auto_submit=True, daily_cap=1)
    _job(session, demo, "wellfound", suffix="-a")
    _job(session, demo, "wellfound", suffix="-b", score=70)
    monkeypatch.setenv("JOBAPPLY_DRY_RUN", "false")
    get_settings.cache_clear()
    set_value(session, "dry_run", "off")
    session.commit()
    calls, *_ = _wire(monkeypatch, [{"status": "applied", "reason": "submitted", "screenshot": "s.png"}])
    out = apply_assisted_user(None, session, demo, "wellfound", limit=5, dry_run=False)
    assert calls[0]["dry_run"] is False and calls[0]["auto_submit"] is True
    # per-site daily cap (1): the second pick is not attempted after the first real submit
    assert len(calls) == 1 and [o.status for o in out] == [AppStatus.applied]
    get_settings.cache_clear()


def test_apply_routing_kill_switch_and_disabled_and_throttled(session, demo, monkeypatch):
    from jobapply.switches import set_value

    _job(session, demo, "naukri")
    calls, notified, contexts, _ = _wire(monkeypatch, [{"status": "dry_run"}])
    # site disabled (default for assisted sites)
    assert apply_assisted_user(None, session, demo, "naukri", limit=5) == [] and not contexts
    _enable(session, demo, "naukri")
    # throttled after a challenge
    monkeypatch.setattr(apply_mod, "_site_throttled", lambda site, u, s: True)
    assert apply_assisted_user(None, session, demo, "naukri", limit=5) == [] and not contexts
    monkeypatch.setattr(apply_mod, "_site_throttled", lambda site, u, s: False)
    # kill switch
    set_value(session, "kill_switch", "on")
    session.commit()
    assert apply_assisted_user(None, session, demo, "naukri", limit=5) == [] and not contexts and not calls
    set_value(session, "kill_switch", "off")
    session.commit()
    assert len(apply_assisted_user(None, session, demo, "naukri", limit=5)) == 1
    with pytest.raises(ValueError):
        apply_assisted_user(None, session, demo, "linkedin", limit=5)


@pytest.mark.parametrize("status", ["login", "challenge", "unknown"])
def test_apply_routing_stops_when_session_not_ok(session, demo, monkeypatch, status):
    _enable(session, demo, "instahyre")
    _job(session, demo, "instahyre")
    calls, notified, contexts, _ = _wire(monkeypatch, [{"status": "dry_run"}], status=status)
    assert apply_assisted_user(None, session, demo, "instahyre", limit=5) == []
    assert calls == [] and contexts[0].closed
    assert (notified == []) if status == "unknown" else (notified and notified[0][0] == "paused")


def test_apply_routing_challenge_mid_run_stops_and_records(session, demo, monkeypatch):
    _enable(session, demo, "cutshort")
    _job(session, demo, "cutshort", suffix="-a")
    _job(session, demo, "cutshort", suffix="-b", score=70)
    calls, notified, _, sessions = _wire(monkeypatch, [{"status": "paused", "reason": "captcha on the job page",
                                                        "screenshot": "c.png"}])
    out = apply_assisted_user(None, session, demo, "cutshort", limit=5)
    assert len(calls) == 1 and [o.status for o in out] == [AppStatus.paused]
    assert sessions[0].challenges == 1 and notified and "challenged" in notified[-1][1]


def test_apply_routing_login_mid_run_marks_expired(session, demo, monkeypatch):
    row = _enable(session, demo, "naukri", login_status="ok")
    _job(session, demo, "naukri", suffix="-a")
    _job(session, demo, "naukri", suffix="-b", score=70)
    calls, notified, _, _ = _wire(monkeypatch, [{"status": "paused", "reason": "naukri login required",
                                                 "login_status": "login"}])
    out = apply_assisted_user(None, session, demo, "naukri", limit=5)
    assert len(calls) == 1 and out[0].status == AppStatus.paused
    session.refresh(row)
    assert row.login_status == "expired" and "expired" in notified[-1][1]


@pytest.mark.parametrize("reason", ["outside active hours (09:00-21:00)", "kill switch on", "daily cap reached",
                                    "wellfound throttled: cooldown after a challenge",
                                    "wellfound disabled for this user", "cutshort session unknown"])
def test_apply_routing_gate_skip_ends_run_without_booking(session, demo, monkeypatch, reason):
    _enable(session, demo, "wellfound")
    app, _ = _job(session, demo, "wellfound")
    _job(session, demo, "wellfound", suffix="-b", score=70)
    calls, *_ = _wire(monkeypatch, [{"status": "skipped", "reason": reason}])
    assert apply_assisted_user(None, session, demo, "wellfound", limit=5) == []
    assert len(calls) == 1
    session.refresh(app)
    assert app.status == AppStatus.queued
    assert apply_mod.is_gate_skip({"status": "skipped", "reason": "already applied", "gate": True})
    assert not apply_mod.is_gate_skip({"status": "skipped", "reason": "kill switch on", "gate": False})


def test_apply_routing_per_job_skip_is_booked_and_the_next_pick_runs(session, demo, monkeypatch):
    """Naukri's "already applied on Naukri" is a per-job skip: booked as paused so it leaves the pick set, and
    the run goes on to the next application instead of stalling the queue on the same row every run."""
    _enable(session, demo, "naukri")
    app_a, _ = _job(session, demo, "naukri", suffix="-a", score=90)
    app_b, _ = _job(session, demo, "naukri", suffix="-b", score=70)
    calls, *_ = _wire(monkeypatch, [{"status": "skipped", "reason": "already applied on Naukri"},
                                    {"status": "dry_run", "reason": "stopped before the click"}])
    out = apply_assisted_user(None, session, demo, "naukri", limit=5)
    assert [c["app"] for c in calls] == [app_a.id, app_b.id]
    assert [o.status for o in out] == [AppStatus.paused, AppStatus.dry_run]
    session.refresh(app_a)
    assert app_a.status == AppStatus.paused and "already applied" in (app_a.error or "")
    assert app_a.submitted_at is None
    # A has left the pick set: the next run cannot stall on it again
    assert app_a.id not in [a.id for a, _ in apply_mod._site_pick("naukri", session, demo, 5)]


def _real_runs(session, monkeypatch):
    from jobapply.switches import set_value

    monkeypatch.setenv("JOBAPPLY_DRY_RUN", "false")
    get_settings.cache_clear()
    set_value(session, "dry_run", "off")
    session.commit()


def test_apply_routing_registered_but_paused_counts_toward_caps(session, demo, monkeypatch):
    """Naukri's Apply click is the submit: a post-click chatbot that needs a human still counts for the caps."""
    _real_runs(session, monkeypatch)
    _enable(session, demo, "naukri", auto_submit=True, daily_cap=1)
    app_a, _ = _job(session, demo, "naukri", suffix="-a", score=90)
    _job(session, demo, "naukri", suffix="-b", score=70)
    calls, *_ = _wire(monkeypatch, [{"status": "needs_human", "reason": "chatbot question needs you: Notice period?",
                                     "clicked_apply": True, "screenshot": "n.png"}])
    out = apply_assisted_user(None, session, demo, "naukri", limit=5, dry_run=False)
    assert [o.status for o in out] == [AppStatus.paused]
    session.refresh(app_a)
    assert app_a.status == AppStatus.paused and app_a.submitted_at is not None
    assert apply_mod.applied_today(session, demo.id, "naukri") == 1
    assert len(calls) == 1  # site cap 1: the second pick is not attempted
    # the same for Instahyre's interest toast followed by a questionnaire
    _enable(session, demo, "instahyre", auto_submit=True, daily_cap=5)
    app_i, _ = _job(session, demo, "instahyre")
    _wire(monkeypatch, [{"status": "paused", "reason": "post-apply questionnaire needs a human",
                         "interest_sent": True}])
    apply_assisted_user(None, session, demo, "instahyre", limit=5, dry_run=False)
    session.refresh(app_i)
    assert app_i.status == AppStatus.paused and app_i.submitted_at is not None
    get_settings.cache_clear()


def test_apply_routing_external_submits_count_toward_user_cap(session, demo, monkeypatch):
    from jobapply.forms.runner import FillResult, FormRunner

    _real_runs(session, monkeypatch)
    demo.preferences = {**(demo.preferences or {}), "daily_cap": 1}
    session.add(demo)
    session.commit()
    _enable(session, demo, "naukri", auto_submit=True, daily_cap=50)
    _enable(session, demo, "greenhouse", auto_submit=True, daily_cap=50)
    for n in range(3):
        _job(session, demo, "naukri", suffix=f"-{n}", score=90 - n)
    runner_calls: list[bool] = []

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        runner_calls.append(submit)
        return FillResult(url=url or "", profile_origin="builtin", submitted=submit)

    monkeypatch.setattr(FormRunner, "run", fake_run)
    calls, *_ = _wire(monkeypatch, [{"status": "external", "reason": "apply on company site",
                                     "apply_url": "https://boards.greenhouse.io/acme/jobs/9"}])
    out = apply_assisted_user(None, session, demo, "naukri", limit=5, dry_run=False)
    assert [o.status for o in out] == [AppStatus.applied]
    assert runner_calls == [True] and len(calls) == 1  # user cap 1 stops the run after one real submit
    get_settings.cache_clear()


def test_apply_routing_crash_is_recorded_as_failed(session, demo, monkeypatch):
    _enable(session, demo, "instahyre")
    app, _ = _job(session, demo, "instahyre")
    _wire(monkeypatch, [{"status": "dry_run"}])

    def boom(*a, **k):
        raise TimeoutError("page.goto timed out")

    monkeypatch.setattr(apply_mod, "_site_apply_job", boom)
    out = apply_assisted_user(None, session, demo, "instahyre", limit=5)
    assert out[0].status == AppStatus.failed and "TimeoutError" in out[0].detail


def test_external_result_routes_known_ats_through_form_runner(session, demo, monkeypatch):
    from jobapply.forms.runner import FillResult, FormRunner

    _enable(session, demo, "naukri")
    gh = session.exec(select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()
    assert gh.enabled and not gh.auto_submit
    app, job = _job(session, demo, "naukri")
    runner_calls: list[dict] = []

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        runner_calls.append({"url": url, "ats_hint": ats_hint, "submit": submit})
        return FillResult(url=url or "", profile_origin="builtin", submitted=submit)

    monkeypatch.setattr(FormRunner, "run", fake_run)
    calls, *_ = _wire(monkeypatch, [{"status": "external", "reason": "apply on company site",
                                     "apply_url": "https://boards.greenhouse.io/acme/jobs/9"}])
    out = apply_assisted_user(None, session, demo, "naukri", limit=5, dry_run=True)
    assert [o.status for o in out] == [AppStatus.dry_run]
    assert runner_calls == [{"url": "https://boards.greenhouse.io/acme/jobs/9", "ats_hint": "greenhouse",
                             "submit": False}]
    session.refresh(job)
    assert job.ats_type == "greenhouse" and job.apply_url.startswith("https://boards.greenhouse.io/")


def test_external_result_unknown_host_is_paused(session, demo):
    _enable(session, demo, "naukri")
    app, job = _job(session, demo, "naukri")
    out = route_external_result(session, demo, app, job,
                                {"status": "external", "apply_url": "https://acme.example/careers/9"}, "naukri")
    assert out.status == AppStatus.paused and "manually" in out.detail
    session.refresh(job)
    assert job.apply_url == "https://acme.example/careers/9" and job.ats_type is None
    # a URL only revealed by a click that was not made: paused too, job untouched
    app2, job2 = _job(session, demo, "naukri", suffix="-2")
    out2 = route_external_result(session, demo, app2, job2, {"status": "external", "apply_url": None}, "naukri")
    assert out2.status == AppStatus.paused and "not clicked" in out2.detail
    session.refresh(job2)
    assert job2.ats_type == "naukri"


def test_external_result_known_ats_without_context_is_left_queued(session, demo):
    _enable(session, demo, "naukri")
    app, job = _job(session, demo, "naukri", status=AppStatus.approved)
    out = route_external_result(session, demo, app, job,
                                {"status": "external", "apply_url": "https://jobs.lever.co/acme/1"}, "naukri")
    assert out is None
    session.refresh(app)
    session.refresh(job)
    assert app.status == AppStatus.approved and job.ats_type == "lever"
    assert [a.id for a, _, _ in pick_applications(session, demo, 10)] == [app.id]  # now the ATS filler's


def test_fill_result_mapping_and_stop_reason():
    job = Job(dedupe_key="x", title="t", company="c", apply_url="https://cutshort.io/job/x")
    applied = fill_result_from({"status": "applied", "reason": "done", "screenshot": "a.png"}, job, "cutshort")
    assert applied.submitted and applied.screenshot_path == "a.png" and applied.url == job.apply_url
    assert fill_result_from({"status": "dry_run"}, job, "naukri").paused_reason == ""
    paused = fill_result_from({"status": "needs_human", "reason": "date picker", "needs_human": ["Start date"]},
                              job, "naukri")
    assert paused.paused_reason == "date picker" and paused.needs_human == ["Start date"]
    failed = fill_result_from({"status": "failed", "reason": "no apply button"}, job, "wellfound")
    assert failed.error == "no apply button" and not failed.submitted
    assert fill_result_from({"status": "failed"}, job, "instahyre").error == "instahyre apply failed"
    from jobapply.forms.runner import FillResult

    own = FillResult(url="u", profile_origin="builtin:instahyre", submitted=True)
    assert fill_result_from({"status": "applied", "result": own}, job, "instahyre") is own
    assert stop_reason_for({"status": "paused", "reason": "security check / captcha"}) == "challenge"
    assert stop_reason_for({"status": "paused", "reason": "instahyre login needed"}) == "login"
    assert stop_reason_for({"status": "paused", "reason": "cutshort answered 429"}) == "challenge"
    assert stop_reason_for({"status": "dry_run", "login_status": "challenge"}) == "challenge"
    assert stop_reason_for({"status": "paused", "reason": "required fields left empty"}) == ""
    assert stop_reason_for({"status": "failed", "reason": "captcha"}) == ""
    # the fixed challenge strings every site applier emits still stop the run
    for reason in ("captcha on page (recaptcha)", "security check page", "security check / captcha page",
                   "challenge page after clicking apply", "naukri answered 403", "instahyre answered 429",
                   "wellfound answered 403 (challenge / block)"):
        assert stop_reason_for({"status": "paused", "reason": reason}) == "challenge", reason
    assert stop_reason_for({"status": "paused",
                            "reason": "wellfound login required: import cookies and retry"}) == "login"
    # question text riding along in the reason is never read as a challenge
    for reason in ("a question needs a human; Describe a technical challenge you solved: no answer available",
                   "chatbot question needs you: Have you been blocked from any client site?",
                   "chatbot did not accept the answer to: Rate your OTP-based auth experience",
                   "a question needs a human; Carbon footprint of your last project?: no answer available",
                   "chatbot question needs you: Do you enjoy a challenge?"):
        assert stop_reason_for({"status": "needs_human", "reason": reason}) == "", reason


def test_apply_assisted_entry_respects_active_hours_and_site_names(engine, monkeypatch):
    from jobapply.sites import browser

    monkeypatch.setattr(browser, "is_active_hours", lambda now=None, spec="": False)
    assert apply_assisted("naukri", engine=engine) == []
    with pytest.raises(ValueError):
        apply_assisted("linkedin", engine=engine)
    monkeypatch.setenv("JOBAPPLY_CUTSHORT_ACTIVE_HOURS", "01:00-02:00")
    get_settings.cache_clear()
    assert apply_mod.site_active_hours("cutshort") == "01:00-02:00"
    assert apply_mod.site_active_hours("naukri") == get_settings().linkedin_active_hours
    get_settings.cache_clear()


# ------------------------------------------------------------------ CLI


def test_cli_sites_help_smoke():
    runner = CliRunner()
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0 and "sites" in result.output and "naukri" in result.output
    result = runner.invoke(cli_app, ["sites", "--help"])
    assert result.exit_code == 0
    for cmd in ("check", "import-cookies", "search", "apply"):
        assert cmd in result.output
    result = runner.invoke(cli_app, ["naukri", "--help"])
    assert result.exit_code == 0 and "refresh-profile" in result.output
    for cmd in (["sites", "search", "--help"], ["sites", "apply", "--help"], ["sites", "import-cookies", "--help"],
                ["naukri", "refresh-profile", "--help"]):
        assert runner.invoke(cli_app, cmd).exit_code == 0
    bad = runner.invoke(cli_app, ["sites", "check", "-u", "nobody", "-s", "monster"])
    assert bad.exit_code == 2 and "unknown site" in (bad.output + str(bad.exception or ""))


# ------------------------------------------------------------------ web


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "Jl0tE_KZf7aYyq3W_e7u6bSJ5l1W9mY2y5V7q9cN6uM=")
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(engine.url.database) + "-data")
    get_settings.cache_clear()
    seed(engine)
    app = create_app(engine=engine, secret_key="test-secret")
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def _csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def _login(client, username="demo", password="demo1234"):
    csrf = _csrf(client.get("/login").text)
    client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})
    return csrf


def _site_row(engine, username, site):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == username)).one()
        return s.exec(select(UserSite).where(UserSite.user_id == u.id, UserSite.site == site)).one()


def _fake_connect(monkeypatch, status="ok", count=5):
    from jobapply.sites import browser

    calls = {}

    def fake_import(user_id, site, path, allowed=("linkedin.com",)):
        calls["import"] = (user_id, site, str(path), allowed)
        return count

    def fake_check(site, user_id, engine=None):
        calls["check"] = (site, user_id)
        return status

    monkeypatch.setattr(browser, "import_cookies_file", fake_import)
    monkeypatch.setattr(connect, "check_session_for", fake_check)
    return calls


def test_settings_page_lists_connect_controls(client):
    _login(client)
    page = client.get("/settings").text
    for site in COOKIE_SITES:
        assert f"/settings/sites/{site}/cookies" in page and f"/settings/sites/{site}/check" in page
    assert "/settings/sites/linkedin/cookies" in page
    assert "/settings/sites/greenhouse/cookies" not in page
    for site in ACCOUNT_SITES:  # account sites: no cookie upload, no re-login button
        assert f"/settings/sites/{site}/cookies" not in page and f"/settings/sites/{site}/relogin" not in page


@pytest.mark.parametrize("site", COOKIE_SITES)
def test_settings_connect_site_imports_checks_and_records(client, engine, monkeypatch, site):
    calls = _fake_connect(monkeypatch)
    csrf = _login(client)
    resp = client.post(f"/settings/sites/{site}/cookies",
                       data={"csrf": csrf, "cookies_json": '[{"name": "s", "value": "x", "domain": ".x.com"}]'})
    assert resp.status_code == 303
    row = _site_row(engine, "demo", site)
    assert row.login_status == "ok" and row.last_login_at is not None
    assert calls["import"][1] == site and calls["import"][3] == connect.COOKIE_DOMAINS[site]
    assert calls["check"] == (site, row.user_id)
    assert f"{site}.json" in calls["import"][2] and str(row.user_id) in calls["import"][2]
    assert "Imported 5 cookies" in client.get("/settings").text
    # the export landed under the per-user data dir, never in the repo
    assert connect.cookie_file(row.user_id, site).exists()


def _browser_multipart(fields: list[tuple[str, str | None, bytes]]) -> tuple[bytes, str]:
    """A browser-faithful multipart body: every file input of the Sites form is posted, the untouched ones as
    `filename=""` parts. `fields` is (name, filename or None for a text field, content)."""
    boundary = "----WebKitFormBoundaryJobApplyTest"
    body = b""
    for name, filename, content in fields:
        body += f"--{boundary}\r\n".encode()
        if filename is None:
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        else:
            body += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_settings_connect_file_upload_works_on_any_row(client, engine, monkeypatch):
    """The chosen export is found whichever row's file input it sits in (the others post as empty parts)."""
    from jobapply.sites import browser

    seen: list[str] = []

    def fake_import(user_id, site, path, allowed=("linkedin.com",)):
        from pathlib import Path

        seen.append(Path(path).read_text(encoding="utf-8"))
        return 2

    monkeypatch.setattr(browser, "import_cookies_file", fake_import)
    monkeypatch.setattr(connect, "check_session_for", lambda site, user_id, engine=None: "ok")
    csrf = _login(client)
    export = b'[{"name": "s", "value": "x", "domain": ".naukri.com"}]'
    rows = ["cutshort", "instahyre", "linkedin", "naukri", "wellfound"]
    for chosen in ("cutshort", "naukri"):  # first row, middle row: never the last one
        seen.clear()
        body, ctype = _browser_multipart(
            [("csrf", None, csrf.encode())]
            + [("file", "naukri.json" if row == chosen else "", export if row == chosen else b"") for row in rows])
        resp = client.post(f"/settings/sites/{chosen}/cookies", content=body, headers={"content-type": ctype})
        assert resp.status_code == 303
        assert seen == [export.decode()], chosen
        assert _site_row(engine, "demo", chosen).login_status == "ok"
    # no file chosen anywhere: the "upload first" message, nothing imported
    seen.clear()
    body, ctype = _browser_multipart([("csrf", None, csrf.encode())] + [("file", "", b"") for _ in rows])
    client.post("/settings/sites/naukri/cookies", content=body, headers={"content-type": ctype})
    assert seen == [] and "cookie export first" in client.get("/settings").text


def test_settings_connect_reports_login_and_challenge(client, engine, monkeypatch):
    _fake_connect(monkeypatch, status="login")
    csrf = _login(client)
    client.post("/settings/sites/naukri/cookies", data={"csrf": csrf, "cookies_json": "[]"})
    assert _site_row(engine, "demo", "naukri").login_status == "expired"
    page = client.get("/settings").text
    assert "session check says login" in page
    _fake_connect(monkeypatch, status="challenge")
    client.post("/settings/sites/naukri/cookies", data={"csrf": csrf, "cookies_json": "[]"})
    assert _site_row(engine, "demo", "naukri").login_status == "challenge"


def test_settings_connect_bad_export_marks_expired(client, engine, monkeypatch):
    from jobapply.sites import browser

    def boom(*a, **k):
        raise ValueError("no cookies for ('cutshort.io',) found in the export")

    monkeypatch.setattr(browser, "import_cookies_file", boom)
    csrf = _login(client)
    client.post("/settings/sites/cutshort/cookies", data={"csrf": csrf, "cookies_json": "[]"})
    assert _site_row(engine, "demo", "cutshort").login_status == "expired"
    assert "Could not connect Cutshort" in client.get("/settings").text
    # empty form: nothing imported
    resp = client.post("/settings/sites/cutshort/cookies", data={"csrf": csrf})
    assert resp.status_code == 303 and "cookie export first" in client.get("/settings").text
    # an ATS site has no connect flow
    resp = client.post("/settings/sites/greenhouse/cookies", data={"csrf": csrf, "cookies_json": "[]"})
    assert resp.status_code == 404


def test_settings_check_site(client, engine, monkeypatch):
    calls = _fake_connect(monkeypatch, status="ok")
    csrf = _login(client)
    assert client.post("/settings/sites/wellfound/check", data={"csrf": csrf}).status_code == 303
    assert calls["check"][0] == "wellfound" and _site_row(engine, "demo", "wellfound").login_status == "ok"
    _fake_connect(monkeypatch, status="login")
    client.post("/settings/sites/wellfound/check", data={"csrf": csrf})
    assert _site_row(engine, "demo", "wellfound").login_status == "expired"
    assert client.post("/settings/sites/lever/check", data={"csrf": csrf}).status_code == 404
    assert client.post("/settings/sites/wellfound/check", data={}).status_code == 403  # CSRF


def test_settings_sites_toggles_work_for_assisted_sites(client, engine):
    csrf = _login(client)
    data = {"csrf": csrf, "enabled__naukri": "1", "auto__naukri": "1", "cap__naukri": "9", "enabled__wellfound": "1",
            "cap__wellfound": "3"}
    assert client.post("/settings/sites", data=data).status_code == 303
    nk, wf, ih = (_site_row(engine, "demo", s) for s in ("naukri", "wellfound", "instahyre"))
    assert nk.enabled and nk.auto_submit and nk.daily_cap == 9
    assert wf.enabled and not wf.auto_submit and wf.daily_cap == 3
    assert not ih.enabled and not ih.auto_submit


def test_onboarding_connect_step_is_generic(client, engine, monkeypatch):
    calls = _fake_connect(monkeypatch, status="ok", count=3)
    csrf = _csrf(client.get("/signup").text)
    client.post("/signup", data={"username": "connector", "password": "longpassword1", "confirm": "longpassword1",
                                 "email": "", "csrf": csrf})
    page = client.get("/onboarding?step=linkedin").text
    for site in connect.SITES:
        assert f"/onboarding/connect/{site}" in page
    assert "Connect Naukri" in page and "Connect LinkedIn" in page
    csrf = _csrf(page)
    resp = client.post("/onboarding/connect/instahyre",
                       data={"csrf": csrf, "cookies_json": '[{"name": "s", "value": "x", "domain": ".instahyre.com"}]'})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=linkedin"
    assert calls["import"][1] == "instahyre" and calls["check"][0] == "instahyre"
    assert _site_row(engine, "connector", "instahyre").login_status == "ok"
    assert "Instahyre connected. Imported 3 cookies" in client.get("/onboarding?step=linkedin").text
    assert client.post("/onboarding/connect/greenhouse", data={"csrf": csrf, "cookies_json": "[]"}).status_code == 404
    # the LinkedIn alias still works and never runs a session check
    calls.clear()
    client.post("/onboarding/linkedin",
                data={"csrf": csrf, "cookies_json": '[{"name": "li_at", "value": "x", "domain": ".linkedin.com"}]'})
    assert calls["import"][1] == "linkedin" and "check" not in calls
    assert _site_row(engine, "connector", "linkedin").login_status == "ok"


def test_connect_helpers_are_pure():
    assert connect.is_site("naukri") and not connect.is_site("greenhouse")
    assert connect.label("wellfound") == "Wellfound" and connect.label("x") == "x"
    r = connect.ConnectResult("naukri", 2, "login")
    assert r.login_status == "expired" and connect.ConnectResult("naukri", 2, "ok").login_status == "ok"
    with pytest.raises(ValueError):
        connect.import_cookies_for("greenhouse", 1, "nope.json")
