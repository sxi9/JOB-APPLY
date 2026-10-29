"""Instahyre one-click apply: the real runner in headless Chromium against synthetic opportunity pages (the
signed-in markup was reconstructed from the app code; no account was available), the safety gates, and the
pipeline routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.models import Application, AppStatus, Job, JobSource, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.sites.instahyre import apply as ia
from jobapply.sites.instahyre import session as ih
from jobapply.sites.instahyre.apply import (
    ApplyResult,
    OneClickApplyRunner,
    apply_instahyre_user,
    apply_job,
    pick_instahyre_applications,
)
from jobapply.switches import set_value

NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(30)  # noqa: E731

NAV_OUT = ('<ul><li><a id="nav-search-jobs" href="/search-jobs/">Search jobs</a></li>'
           '<li><a id="nav-user-login" href="/login/">Login</a></li>'
           '<li><a id="nav-user-signup" href="/candidates/register/">Signup</a></li></ul>')
NAV_IN = ('<ul><li><a id="nav-search-jobs" href="/search-jobs/">Search jobs</a></li>'
          '<li><a href="/candidate/opportunities/">Opportunities</a></li><li><a href="/logout/">Logout</a></li></ul>')
PUBLIC_BUTTON = '<button id="job_details_top" type="button" ng-click="setPostAuthRedirectionUrl()">Apply now</button>'
APPLY_BUTTON = '<button id="job_details_top" type="button" ng-click="submitChoiceNonMatching()">Apply</button>'
SCRIPT = """
<script>
  const state = document.getElementById('state');
  const variant = document.body.dataset.variant;
  const btn = document.getElementById('job_details_top');
  function toast() {
    const c = document.createElement('div'); c.id = 'toast-container';
    c.innerHTML = '<div class="toast toast-success"><div class="toast-message">'
      + 'Application sent to Acme Labs!</div></div>';
    document.body.appendChild(c);
  }
  function modal(withFields) {
    const m = document.createElement('div'); m.className = 'modal in'; m.style.display = 'block';
    m.innerHTML = withFields
      ? '<div class="modal-dialog"><h3>A few questions</h3>'
        + '<label>Notice period<input id="q1" type="text"></label>'
        + '<label>Expected CTC<select id="q2"><option>10 LPA</option></select></label>'
        + '<button type="button">Submit</button></div>'
      : '<div class="modal-dialog"><h3>Go premium</h3><p>Stand out</p>'
        + '<button type="button" class="close" ng-click="closeModal()">Close</button></div>';
    document.body.appendChild(m);
  }
  if (btn) btn.addEventListener('click', () => {
    state.dataset.clicked = String(Number(state.dataset.clicked) + 1);
    if (variant === 'toast') toast();
    if (variant === 'questionnaire') modal(true);
    if (variant === 'post_questionnaire') { toast(); modal(true); }
    if (variant === 'premium') { toast(); modal(false); }
    if (variant === 'error') {
      const g = document.createElement('div'); g.className = 'growl-item alert alert-danger';
      g.textContent = 'Please complete your profile before applying.'; document.body.appendChild(g);
    }
    if (variant === 'late_toast') setTimeout(toast, 1500);
  });
  document.body.addEventListener('click', (e) => {
    if (e.target.classList.contains('close')) e.target.closest('.modal').remove();
  });
</script>"""


def page_html(variant: str = "toast", *, logged_in: bool = True, applied: bool = False,
              title: str = "Python Developer job at Acme Labs - Instahyre") -> str:
    nav = NAV_IN if logged_in else NAV_OUT
    button = APPLY_BUTTON if logged_in else PUBLIC_BUTTON
    if applied:
        button = '<div class="application-status-box"><span class="status-label">Applied</span></div>'
    body = f"""{nav}<div id="state" data-clicked="0"></div>
