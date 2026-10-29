"""LinkedIn Easy Apply filler: runs the real runner in headless Chromium against modal fixtures, plus unit
tests for the selector file, the label mapping and the apply-pipeline routing."""

from pathlib import Path

import pytest
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.forms.profiles import VaultView, load_builtin_profiles
from jobapply.forms.runner import FillResult
from jobapply.models import Application, ApplicationEvent, AppStatus, Job, JobSource, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.browser import is_active_hours
from jobapply.sites.linkedin import easy_apply as ea
from jobapply.sites.linkedin.easy_apply import (
    EasyApplyRunner,
    apply_linkedin_user,
    key_for_label,
    load_selectors,
    pick_linkedin_applications,
    skill_years,
)
from jobapply.switches import set_value
from jobapply.users import create_user

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin"
CONFIG = Path(__file__).parent.parent / "config"
NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(30)  # noqa: E731


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        exe = get_settings().chromium_executable
        b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture
def demo(session, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    user = create_user(session, "demo", "demo1234", profile=DEMO_PROFILE, preferences={"daily_cap": 20})
    session.commit()
    yield user
    get_settings.cache_clear()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


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


def make_runner(session, user, resume, answers=None, profile=None, **kw):
    vault = VaultView(profile=dict(profile or DEMO_PROFILE), resume_path=str(resume))
    kw.setdefault("resume_path", str(resume))
    kw.setdefault("selectors", load_selectors(CONFIG / "sites" / "linkedin.yaml"))
    return EasyApplyRunner(session, user, vault, answers=answers, llm=None, pause=NO_PAUSE, **kw)


def variant(resume, name: str, old: str, new: str) -> str:
    """A copy of the basic fixture with one substitution, as a file:// URL."""
    html = (FIXTURES / "easy_apply_modal_basic.html").read_text(encoding="utf-8")
    assert old in html, old
    path = resume.parent / f"{name}.html"
    path.write_text(html.replace(old, new), encoding="utf-8")
    return path.resolve().as_uri()


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


def state(page) -> dict[str, str]:
    return page.evaluate(
        "() => Object.fromEntries(Array.from(document.getElementById('state').attributes)"
        ".filter(a => a.name.startsWith('data-')).map(a => [a.name.slice(5), a.value]))"
    )


# ------------------------------------------------------------------ units


def test_selectors_file_is_not_a_form_profile():
    sel = load_selectors(CONFIG / "sites" / "linkedin.yaml")
    for key in ("easy_apply_button", "modal", "next", "review", "submit", "dismiss", "discard", "resume_upload",
                "uploaded_resume_card", "error", "follow_company_checkbox", "progress"):
        assert sel[key], key
    assert "linkedin" not in load_builtin_profiles(CONFIG / "form_profiles")
    assert load_selectors(Path("nope.yaml"))["modal"] == ea.DEFAULT_SELECTORS["modal"]


def test_label_mapping_and_skill_years():
    assert key_for_label("Email address") == "profile.email"
    assert key_for_label("Phone country code") == "derived.phone_country_code"
    assert key_for_label("Mobile phone number") == "profile.phone"
    assert key_for_label("First name") == "profile.first_name"
    assert key_for_label("Notice period") == "profile.notice_period"
    assert key_for_label("What is your expected CTC?") == "profile.expected_ctc"
    assert key_for_label("Expected salary (in LPA)") == "profile.expected_ctc"
    assert key_for_label("Current CTC") == "profile.current_ctc"
    # joining-date questions are notice-period questions, not salary ones
    assert key_for_label("Expected date of joining") == "profile.notice_period"
    assert key_for_label("Expected joining date") == "profile.notice_period"
    assert key_for_label("Tentative date of joining?") == "profile.notice_period"
    assert key_for_label("Expected graduation year") is None  # "expected" alone is not a salary question
    assert ea.local_phone("+91 98765 43210") == "9876543210" and ea.local_phone("98765-43210") == "9876543210"
    assert ea._dial_code_option(["Select an option", "United States (+1)", "India (+91)"], "+1") == "United States (+1)"
    assert ea._dial_code_option(["India (+91)"], "+1") is None
    assert key_for_label("Will you require visa sponsorship?") == "derived.needs_sponsorship"
    assert key_for_label("Are you willing to relocate?") == "derived.willing_to_relocate"
    assert key_for_label("Are you legally authorized to work in India?") == "derived.work_authorized"
    assert key_for_label("Total years of experience") == "profile.years_experience"
    assert key_for_label("Why do you want to work here?") is None
    skills = DEMO_PROFILE["skills"]
    assert skill_years("How many years of work experience do you have with Python?", skills) == "6"
    assert skill_years("Years of experience with Go", skills) == "1"
    assert skill_years("Do you know Python?", skills) is None  # no "years" -> not a numeric question
    assert skill_years("How many years of experience with Rust?", skills) is None
    assert ea.phone_country_code("+91 98765 43210") == "+91" and ea.phone_country_code("98765") is None


# ------------------------------------------------------------------ runner on fixtures


def test_dry_run_fills_every_step_and_discards_draft(page, session, demo, resume, tmp_path):
    answers = StubAnswers()
    runner = make_runner(session, demo, resume, answers)
    shot = tmp_path / "shots" / "li.png"
    result = runner.run(page, fixture_url("easy_apply_modal_basic.html"), screenshot_path=shot)

    assert result.profile_origin == "builtin:linkedin"
    assert result.error == "" and result.paused_reason == "", result.summary()
    assert result.submitted is False
    assert shot.exists() and result.screenshot_path == str(shot)
    st = state(page)
    assert st["submitted"] == "0"  # dry run never clicks submit
    assert st["discarded"] == "1" and st["dismissed"] == "1"  # no draft left on LinkedIn
    assert st["step"] == "4"  # the review step was reached before discarding

    answers_sent = result.answers
    assert answers_sent["First name"] == "Demo" and answers_sent["Last name"] == "User"
    assert answers_sent["Email address"] == "demo.applies@example.com"
    assert answers_sent["Phone country code"] == "India (+91)"
    assert answers_sent["Mobile phone number"] == "9876543210"  # the dial code went into its own select
    assert answers_sent["Will you now or in the future require visa sponsorship?"] == "No"
    assert answers_sent["Are you willing to relocate to Bengaluru?"] == "Yes"
    assert answers_sent["How many years of work experience do you have with Python?"] == "6"
    assert answers_sent["How many years of experience do you have with FastAPI?"] == "3"
    assert answers_sent["Notice period"] == "30 days"
    assert answers_sent["What is your expected CTC (in LPA)?"] == "40 LPA"
    assert answers_sent["Upload resume"] == "resume.pdf"
    assert not answers.asked  # everything came from the vault / skills
    follow = next(p for p in result.plan if p.field.id == "follow-company-checkbox")
    assert follow.done and follow.value == "unchecked"
    assert all(isinstance(v, str) for v in result.answers.values())


def test_one_digit_dial_code_matches_its_option(page, session, demo, resume):
    profile = {**DEMO_PROFILE, "phone": "+1 415 555 0100"}
    runner = make_runner(session, demo, resume, StubAnswers(), profile=profile)
    result = runner.run(page, fixture_url("easy_apply_modal_basic.html"))
    assert result.answers["Phone country code"] == "United States (+1)"
    assert result.answers["Mobile phone number"] == "4155550100"
    assert result.paused_reason == "" and result.error == "", result.summary()


def test_unmapped_required_select_is_left_for_a_human(page, session, demo, resume):
    url = variant(resume, "team", '<label for="ctc">What is your expected CTC (in LPA)?</label>',
                  '<label for="team">Which team are you applying for?</label>'
                  '<select id="team" name="team" required><option value="">Select an option</option>'
                  '<option value="pay">Payments</option><option value="risk">Risk</option></select>'
                  '<label for="ctc">What is your expected CTC (in LPA)?</label>')
    runner = make_runner(session, demo, resume, answers=None)  # no answer bank, no LLM
    result = runner.run(page, url, submit=True)
    assert result.submitted is False and result.paused_reason == "required fields left empty"
    assert any(u.startswith("Which team are you applying for?") for u in result.unfilled_required)
    team = next(p for p in result.plan if p.field.question.startswith("Which team"))
    assert team.kind == "select" and team.done is False  # never "kept" at its placeholder
    assert result.answers.get("Which team are you applying for?") in (None, "")
    assert state(page)["discarded"] == "1"


def test_missing_resume_file_is_flagged(page, session, demo, resume):
    runner = make_runner(session, demo, resume, StubAnswers(), resume_path=str(resume.parent / "missing.pdf"))
    result = runner.run(page, fixture_url("easy_apply_modal_basic.html"), submit=True)
    assert result.submitted is False and result.paused_reason == "required fields left empty"
    assert any(u.startswith("Upload resume (resume file missing)") for u in result.unfilled_required)
    assert state(page)["submitted"] == "0"


def test_job_description_mentioning_security_check_is_not_a_challenge(page, session, demo, resume):
    url = variant(resume, "soc", "We are looking for a backend engineer with Python, FastAPI and Postgres.",
                  "SOC role: you will detect unusual activity and perform a security check on every login; "
                  "successful candidates must pass a security check. Are you a robot? No.")
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, url)
    assert result.paused_reason == "" and result.error == "", result.summary()
    assert state(page)["step"] == "4"


