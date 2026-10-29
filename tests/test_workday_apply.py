"""Workday apply: ``apply_job`` end to end on the mini-Workday fixture (account step + wizard, real headless
Chromium, fake IMAP, real vault with a test key) and the gated per-user orchestrator with ``apply_job`` stubbed."""

from __future__ import annotations

import imaplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobapply.config import get_settings
from jobapply.models import Application, AppStatus, Job, UserSite
from jobapply.repo import UserScope
from jobapply.sites.workday import apply as wa
from jobapply.sites.workday import session as ws
from jobapply.sites.workday.account import MAILBOX_LOGIN_FAILED, password_ok
from jobapply.sites.workday.apply import (
    ApplyResult,
    apply_job,
    apply_workday_user,
    mailbox_reader,
    pick_workday_applications,
)
from jobapply.switches import set_value
from jobapply.vault import Vault
from tests.test_workday_account import (
    CODE,
    JOB_URL,
    NO_PAUSE,
    client_factory_for,
    code_mail,
    fixture_html,
    raw_mail,
    route_tenant,
    vault,  # noqa: F401 - fixture
    wd_state,
)
from tests.test_workday_wizard import StubAnswers

PROFILES = Path(__file__).parent.parent / "config" / "form_profiles"


# ------------------------------------------------------------------ helpers


def _job_and_app(session, user, url: str = JOB_URL, *, ats: str | None = "workday", status=AppStatus.approved,
                 resume: Path | None = None, score: int = 80) -> tuple[Job, Application]:
    job = Job(dedupe_key=f"wd:{url[-24:]}", title="Python Developer", company="Acme", url=url, apply_url=url,
              source="workday", ats_type=ats)
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score,
                      resume_variant=str(resume) if resume else None)
    session.add(app)
    session.commit()
    return job, app


def _mailbox(vault_obj: Vault, session, user) -> None:
    vault_obj.store(session, user.id, "mailbox", "imap", "demo.applies@example.com",
                    {"password": "mailbox-secret", "host": "imap.example.com", "port": "993"})
    session.commit()


def _run(page, session, demo, app, *, dry_run, auto_submit, vault_obj=None, messages=None, shot=None, **kw):
    wd = ws.WorkdaySession(None, demo, session, page=page)
    return apply_job(wd, app, dry_run=dry_run, auto_submit=auto_submit, answers=StubAnswers(),
                     screenshot_path=shot, vault=vault_obj, client_factory=client_factory_for(messages or []),
                     mail_timeout=1, pause=NO_PAUSE, profile=PROFILES / "workday.yaml", **kw)


# ------------------------------------------------------------------ apply_job on the fixture


def test_apply_result_maps_onto_fill_result():
    r = ApplyResult(url="u", status="applied", reason="applied on acme", screenshot="s.png",
                    answers={"Notice": "30"}, recoveries=["x"])
    f = r.to_fill_result()
    assert f.submitted and f.screenshot_path == "s.png" and f.profile_origin == wa.PROFILE_ORIGIN
    assert f.answers == {"Notice": "30"} and f.recoveries == ["x"]
    assert ApplyResult(url="u", status="dry_run").to_fill_result().paused_reason == ""
    assert ApplyResult(url="u", status="needs_human", reason="q").to_fill_result().paused_reason == "q"
    assert ApplyResult(url="u", status="paused", reason="mailbox not configured").to_fill_result().paused_reason
    assert ApplyResult(url="u", status="skipped", reason="already").to_fill_result().paused_reason == "already"
    assert ApplyResult(url="u", status="failed", reason="boom").to_fill_result().error == "boom"
    assert set(ApplyResult(url="u").as_dict()) >= {"status", "screenshot", "reason", "apply_url", "ats_type",
                                                   "login_status", "stop_run", "tenant", "steps"}


def test_non_workday_url_fails_fast(page, session, demo):
    _, app = _job_and_app(session, demo, "https://boards.greenhouse.io/acme/jobs/1", ats="greenhouse")
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False)
    assert out["status"] == "failed" and out["reason"].startswith("not a Workday job URL")