<div id="employer-profile"><h1>Python Developer</h1>
<div class="apply-button" ng-controller="candidateOpportunityCtrl">{button}</div>
<div class="quill-formatted"><p>We need a Python engineer. Security check of code is part of the job.</p></div>
</div>"""
    return (f'<!DOCTYPE html><html><head><title>{title}</title></head>'
            f'<body data-variant="{variant}">{body}{SCRIPT}</body></html>')


CLOUDFLARE = ('<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><form id="challenge-form">'
              '<div class="cf-turnstile"></div></form></body></html>')


def write(tmp_path: Path, name: str, html: str) -> str:
    p = tmp_path / f"{name}.html"
    p.write_text(html, encoding="utf-8")
    return p.resolve().as_uri()


def clicks(page) -> int:
    return int(page.evaluate("() => document.getElementById('state').dataset.clicked"))


def runner(**kw) -> OneClickApplyRunner:
    kw.setdefault("pause", NO_PAUSE)
    kw.setdefault("confirm_timeout_ms", 3000)
    kw.setdefault("button_timeout_ms", 1500)
    return OneClickApplyRunner(**kw)


# ------------------------------------------------------------------ runner on fixtures


def test_dry_run_stops_before_the_click(page, tmp_path):
    url = write(tmp_path, "toast", page_html("toast"))
    shot = tmp_path / "shots" / "ih.png"
    r = runner().run(page, url, submit=False, screenshot_path=shot, stop_reason="dry run")
    assert r.status == "dry_run" and r.reason == "dry run" and r.interest_sent is False
    assert clicks(page) == 0 and shot.exists() and r.screenshot == str(shot)
    fr = r.to_fill_result()
    assert fr.submitted is False and fr.paused_reason == "" and fr.error == "" and fr.screenshot_path == str(shot)


def test_submit_clicks_and_reads_the_toast(page, tmp_path):
    url = write(tmp_path, "toast", page_html("toast"))
    r = runner().run(page, url, submit=True, screenshot_path=tmp_path / "s.png")
    assert r.status == "applied" and r.interest_sent is True and clicks(page) == 1
    assert r.to_fill_result().submitted is True and (tmp_path / "s.png").exists()
    # a toast that arrives late is still caught within the confirmation window
    url = write(tmp_path, "late", page_html("late_toast"))
    r = runner().run(page, url, submit=True)
    assert r.status == "applied" and r.interest_sent is True


def test_screening_questionnaire_pauses_without_filling(page, tmp_path):
    url = write(tmp_path, "q", page_html("questionnaire"))
    r = runner().run(page, url, submit=True, screenshot_path=tmp_path / "q.png")
    assert r.status == "paused" and r.reason == "screening questionnaire needs a human"
    assert r.interest_sent is False and r.needs_human and "screening questions" in r.needs_human[0]
    assert page.locator("#q1").input_value() == ""  # nothing was typed into the questionnaire
    fr = r.to_fill_result()
    assert fr.paused_reason == r.reason and fr.submitted is False and fr.needs_human == r.needs_human


def test_post_apply_questionnaire_pauses_but_keeps_the_interest(page, tmp_path):
    url = write(tmp_path, "pq", page_html("post_questionnaire"))
    r = runner().run(page, url, submit=True)
    assert r.status == "paused" and r.reason == "post-apply questionnaire needs a human"
    assert r.interest_sent is True and "application sent" in r.needs_human[0]
    assert r.to_fill_result().submitted is False  # booked as paused, the interest is reported separately
    assert r.as_dict()["interest_sent"] is True


def test_premium_prompt_is_closed_and_counts_as_applied(page, tmp_path):
    url = write(tmp_path, "premium", page_html("premium"))
    r = runner().run(page, url, submit=True)
    assert r.status == "applied" and r.interest_sent is True
    assert page.locator(".modal.in").count() == 0  # the prompt was dismissed


def test_site_error_fails_and_silence_pauses(page, tmp_path):
    url = write(tmp_path, "err", page_html("error"))
    r = runner().run(page, url, submit=True)
    assert r.status == "failed" and "complete your profile" in r.reason
    url = write(tmp_path, "silent", page_html("silent"))
    r = runner(confirm_timeout_ms=800).run(page, url, submit=True)
    assert r.status == "paused" and r.reason.startswith("apply clicked but no confirmation")
    assert r.interest_sent is False and r.needs_human


def test_logged_out_page_pauses_for_login(page, tmp_path):
    url = write(tmp_path, "out", page_html("toast", logged_in=False))
    r = runner().run(page, url, submit=True)
    assert r.status == "paused" and r.reason == "instahyre login needed" and clicks(page) == 0


def test_already_applied_and_missing_button(page, tmp_path):
    url = write(tmp_path, "done", page_html("toast", applied=True))
    r = runner().run(page, url, submit=True)
    assert r.status == "failed" and r.reason.startswith("already applied")
    url = write(tmp_path, "gone", page_html("toast").replace(APPLY_BUTTON, "<p>This job is no longer active</p>"))
    r = runner().run(page, url, submit=True, screenshot_path=tmp_path / "gone.png")
    assert r.status == "failed" and r.reason.startswith("apply button not found") and (tmp_path / "gone.png").exists()
    url = write(tmp_path, "applied_btn", page_html("toast").replace(">Apply</button>", ">Applied</button>"))
    assert runner().run(page, url, submit=True).reason.startswith("already applied")


def test_cloudflare_page_pauses_before_touching_anything(page, tmp_path):
    url = write(tmp_path, "cf", CLOUDFLARE)
    r = runner().run(page, url, submit=True, screenshot_path=tmp_path / "cf.png")
    assert r.status == "paused" and r.reason == "challenge page" and (tmp_path / "cf.png").exists()
    assert ia._stop_reason(r.as_dict()) == "challenge"


def test_blocked_status_code_pauses_without_reading_the_page():
    class Resp:
        status = 403

    class Page:
        url = "https://www.instahyre.com/job-1-x/"

        def goto(self, url, **kw):
            return Resp()

        def locator(self, sel):
            raise AssertionError("page must not be read after a 403")

    r = runner().run(Page(), "https://www.instahyre.com/job-1-x/", submit=True)
    assert r.status == "paused" and r.reason == "instahyre answered 403"
    assert ia._stop_reason(r.as_dict()) == "challenge"
    assert ia._stop_reason({"status": "paused", "reason": "instahyre login needed"}) == "login"
    assert ia._stop_reason({"status": "paused", "reason": "screening questionnaire needs a human"}) == ""
    assert ia._stop_reason({"status": "applied", "reason": "challenge"}) == "" and ia._stop_reason(None) == ""


def test_apply_result_vocabulary():
    for status in ia.STATUSES:
        fr = ApplyResult(status, "why", url="u").to_fill_result()
        assert fr.profile_origin == "builtin:instahyre"
        if status == "applied":
            assert fr.submitted
        elif status == "paused":
            assert fr.paused_reason == "why"
        elif status in ("failed", "skipped"):
            assert fr.error == "why"
        else:
            assert not (fr.submitted or fr.paused_reason or fr.error)
    d = ApplyResult("dry_run", url="u").as_dict()
    assert set(d) >= {"status", "screenshot", "reason", "needs_human", "interest_sent", "result"}


# ------------------------------------------------------------------ apply_job gates


def _job(session, user, status=AppStatus.queued, *, source="instahyre", ats="instahyre", score=80,
         url="https://www.instahyre.com/job-{score}-python-developer-at-acme/"):
    job = Job(dedupe_key=f"k-{source}-{ats}-{score}-{status}", title="Engineer", company="Acme", source=source,
              ats_type=ats, apply_url=url.format(score=score))
    session.add(job)
    session.flush()
    session.add(JobSource(job_id=job.id, source=source, source_id=str(score), raw={"one_click_apply": True}))
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _enable(session, user, **kw):
    site = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == "instahyre")).one()
    site.enabled = True
    for k, v in kw.items():
        setattr(site, k, v)
    session.add(site)
    session.commit()
    return site


def _allow_real_submits(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "dry_run", False)
    set_value(session, "dry_run", "off")
    session.commit()
    monkeypatch.setattr(ia, "_in_active_hours", lambda: True)


class NoTouchPage:
    """A page the gates must never reach."""

    url = "about:blank"

    def goto(self, *a, **k):
        raise AssertionError("browser touched although a gate should have stopped the apply")


def _apply(session, demo, app, page, **kw):
    kw.setdefault("dry_run", True)
    kw.setdefault("auto_submit", False)
    kw.setdefault("pause", NO_PAUSE)
    ih_session = ih.InstahyreSession(context=None, user=demo, session_db=session)
    return apply_job(ih_session, app, page=page, **kw)


def test_apply_job_gates_never_touch_the_browser(session, demo, monkeypatch):
    app, job = _job(session, demo)
    out = _apply(session, demo, app, NoTouchPage())
    assert out["status"] == "skipped" and "disabled" in out["reason"]
    site = _enable(session, demo, auto_submit=True)
    set_value(session, "kill_switch", "on")
    session.commit()
    out = _apply(session, demo, app, NoTouchPage(), dry_run=False, auto_submit=True)
    assert out["status"] == "skipped" and out["reason"] == "kill switch on"
    set_value(session, "kill_switch", "off")
    session.commit()
    # cooldown after a challenge
    ih.InstahyreSession(None, demo, session).set_cooldown()
    session.commit()
    out = _apply(session, demo, app, NoTouchPage(), dry_run=False, auto_submit=True)
    assert out["status"] == "skipped" and "cooldown" in out["reason"]
    ih.InstahyreSession(None, demo, session).clear_cooldown()
    session.commit()
    # daily caps (only checked for a real submit)
    _allow_real_submits(session, monkeypatch)
    monkeypatch.setattr(apply_mod, "applied_today", lambda s, uid, site=None: 99)
    out = _apply(session, demo, app, NoTouchPage(), dry_run=False, auto_submit=True)
    assert out["status"] == "skipped" and "daily cap" in out["reason"]
    monkeypatch.setattr(apply_mod, "applied_today", lambda s, uid, site=None: 0)
    # outside the active hours a real submit is skipped
    monkeypatch.setattr(ia, "_in_active_hours", lambda: False)
    out = _apply(session, demo, app, NoTouchPage(), dry_run=False, auto_submit=True)
    assert out["status"] == "skipped" and "active hours" in out["reason"]
    assert site.auto_submit is True
    # an application without a job url fails without a browser
    app2, job2 = _job(session, demo, score=70, url="")
    out = _apply(session, demo, app2, NoTouchPage())
    assert out["status"] == "failed"


def test_apply_job_dry_run_and_auto_submit_off_stop_before_the_click(page, session, demo, tmp_path, monkeypatch):
    _enable(session, demo, auto_submit=False)
    app, job = _job(session, demo, url=write(tmp_path, "toast", page_html("toast")))
    out = _apply(session, demo, app, page, dry_run=True, auto_submit=True)
    assert out["status"] == "dry_run" and out["reason"].startswith("dry run") and clicks(page) == 0
    assert out["screenshot"] and Path(out["screenshot"]).exists()
    assert str(get_settings().screenshots_dir) in out["screenshot"]
    # dry_run=False still never submits while the global dry-run switch is on
    out = _apply(session, demo, app, page, dry_run=False, auto_submit=True)
    assert out["status"] == "dry_run" and clicks(page) == 0
    # ... and with the switches off, the site's auto_submit flag still gates the click
    _allow_real_submits(session, monkeypatch)
    out = _apply(session, demo, app, page, dry_run=False, auto_submit=True)
    assert out["status"] == "dry_run" and "auto-submit off" in out["reason"] and clicks(page) == 0
    booked = apply_mod.record_outcome(session, demo, app, job, out["result"])
    assert booked.status == AppStatus.dry_run and app.screenshot_path == out["screenshot"]


def test_apply_job_real_submit_and_bookkeeping(page, session, demo, tmp_path, monkeypatch):
    _enable(session, demo, auto_submit=True)
    _allow_real_submits(session, monkeypatch)
    sent = []
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    app, job = _job(session, demo, url=write(tmp_path, "toast", page_html("toast")))
    out = _apply(session, demo, app, page, dry_run=False, auto_submit=True)
    assert out["status"] == "applied" and out["interest_sent"] is True and clicks(page) == 1
    booked = apply_mod.record_outcome(session, demo, app, job, out["result"])
    assert booked.status == AppStatus.applied and app.submitted_at is not None
    assert apply_mod.applied_today(session, demo.id, "instahyre") == 1
    # the caller's auto_submit=False wins even when the site flag is on
    app2, _ = _job(session, demo, score=70, url=write(tmp_path, "toast2", page_html("toast")))
    out = _apply(session, demo, app2, page, dry_run=False, auto_submit=False)
    assert out["status"] == "dry_run" and clicks(page) == 0
    # a questionnaire pauses and notifies; nothing is retried
    app3, job3 = _job(session, demo, score=60, url=write(tmp_path, "q", page_html("questionnaire")))
    out = _apply(session, demo, app3, page, dry_run=False, auto_submit=True)
    assert out["status"] == "paused" and out["needs_human"]
    booked = apply_mod.record_outcome(session, demo, app3, job3, out["result"])
    assert booked.status == AppStatus.paused and sent[-1][0] == "paused"
    assert "questionnaire" in (app3.error or "")


def test_apply_job_records_login_and_challenge(page, session, demo, tmp_path, monkeypatch):
    _enable(session, demo, auto_submit=True)
    _allow_real_submits(session, monkeypatch)
    site = session.exec(select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "instahyre")).one()
    app, job = _job(session, demo, url=write(tmp_path, "out", page_html("toast", logged_in=False)))
    out = _apply(session, demo, app, page, dry_run=False, auto_submit=True)
    assert out["status"] == "paused" and out["reason"] == "instahyre login needed" and clicks(page) == 0
    session.refresh(site)
    assert site.login_status == "expired"
    app2, _ = _job(session, demo, score=70, url=write(tmp_path, "cf", CLOUDFLARE))
    out = _apply(session, demo, app2, page, dry_run=False, auto_submit=True)
    assert out["status"] == "paused" and out["reason"] == "challenge page"
    session.refresh(site)
    assert site.login_status == "challenge" and ih.throttled(demo, site) is True
    # the cooldown now blocks the next apply before any navigation
    app3, _ = _job(session, demo, score=60)
    out = _apply(session, demo, app3, NoTouchPage(), dry_run=False, auto_submit=True)
    assert out["status"] == "skipped" and "cooldown" in out["reason"]


def test_apply_job_opens_and_closes_its_own_page(session, demo, tmp_path, monkeypatch):
    _enable(session, demo)
    events: list[str] = []

    class FakePage:
        url = "about:blank"

        def goto(self, url, **kw):
            events.append("goto")

            class R:
                status = 403

            return R()

        def close(self):
            events.append("close")

    class Ctx:
        def new_page(self):
            events.append("new_page")
            return FakePage()

    app, job = _job(session, demo)
    out = apply_job(ih.InstahyreSession(Ctx(), demo, session), app, dry_run=True, auto_submit=False, pause=NO_PAUSE)
    assert out["status"] == "paused" and out["reason"] == "instahyre answered 403"
    assert events == ["new_page", "goto", "close"]


# ------------------------------------------------------------------ pipeline routing


def test_pick_instahyre_applications(session, demo):
    _enable(session, demo)
    a1, _ = _job(session, demo, status=AppStatus.approved, score=90)
    a2, _ = _job(session, demo, score=80)
    _job(session, demo, source="linkedin", ats=None, score=70, url="https://www.linkedin.com/jobs/view/70/")
    _job(session, demo, ats=None, score=60)  # source instahyre, no ats_type: site_for_job still says instahyre
    picked = pick_instahyre_applications(session, demo, limit=10)
    assert [a.id for a, _ in picked][:2] == [a1.id, a2.id] and len(picked) == 3
    assert all(apply_mod.site_for_job(j) == "instahyre" for _, j in picked)
    assert pick_instahyre_applications(session, demo, limit=1)[0][0].id == a1.id


class _FakePage:
    url = "https://www.instahyre.com/candidate/opportunities/"

    def wait_for_timeout(self, ms):
        pass


class _FakeContext:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, status, user, db):
        self.status = status
        self.user, self.db = user, db
        self.challenges = 0

    def check(self, page):
        return self.status

    def record_challenge(self):
        self.challenges += 1


def _wire(monkeypatch, status="ok", throttled=False, outcome=None):
    contexts: list[_FakeContext] = []
    runs: list[dict] = []
    sent: list[tuple[str, str]] = []
    sessions: list[_FakeSession] = []

    def launch(pw, user_id):
        ctx = _FakeContext()
        contexts.append(ctx)
        return ctx

    def fake_session(ctx, user, db):
        sessions.append(_FakeSession(status, user, db))
        return sessions[-1]

    def fake_apply(sess, app, *, dry_run, auto_submit, page=None, **kw):
        runs.append({"app": app.id, "dry_run": dry_run, "auto_submit": auto_submit})
        return outcome or ApplyResult("dry_run", "dry run", url="u").as_dict()

    monkeypatch.setattr(ia, "_launch_persistent", launch)
    monkeypatch.setattr(ia, "_instahyre_session", fake_session)
    monkeypatch.setattr(ia, "_is_throttled", lambda user, site: throttled)
    monkeypatch.setattr(ia, "_between_applications", lambda page: None)
    monkeypatch.setattr(ia, "apply_job", fake_apply)
    import jobapply.notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    return contexts, runs, sent, sessions


def test_apply_instahyre_user_dry_run(session, demo, monkeypatch):
    contexts, runs, sent, _ = _wire(monkeypatch)
    assert apply_instahyre_user(None, session, demo, limit=5) == []  # site disabled by default
    site = _enable(session, demo, auto_submit=True)
    assert apply_instahyre_user(None, session, demo, limit=5) == [] and contexts == []  # nothing to apply to
    a1, _ = _job(session, demo, status=AppStatus.approved, score=90)
    a2, _ = _job(session, demo, score=80)
    out = apply_instahyre_user(None, session, demo, limit=5, dry_run=True)
    assert {o.app_id for o in out} == {a1.id, a2.id} and all(o.status == AppStatus.dry_run for o in out)
    assert len(runs) == 2 and all(r["dry_run"] for r in runs) and contexts and contexts[0].closed
    assert all(r["auto_submit"] is True for r in runs)  # the site flag is handed to apply_job
    session.refresh(site)
    assert site.login_status == "unknown"  # the fake session does not record; apply_job's own gates do


def test_apply_instahyre_user_login_needed_and_unknown(session, demo, monkeypatch):
    _enable(session, demo)
    _job(session, demo)
    contexts, runs, sent, _ = _wire(monkeypatch, status="login")
    assert apply_instahyre_user(None, session, demo, limit=5) == []
    assert runs == [] and sent and sent[0][0] == "paused" and "cookies" in sent[0][1]
    contexts, runs, sent, _ = _wire(monkeypatch, status="challenge")
    assert apply_instahyre_user(None, session, demo, limit=5) == [] and "security check" in sent[0][1]
    contexts, runs, sent, _ = _wire(monkeypatch, status="unknown")
    assert apply_instahyre_user(None, session, demo, limit=5) == [] and runs == [] and sent == []


def test_apply_instahyre_user_switches_and_stops(session, demo, monkeypatch):
    _enable(session, demo, auto_submit=True)
    _job(session, demo, score=90)
    _job(session, demo, score=80)
    contexts, runs, sent, _ = _wire(monkeypatch, throttled=True)
    assert apply_instahyre_user(None, session, demo, limit=5, dry_run=False) == [] and contexts == []
    set_value(session, "kill_switch", "on")
    session.commit()
    contexts, runs, sent, _ = _wire(monkeypatch)
    assert apply_instahyre_user(None, session, demo, limit=5, dry_run=False) == [] and contexts == []
    set_value(session, "kill_switch", "off")
    session.commit()
    # global dry run on (the default): a dry_run=False call is still a dry run
    contexts, runs, sent, _ = _wire(monkeypatch)
    out = apply_instahyre_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 2 and all(r["dry_run"] is True for r in runs)
    # a challenge mid-run stops the loop and records the cooldown through the session
    for app_id in {o.app_id for o in out}:
        a = session.get(Application, app_id)
        a.status = AppStatus.queued
        session.add(a)
    session.commit()
    blocked = ApplyResult("paused", "challenge page", url="u").as_dict()
    contexts, runs, sent, sessions = _wire(monkeypatch, outcome=blocked)
    out = apply_instahyre_user(None, session, demo, limit=5)
    assert len(out) == 1 and out[0].status == AppStatus.paused and len(runs) == 1
    assert sent[-1][0] == "paused" and "blocked or challenged" in sent[-1][1]
    # a skipped gate (cap) ends the run without booking anything
    for app_id in {o.app_id for o in out}:
        a = session.get(Application, app_id)
        a.status = AppStatus.queued
        session.add(a)
    session.commit()
    capped = ApplyResult("skipped", "daily cap reached for instahyre (15)", url="u").as_dict()
    contexts, runs, sent, sessions = _wire(monkeypatch, outcome=capped)
    assert apply_instahyre_user(None, session, demo, limit=5) == [] and len(runs) == 1


def test_apply_instahyre_user_real_submit_counts(session, demo, monkeypatch):
    _enable(session, demo, auto_submit=True)
    _allow_real_submits(session, monkeypatch)
    app, job = _job(session, demo)
    applied = ApplyResult("applied", "interest sent", interest_sent=True, url=job.apply_url).as_dict()
    contexts, runs, sent, _ = _wire(monkeypatch, outcome=applied)
    out = apply_instahyre_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 1 and out[0].status == AppStatus.applied and runs[0]["dry_run"] is False
    assert apply_mod.applied_today(session, demo.id, "instahyre") == 1
    # a crash in apply_job is booked as failed and the run goes on
    _job(session, demo, score=70)

    def boom(*a, **k):
        raise TimeoutError("navigation")

    monkeypatch.setattr(ia, "apply_job", boom)
    out = apply_instahyre_user(None, session, demo, limit=5, dry_run=False)
    assert [o.status for o in out] == [AppStatus.failed] and "TimeoutError" in out[0].detail


def test_apply_instahyre_wrapper(session, engine, demo, monkeypatch):
    calls: list[dict] = []

    class _PW:
        def __enter__(self):
            return "pw"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    monkeypatch.setattr(ia, "apply_instahyre_user", lambda pw, s, user, limit=3, dry_run=True: calls.append(
        {"user": user.username, "limit": limit, "dry_run": dry_run}) or [])
    monkeypatch.setattr(get_settings(), "linkedin_active_hours", "00:00-00:00")  # always active
    assert ia.active_hours_spec() == "00:00-00:00"
    assert ia.apply_instahyre(username="demo", limit=2, dry_run=False, engine=engine) == []
    assert calls == [{"user": "demo", "limit": 2, "dry_run": True}]  # global dry run forced it
    set_value(session, "kill_switch", "on")
    session.commit()
    calls.clear()
    assert ia.apply_instahyre(limit=2, engine=engine) == [] and calls == []
    set_value(session, "kill_switch", "off")
    session.commit()
    monkeypatch.setattr(ia, "_in_active_hours", lambda: False)
    assert ia.apply_instahyre(limit=2, engine=engine) == [] and calls == []


def test_persistent_profile_is_launched_headless(monkeypatch):
    from jobapply.sites import browser

    seen: dict = {}
    monkeypatch.setattr(browser, "launch_persistent",
                        lambda pw, user_id, site, headless=None, **kw: seen.update(site=site, headless=headless))
    ia._launch_persistent("pw", 3)
    assert seen == {"site": "instahyre", "headless": True}


@pytest.mark.parametrize("variant", ["toast", "questionnaire"])
def test_job_text_mentioning_security_is_not_a_challenge(page, tmp_path, variant):
    # the description says "Security check" and the classifier must not pause on it
    url = write(tmp_path, f"sec-{variant}", page_html(variant))
    r = runner().run(page, url, submit=False, stop_reason="dry run")
    assert r.status == "dry_run"
