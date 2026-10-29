"""Pipeline routing for unknown external hosts (navigator path), the synthetic `external` site, and mailto:
apply links (email drafts in dry run, SMTP send only behind every gate). No browser, no network: FormRunner.run
is stubbed and SMTP goes to a fake client."""

from __future__ import annotations

import email as email_lib
from email import policy as email_policy

import pytest
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.forms.runner import FillResult, FormRunner
from jobapply.models import Application, AppStatus, Job, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.pipeline.apply import (
    EXTERNAL_SITE,
    apply_by_email,
    external_site,
    parse_mailto,
    pick_applications,
    route_external_result,
    run_one,
)
from jobapply.seed import DEMO_PROFILE
from jobapply.users import SITES, create_user
from jobapply.vault import Vault, generate_key


def _job(session, user, url, *, ats=None, source="hn", status=AppStatus.approved, suffix=""):
    job = Job(dedupe_key=f"k-{url}{suffix}", title="Engineer", company="Acme", ats_type=ats, apply_url=url,
              source=source, description="Python backend role")
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=80, cover_letter="CL")
    session.add(app)
    session.commit()
    return app, job


def _site(session, user, name) -> UserSite:
    return session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == name)).one()


class _Page:
    def close(self):
        pass


class _Ctx:
    def new_page(self):
        return _Page()


def _stub_runner(monkeypatch, result_factory=None):
    calls: list[dict] = []

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        calls.append({"url": url, "ats_hint": ats_hint, "submit": submit})
        if result_factory is not None:
            return result_factory(url, submit)
        return FillResult(url=url or "", profile_origin="ai", submitted=submit)

    monkeypatch.setattr(FormRunner, "run", fake_run)
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: None)
    return calls


class FakeSmtp:
    sent: list = []

    def __init__(self, cfg):
        self.cfg = cfg

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _clear_smtp():
    FakeSmtp.sent = []