def test_external_apply_button_is_not_clicked(page, session, demo, resume):
    url = variant(resume, "ext", 'aria-label="Easy Apply to Senior Python Developer at Acme" id="ember-easy-apply">'
                  "Easy Apply</button>",
                  'aria-label="Apply to Senior Python Developer at Acme on company website" id="ember-easy-apply">'
                  "Apply</button>")
    runner = make_runner(session, demo, resume, StubAnswers(),
                         selectors={"easy_apply_button": "button.jobs-apply-button"})
    result = runner.run(page, url, submit=True)
    assert result.error.startswith("external apply") and result.plan == [] and result.submitted is False
    assert state(page)["step"] == "0"  # the button was never clicked


def test_expired_session_on_job_page_pauses_the_run(page, session, demo, resume):
    runner = make_runner(session, demo, resume, StubAnswers())
    expired = resume.parent / "authwall.html"
    expired.write_text("<html><body><form class='login__form'><input id='session_key'></form></body></html>",
                       encoding="utf-8")
    result = runner.run(page, expired.resolve().as_uri(), submit=True)
    assert result.paused_reason == "linkedin session expired" and result.plan == []
    assert ea._stop_reason(result) == "expired"

    def paused(reason):
        return FillResult(url="x", profile_origin="builtin:linkedin", paused_reason=reason)

    assert ea._stop_reason(paused("linkedin answered 999")) == "challenge"
    assert ea._stop_reason(paused("captcha on page")) == "challenge"
    assert ea._stop_reason(paused("required fields left empty")) == ""
    assert ea._stop_reason(None) == ""


