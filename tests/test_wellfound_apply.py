"""Wellfound apply: the safety gates without a browser, and the modal flow in headless Chromium against an
inline fixture (job page + apply dialog) written to tmp_path. The modal markup is a stand-in: the real
logged-in modal could not be recorded (docs/sites/WELLFOUND.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobapply.config import get_settings
from jobapply.models import Application, AppStatus, Job, utcnow
from jobapply.repo import UserScope
from jobapply.sites.wellfound import apply as wa
from jobapply.sites.wellfound.session import COOLDOWN_KEY, WellfoundSession
from jobapply.switches import set_value

NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(20)  # noqa: E731

FIXTURE = """<!DOCTYPE html><html><head><title>Python Developer at Acme | Wellfound</title></head><body>
<div id="state" data-submitted="0" data-closed="0"></div>
<div id="__next"><header><nav>{header}</nav></header>
<div data-test="JobListing"><h1>Python Developer</h1>
{apply}
<div id="job-description"><p>Build things in Python.</p></div></div>
<div id="dlg" role="dialog" data-test="JobApplicationModal" hidden>
  <label for="note">Write a note to Acme</label>
  <textarea id="note" name="note" placeholder="Write a note to Acme"></textarea>
  <label for="q1">Years of Python experience</label>
  <input id="q1" name="q1" type="text" required>
  <label for="q2">Are you authorized to work in India?</label>
  <select id="q2" name="q2"><option value="">Select</option><option>Yes</option><option>No</option></select>
  {extra}
  <button type="button" data-test="JobApplicationSubmitButton"
    onclick="document.getElementById('state').dataset.submitted='1';document.getElementById('dlg').hidden=true;
             document.getElementById('done').hidden=false;">Send application</button>
  <button type="button" aria-label="Close" onclick="document.getElementById('dlg').hidden=true;
    document.getElementById('state').dataset.closed='1';">Close</button>
</div>
<div id="done" data-test="JobApplicationSuccess" hidden>Application sent</div>
</body></html>"""
APPLY_BUTTON = ("<button data-test=\"JobApplicationApplyButton\" type=\"button\" "
                "onclick=\"document.getElementById('dlg').hidden=false\">Apply Now</button>")
LOGIN_HEADER = "<button onclick=\"window.location.href='/login?after_sign_in=%2Fjobs'\">Log In</button>"


def fixture(tmp_path: Path, name: str = "job", *, header: str = "<a href='/logout'>Log out</a>",
            apply: str = APPLY_BUTTON, extra: str = "") -> str:
    path = tmp_path / f"{name}.html"
    path.write_text(FIXTURE.format(header=header, apply=apply, extra=extra), encoding="utf-8")
    return path.resolve().as_uri()


def state(page) -> dict[str, str]:
    return page.evaluate("() => ({...document.getElementById('state').dataset})")


class StubAnswers:
    def __init__(self, canned: dict[str, tuple[str | None, int]] | None = None):
        self.canned = canned if canned is not None else {"years": ("4", 100), "authorized": ("Yes", 90)}
        self.asked: list[tuple[str, list[str] | None]] = []

    def answer(self, question, options=None):
        self.asked.append((question, options))
        for needle, (answer, conf) in self.canned.items():
            if needle.lower() in question.lower():
                return answer, conf
        return None, 0


@pytest.fixture
def job_app(session, demo):
    job = Job(dedupe_key="wf-1", title="Python Developer", company="Acme", location="Pune",
              url="https://wellfound.com/jobs/1-python-developer", apply_url="https://wellfound.com/jobs/1-python-developer",
              description="Build things in Python.", ats_type="wellfound", source="wellfound")
    session.add(job)
    session.flush()
    app = Application(user_id=demo.id, job_id=job.id, status=AppStatus.approved, score=80, message="Hello Acme, I fit.")
    session.add(app)
    session.commit()
    return app, job


@pytest.fixture
def enabled_site(session, demo):
    site = UserScope(session, demo).site("wellfound")
    site.enabled = True
    site.login_status = "ok"
    session.add(site)
    session.commit()
    return site


@pytest.fixture
def wf(session, demo, browser, monkeypatch):
    """A WellfoundSession over a fresh headless context; file:// fixture URLs pass the job-url check."""
    monkeypatch.setattr(wa, "is_wellfound_job_url", lambda url: True)
    monkeypatch.setattr(wa, "active_hours_spec", lambda: "00:00-00:00")
    ctx = browser.new_context()
    s = WellfoundSession(ctx, demo, session, answers=StubAnswers())
    yield s
    ctx.close()


def run(wf, app, job, *, dry_run=True, auto_submit=False, **kw):
    kw.setdefault("pause", NO_PAUSE)
    return wa.apply_job(wf, app, dry_run=dry_run, auto_submit=auto_submit, job=job, **kw)


def allow_real_submit(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "dry_run", False)
    set_value(session, "dry_run", "off")
    session.commit()