@pytest.fixture
def vault_key(demo, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ the synthetic `external` site


def test_external_site_default_and_lazy_row(session, demo):
    assert SITES[EXTERNAL_SITE] == {"daily_cap": 10, "kind": "ats"}
    row = _site(session, demo, EXTERNAL_SITE)
    assert row.enabled and not row.auto_submit and row.daily_cap == 10 and row.login_status == "n/a"
    # an account created before the key existed gets the row on first use
    old = create_user(session, "older", "older1234", profile=DEMO_PROFILE)
    session.delete(_site(session, old, EXTERNAL_SITE))
    session.commit()
    made = external_site(session, old)
    assert made.site == EXTERNAL_SITE and made.daily_cap == 10 and not made.auto_submit
    assert external_site(session, old).id == made.id


# ------------------------------------------------------------------ route_external_result


def test_unknown_host_with_context_goes_through_the_navigator_path(session, demo, monkeypatch):
    calls = _stub_runner(monkeypatch)
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-1", ats=None, source="naukri")
    out = route_external_result(session, demo, app, job, {"status": "external",
                                                         "apply_url": "https://acme.example/careers/9"},
                                "naukri", context=_Ctx(), llm=None, dry_run=True)
    assert out is not None and out.status == AppStatus.dry_run, out
    assert calls == [{"url": "https://acme.example/careers/9", "ats_hint": None, "submit": False}]
    session.refresh(job)
    assert job.apply_url == "https://acme.example/careers/9" and job.ats_type is None  # no fake ATS written


def test_unknown_host_real_submit_needs_the_external_site_gates(session, demo, monkeypatch):
    calls = _stub_runner(monkeypatch)
    ext = _site(session, demo, EXTERNAL_SITE)
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-2", source="naukri")
    result = {"status": "external", "apply_url": "https://acme.example/careers/9"}
    route_external_result(session, demo, app, job, result, "naukri", context=_Ctx(), dry_run=False)
    assert calls[-1]["submit"] is False  # auto_submit off by default for `external`
    ext.auto_submit = True
    session.add(ext)
    session.commit()
    app2, job2 = _job(session, demo, "https://www.naukri.com/job-listings-3", source="naukri")
    out = route_external_result(session, demo, app2, job2, result, "naukri", context=_Ctx(), dry_run=False)
    assert calls[-1]["submit"] is True and out.status == AppStatus.applied
    # the site cap counts it: once at the cap, the next one is filled for review only
    ext.daily_cap = 1
    session.add(ext)
    session.commit()
    app3, job3 = _job(session, demo, "https://www.naukri.com/job-listings-4", source="naukri")
    assert apply_mod.applied_today(session, demo.id, EXTERNAL_SITE) == 0  # ats_type stays None for unknown hosts
    ext.enabled = False
    session.add(ext)
    session.commit()
    route_external_result(session, demo, app3, job3, result, "naukri", context=_Ctx(), dry_run=False)
    assert calls[-1]["submit"] is False  # a disabled external site never submits


def test_unknown_host_without_context_still_pauses(session, demo, monkeypatch):
    calls = _stub_runner(monkeypatch)
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-5", source="naukri")
    out = route_external_result(session, demo, app, job, {"status": "external",
                                                         "apply_url": "https://acme.example/careers/9"}, "naukri")
    assert out.status == AppStatus.paused and "manually" in out.detail and calls == []


def test_navigator_pause_reason_is_recorded(session, demo, monkeypatch):
    def paused(url, submit):
        return FillResult(url=url, profile_origin="none",
                          paused_reason="acme.example needs an account; not created (dry run or auto-submit off)")

    _stub_runner(monkeypatch, paused)
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-6", source="naukri")
    out = route_external_result(session, demo, app, job, {"status": "external",
                                                         "apply_url": "https://acme.example/careers/9"},
                                "naukri", context=_Ctx(), dry_run=True)
    assert out.status == AppStatus.paused and "needs an account" in app.error


# ------------------------------------------------------------------ LinkedIn offsite loop


def test_linkedin_offsite_unknown_host_uses_the_external_site(session, demo, monkeypatch):
    from jobapply.sites.linkedin.resolve import ApplyResolution
    from tests.test_pipeline import _enable_linkedin, _li_job, _wire_external

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="901")
    seen: dict = {}
    real = apply_mod._run_resolved_external

    def spy(session_, user, app_, job_, gov_site, page, llm, dry_run):
        seen["gov_site"] = gov_site.site if gov_site is not None else None
        seen["dry_run"] = dry_run
        return real(session_, user, app_, job_, gov_site, page, llm, dry_run)

    monkeypatch.setattr(apply_mod, "_run_resolved_external", spy)
    _, runner_calls, _ = _wire_external(monkeypatch, {job.apply_url: ApplyResolution("offsite",
                                                                                     "https://acme.example/jobs/1",
                                                                                     None)})
    out = apply_mod.apply_external_linkedin_user(None, session, demo, limit=5, dry_run=False)
    assert [o.status for o in out] == [AppStatus.dry_run]  # external auto_submit is off: filled for review
    # the unknown host is governed by the `external` site; the global dry-run switch (on by default) wins
    assert seen == {"gov_site": EXTERNAL_SITE, "dry_run": True}
    assert runner_calls == [{"url": "https://acme.example/jobs/1", "ats_hint": None, "submit": False}]


# ------------------------------------------------------------------ mailto: apply links


def test_parse_mailto():
    assert parse_mailto("mailto:jobs@acme.example?subject=Backend%20role&body=Hi") == ("jobs@acme.example",
                                                                                        "Backend role", "Hi")
    assert parse_mailto("mailto:a@x.io,b@x.io") == ("a@x.io", "", "")
    assert parse_mailto("https://x.io") == ("", "", "")
    assert parse_mailto("MAILTO:?to=hr@x.io")[0] == "hr@x.io"


def test_pick_applications_includes_mailto_under_the_email_site(session, demo):
    app, job = _job(session, demo, "mailto:jobs@acme.example")
    picked = pick_applications(session, demo, limit=10)
    assert [(a.id, s.site) for a, _, s in picked] == [(app.id, "email")]
    email_site = _site(session, demo, "email")
    email_site.enabled = False
    session.add(email_site)
    session.commit()
    assert pick_applications(session, demo, limit=10) == []