def test_blocked_status_code_pauses_without_touching_the_page(session, demo, resume):
    class Resp:
        status = 999

    class Page:
        url = "https://www.linkedin.com/jobs/view/1/"

        def goto(self, url, **kw):
            return Resp()

        def locator(self, sel):
            raise AssertionError("page must not be read after a 999")

    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(Page(), "https://www.linkedin.com/jobs/view/1/")
    assert result.paused_reason == "linkedin answered 999" and result.plan == []


def test_resume_uploaded_and_card_respected(page, session, demo, resume):
    runner = make_runner(session, demo, resume, StubAnswers())
    uploads: list[int] = []
    page.on("filechooser", lambda fc: uploads.append(1))
    result = runner.run(page, fixture_url("easy_apply_modal_basic.html"))
    file_plan = [p for p in result.plan if p.kind == "file"]
    assert len(file_plan) == 1 and file_plan[0].done and file_plan[0].value == str(resume)
    assert result.unfilled_required == []


def test_submit_true_clicks_submit(page, session, demo, resume, tmp_path):
    runner = make_runner(session, demo, resume, StubAnswers())
    url = fixture_url("easy_apply_modal_basic.html")
    result = runner.run(page, url, submit=True, screenshot_path=tmp_path / "s.png")
    assert result.submitted is True and result.error == "", result.summary()
    st = state(page)
    assert st["submitted"] == "1" and st["discarded"] == "0"
    assert page.locator("#sent").is_hidden()  # the "application sent" dialog was dismissed


def test_inline_error_pauses_and_discards(page, session, demo, resume):
    runner = make_runner(session, demo, resume, StubAnswers())
    result = runner.run(page, fixture_url("easy_apply_modal_error.html"), submit=True)
    assert result.submitted is False
    assert result.paused_reason == "required fields left empty"
    assert any("valid phone number" in u for u in result.unfilled_required)
    st = state(page)
    assert st["submitted"] == "0" and st["discarded"] == "1"
    assert page.locator("#first-name").input_value() == ""  # the form was reset by the discard