def test_dry_run_without_an_account_creates_nothing(page, session, demo, vault, tmp_path):  # noqa: F811
    route_tenant(page, fixture_html(social=True, verify=True, code=CODE))
    _, app = _job_and_app(session, demo)
    shot = tmp_path / "shots" / "wd.png"
    out = _run(page, session, demo, app, dry_run=True, auto_submit=True, vault_obj=vault, messages=[code_mail()],
               shot=shot)
    assert out["status"] == "dry_run" and out["reason"].startswith("global dry run: no account on acme")
    assert out["account_state"] == "login" and out["account_created"] is False and out["tenant"] == "acme"
    assert wd_state(page).get("created") is None and wd_state(page).get("sign_in_attempts", 0) == 0
    assert vault.load(session, demo.id, "workday:acme", "login") is None  # no password generated either
    assert out["screenshot"] == str(shot) and shot.exists()


def test_real_run_without_a_mailbox_pauses_and_stops_the_run(page, session, demo, vault):  # noqa: F811
    route_tenant(page, fixture_html(verify=True, code=CODE))
    _, app = _job_and_app(session, demo)
    out = _run(page, session, demo, app, dry_run=False, auto_submit=True, vault_obj=vault)
    assert out["status"] == "paused" and out["reason"] == "mailbox not configured" and out["stop_run"] is True
    assert wd_state(page).get("created") is None
    assert vault.load(session, demo.id, "workday:acme", "login") is None


def test_real_run_without_a_vault_pauses(page, session, demo, monkeypatch):
    monkeypatch.setattr(wa, "_vault_or_none", lambda: None)  # JOBAPPLY_VAULT_KEY unset / unreadable
    route_tenant(page, fixture_html())
    _, app = _job_and_app(session, demo)
    out = _run(page, session, demo, app, dry_run=False, auto_submit=True, vault_obj=None)
    assert out["status"] == "paused" and out["reason"].startswith("vault not configured") and out["stop_run"]


def test_real_run_creates_the_account_verifies_by_mail_and_walks_the_wizard(page, session, demo, vault,  # noqa: F811
                                                                             resume, tmp_path):
    route_tenant(page, fixture_html(social=True, verify=True, code=CODE))
    _mailbox(vault, session, demo)
    _, app = _job_and_app(session, demo, resume=resume)
    shot = tmp_path / "shots" / "wd.png"
    out = _run(page, session, demo, app, dry_run=False, auto_submit=False, vault_obj=vault,
               messages=[raw_mail("noreply@linkedin.com", "Code", "Your verification code is 111111"), code_mail()],
               shot=shot)
    assert out["status"] == "dry_run", out["reason"]
    assert out["reason"] == "auto_submit off for workday: Review reached, Submit not clicked"
    assert out["account_created"] is True and out["account_state"] == "wizard" and out["reached_review"]
    assert out["submit_clicked"] is False and out["submitted"] is False
    st = wd_state(page)
    assert st["created"] == 1 and st["verified"] == 1 and st["next_clicks"] == 4 and st.get("submitted") is None
    assert st["created_email"] == "demo.applies@example.com" and st["marketing_checked"] is False
    stored = vault.load(session, demo.id, "workday:acme", "login")
    assert stored["username"] == "demo.applies@example.com" and stored["verified"] is True
    assert password_ok(stored["password"]) and stored["password"] == st["created_password"]
    assert stored["password"] != "mailbox-secret"  # never the mailbox password
    assert "password" not in out["reason"] and st["created_password"] not in str(out)  # never in results
    assert [s["step"] for s in out["steps"]] == ["My Information", "My Experience", "Application Questions",
                                                 "Voluntary Disclosures"]
    assert out["answers"]["What is your notice period?*"] == "30 days"
    assert Path(out["screenshot"]).exists() and (shot.parent / "wd-my-experience.png").exists()


