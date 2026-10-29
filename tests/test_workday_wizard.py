"""Workday wizard: the FormRunner walks the five fixture steps (real headless Chromium), Save and Continue is
clicked on every step, Review is reached, and Submit is clicked only with ``submit=True``."""

from __future__ import annotations

from pathlib import Path

from jobapply.forms.profiles import FormProfileSpec
from jobapply.forms.runner import FormRunner
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.workday import wizard as wz
from jobapply.sites.workday.wizard import (
    STEP_MY_EXPERIENCE,
    STEP_MY_INFORMATION,
    STEP_QUESTIONS,
    STEP_REVIEW,
    WorkdayWizard,
    canonical_step,
    load_step_profiles,
    step_spec,
    workday_vault_view,
)
from tests.test_workday_account import NO_PAUSE, TENANT, fixture_html, route_tenant, wd_state

PROFILES = Path(__file__).parent.parent / "config" / "form_profiles"


class StubAnswers:
    """Answer source stand-in: canned answers by substring, nothing else."""

    def __init__(self, canned: dict[str, tuple[str | None, int]] | None = None):
        self.canned = canned if canned is not None else DEFAULT_CANNED
        self.asked: list[tuple[str, list[str] | None]] = []

    def answer(self, question, options=None):
        self.asked.append((question, list(options) if options else None))
        for needle, (answer, conf) in self.canned.items():
            if needle.lower() in question.lower():
                return answer, conf
        return None, 0


DEFAULT_CANNED = {
    "previously worked": ("No", 90),
    "authorized to work": ("Yes", 95),
    "sponsorship": ("No", 95),
    "notice period": ("30 days", 90),
    "degree": ("Bachelor's Degree", 85),
}


class App:
    resume_variant: str | None = None
    cover_letter = "Dear team"
    message = ""


def make_wizard(session, demo, resume, answers=None, profile=None) -> WorkdayWizard:
    app = App()
    app.resume_variant = str(resume)
    view = workday_vault_view(demo, app)
    runner = FormRunner(session, view, answers=answers if answers is not None else StubAnswers(), llm=None,
                        user=demo, profiles_dir=PROFILES, learn=False, navigate=False)
    return WorkdayWizard(runner, pause=NO_PAUSE, settle_ms=80)


def open_wizard(page, **cfg) -> None:
    route_tenant(page, fixture_html(signed_in=True, **cfg))
    page.goto(TENANT.apply_manually_url, wait_until="domcontentloaded")
    page.wait_for_timeout(50)


# ------------------------------------------------------------------ units


def test_step_profiles_load_from_the_yaml():
    base, steps = load_step_profiles()
    assert base.ats == "workday" and base.fields and "input[name=website]" in base.never
    assert set(steps) >= {STEP_MY_INFORMATION, STEP_MY_EXPERIENCE, STEP_QUESTIONS, "Voluntary Disclosures"}
    assert steps[STEP_MY_INFORMATION]["label=First Name"] == "profile.first_name"
    assert steps[STEP_QUESTIONS] == {}  # answered by the bank / heuristics / LLM, never hardcoded
    spec = step_spec(base, steps[STEP_MY_EXPERIENCE])
    assert isinstance(spec, FormProfileSpec) and spec.fields["input[type=file]"] == "derived.resume_path"
    assert spec.detect == base.detect and "Submit" in spec.submit


def test_canonical_step_aliases():
    assert canonical_step("My Information") == STEP_MY_INFORMATION
    assert canonical_step("Voluntary Self-Identification") == "Voluntary Disclosures"
    assert canonical_step("Self Identify") == "Self Identify"
    assert canonical_step("Review") == STEP_REVIEW and canonical_step("Something Else") == "Something Else"


def test_workday_vault_view_derives_extras(demo):
    view = workday_vault_view(demo, None)
    x = view.extra
    assert x["city"] == "Bengaluru" and x["phone_country"] == "India" and x["full_name"] == "Demo User"
    assert x["school"] == "Example Institute of Technology" and x["degree"] == "B.Tech"
    assert x["field_of_study"] == "Computer Science" and x["graduation_year"] == "2019"
    assert x["work_start_year"] == "2022" and x["work_start_month"] == "01" and x["currently_work_here"] is True
    assert "Python" in x["skills_text"] and x["source"] == "Job Board" and len(x["today"]) == 10
    assert "payments" in x["role_description"].lower()


# ------------------------------------------------------------------ the wizard on the fixture