def test_captcha_page_pauses_before_touching_anything(page, session, demo, resume, tmp_path):
    runner = make_runner(session, demo, resume, StubAnswers())
    url = fixture_url("easy_apply_modal_captcha.html")
    result = runner.run(page, url, submit=True, screenshot_path=tmp_path / "c.png")
    assert result.paused_reason.startswith("captcha") or "security" in result.paused_reason
    assert result.submitted is False and result.plan == []
    assert (tmp_path / "c.png").exists()


def test_unknown_question_goes_to_answer_source_and_flags_low_confidence(page, session, demo, resume):
    html = (FIXTURES / "easy_apply_modal_basic.html").read_text(encoding="utf-8").replace(
        '<label for="ctc">What is your expected CTC (in LPA)?</label>',
        '<label for="why">Why do you want to join Acme?</label><input id="why" name="why" type="text" required>'
        '<label for="ctc">What is your expected CTC (in LPA)?</label>',
    )
    custom = resume.parent / "custom.html"
    custom.write_text(html, encoding="utf-8")
    answers = StubAnswers({"join acme": ("Because of the payments platform.", 40)})
    runner = make_runner(session, demo, resume, answers)
    result = runner.run(page, custom.resolve().as_uri(), submit=True)
    assert [q for q, _ in answers.asked] == ["Why do you want to join Acme?"]
    assert result.answers["Why do you want to join Acme?"] == "Because of the payments platform."
    assert result.needs_human and "low confidence" in result.needs_human[0]
    assert result.paused_reason == "a question needs a human"
    assert result.submitted is False and state(page)["discarded"] == "1"


# ------------------------------------------------------------------ apply pipeline routing


def _job(session, user, status=AppStatus.queued, *, source="linkedin", ats=None, easy_apply=True, score=80):
    job = Job(
        dedupe_key=f"k-{source}-{ats}-{easy_apply}-{score}-{status}", title="Engineer", company="Acme",
        source=source, ats_type=ats, apply_url=f"https://www.linkedin.com/jobs/view/{score}{int(easy_apply)}/",
    )
    session.add(job)
    session.flush()
    session.add(JobSource(job_id=job.id, source=source, source_id=str(score), raw={"easy_apply": easy_apply}))
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _enable_linkedin(session, user, **kw):
    site = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == "linkedin")).one()
    site.enabled = True
    for k, v in kw.items():
        setattr(site, k, v)
    session.add(site)
    session.commit()
    return site


def test_form_runner_path_skips_linkedin_jobs(session, demo):
    _enable_linkedin(session, demo)
    li_app, _ = _job(session, demo)
    assert apply_mod.site_for_job(session.get(Job, li_app.job_id)) == "linkedin"
    picked = apply_mod.pick_applications(session, demo, limit=10)
    assert li_app.id not in [p[0].id for p in picked]
    # and the LinkedIn picker takes only Easy Apply postings
    _job(session, demo, easy_apply=False, score=70)
    picked_li = pick_linkedin_applications(session, demo, limit=10)
    assert [a.id for a, _ in picked_li] == [li_app.id]


def test_applied_today_counts_linkedin_rows(session, demo):
    app, _ = _job(session, demo, status=AppStatus.applied)
    app.submitted_at = apply_mod.utcnow()
    session.add(app)
    session.commit()
    assert apply_mod.applied_today(session, demo.id, "linkedin") == 1
    assert apply_mod.applied_today(session, demo.id, "greenhouse") == 0


def test_record_outcome_shared_bookkeeping(session, demo, monkeypatch):
    sent = []
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    app, job = _job(session, demo)
    r = FillResult(url=job.apply_url, profile_origin="builtin:linkedin", paused_reason="captcha on page")
    out = apply_mod.record_outcome(session, demo, app, job, r)
    assert out.status == AppStatus.paused and sent[0][0] == "paused"
    app2, job2 = _job(session, demo, score=81)
    out = apply_mod.record_outcome(session, demo, app2, job2, None, error="TimeoutError: boom")
    assert out.status == AppStatus.failed and app2.error.startswith("TimeoutError")
    events = session.exec(select(ApplicationEvent).where(ApplicationEvent.application_id == app2.id)).all()
    assert events and events[-1].kind == "failed"


