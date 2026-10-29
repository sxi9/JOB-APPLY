"""Wiring of the Workday account site and the universal external path into one product: routing (a Workday job
never reaches the generic FormRunner / navigator, an unknown host does), the gate stack, the `jobapply mail` and
`jobapply sites` CLI, the Settings site card and the Queue mailbox link.

No browser is launched and no network request is made: FormRunner.run and the site hooks are stubbed.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from typer.testing import CliRunner

from jobapply.cli import app as cli_app
from jobapply.config import get_settings
from jobapply.forms.navigator import STATUS_ACCOUNT_WALL, Navigator, account_site_for_url
from jobapply.forms.runner import FillResult, FormRunner
from jobapply.models import Application, AppStatus, Credential, Job, User, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.pipeline.apply import (
    ACCOUNT_SITES,
    ASSISTED_SITES,
    COOKIE_SITES,
    SITE_APPLIERS,
    account_site_of,
    apply_assisted_user,
    pick_applications,
    route_external_result,
    run_one,
)
from jobapply.seed import seed
from jobapply.sites.workday.account import count_accounts, forget_account, tenant_accounts
from jobapply.sites.workday.apply import pick_workday_applications
from jobapply.users import SITES
from jobapply.vault import Vault, generate_key
from tests.test_sites_wiring import _enable, _wire

WD_URL = "https://acme.wd5.myworkdayjobs.com/Careers/job/Bengaluru/Python-Developer_JR1"
WD_URL_2 = "https://globex.wd1.myworkdayjobs.com/en-US/External/details/Backend-Engineer_R-22"
EXT_URL = "https://acme.example/careers/9"


def _job(session, user, url, *, ats=None, source="hn", status=AppStatus.approved, suffix="", score=80):
    job = Job(dedupe_key=f"k-{url}{suffix}", title="Engineer", company="Acme", ats_type=ats, apply_url=url, url=url,
              source=source, description="Python backend role")
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _site(session, user, name) -> UserSite:
    return session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == name)).one()


class _Page:
    url = "about:blank"

    def close(self):
        pass


class _Ctx:
    def new_page(self):
        return _Page()


def _stub_runner(monkeypatch):
    calls: list[dict] = []

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        calls.append({"url": url, "ats_hint": ats_hint, "submit": submit})
        return FillResult(url=url or "", profile_origin="ai", submitted=submit)

    monkeypatch.setattr(FormRunner, "run", fake_run)
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: None)
    return calls


# ------------------------------------------------------------------ registration


def test_workday_is_registered_as_an_account_site():
    assert SITES["workday"]["kind"] == "account" and ACCOUNT_SITES == ("workday",)
    assert "workday" in SITE_APPLIERS and "workday" in ASSISTED_SITES and "workday" not in COOKIE_SITES
    assert account_site_for_url(WD_URL) == "workday" and account_site_for_url(WD_URL_2) == "workday"
    assert account_site_for_url(EXT_URL) is None and account_site_for_url("https://jobs.lever.co/acme/1") is None
    job = Job(dedupe_key="x", title="t", company="c", apply_url=WD_URL)
    assert account_site_of(job) == "workday"
    job.apply_url, job.ats_type = EXT_URL, "workday"
    assert account_site_of(job) == "workday"  # ats_type wins
    job.ats_type = None
    assert account_site_of(job) is None


def test_generic_filler_leaves_workday_jobs_to_the_workday_applier(session, demo):
    """The direct ATS pool: an enabled `workday` site still never hands the job to the generic FormRunner."""
    _enable(session, demo, "workday")
    app, job = _job(session, demo, WD_URL, ats="workday", source="greenhouse")
    bare_app, bare_job = _job(session, demo, WD_URL_2, ats=None, source="hn", suffix="-bare")
    assert pick_applications(session, demo, 10) == []
    picked = [a.id for a, _ in pick_workday_applications(session, demo, 10)]
    assert app.id in picked and bare_app.id in picked


def test_run_one_hands_a_workday_host_over_even_when_called_directly(session, demo, monkeypatch):
    calls = _stub_runner(monkeypatch)
    gh = _site(session, demo, "greenhouse")
    app, job = _job(session, demo, WD_URL, ats=None, source="greenhouse")
    out = run_one(session, demo, app, job, gh, page=_Page(), llm=None, dry_run=True)
    assert calls == [] and out.status == AppStatus.approved and "workday applier" in out.detail
    session.refresh(job)
    assert job.ats_type == "workday"


# ------------------------------------------------------------------ routing from the other sources


def test_external_result_on_a_workday_host_is_handed_over_not_navigated(session, demo, monkeypatch):
    """Naukri / Instahyre "apply on company site" -> a Workday tenant: no FormRunner, no navigator sign-up; the
    job is stamped `workday` and left for the workday applier (with or without a browser context)."""
    calls = _stub_runner(monkeypatch)
    _enable(session, demo, "naukri")
    for ctx in (_Ctx(), None):
        app, job = _job(session, demo, "https://www.naukri.com/job-listings-1", ats="naukri", source="naukri",
                        status=AppStatus.queued, suffix=f"-{ctx is None}")
        out = route_external_result(session, demo, app, job, {"status": "external", "apply_url": WD_URL},
                                    "naukri", context=ctx, llm=None, dry_run=True)
        assert out is None and calls == []
        session.refresh(job)
        session.refresh(app)
        assert job.ats_type == "workday" and job.apply_url == WD_URL and app.status == AppStatus.queued
        assert app.id in [a.id for a, _ in pick_workday_applications(session, demo, 10)]


def test_unknown_host_still_reaches_the_navigator_path(session, demo, monkeypatch):
    calls = _stub_runner(monkeypatch)
    _enable(session, demo, "naukri")
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-2", ats="naukri", source="naukri")
    out = route_external_result(session, demo, app, job, {"status": "external", "apply_url": EXT_URL}, "naukri",
                                context=_Ctx(), llm=None, dry_run=True)
    assert out is not None and out.status == AppStatus.dry_run
    assert calls == [{"url": EXT_URL, "ats_hint": None, "submit": False}]
    session.refresh(job)
    assert job.ats_type is None


def test_linkedin_offsite_resolving_to_workday_is_handed_over(session, demo, monkeypatch):
    from jobapply.sites.linkedin.resolve import ApplyResolution
    from tests.test_pipeline import _enable_linkedin, _li_job, _wire_external

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="777")
    _, runner_calls, _ = _wire_external(monkeypatch, {job.apply_url: ApplyResolution("offsite", WD_URL, "workday")})
    out = apply_mod.apply_external_linkedin_user(None, session, demo, limit=5, dry_run=False)
    assert runner_calls == []
    assert [o.status for o in out] == [AppStatus.queued] and "workday applier" in out[0].detail
    session.refresh(job)
    assert job.ats_type == "workday" and job.apply_url == WD_URL
    # resolved and stamped: the LinkedIn picker no longer sees it, the Workday picker does
    assert apply_mod.pick_external_linkedin_applications(session, demo, 10) == []
    assert app.id in [a.id for a, _ in pick_workday_applications(session, demo, 10)]


# ------------------------------------------------------------------ the gate stack in front of sites.workday.apply


def test_workday_apply_goes_through_apply_assisted_user_and_dry_run_never_submits(session, demo, monkeypatch):
    _enable(session, demo, "workday", auto_submit=True)
    app, job = _job(session, demo, WD_URL, ats="workday", source="greenhouse")
    calls, notified, contexts, _ = _wire(monkeypatch, [{"status": "dry_run", "screenshot": "wd.png",
                                                        "reason": "global dry run: Review reached, Submit not clicked",
                                                        "answers": {"Notice period": "30"}, "submitted": False}])
    # the caller asks for a real run, the global dry-run switch (on by default) still forces dry_run=True
    out = apply_assisted_user(None, session, demo, "workday", limit=5, dry_run=False)
    assert calls == [{"site": "workday", "app": app.id, "dry_run": True, "auto_submit": True}]
    assert [o.status for o in out] == [AppStatus.dry_run] and contexts[0].closed
    session.refresh(app)
    assert app.status == AppStatus.dry_run and app.screenshot_path == "wd.png" and app.submitted_at is None
    assert app.answers.get("Notice period") == "30" and notified == []


def test_workday_mailbox_not_configured_pauses_once_and_stops_the_run(session, demo, monkeypatch):
    _enable(session, demo, "workday")
    app_a, _ = _job(session, demo, WD_URL, ats="workday", source="greenhouse", score=90)
    _job(session, demo, WD_URL_2, ats="workday", source="greenhouse", suffix="-b", score=70)
    calls, notified, _, _ = _wire(monkeypatch, [{"status": "paused", "reason": "mailbox not configured",
                                                 "stop_run": True}])
    out = apply_assisted_user(None, session, demo, "workday", limit=5)
    assert len(calls) == 1 and [o.status for o in out] == [AppStatus.paused]
    session.refresh(app_a)
    assert app_a.status == AppStatus.paused and app_a.error.startswith("mailbox not configured")
    assert notified and "run stopped" in notified[-1][1]


def test_workday_challenge_records_the_cooldown_through_the_shared_stack(session, demo, monkeypatch):
    _enable(session, demo, "workday")
    _job(session, demo, WD_URL, ats="workday", source="greenhouse")
    _job(session, demo, WD_URL_2, ats="workday", source="greenhouse", suffix="-b")
    calls, notified, _, sessions = _wire(monkeypatch, [{"status": "paused", "login_status": "challenge",
                                                        "reason": "captcha / security check on the apply page"}])
    out = apply_assisted_user(None, session, demo, "workday", limit=5)
    assert len(calls) == 1 and out[0].status == AppStatus.paused
    assert sessions[0].challenges == 1 and "challenged" in notified[-1][1]


def test_real_workday_session_class_is_what_the_stack_builds(session, demo):
    """Without the hooks: the stack resolves `WorkdaySession` / `throttled` / `pick_workday_applications`."""
    from jobapply.sites.workday.session import WorkdaySession

    wd = apply_mod._site_session("workday", None, demo, session)
    assert isinstance(wd, WorkdaySession) and wd.check(None) == "ok"
    assert apply_mod._site_throttled("workday", demo, _site(session, demo, "workday")) is False
    app, _ = _job(session, demo, WD_URL, ats="workday", source="greenhouse")
    assert [a.id for a, _ in apply_mod._site_pick("workday", session, demo, 5)] == [app.id]
    assert apply_mod.site_active_hours("workday") == get_settings().linkedin_active_hours


# ------------------------------------------------------------------ the navigator never signs up on a Workday host


class _WallPage:
    def __init__(self, url):
        self.url = url
        self.evaluated = 0

    def evaluate(self, *a, **k):
        self.evaluated += 1
        return {"password": 2, "inputs": 4, "text": "Create Account"}


def test_navigator_refuses_the_generic_signup_on_a_workday_host(session, demo):
    page = _WallPage(WD_URL + "/apply/applyManually")
    nav = Navigator(page, session=session, user=demo, allow_final=True)
    out = nav.handle_account_wall()
    assert out is not None and out.status == STATUS_ACCOUNT_WALL
    assert "workday" in out.reason and "acme.wd5.myworkdayjobs.com" in out.reason
    assert page.evaluated == 0  # decided from the URL alone: no form read, nothing typed, no vault row
    assert session.exec(select(Credential).where(Credential.user_id == demo.id)).all() == []


# ------------------------------------------------------------------ stored accounts helpers


def test_tenant_accounts_and_forget_account(session, demo, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    vault = Vault()
    assert tenant_accounts(session, demo.id) == [] and count_accounts(session, demo.id) == 0
    vault.store(session, demo.id, "workday:acme", "login", "demo@example.com",
                {"password": "generated-test-only", "verified": True, "created_at": "2026-10-29T00:00:00"})
    vault.store(session, demo.id, "workday:globex", "login", "demo@example.com", {"password": "x", "verified": False})
    vault.store(session, demo.id, "mailbox", "imap", "demo@example.com", {"password": "app-pw"})
    session.commit()
    rows = tenant_accounts(session, demo.id, vault)
    assert [(r["tenant"], r["verified"]) for r in rows] == [("acme", True), ("globex", False)]
    assert all("password" not in r for r in rows)
    assert tenant_accounts(session, demo.id)[0]["verified"] is None  # no vault: nothing decrypted
    assert count_accounts(session, demo.id) == 2
    assert forget_account(session, demo.id, "ACME") and not forget_account(session, demo.id, "acme")
    assert [r["tenant"] for r in tenant_accounts(session, demo.id)] == ["globex"]
    get_settings.cache_clear()


# ------------------------------------------------------------------ CLI


def test_cli_mail_and_sites_workday_help():
    runner = CliRunner()
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0 and "mail" in result.output and "sites" in result.output
    result = runner.invoke(cli_app, ["mail", "--help"])
    assert result.exit_code == 0 and "test" in result.output and "wait-code" in result.output
    for cmd in (["mail", "test", "--help"], ["mail", "wait-code", "--help"], ["sites", "forget-account", "--help"],
                ["sites", "check", "--help"], ["sites", "apply", "--help"]):
        out = runner.invoke(cli_app, cmd)
        assert out.exit_code == 0, cmd
    assert "--show" in runner.invoke(cli_app, ["mail", "wait-code", "--help"]).output
    sites_help = runner.invoke(cli_app, ["sites", "--help"]).output
    assert "forget-account" in sites_help and "Workday" in sites_help
    # the cookie-only commands refuse the account site with a clear message, exit 2
    for cmd in (["sites", "import-cookies", "-u", "demo", "-s", "workday", "-f", "x.json"],
                ["sites", "search", "-s", "workday", "-q", "python"]):
        bad = runner.invoke(cli_app, cmd)
        assert bad.exit_code == 2 and "not supported" in (bad.output + str(bad.exception or "")), cmd
    bad = runner.invoke(cli_app, ["sites", "forget-account", "-u", "demo", "-s", "naukri", "-t", "x"])
    assert bad.exit_code == 2 and "not supported" in (bad.output + str(bad.exception or ""))


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


def create_app(**kw):
    from jobapply.web.app import create_app as _create

    return _create(**kw)


def _login(client, username="demo", password="demo1234"):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})
    return csrf


def test_settings_workday_card_counts_accounts_and_points_at_the_mailbox(client, engine):
    _login(client)
    page = client.get("/settings").text
    assert 'id="account-site-workday"' in page and "account per company, created automatically" in page
    assert "0 company accounts" in page and "set up the Mailbox first" in page
    assert "/settings/sites/workday/relogin" not in page and "/settings/sites/workday/cookies" not in page
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        vault = Vault()
        vault.store(s, demo.id, "workday:acme", "login", "demo@example.com", {"password": "test-only"})
        vault.store(s, demo.id, "mailbox", "imap", "demo@gmail.com", {"password": "app-pw", "host": "imap.gmail.com"})
        s.commit()
    page = client.get("/settings").text
    assert "1 company account<" in page and "set up the Mailbox first" not in page and "mailbox" in page
    assert "test-only" not in page and "app-pw" not in page


def test_queue_links_a_mailbox_not_configured_pause_to_settings(client, engine):
    _login(client)
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        app = s.exec(select(Application).where(Application.user_id == demo.id,
                                                Application.status == AppStatus.queued)).first()
        app.status, app.error = AppStatus.paused, "mailbox not configured; My Information"
        other = s.exec(select(Application).where(Application.user_id == demo.id,
                                                  Application.status == AppStatus.dry_run)).first()
        other.error = "captcha on page (recaptcha)"
        s.add(app)
        s.add(other)
        s.commit()
    page = client.get("/queue").text
    assert page.count('href="/settings#mailbox"') == 1 and "Set up your Mailbox" in page