def test_real_run_with_stored_credentials_signs_in_and_submits(page, session, demo, vault, resume):  # noqa: F811
    vault.store(session, demo.id, "workday:acme", "login", "demo.applies@example.com",
                {"password": "Stored-Pw-1", "tenant": "acme", "site": "Acme", "verified": True})
    session.commit()
    route_tenant(page, fixture_html(account_email="demo.applies@example.com", account_password="Stored-Pw-1"))
    _, app = _job_and_app(session, demo, resume=resume)
    out = _run(page, session, demo, app, dry_run=False, auto_submit=True, vault_obj=vault)
    assert out["status"] == "applied" and out["submitted"] and out["submit_clicked"], out["reason"]
    assert out["account_created"] is False
    st = wd_state(page)
    assert st["sign_in_attempts"] == 1 and st.get("created") is None and st["submitted"] == 1
    assert st["submit_clicks"] == 1 and st["honeypot"] == ""


def test_stored_credentials_rejected_pauses_without_creating(page, session, demo, vault):  # noqa: F811
    vault.store(session, demo.id, "workday:acme", "login", "demo.applies@example.com",
                {"password": "Old-Pw-1", "tenant": "acme", "site": "Acme"})
    session.commit()
    route_tenant(page, fixture_html(account_email="demo.applies@example.com", account_password="New-Pw-1"))
    _, app = _job_and_app(session, demo)
    out = _run(page, session, demo, app, dry_run=False, auto_submit=True, vault_obj=vault)
    assert out["status"] == "paused" and out["reason"].startswith("workday sign-in rejected on acme")
    assert "incorrect" in out["reason"] and out["login_status"] is None and not out["stop_run"]
    assert wd_state(page).get("created") is None


def test_sign_in_that_asks_for_a_code_reads_the_mailbox(page, session, demo, vault, resume):  # noqa: F811
    vault.store(session, demo.id, "workday:acme", "login", "demo.applies@example.com",
                {"password": "Stored-Pw-1", "tenant": "acme", "site": "Acme"})
    _mailbox(vault, session, demo)
    route_tenant(page, fixture_html(account_email="demo.applies@example.com", account_password="Stored-Pw-1",
                                    verify_on_sign_in=True, code=CODE))
    _, app = _job_and_app(session, demo, resume=resume)
    out = _run(page, session, demo, app, dry_run=True, auto_submit=True, vault_obj=vault, messages=[code_mail()])
    assert out["status"] == "dry_run" and out["reached_review"], out["reason"]
    assert wd_state(page)["verified"] == 1 and wd_state(page).get("submitted") is None


def test_mailbox_login_refused_pauses_and_stops_the_run(page, session, demo, vault):  # noqa: F811
    route_tenant(page, fixture_html(verify=True, code=CODE))
    _mailbox(vault, session, demo)
    _, app = _job_and_app(session, demo)
    attempts: list[int] = []

    def refused(cfg):
        attempts.append(1)
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    wd = ws.WorkdaySession(None, demo, session, page=page)
    out = apply_job(wd, app, dry_run=False, auto_submit=False, answers=StubAnswers(), vault=vault,
                    client_factory=refused, mail_timeout=1, pause=NO_PAUSE, profile=PROFILES / "workday.yaml")
    assert out["status"] == "paused" and out["reason"] == MAILBOX_LOGIN_FAILED and out["stop_run"] is True
    assert len(attempts) == 1 and out["account_created"] is True


def test_uncreated_stored_row_falls_through_to_account_creation(page, session, demo, vault, resume):  # noqa: F811
    # The previous run generated a password but never got the create form sent (`created` False): there is no
    # account to sign in to, so the row is replaced and the account created instead of "sign-in rejected" forever.
    vault.store(session, demo.id, "workday:acme", "login", "demo.applies@example.com",
                {"password": "Never-Sent-1", "tenant": "acme", "site": "Acme", "verified": False, "created": False})
    _mailbox(vault, session, demo)
    route_tenant(page, fixture_html(verify=True, code=CODE))
    _, app = _job_and_app(session, demo, resume=resume)
    out = _run(page, session, demo, app, dry_run=False, auto_submit=False, vault_obj=vault, messages=[code_mail()])
    assert out["status"] == "dry_run" and out["account_created"] is True and out["reached_review"], out["reason"]
    st = wd_state(page)
    assert st.get("sign_in_attempts", 0) == 0 and st["created"] == 1 and st["verified"] == 1
    stored = vault.load(session, demo.id, "workday:acme", "login")
    assert stored["created"] is True and stored["verified"] is True and stored["password"] != "Never-Sent-1"
    assert stored["password"] == st["created_password"]


