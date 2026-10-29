"""Cutshort apply filler: the real runner in headless Chromium against inline HTML fixtures (the live
logged-in markup is unverified, so the fixtures model the flow the public page advertises: a one-click
"Apply to this job", optionally followed by a screening questionnaire dialog), plus the uniform `apply_job`
wrapper, the per-user run with fakes, and the form profile YAML."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.forms.profiles import VaultView, load_builtin_profiles
from jobapply.forms.runner import FillResult
from jobapply.models import Application, AppStatus, Job, JobSource, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.cutshort import apply as ca
from jobapply.sites.cutshort import session as css
from jobapply.sites.cutshort.apply import (
    CutshortApplyRunner,
    apply_cutshort_user,
    apply_job,
    pick_cutshort_applications,
)
from jobapply.switches import set_value

CONFIG = Path(__file__).parent.parent / "config"
NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(30)  # noqa: E731


class StubAnswers:
    """AnswerSource stand-in: canned answers by substring, everything else low confidence."""

    def __init__(self, canned: dict[str, tuple[str | None, int]] | None = None):
        self.canned = canned or {}
        self.asked: list[tuple[str, list[str] | None]] = []

    def answer(self, question, options=None):
        self.asked.append((question, options))
        for needle, (answer, conf) in self.canned.items():
            if needle.lower() in question.lower():
                return answer, conf
        if options:
            return options[0], 25
        return "I would be glad to discuss this in a conversation.", 30


# ------------------------------------------------------------------ fixtures (inline HTML)

JOB_PAGE = """<!doctype html><html><head><title>Acme is hiring Senior Python Developer | Cutshort</title></head>
<body>
<div id="state" data-clicked="0" data-submitted="0" data-closed="0"></div>
<header><a href="/profile/candidate-dashboard">Find jobs</a><span>Demo User</span></header>
<main>
<h1>Senior Python Developer</h1><p>Acme Labs, Pune. 3 - 6 years.</p>
<p>You will review unusual activity reports and run the security check of every release.</p>
<button type="button" id="apply" onclick="applyClicked()">Apply to this job</button>
<div id="done" style="display:none">Application sent to Acme Labs</div>
__DIALOG__
</main>
<script>
function st(k, v) { document.getElementById('state').setAttribute('data-' + k, v); }
function applyClicked() {
  st('clicked', '1');
  var d = document.getElementById('questions');
  if (d) { d.style.display = 'block'; document.getElementById('apply').style.display = 'none'; return; }
  st('submitted', '1');
  document.getElementById('apply').style.display = 'none';
  document.getElementById('done').style.display = 'block';
}
function submitQ() {
  var d = document.getElementById('questions');
  var req = d.querySelectorAll('[required]');
  for (var i = 0; i < req.length; i++) {
    if (!req[i].value) { document.getElementById('qerr').style.display = 'block'; return; }
  }
  st('submitted', '1'); d.style.display = 'none'; document.getElementById('done').style.display = 'block';
}
function closeQ() { st('closed', '1'); document.getElementById('questions').style.display = 'none';
  document.getElementById('apply').style.display = ''; }
</script></body></html>"""

QUESTIONS = """<div id="questions" role="dialog" style="display:none">
  <h2>A few questions from Acme Labs</h2>
  <label for="notice">What is your notice period?</label><input id="notice" name="notice" type="text" required>
  <label for="ctc">Expected CTC (in LPA)</label><input id="ctc" name="ctc" type="text" required>
  <label for="why">Why do you want to join Acme?</label><input id="why" name="why" type="text" required>
  <fieldset><legend>Are you willing to relocate to Pune?</legend>
    <label><input type="radio" name="relocate" value="yes" required> Yes</label>
    <label><input type="radio" name="relocate" value="no"> No</label></fieldset>
  <label for="resume">Upload resume</label><input id="resume" name="resume" type="file">
  <div id="qerr" role="alert" style="display:none">Please answer every required question</div>
  <button type="button" onclick="closeQ()">Cancel</button>
  <button type="button" id="submit" onclick="submitQ()">Submit answers</button>