# ------------------------------------------------------------------ gates (no browser)


def test_gates_stop_before_the_browser(session, demo, job_app, monkeypatch):
    app, job = job_app
    s = WellfoundSession(None, demo, session)  # no context: reaching the browser would raise
    monkeypatch.setattr(wa, "active_hours_spec", lambda: "00:00-00:00")
    r = run(s, app, job)
    assert r["status"] == "skipped" and "disabled" in r["reason"]
    site = UserScope(session, demo).site("wellfound")
    site.enabled = True
    session.add(site)
    session.commit()
    set_value(session, "kill_switch", "on")
    session.commit()
    assert run(s, app, job)["reason"] == "kill switch on"
    set_value(session, "kill_switch", "off")
    demo.preferences = {**demo.preferences, COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    assert "throttled" in run(s, app, job)["reason"]
    demo.preferences = {k: v for k, v in demo.preferences.items() if k != COOLDOWN_KEY}
    monkeypatch.setattr("jobapply.sites.browser.is_active_hours", lambda now=None, spec="": False)
    assert "active hours" in run(s, app, job)["reason"]
    monkeypatch.setattr("jobapply.sites.browser.is_active_hours", lambda now=None, spec="": True)
    job.apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    r = run(s, app, job)
    assert r["status"] == "failed" and r["reason"] == "not a wellfound job page"


def test_daily_caps_apply_only_to_real_runs(session, demo, job_app, enabled_site, monkeypatch):
    app, job = job_app
    s = WellfoundSession(None, demo, session)
    monkeypatch.setattr(wa, "active_hours_spec", lambda: "00:00-00:00")
    other = Job(dedupe_key="wf-2", title="Done", company="Acme", apply_url="https://wellfound.com/jobs/2-x",
                ats_type="wellfound", source="wellfound")
    session.add(other)
    session.flush()
    session.add(Application(user_id=demo.id, job_id=other.id, status=AppStatus.applied, submitted_at=utcnow()))
    enabled_site.daily_cap = 1
    session.add(enabled_site)
    session.commit()
    allow_real_submit(session, monkeypatch)
    r = run(s, app, job, dry_run=False, auto_submit=True)
    assert r["status"] == "skipped" and r["reason"] == "wellfound daily cap reached"
    enabled_site.daily_cap = 5
    demo.preferences = {**demo.preferences, "daily_cap": 1}
    session.add(enabled_site)
    session.commit()
    assert run(s, app, job, dry_run=False, auto_submit=True)["reason"] == "daily cap reached"


# ------------------------------------------------------------------ the modal in headless Chromium


def test_dry_run_fills_note_screenshots_and_closes(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    shot = tmp_path / "shots" / "1.png"
    r = run(wf, app, job, screenshot_path=shot)
    assert r["status"] == "dry_run" and r["reason"].startswith("dry run") and r["submitted"] is False
    assert r["screenshot"] == str(shot) and shot.exists()
    assert r["answers"][wa.NOTE_LABEL] == "Hello Acme, I fit."
    assert r["answers"]["Years of Python experience"] == "4"
    assert r["answers"]["Are you authorized to work in India?"] == "Yes"
    assert r["needs_human"] == [] and r["unfilled_required"] == []
    assert wf.site.login_status == "ok"


def test_dry_run_never_submits_even_with_auto_submit(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    enabled_site.auto_submit = True
    session.add(enabled_site)
    session.commit()
    # global dry-run is on by default: auto_submit=True must still stop before the final click
    r = run(wf, app, job, dry_run=False, auto_submit=True, screenshot_path=tmp_path / "a.png")
    assert r["status"] == "dry_run" and "global dry-run" in r["reason"] and r["submitted"] is False


def test_auto_submit_off_stops_before_submit(wf, session, demo, job_app, enabled_site, tmp_path, monkeypatch):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    allow_real_submit(session, monkeypatch)
    # site auto_submit stays off (the default): filled, screenshotted, not submitted
    r = run(wf, app, job, dry_run=False, auto_submit=True, screenshot_path=tmp_path / "b.png")
    assert r["status"] == "dry_run" and "auto-submit off" in r["reason"] and Path(r["screenshot"]).exists()
    # the caller's auto_submit flag alone is not enough either
    enabled_site.auto_submit = True
    session.add(enabled_site)
    session.commit()
    r = run(wf, app, job, dry_run=False, auto_submit=False, screenshot_path=tmp_path / "c.png")
    assert r["status"] == "dry_run" and r["submitted"] is False


def test_real_submit_when_every_gate_allows(wf, session, demo, job_app, enabled_site, tmp_path, monkeypatch):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    allow_real_submit(session, monkeypatch)
    enabled_site.auto_submit = True
    session.add(enabled_site)
    session.commit()
    r = run(wf, app, job, dry_run=False, auto_submit=True, screenshot_path=tmp_path / "d.png")
    assert r["status"] == "applied" and r["submitted"] is True and r["reason"] == "submitted"
    assert Path(r["screenshot"]).exists()
    fr = wa.as_fill_result(r)
    assert fr.submitted is True and fr.answers[wa.NOTE_LABEL] == "Hello Acme, I fit." and fr.filled == 3


def test_note_falls_back_to_template_without_message(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    app.message = None
    session.add(app)
    session.commit()
    job.apply_url = fixture(tmp_path)
    r = run(wf, app, job, screenshot_path=tmp_path / "e.png")
    note = r["answers"][wa.NOTE_LABEL]
    assert r["status"] == "dry_run" and "Python Developer" in note and "Acme" in note
    assert len(note) <= wa.NOTE_MAX_CHARS


def test_required_question_without_answer_pauses(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    wf.answers = StubAnswers({"authorized": ("Yes", 90)})
    r = run(wf, app, job, screenshot_path=tmp_path / "f.png")
    assert r["status"] == "paused" and r["reason"] == "required fields left empty"
    assert r["unfilled_required"] == ["Years of Python experience"] and Path(r["screenshot"]).exists()
    wf.answers = StubAnswers({"years": ("3", 20), "authorized": ("Yes", 90)})
    r = run(wf, app, job, screenshot_path=tmp_path / "g.png")
    assert r["status"] == "paused" and r["reason"] == "a question needs a human"
    assert r["needs_human"] == ["Years of Python experience: low confidence answer, review it"]
    fr = wa.as_fill_result(r)
    assert fr.paused_reason == "a question needs a human" and fr.needs_human == r["needs_human"]


def test_logged_out_job_page_pauses_and_records_expired(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path, "out", header=LOGIN_HEADER)
    r = run(wf, app, job, screenshot_path=tmp_path / "h.png")
    assert r["status"] == "paused" and "login required" in r["reason"]
    assert UserScope(session, demo).site("wellfound").login_status == "expired"


def test_challenge_pauses_records_cooldown(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path, "chl", header="<form id='challenge-form'></form>")
    r = run(wf, app, job, screenshot_path=tmp_path / "i.png")
    assert r["status"] == "paused" and "security check" in r["reason"] and Path(r["screenshot"]).exists()
    site = UserScope(session, demo).site("wellfound")
    assert site.login_status == "challenge" and COOLDOWN_KEY in demo.preferences
    # and the next call is throttled before any navigation
    r = run(wf, app, job, screenshot_path=tmp_path / "j.png")
    assert r["status"] == "skipped" and "throttled" in r["reason"]


def test_already_applied_and_missing_button_fail(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path, "done", apply="<button type='button'>Applied</button>")
    r = run(wf, app, job, screenshot_path=tmp_path / "k.png")
    assert r["status"] == "failed" and "already applied" in r["reason"]
    job.apply_url = fixture(tmp_path, "none", apply="<span>Closed</span>")
    r = run(wf, app, job, screenshot_path=tmp_path / "l.png")
    assert r["status"] == "failed" and "Apply button not found" in r["reason"]
    fr = wa.as_fill_result(r)
    assert fr.error == r["reason"] and fr.plan == []


def test_default_screenshot_path_and_selector_overlay(wf, session, demo, job_app, enabled_site, tmp_path):
    app, job = job_app
    job.apply_url = fixture(tmp_path)
    r = run(wf, app, job)
    assert r["status"] == "dry_run"
    shot = Path(r["screenshot"])
    assert shot.exists() and shot.parent == get_settings().screenshots_dir / str(demo.id)
    overlay = tmp_path / "wellfound.yaml"
    overlay.write_text("site: wellfound\nselectors:\n  note: 'textarea#note'\n  bogus: ''\n", encoding="utf-8")
    sel = wa.load_selectors(overlay)
    assert sel["note"] == "textarea#note" and sel["apply_button"] == wa.DEFAULT_SELECTORS["apply_button"]
    assert "bogus" not in sel
    assert wa.load_selectors(tmp_path / "missing.yaml") == wa.DEFAULT_SELECTORS


def test_pick_wellfound_applications(session, demo, job_app):
    app, job = job_app
    other = Job(dedupe_key="gh-1", title="Other", company="Acme", apply_url="https://boards.greenhouse.io/a/jobs/1",
                ats_type="greenhouse", source="greenhouse")
    session.add(other)
    session.flush()
    session.add(Application(user_id=demo.id, job_id=other.id, status=AppStatus.queued))
    session.commit()
    picks = wa.pick_wellfound_applications(session, demo, 5)
    assert [(a.id, j.id) for a, j in picks] == [(app.id, job.id)]
    assert wa.is_wellfound_job_url("https://wellfound.com/jobs/1053169-python-developer")
    assert not wa.is_wellfound_job_url("https://wellfound.com/company/x/jobs")