def test_apply_job_stamps_ats_type_on_a_bare_workday_url(page, session, demo, vault):  # noqa: F811
    route_tenant(page, fixture_html())
    job, app = _job_and_app(session, demo, ats=None)
    job.source = "rss"  # a non-ATS collector found the posting
    session.add(job)
    session.commit()
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "dry_run" and session.get(Job, job.id).ats_type == "workday"


def test_apply_job_builds_the_runner_without_the_navigator(page, session, demo, vault, resume, monkeypatch):  # noqa: F811
    built: list[bool] = []

    class Recording(wa.FormRunner):
        def __init__(self, *args, **kwargs):
            built.append(kwargs.get("navigate", True))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(wa, "FormRunner", Recording)
    route_tenant(page, fixture_html(signed_in=True))
    _, app = _job_and_app(session, demo, resume=resume)
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "dry_run" and out["reached_review"], out["reason"]
    assert built == [False]  # the WorkdayWizard owns every Save and Continue click
    assert wd_state(page)["next_clicks"] == 4


def test_captcha_and_already_applied_and_unknown_pages(page, session, demo, vault):  # noqa: F811
    _, app = _job_and_app(session, demo)
    route_tenant(page, fixture_html(captcha=True))
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "paused" and out["login_status"] == "challenge" and "captcha" in out["reason"]
    page.unroute("https://acme.wd5.myworkdayjobs.com/**")
    route_tenant(page, fixture_html(already_applied=True))
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "skipped" and out["reason"].startswith("already applied on acme")
    page.unroute("https://acme.wd5.myworkdayjobs.com/**")
    route_tenant(page, "<html><body><p>Loading</p></body></html>")
    out = _run(page, session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "paused" and out["reason"].startswith("apply page did not render")


def test_navigation_failure_is_failed(session, demo, vault):  # noqa: F811
    class P:
        url = JOB_URL

        def goto(self, *a, **kw):
            raise TimeoutError("page.goto timed out")

    _, app = _job_and_app(session, demo)
    out = _run(P(), session, demo, app, dry_run=True, auto_submit=False, vault_obj=vault)
    assert out["status"] == "failed" and out["reason"].startswith("apply page did not load")


def test_default_screenshot_lives_under_data_screenshots(page, session, demo, vault):  # noqa: F811
    route_tenant(page, fixture_html())
    _, app = _job_and_app(session, demo)
    wd = ws.WorkdaySession(None, demo, session, page=page)
    out = apply_job(wd, app, dry_run=True, auto_submit=False, answers=StubAnswers(), vault=vault, pause=NO_PAUSE)
    assert out["status"] == "dry_run"
    assert Path(out["screenshot"]).is_relative_to(get_settings().screenshots_dir / str(demo.id))
    assert Path(out["screenshot"]).exists()


def test_mailbox_reader_needs_a_configured_mailbox(session, demo, vault):  # noqa: F811
    assert mailbox_reader(session, demo) is None
    _mailbox(vault, session, demo)
    reader = mailbox_reader(session, demo, client_factory=client_factory_for([]))
    assert reader is not None and reader.cfg.host == "imap.example.com"


# ------------------------------------------------------------------ picks


def test_pick_workday_applications(session, demo):
    j1, a1 = _job_and_app(session, demo, JOB_URL)
    j2, a2 = _job_and_app(session, demo, "https://beta.wd3.myworkdayjobs.com/Careers/job/Remote/Dev_R-2", ats=None,
                          status=AppStatus.queued)
    _job_and_app(session, demo, "https://boards.greenhouse.io/acme/jobs/1", ats="greenhouse")
    _job_and_app(session, demo, "https://acme.wd5.myworkdayjobs.com/Acme/job/Pune/Dev_JR9", status=AppStatus.new)
    picks = pick_workday_applications(session, demo, 10)
    assert [a.id for a, _ in picks] == [a1.id, a2.id]
    assert len(pick_workday_applications(session, demo, 1)) == 1


# ------------------------------------------------------------------ orchestrator (apply_job stubbed)


@pytest.fixture
def workday_site(session, demo) -> UserSite:
    site = UserScope(session, demo).site("workday")
    site.enabled = True
    session.add(site)
    session.commit()
    return site


class FakePage:
    url = "about:blank"

    def wait_for_timeout(self, ms):
        pass


def _stub_apply(monkeypatch, results: list[dict]):
    calls: list[dict] = []

    def fake(wd, app, *, dry_run, auto_submit, **kw):
        calls.append({"app": app.id, "dry_run": dry_run, "auto_submit": auto_submit})
        r = results.pop(0)
        return {**ApplyResult(url="u").as_dict(), "status": "dry_run", **r}

    monkeypatch.setattr(wa, "apply_job", fake)
    return calls


def test_orchestrator_kill_switch_and_disabled_site(session, demo, monkeypatch):
    _job_and_app(session, demo)
    calls = _stub_apply(monkeypatch, [])
    assert apply_workday_user(None, session, demo, page=FakePage()) == []  # site disabled by default
    site = UserScope(session, demo).site("workday")
    site.enabled = True
    session.commit()
    set_value(session, "kill_switch", "on")
    session.commit()
    assert apply_workday_user(None, session, demo, page=FakePage()) == []
    assert calls == []


def test_orchestrator_global_dry_run_wins_and_auto_submit_is_passed(session, demo, workday_site, monkeypatch):
    _, app = _job_and_app(session, demo)
    calls = _stub_apply(monkeypatch, [{"status": "dry_run", "reason": "global dry run: Review reached"}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), dry_run=False, between=lambda p: None)
    assert calls == [{"app": app.id, "dry_run": True, "auto_submit": False}]  # admin dry_run switch defaults to on
    assert [o.status for o in outcomes] == [AppStatus.dry_run]
    assert session.get(Application, app.id).status == AppStatus.dry_run


def test_orchestrator_real_submit_needs_every_switch_off(session, demo, workday_site, monkeypatch):
    _, app = _job_and_app(session, demo)
    workday_site.auto_submit = True
    session.add(workday_site)
    set_value(session, "dry_run", "off")
    session.commit()
    monkeypatch.setattr(get_settings(), "dry_run", False)
    calls = _stub_apply(monkeypatch, [{"status": "applied", "reason": "applied on acme", "submitted": True}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), dry_run=False, between=lambda p: None)
    assert calls == [{"app": app.id, "dry_run": False, "auto_submit": True}]
    assert [o.status for o in outcomes] == [AppStatus.applied]
    a = session.get(Application, app.id)
    assert a.status == AppStatus.applied and a.submitted_at is not None


def test_orchestrator_cooldown_skips_and_challenge_records_it(session, demo, workday_site, monkeypatch):
    _, a1 = _job_and_app(session, demo)
    _job_and_app(session, demo, "https://acme.wd5.myworkdayjobs.com/Acme/job/Pune/Dev_JR9")
    calls = _stub_apply(monkeypatch, [{"status": "paused", "reason": "captcha / security check on the apply page",
                                       "login_status": "challenge"}, {"status": "applied"}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), limit=5, between=lambda p: None)
    assert len(outcomes) == 1 and len(calls) == 1 and outcomes[0].status == AppStatus.paused
    assert workday_site.login_status == "challenge" and ws.cooldown_until(demo) > datetime.now(UTC)
    # the cooldown keeps the next run away entirely
    assert apply_workday_user(None, session, demo, page=FakePage(), limit=5) == []
    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    demo.preferences = {**(demo.preferences or {}), ws.COOLDOWN_KEY: expired}
    workday_site.login_status = "ok"
    session.add(demo)
    session.add(workday_site)
    session.commit()
    assert ws.throttled(demo, workday_site) is False


def test_orchestrator_stop_run_ends_after_the_first_application(session, demo, workday_site, monkeypatch):
    monkeypatch.setattr(wa.random, "shuffle", lambda items: None)
    _, a1 = _job_and_app(session, demo, score=90)
    _job_and_app(session, demo, "https://acme.wd5.myworkdayjobs.com/Acme/job/Pune/Dev_JR9", score=70)
    calls = _stub_apply(monkeypatch, [{"status": "paused", "reason": "mailbox not configured", "stop_run": True},
                                      {"status": "dry_run"}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), limit=5, between=lambda p: None)
    assert len(calls) == 1 and [o.status for o in outcomes] == [AppStatus.paused]
    a = session.get(Application, a1.id)
    assert a.status == AppStatus.paused and "mailbox not configured" in (a.error or "")


def test_orchestrator_needs_human_is_paused_and_daily_caps_hold(session, demo, workday_site, monkeypatch):
    monkeypatch.setattr(wa.random, "shuffle", lambda items: None)  # keep the pick order deterministic
    _, a1 = _job_and_app(session, demo, score=90)
    _, a2 = _job_and_app(session, demo, "https://acme.wd5.myworkdayjobs.com/Acme/job/Pune/Dev_JR9", score=70)
    _stub_apply(monkeypatch, [{"status": "needs_human", "reason": "a question needs a human",
                               "needs_human": ["Which team?"]}, {"status": "dry_run"}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), limit=5, between=lambda p: None)
    assert [o.status for o in outcomes] == [AppStatus.paused, AppStatus.dry_run]
    assert "Which team" in (session.get(Application, a1.id).error or "")
    # caps: one real submit fills the site cap of 1, the second pick is not attempted
    for a in (a1, a2):
        a.status = AppStatus.approved
        session.add(a)
    workday_site.auto_submit = True
    workday_site.daily_cap = 1
    session.add(workday_site)
    set_value(session, "dry_run", "off")
    session.commit()
    monkeypatch.setattr(get_settings(), "dry_run", False)
    calls = _stub_apply(monkeypatch, [{"status": "applied", "submitted": True},
                                      {"status": "applied", "submitted": True}])
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), limit=5, dry_run=False,
                                  between=lambda p: None)
    assert len(outcomes) == 1 and len(calls) == 1 and outcomes[0].status == AppStatus.applied
    assert apply_workday_user(None, session, demo, page=FakePage(), limit=5, dry_run=False,
                              between=lambda p: None) == []


def test_orchestrator_crash_is_recorded_as_failed(session, demo, workday_site, monkeypatch):
    _, app = _job_and_app(session, demo)

    def boom(*a, **k):
        raise TimeoutError("page.goto timed out")

    monkeypatch.setattr(wa, "apply_job", boom)
    outcomes = apply_workday_user(None, session, demo, page=FakePage(), between=lambda p: None)
    assert outcomes[0].status == AppStatus.failed and "TimeoutError" in outcomes[0].detail


def test_active_hours_gate(monkeypatch):
    import jobapply.sites.browser as b

    monkeypatch.setattr(b, "is_active_hours", lambda **kw: False)
    assert wa.apply_workday() == []


def test_session_check_is_per_tenant(session, demo, workday_site):
    wd = ws.WorkdaySession(None, demo, session, page=FakePage())
    assert wd.check() == "ok"
    wd.record_challenge()
    assert wd.check() == "challenge" and ws.throttled(demo, workday_site)
    wd.record_ok()
    assert wd.check() == "ok" and ws.cooldown_until(demo) is None
    with pytest.raises(RuntimeError):
        _ = ws.WorkdaySession(None, demo, session).page