</div>"""


def make_runner(session, user, resume, answers=None, profile=None, **kw):
    vault = VaultView(profile=dict(profile or DEMO_PROFILE), resume_path=str(resume))
    kw.setdefault("resume_path", str(resume))
    kw.setdefault("spec", load_builtin_profiles(CONFIG / "form_profiles").get("cutshort"))
    return CutshortApplyRunner(session, user, vault, answers=answers, llm=None, pause=NO_PAUSE, **kw)


def fixture(tmp_path: Path, name: str, html: str) -> str:
    path = tmp_path / f"{name}.html"
    path.write_text(html, encoding="utf-8")
    return path.resolve().as_uri()


def one_click(tmp_path) -> str:
    return fixture(tmp_path, "one_click", JOB_PAGE.replace("__DIALOG__", ""))


def questionnaire(tmp_path, html: str = QUESTIONS) -> str:
    return fixture(tmp_path, "questions", JOB_PAGE.replace("__DIALOG__", html))


def state(page) -> dict[str, str]:
    return page.evaluate(
        "() => Object.fromEntries(Array.from(document.getElementById('state').attributes)"
        ".filter(a => a.name.startsWith('data-')).map(a => [a.name.slice(5), a.value]))"
    )


# ------------------------------------------------------------------ units


def test_form_profile_loads_and_feeds_the_selectors():
    profiles = load_builtin_profiles(CONFIG / "form_profiles")
    spec = profiles["cutshort"]
    assert spec.ats == "cutshort" and spec.steps == []  # the Apply click is never a profile step
    assert spec.detect_re().search("https://cutshort.io/job/Senior-Dev-Acme-koBBDTQ1")
    assert not spec.detect_re().search("https://cutshort.io/jobs/python-jobs")
    assert spec.fields["label=notice period"] == "profile.notice_period"
    assert spec.fields["input[type=file]"] == "derived.resume_path"
    sel = ca.selectors_from(spec)
    assert sel["submit"] == spec.submit and "Applied" in sel["success"]
    assert ca.selectors_from(None)["submit"] == ca.DEFAULT_SELECTORS["submit"]
    assert ca.load_profile(CONFIG / "form_profiles").ats == "cutshort"
    assert ca.load_profile(Path("nope")) is None


def test_status_mapping():
    assert ca._status_of(None, "TimeoutError: boom") == ("failed", "TimeoutError: boom")
    r = FillResult(url="x", profile_origin="builtin:cutshort")
    assert ca._status_of(r)[0] == "dry_run"
    r.submitted = True
    assert ca._status_of(r)[0] == "applied"
    r = FillResult(url="x", profile_origin="builtin:cutshort", paused_reason="cutshort login needed")
    assert ca._status_of(r) == ("paused", "cutshort login needed") and ca._stop_reason(r) == "login"
    r = FillResult(url="x", profile_origin="builtin:cutshort", error="Apply button not found")
    assert ca._status_of(r)[0] == "failed"
    assert ca._stop_reason(None) == ""


# ------------------------------------------------------------------ runner on fixtures


def test_dry_run_never_clicks_apply(page, session, demo, resume, tmp_path):
    runner = make_runner(session, demo, resume, StubAnswers())
    shot = tmp_path / "shots" / "cs.png"
    result = runner.run(page, one_click(tmp_path), submit=False, screenshot_path=shot)
    assert result.profile_origin == "builtin:cutshort"
    assert result.error == "" and result.paused_reason == "" and result.submitted is False
    assert shot.exists() and result.screenshot_path == str(shot)
    assert state(page)["clicked"] == "0"  # the Apply button is the final action: untouched in a dry run
    assert ca._status_of(result)[0] == "dry_run"
    # the same for a job with a questionnaire: nothing is opened in a dry run
    result = runner.run(page, questionnaire(tmp_path), submit=False)
    assert state(page)["clicked"] == "0" and result.plan == [] and ca._status_of(result)[0] == "dry_run"


def test_submit_one_click_applies(page, session, demo, resume, tmp_path):
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, one_click(tmp_path), submit=True, screenshot_path=tmp_path / "s.png")
    assert result.submitted is True and result.error == "" and result.paused_reason == ""
    st = state(page)
    assert st["clicked"] == "1" and st["submitted"] == "1"
    assert ca._status_of(result)[0] == "applied"


def test_submit_fills_questionnaire_from_vault_and_answer_source(page, session, demo, resume, tmp_path):
    answers = StubAnswers({"join acme": ("Because of the payments platform.", 90)})
    runner = make_runner(session, demo, resume, answers)
    result = runner.run(page, questionnaire(tmp_path), submit=True, screenshot_path=tmp_path / "q.png")
    assert result.submitted is True and result.paused_reason == "" and result.error == "", result.summary()
    st = state(page)
    assert st["clicked"] == "1" and st["submitted"] == "1" and st["closed"] == "0"
    sent = result.answers
    assert sent["What is your notice period?"] == "30 days"  # YAML label map -> profile.notice_period
    assert sent["Expected CTC (in LPA)"] == "40 LPA"
    assert sent["Why do you want to join Acme?"] == "Because of the payments platform."
    assert sent["Are you willing to relocate to Pune?"] == "Yes"  # derived.willing_to_relocate
    assert sent["Upload resume"] == "resume.pdf"
    assert [q for q, _ in answers.asked] == ["Why do you want to join Acme?"]
    assert (tmp_path / "q.png").exists()


def test_low_confidence_answer_pauses_and_closes_the_dialog(page, session, demo, resume, tmp_path):
    runner = make_runner(session, demo, resume, StubAnswers())  # "why" gets a 30-confidence answer
    result = runner.run(page, questionnaire(tmp_path), submit=True, screenshot_path=tmp_path / "p.png")
    assert result.submitted is False and result.paused_reason == "a question needs a human"
    assert result.needs_human and "low confidence" in result.needs_human[0]
    st = state(page)
    assert st["clicked"] == "1" and st["submitted"] == "0" and st["closed"] == "1"  # nothing sent, dialog closed
    assert (tmp_path / "p.png").exists() and ca._status_of(result)[0] == "paused"
    # no answer source at all: the custom question has no answer, is left empty and flagged for a human
    runner = make_runner(session, demo, resume, answers=None)
    result = runner.run(page, questionnaire(tmp_path), submit=True)
    assert result.submitted is False and result.paused_reason == "a question needs a human"
    assert any(u.startswith("Why do you want to join Acme?") for u in result.unfilled_required)
    assert any(n.startswith("Why do you want to join Acme?") and "no answer" in n for n in result.needs_human)
    assert state(page)["submitted"] == "0" and state(page)["closed"] == "1"


def test_login_page_pauses_before_clicking(page, session, demo, resume, tmp_path):
    header = '<header><a href="/profile/candidate-dashboard">Find jobs</a><span>Demo User</span></header>'
    unauth = '<header><button type="button">Candidate login</button></header>'
    html = JOB_PAGE.replace("__DIALOG__", "").replace(header, unauth)
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, fixture(tmp_path, "unauth", html), submit=True, screenshot_path=tmp_path / "l.png")
    assert result.paused_reason == "cutshort login needed" and result.plan == []
    assert state(page)["clicked"] == "0" and (tmp_path / "l.png").exists()
    assert ca._stop_reason(result) == "login"
    # the login redirect URL is enough on its own
    redirected = type("P", (), {"url": "https://cutshort.io/?redirect_url=%2Fprofile%2Fall-jobs"})()
    assert css.classify_page(redirected) == "login"


def test_already_applied_and_missing_button(page, session, demo, resume, tmp_path):
    html = JOB_PAGE.replace("__DIALOG__", "").replace(
        '<button type="button" id="apply" onclick="applyClicked()">Apply to this job</button>',
        '<button type="button" disabled>Applied</button>')
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, fixture(tmp_path, "applied", html), submit=True)
    assert result.error.startswith("already applied") and ca._status_of(result)[0] == "failed"
    html = JOB_PAGE.replace("__DIALOG__", "").replace(
        '<button type="button" id="apply" onclick="applyClicked()">Apply to this job</button>', "<p>Job closed</p>")
    result = runner.run(page, fixture(tmp_path, "closed", html), submit=True)
    assert result.error.startswith("Apply button not found")


def test_captcha_pauses_before_touching_anything(page, session, demo, resume, tmp_path):
    html = JOB_PAGE.replace("__DIALOG__", '<iframe src="https://www.google.com/recaptcha/api2/anchor" '
                                          'style="width:300px;height:80px"></iframe>')
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, fixture(tmp_path, "captcha", html), submit=True, screenshot_path=tmp_path / "c.png")
    assert result.paused_reason.startswith("captcha") and result.plan == [] and result.submitted is False
    assert state(page)["clicked"] == "0" and (tmp_path / "c.png").exists()
    assert ca._stop_reason(result) == "challenge"


def test_blocked_status_code_pauses_without_touching_the_page(session, demo, resume):
    class Resp:
        status = 429

    class Page:
        url = "https://cutshort.io/job/x"

        def goto(self, url, **kw):
            return Resp()

        def locator(self, sel):
            raise AssertionError("page must not be read after a 429")

    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(Page(), "https://cutshort.io/job/x", submit=True)
    assert result.paused_reason == "cutshort answered 429" and result.plan == []
    assert ca._stop_reason(result) == "challenge"


# ------------------------------------------------------------------ apply_job (uniform interface)


def _job(session, user, status=AppStatus.queued, *, source="cutshort", ats="cutshort", score=80,
         url="https://cutshort.io/job/x"):
    job = Job(dedupe_key=f"k-{source}-{ats}-{score}-{status}", title="Engineer", company="Acme", source=source,
              ats_type=ats, apply_url=url, url=url)
    session.add(job)
    session.flush()
    session.add(JobSource(job_id=job.id, source=source, source_id=str(score), raw={"one_click_apply": True}))
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _enable(session, user, **kw):
    site = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == "cutshort")).one()
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


def test_apply_job_with_open_session_honours_every_gate(browser, session, demo, resume, tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "_human_pause", NO_PAUSE)
    ctx = browser.new_context()
    try:
        cs = css.CutshortSession(ctx, demo, session)
        app, job = _job(session, demo, url=one_click(tmp_path))
        # global dry run (the default) wins over the flags
        out = apply_job(cs, app, dry_run=False, auto_submit=True)
        assert out["status"] == "dry_run" and out["screenshot"] and Path(out["screenshot"]).exists()
        assert str(get_settings().screenshots_dir) in out["screenshot"]
        assert "stopped before the Apply click" in out["reason"]
        _allow_real_submits(session, monkeypatch)
        # auto-submit off: still never clicks
        assert apply_job(cs, app, dry_run=False, auto_submit=False)["status"] == "dry_run"
        assert apply_job(cs, app, dry_run=True, auto_submit=True)["status"] == "dry_run"
        # kill switch: nothing is opened
        set_value(session, "kill_switch", "on")
        session.commit()
        out = apply_job(cs, app, dry_run=False, auto_submit=True)
        assert out["status"] == "skipped" and out["reason"] == "kill switch on"
        set_value(session, "kill_switch", "off")
        session.commit()
        # everything allowed: the one-click apply goes through
        out = apply_job(cs, app, dry_run=False, auto_submit=True)
        assert out["status"] == "applied" and out["result"].submitted is True
        # a job without an apply URL fails cleanly
        app2, job2 = _job(session, demo, score=70, url="")
        assert apply_job(cs, app2, dry_run=True, auto_submit=False)["status"] == "failed"
    finally:
        ctx.close()


def test_apply_job_with_db_session_opens_the_profile_headless(session, demo, monkeypatch, tmp_path):
    launches: list[tuple] = []
    checks: list[str] = []
    runs: list[dict] = []

    class _Page:
        def close(self):
            pass

    class _Ctx:
        closed = False

        def new_page(self):
            return _Page()

        def close(self):
            self.closed = True

    class _PW:
        def __enter__(self):
            return "pw"

        def __exit__(self, *a):
            return False

    class _CS:
        def __init__(self, status):
            self.status = status

        def check(self, page):
            checks.append(self.status)
            return self.status

    ctx = _Ctx()
    state_ = {"status": "ok"}
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    monkeypatch.setattr(ca, "_launch_persistent", lambda pw, user_id, site=ca.SITE, headless=True: launches.append(
        (user_id, site, headless)) or ctx)
    monkeypatch.setattr(ca, "_cutshort_session", lambda context, user, db: _CS(state_["status"]))
    monkeypatch.setattr(ca, "_run_for", lambda db, user, app, job, page, llm=None, submit=False: runs.append(
        {"submit": submit}) or FillResult(url=job.apply_url, profile_origin="builtin:cutshort"))
    app, job = _job(session, demo)
    out = apply_job(session, app, dry_run=True, auto_submit=True)
    assert out["status"] == "dry_run" and launches == [(demo.id, "cutshort", True)] and ctx.closed
    assert runs == [{"submit": False}] and checks == ["ok"]
    state_["status"] = "login"
    out = apply_job(session, app, dry_run=True, auto_submit=False)
    assert out["status"] == "paused" and out["reason"] == "cutshort session login" and len(runs) == 1
    state_["status"] = "unknown"
    assert apply_job(session, app, dry_run=True, auto_submit=False)["status"] == "skipped"


# ------------------------------------------------------------------ per-user run (fakes)


class _FakePage:
    url = "https://cutshort.io/profile/all-jobs"

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, status="ok"):
        self.status = status
        self.challenges = 0

    def check(self, page):
        return self.status

    def record_challenge(self):
        self.challenges += 1


class _Runs(list):
    sessions: list


def _wire(monkeypatch, status="ok", throttled=False, result=None):
    contexts: list[_FakeContext] = []
    runs = _Runs()
    sent: list[tuple[str, str]] = []
    sessions: list[_FakeSession] = []

    def launch(pw, user_id, site=ca.SITE, headless=True):
        assert site == "cutshort" and user_id and headless is True
        ctx = _FakeContext()
        contexts.append(ctx)
        return ctx

    def fake_run(db, user, app, job, page, llm=None, submit=False):
        runs.append({"url": job.apply_url, "submit": submit})
        return result or FillResult(url=job.apply_url, profile_origin="builtin:cutshort")

    def fake_session(ctx, user, session):
        sessions.append(_FakeSession(status))
        return sessions[-1]

    monkeypatch.setattr(ca, "_launch_persistent", launch)
    monkeypatch.setattr(ca, "_cutshort_session", fake_session)
    runs.sessions = sessions
    monkeypatch.setattr(ca, "_is_throttled", lambda user, site: throttled)
    monkeypatch.setattr(ca, "_between_applications", lambda page: None)
    monkeypatch.setattr(ca, "_run_for", fake_run)
    import jobapply.notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    return contexts, runs, sent


def test_picker_takes_only_cutshort_jobs(session, demo):
    _enable(session, demo)
    a1, _ = _job(session, demo, status=AppStatus.approved, score=90)
    a2, _ = _job(session, demo, score=80)
    _job(session, demo, source="linkedin", ats=None, score=70, url="https://www.linkedin.com/jobs/view/70/")
    _job(session, demo, source="greenhouse", ats="greenhouse", score=60, url="https://boards.greenhouse.io/a/1")
    picked = pick_cutshort_applications(session, demo, limit=10)
    assert [a.id for a, _ in picked] == [a1.id, a2.id]  # approved first, then by score
    assert apply_mod.site_for_job(session.get(Job, a1.job_id)) == "cutshort"
    assert apply_mod.applied_today(session, demo.id, "cutshort") == 0


def test_apply_cutshort_user_dry_run_and_switches(session, demo, monkeypatch):
    contexts, runs, sent = _wire(monkeypatch)
    _job(session, demo, score=90)
    assert apply_cutshort_user(None, session, demo, limit=5) == []  # site disabled by default
    assert contexts == []
    site = _enable(session, demo, auto_submit=True)
    _job(session, demo, status=AppStatus.approved, score=80)
    out = apply_cutshort_user(None, session, demo, limit=5, dry_run=False)
    # global dry run on (the default): never submits, outcomes are dry_run
    assert len(out) == 2 and all(o.status == AppStatus.dry_run for o in out)
    assert len(runs) == 2 and not any(r["submit"] for r in runs) and contexts[0].closed
    session.refresh(site)
    assert site.login_status == "ok"
    # kill switch: nothing runs, no browser
    _allow_real_submits(session, monkeypatch)
    set_value(session, "kill_switch", "on")
    session.commit()
    contexts, runs, sent = _wire(monkeypatch)
    assert apply_cutshort_user(None, session, demo, limit=5, dry_run=False) == [] and contexts == []
    set_value(session, "kill_switch", "off")
    session.commit()
    # throttled: no browser either
    contexts, runs, sent = _wire(monkeypatch, throttled=True)
    assert apply_cutshort_user(None, session, demo, limit=5, dry_run=False) == [] and contexts == []
    # real submits allowed and auto_submit on: submit=True reaches the runner, outcome applied (the two dry-run
    # rows left the apply statuses, so two fresh picks)
    _job(session, demo, score=75)
    _job(session, demo, score=65)
    submitted = FillResult(url="x", profile_origin="builtin:cutshort", submitted=True)
    contexts, runs, sent = _wire(monkeypatch, result=submitted)
    out = apply_cutshort_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 2 and all(o.status == AppStatus.applied for o in out) and all(r["submit"] for r in runs)
    # per-site daily cap: both rows now count as applied today, so a further run stops before the browser
    _job(session, demo, score=70)
    site.daily_cap = 2
    session.add(site)
    session.commit()
    contexts, runs, sent = _wire(monkeypatch, result=submitted)
    assert apply_cutshort_user(None, session, demo, limit=5, dry_run=False) == [] and runs == []
    # auto_submit off: the runner is told not to submit even with every switch open
    site.daily_cap = 50
    site.auto_submit = False
    session.add(site)
    session.commit()
    contexts, runs, sent = _wire(monkeypatch)
    out = apply_cutshort_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 1 and runs == [{"url": runs[0]["url"], "submit": False}]


def test_apply_cutshort_user_login_and_challenge_stop_the_run(session, demo, monkeypatch):
    site = _enable(session, demo)
    _job(session, demo, score=90)
    _job(session, demo, score=80)
    contexts, runs, sent = _wire(monkeypatch, status="login")
    assert apply_cutshort_user(None, session, demo, limit=5) == []
    assert runs == [] and sent and sent[0][0] == "paused" and "import them for demo" in sent[0][1]
    contexts, runs, sent = _wire(monkeypatch, status="unknown")
    assert apply_cutshort_user(None, session, demo, limit=5) == [] and sent == []
    # challenge mid-run: one outcome, the cooldown recorded through the session, the second pick never opened
    blocked = FillResult(url="x", profile_origin="builtin:cutshort", paused_reason="security check page")
    contexts, runs, sent = _wire(monkeypatch, result=blocked)
    out = apply_cutshort_user(None, session, demo, limit=5)
    assert len(out) == 1 and out[0].status == AppStatus.paused and len(runs) == 1
    assert runs.sessions[0].challenges == 1 and "blocked or challenged" in sent[-1][1]
    # logout mid-run: login_status flips to expired and the loop stops
    expired = FillResult(url="x", profile_origin="builtin:cutshort", paused_reason="cutshort login needed")
    contexts, runs, sent = _wire(monkeypatch, result=expired)
    out = apply_cutshort_user(None, session, demo, limit=5)
    assert len(out) == 1 and len(runs) == 1
    session.refresh(site)
    assert site.login_status == "expired" and "re-import cutshort.io cookies" in sent[-1][1]
    # a crashing runner is recorded as failed and the run goes on (the earlier picks are paused by now)
    _job(session, demo, score=50)
    _job(session, demo, score=40)
    contexts, runs, sent = _wire(monkeypatch)

    def boom(db, user, app, job, page, llm=None, submit=False):
        raise TimeoutError("Timeout 60000ms exceeded")

    monkeypatch.setattr(ca, "_run_for", boom)
    out = apply_cutshort_user(None, session, demo, limit=5)
    assert [o.status for o in out] == [AppStatus.failed, AppStatus.failed]


def test_apply_cutshort_wrapper(session, engine, demo, monkeypatch):
    calls: list[dict] = []

    class _PW:
        def __enter__(self):
            return "pw"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    monkeypatch.setattr(ca, "apply_cutshort_user", lambda pw, s, user, llm=None, limit=3, dry_run=True: calls.append(
        {"user": user.username, "limit": limit, "dry_run": dry_run}) or [])
    monkeypatch.setattr(get_settings(), "linkedin_active_hours", "00:00-00:00")  # always active
    assert ca.apply_cutshort(username="demo", limit=2, dry_run=False, engine=engine) == []
    assert calls == [{"user": "demo", "limit": 2, "dry_run": True}]  # global dry run forced it
    set_value(session, "kill_switch", "on")
    session.commit()
    calls.clear()
    assert ca.apply_cutshort(limit=2, engine=engine) == [] and calls == []
    set_value(session, "kill_switch", "off")
    session.commit()
    monkeypatch.setattr("jobapply.sites.browser.is_active_hours", lambda now=None, spec="": False)
    assert ca.apply_cutshort(limit=2, engine=engine) == [] and calls == []


@pytest.mark.parametrize("reason,expected", [("cutshort login needed", "login"), ("captcha on page (x)", "challenge"),
                                             ("required fields left empty", "")])
def test_stop_reasons(reason, expected):
    assert ca._stop_reason(FillResult(url="x", profile_origin="builtin:cutshort", paused_reason=reason)) == expected