def test_dry_run_fills_every_step_and_never_clicks_submit(page, session, demo, resume, tmp_path):
    open_wizard(page)
    shot = tmp_path / "shots" / "wd.png"
    result = make_wizard(session, demo, resume).run(page, submit=False, screenshot_path=shot)
    assert result.paused_reason == "" and result.error == "", result.as_dict()
    assert result.reached_review and result.final_step == STEP_REVIEW
    assert not result.submit_clicked and not result.submitted
    assert [s.step for s in result.steps] == [STEP_MY_INFORMATION, STEP_MY_EXPERIENCE, STEP_QUESTIONS,
                                              "Voluntary Disclosures"]
    assert all(s.advanced for s in result.steps)
    st = wd_state(page)
    assert st["next_clicks"] == 4 and st.get("submitted") is None and st["submit_clicks"] == 0
    info = st["values"][STEP_MY_INFORMATION]
    assert info["First Name*"] == "Demo" and info["Last Name*"] == "User"
    assert info["Email Address*"] == DEMO_PROFILE["email"] and info["Phone Number*"] == DEMO_PROFILE["phone"]
    assert info["Phone Device Type*"] == "Mobile" and info["City"] == "Bengaluru"
    assert info["Country Phone Code"] == "India" and info["Have you previously worked for Acme?*"] == "No"
    exp = st["values"][STEP_MY_EXPERIENCE]
    assert exp["Job Title*"] == "Senior Software Engineer" and exp["Company*"] == "Example Systems"
    assert exp["Resume/CV*"] == "resume.pdf" and exp["Degree"] == "Bachelor's Degree"
    assert exp["School or University"] == "Example Institute of Technology" and exp["Skills"].startswith("Python")
    assert exp["LinkedIn"] == DEMO_PROFILE["linkedin"]
    q = st["values"][STEP_QUESTIONS]
    assert q["Are you legally authorized to work in India?*"] == "Yes"
    assert q["Will you now or in the future require sponsorship?*"] == "No"
    assert q["What is your notice period?*"] == "30 days"
    vol = st["values"]["Voluntary Disclosures"]
    assert vol["I have read and consent to the terms and conditions.*"] is True
    # per-step screenshots plus the final one
    assert shot.exists() and (shot.parent / "wd-my-information.png").exists()
    assert (shot.parent / "wd-application-questions.png").exists()
    # answers are collected across steps for the Queue page
    assert result.answers["What is your notice period?*"] == "30 days"
    assert any("First Name" in k for k in result.answers)


def test_submit_clicks_submit_on_review_and_sees_the_confirmation(page, session, demo, resume, tmp_path):
    open_wizard(page)
    result = make_wizard(session, demo, resume).run(page, submit=True, screenshot_path=tmp_path / "s.png")
    assert result.submitted and result.submit_clicked and result.reached_review, result.as_dict()
    st = wd_state(page)
    assert st["submitted"] == 1 and st["submit_clicks"] == 1 and st["next_clicks"] == 4
    assert (tmp_path / "s-submitted.png").exists()


def test_low_confidence_answer_reaches_review_but_blocks_submit(page, session, demo, resume):
    open_wizard(page)
    canned = {**DEFAULT_CANNED, "notice period": ("2 weeks maybe", 20)}
    result = make_wizard(session, demo, resume, StubAnswers(canned)).run(page, submit=True)
    assert result.reached_review and result.paused_reason == "a question needs a human"
    assert not result.submit_clicked and not result.submitted
    assert any("notice period" in h.lower() for h in result.needs_human)
    st = wd_state(page)
    assert st.get("submitted") is None and st["next_clicks"] == 4


def test_missing_required_answer_pauses_before_save_and_continue(page, session, demo, resume):
    open_wizard(page)
    canned = {k: v for k, v in DEFAULT_CANNED.items() if k != "previously worked"}
    result = make_wizard(session, demo, resume, StubAnswers(canned)).run(page, submit=True)
    assert result.paused_reason == "My Information: required fields left empty"
    assert any("previously worked" in u.lower() for u in result.unfilled_required)
    assert wd_state(page).get("next_clicks", 0) == 0 and not result.reached_review


def test_site_error_after_save_and_continue_is_reported(page, session, demo, resume):
    open_wizard(page, reject_step=STEP_MY_EXPERIENCE)
    result = make_wizard(session, demo, resume).run(page, submit=True)
    assert result.paused_reason.startswith("My Experience: Something went wrong")
    st = wd_state(page)
    assert st["next_clicks"] == 1 and st.get("submitted") is None


def test_captcha_in_the_wizard_pauses_with_challenge(page, session, demo, resume):
    open_wizard(page)
    page.evaluate("() => { const d = document.createElement('div'); d.className = 'g-recaptcha';"
                  " d.dataset.sitekey = 'x'; d.style.cssText = 'width:304px;height:78px';"
                  " document.body.appendChild(d); }")
    result = make_wizard(session, demo, resume).run(page, submit=True)
    assert result.login_status == "challenge" and "captcha" in result.paused_reason
    assert wd_state(page).get("next_clicks", 0) == 0


def test_wizard_on_a_non_wizard_page_errors_out(page, session, demo, resume):
    route_tenant(page, fixture_html())
    page.goto(TENANT.apply_manually_url, wait_until="domcontentloaded")  # the sign-in step
    result = make_wizard(session, demo, resume).run(page, submit=True)
    assert result.error.startswith("wizard not on the page") and not result.submit_clicked


def test_profile_path_falls_back_to_the_repo_copy(monkeypatch, tmp_path):
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_PROFILES_DIR", str(tmp_path / "nowhere"))
    get_settings.cache_clear()
    try:
        assert wz.profile_path().name == "workday.yaml" and wz.profile_path().exists()
    finally:
        get_settings.cache_clear()