def test_mailto_dry_run_writes_an_eml_draft_and_pauses(session, demo, resume, monkeypatch):
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: None)
    app, job = _job(session, demo, "mailto:jobs@acme.example?subject=Backend%20Engineer")
    app.resume_variant = str(resume)
    session.add(app)
    session.commit()
    out = run_one(session, demo, app, job, _site(session, demo, "email"), page=None, llm=None, dry_run=True)
    assert out.status == AppStatus.paused and "email draft written for review (dry run)" in out.detail
    drafts = list((get_settings().screenshots_dir / str(demo.id)).glob("*.eml"))
    assert len(drafts) == 1
    msg = email_lib.message_from_bytes(drafts[0].read_bytes(), policy=email_policy.default)
    assert msg["To"] == "jobs@acme.example" and msg["Subject"] == "Backend Engineer"
    assert msg["From"].endswith(f"<{DEMO_PROFILE['email']}>")
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Demo User" in body and "Acme" in body and "resume is attached" in body
    assert [p.get_filename() for p in msg.iter_attachments()] == ["resume.pdf"]
    assert app.answers["To"] == "jobs@acme.example" and app.answers["Resume"] == "resume.pdf"
    assert app.submitted_at is None and FakeSmtp.sent == []
    session.refresh(job)
    assert job.ats_type == "email"


def test_mailto_send_needs_every_gate(session, demo, resume, vault_key, monkeypatch):
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: None)
    app, job = _job(session, demo, "mailto:jobs@acme.example")
    app.resume_variant = str(resume)
    session.add(app)
    session.commit()
    # not dry run, auto-submit off: a draft, nothing sent
    out = apply_by_email(session, demo, app, job, None, dry_run=False, auto_submit=False, smtp_factory=FakeSmtp)
    assert out.status == AppStatus.paused and "auto-submit off" in out.detail and FakeSmtp.sent == []
    # every switch on but no mailbox: paused with the reason
    app2, job2 = _job(session, demo, "mailto:jobs@acme.example", suffix="-2")
    out = apply_by_email(session, demo, app2, job2, None, dry_run=False, auto_submit=True, smtp_factory=FakeSmtp)
    assert out.status == AppStatus.paused and "mailbox not configured" in out.detail and FakeSmtp.sent == []
    # mailbox configured: sent, applied, counted
    Vault().store(session, demo.id, "mailbox", "smtp", DEMO_PROFILE["email"],
                  {"password": "app-pw-test-only", "host": "smtp.example.com", "port": "587"})
    session.commit()
    app3, job3 = _job(session, demo, "mailto:jobs@acme.example?subject=Hi", suffix="-3")
    app3.resume_variant = str(resume)
    out = apply_by_email(session, demo, app3, job3, None, dry_run=False, auto_submit=True, smtp_factory=FakeSmtp)
    assert out.status == AppStatus.applied and app3.submitted_at is not None
    assert len(FakeSmtp.sent) == 1 and FakeSmtp.sent[0]["To"] == "jobs@acme.example"
    assert FakeSmtp.sent[0]["Subject"] == "Hi"
    assert [p.get_filename() for p in FakeSmtp.sent[0].iter_attachments()] == ["resume.pdf"]
    assert "app-pw-test-only" not in FakeSmtp.sent[0].as_string()
    session.commit()
    assert apply_mod.applied_today(session, demo.id, "email") == 1


def test_resolved_external_mailto_from_the_navigator_is_applied_by_email(session, demo, resume, monkeypatch):
    def found_mailto(url, submit):
        return FillResult(url=url, profile_origin="none", paused_reason="apply by email: mailto:hr@acme.example",
                          mailto="mailto:hr@acme.example")

    _stub_runner(monkeypatch, found_mailto)
    app, job = _job(session, demo, "https://www.naukri.com/job-listings-7", source="naukri")
    app.resume_variant = str(resume)
    session.add(app)
    session.commit()
    out = route_external_result(session, demo, app, job, {"status": "external",
                                                         "apply_url": "https://acme.example/careers/9"},
                                "naukri", context=_Ctx(), dry_run=True)
    assert out.status == AppStatus.paused and "email draft written" in out.detail
    session.refresh(job)
    assert job.apply_url == "mailto:hr@acme.example" and job.ats_type == "email"
    assert list((get_settings().screenshots_dir / str(demo.id)).glob("*.eml"))