class _FakePage:
    url = "https://www.linkedin.com/feed/"

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
    """The run log, carrying the fake LinkedInSession objects the run created."""

    sessions: list


def _wire(monkeypatch, status="ok", throttled=False, result=None):
    contexts: list[_FakeContext] = []
    runs = _Runs()
    sent: list[tuple[str, str]] = []
    sessions: list[_FakeSession] = []

    def launch(pw, user_id, site, headless=None):
        assert site == "linkedin" and user_id
        ctx = _FakeContext()
        contexts.append(ctx)
        return ctx

    def fake_run(self, page, job_url, submit=False, screenshot_path=None):
        runs.append({"url": job_url, "submit": submit})
        return result or FillResult(url=job_url, profile_origin="builtin:linkedin")

    def fake_session(ctx, user, session):
        sessions.append(_FakeSession(status))
        return sessions[-1]

    monkeypatch.setattr(ea, "_launch_persistent", launch)
    monkeypatch.setattr(ea, "_linkedin_session", fake_session)
    runs.sessions = sessions  # type: ignore[attr-defined]
    monkeypatch.setattr(ea, "_is_throttled", lambda user, site: throttled)
    monkeypatch.setattr(ea, "_between_applications", lambda page: None)
    monkeypatch.setattr(EasyApplyRunner, "run", fake_run)
    import jobapply.notify as notify_mod

    monkeypatch.setattr(notify_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    return contexts, runs, sent


def test_apply_linkedin_user_dry_run(session, demo, monkeypatch):
    contexts, runs, sent = _wire(monkeypatch)
    assert apply_linkedin_user(None, session, demo, limit=5) == []  # site disabled by default
    site = _enable_linkedin(session, demo, auto_submit=True)
    a1, _ = _job(session, demo, status=AppStatus.approved, score=90)
    a2, _ = _job(session, demo, score=80)
    _job(session, demo, easy_apply=False, score=70)
    out = apply_linkedin_user(None, session, demo, limit=5, dry_run=True)
    assert {o.app_id for o in out} == {a1.id, a2.id} and all(o.status == AppStatus.dry_run for o in out)
    assert len(runs) == 2 and not any(r["submit"] for r in runs)  # dry run never submits
    assert contexts and contexts[0].closed
    session.refresh(site)
    assert site.login_status == "ok"


def test_apply_linkedin_user_login_needed(session, demo, monkeypatch):
    contexts, runs, sent = _wire(monkeypatch, status="expired")
    site = _enable_linkedin(session, demo)
    _job(session, demo)
    assert apply_linkedin_user(None, session, demo, limit=5) == []
    assert runs == [] and sent and sent[0][0] == "paused" and "jobapply linkedin login -u demo" in sent[0][1]
    session.refresh(site)
    assert site.login_status == "expired"
    # "unknown" (slow feed) is retried later without an alert and without touching login_status
    site.login_status = "ok"
    session.add(site)
    session.commit()
    contexts, runs, sent = _wire(monkeypatch, status="unknown")
    assert apply_linkedin_user(None, session, demo, limit=5) == []
    assert runs == [] and sent == []
    session.refresh(site)
    assert site.login_status == "ok"


def _allow_real_submits(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "dry_run", False)
    set_value(session, "dry_run", "off")
    session.commit()


def test_apply_linkedin_user_throttled_and_submit_rules(session, demo, monkeypatch):
    contexts, runs, sent = _wire(monkeypatch, throttled=True)
    _enable_linkedin(session, demo, auto_submit=True)
    _job(session, demo)
    _allow_real_submits(session, monkeypatch)
    assert apply_linkedin_user(None, session, demo, limit=5, dry_run=False) == []
    assert runs == [] and contexts == []  # throttled: no browser is even launched
    submitted = FillResult(url="x", profile_origin="builtin:linkedin", submitted=True)
    contexts, runs, sent = _wire(monkeypatch, result=submitted)
    out = apply_linkedin_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 1 and out[0].status == AppStatus.applied and runs[0]["submit"] is True


def test_apply_linkedin_user_honours_global_switches(session, demo, monkeypatch):
    _enable_linkedin(session, demo, auto_submit=True)
    _job(session, demo)
    # global dry run on (the default): a dry_run=False call still never submits
    contexts, runs, sent = _wire(monkeypatch, result=FillResult(url="x", profile_origin="builtin:linkedin"))
    out = apply_linkedin_user(None, session, demo, limit=5, dry_run=False)
    assert len(out) == 1 and runs == [{"url": out and runs[0]["url"], "submit": False}]
    # kill switch: nothing runs, no browser
    _allow_real_submits(session, monkeypatch)
    set_value(session, "kill_switch", "on")
    session.commit()
    contexts, runs, sent = _wire(monkeypatch)
    assert apply_linkedin_user(None, session, demo, limit=5, dry_run=False) == []
    assert runs == [] and contexts == []


def test_challenge_mid_run_stops_the_loop_and_sets_the_cooldown(session, demo, monkeypatch):
    _enable_linkedin(session, demo)
    _job(session, demo, score=90)
    _job(session, demo, score=80)
    blocked = FillResult(url="x", profile_origin="builtin:linkedin", paused_reason="security check page")
    contexts, runs, sent = _wire(monkeypatch, result=blocked)
    out = apply_linkedin_user(None, session, demo, limit=5)
    assert len(out) == 1 and out[0].status == AppStatus.paused
    assert len(runs) == 1  # the second pick was never opened
    assert runs.sessions[0].challenges == 1  # cooldown recorded through LinkedInSession.record_challenge
    assert sent and sent[-1][0] == "paused" and "blocked or challenged" in sent[-1][1]
    # a logout mid-run: login_status flips to expired and the loop stops as well
    site = _enable_linkedin(session, demo)
    _job(session, demo, score=70)
    _job(session, demo, score=60)
    expired = FillResult(url="x", profile_origin="builtin:linkedin", paused_reason="linkedin session expired")
    contexts, runs, sent = _wire(monkeypatch, result=expired)
    out = apply_linkedin_user(None, session, demo, limit=5)
    assert len(out) == 1 and len(runs) == 1
    session.refresh(site)
    assert site.login_status == "expired" and "login -u demo" in sent[-1][1]


def test_notifier_failure_does_not_abort_the_run(session, demo, monkeypatch):
    _enable_linkedin(session, demo)
    _job(session, demo, score=90)
    _job(session, demo, score=80)
    paused = FillResult(url="x", profile_origin="builtin:linkedin", paused_reason="a question needs a human")
    contexts, runs, sent = _wire(monkeypatch, result=paused)
    import jobapply.notify as notify_mod

    def boom(*a, **k):
        raise KeyError("Attempt to overwrite 'message' in LogRecord")

    monkeypatch.setattr(notify_mod, "notify", boom)
    out = apply_linkedin_user(None, session, demo, limit=5)
    assert [o.status for o in out] == [AppStatus.paused, AppStatus.paused] and len(runs) == 2


def test_apply_linkedin_wrapper(session, engine, demo, monkeypatch):
    calls: list[dict] = []

    class _PW:
        def __enter__(self):
            return "pw"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    monkeypatch.setattr(ea, "apply_linkedin_user", lambda pw, s, user, llm=None, limit=3, dry_run=True: calls.append(
        {"user": user.username, "limit": limit, "dry_run": dry_run}) or [])
    monkeypatch.setattr(get_settings(), "linkedin_active_hours", "00:00-00:00")  # always active
    assert ea.apply_linkedin(username="demo", limit=2, dry_run=False, engine=engine) == []
    assert calls == [{"user": "demo", "limit": 2, "dry_run": True}]  # global dry run forced it
    # kill switch: no user is visited
    set_value(session, "kill_switch", "on")
    session.commit()
    calls.clear()
    assert ea.apply_linkedin(limit=2, engine=engine) == [] and calls == []
    set_value(session, "kill_switch", "off")
    session.commit()
    # outside the active hours nothing runs either, whoever the caller is
    now_spec = "08:00-23:00"
    monkeypatch.setattr(get_settings(), "linkedin_active_hours", now_spec)
    monkeypatch.setattr("jobapply.sites.browser.is_active_hours", lambda now=None, spec="": False)
    assert ea.apply_linkedin(limit=2, engine=engine) == [] and calls == []
    assert is_active_hours(spec="00:00-00:00") is True
