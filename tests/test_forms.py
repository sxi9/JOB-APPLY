from pathlib import Path

import pytest
from sqlmodel import select

from jobapply.ai.fake import FakeLLM
from jobapply.config import get_settings
from jobapply.forms import mapper
from jobapply.forms.fields import FormField, extract_fields, match_selector
from jobapply.forms.fingerprint import domain_of, fingerprint
from jobapply.forms.profiles import VaultView, load_builtin_profiles, profile_for_url, resolve_key
from jobapply.forms.runner import AnswerSource, FillResult, FormRunner, vault_view_for
from jobapply.models import AnswerBank, Application, AppStatus, FormProfile, Job, UserSite
from jobapply.pipeline import apply as apply_mod
from jobapply.seed import DEMO_PROFILE

FIXTURES = Path(__file__).parent / "fixtures" / "forms"
PROFILES = Path(__file__).parent.parent / "config" / "form_profiles"
URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/gitlab/jobs/8629019002",
    "lever": "https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd/apply",
    "ashby": "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application",
    "workable": "https://apply.workable.com/j/81B46579FE/apply",
    "smartrecruiters": "https://jobs.smartrecruiters.com/oneclick-ui/company/Experian/publication/7126627d",
}


def vault(resume):
    return VaultView(profile=DEMO_PROFILE, resume_path=str(resume), cover_letter="Dear team, hello.")


# ------------------------------------------------------------------ units


def test_resolve_key():
    v = VaultView(
        profile={"first_name": "Demo", "last_name": "User", "needs_sponsorship": "no", "skills": {"Python": 6}},
        resume_path="/r.pdf",
        cover_letter="CL",
        answers={"Why us?": "Because"},
    )
    assert resolve_key("profile.first_name", v).value == "Demo"
    assert resolve_key("derived.full_name", v).value == "Demo User"
    assert resolve_key("derived.resume_path", v).kind == "file"
    assert resolve_key("derived.needs_sponsorship", v).value is False
    assert resolve_key("profile.skills", v).value == "Python"
    assert resolve_key("const:Yes", v).value == "Yes"
    assert resolve_key("answers.Why us?", v).value == "Because"
    assert resolve_key("answers.New?", v).value is None and resolve_key("answers.New?", v).question == "New?"
    assert resolve_key("skip", v).kind == "skip" and resolve_key("garbage", v).kind == "skip"


def test_match_selector():
    fields = [
        FormField(0, "#first_name", "input", "text", id="first_name", label="First Name*"),
        FormField(1, 'input[name="urls[LinkedIn]"]', "input", "text", name="urls[LinkedIn]", label="LinkedIn URL"),
        FormField(2, '[data-jobapply-idx="2"]', "input", "tel", label="Phone"),
        FormField(3, "x", "input", "radio", name="cards[a][field0]", label="Yes", group="Do you consent?"),
    ]
    assert [f.idx for f in match_selector(fields, "#first_name")] == [0]
    assert [f.idx for f in match_selector(fields, 'input[name="urls[LinkedIn]"]')] == [1]
    assert [f.idx for f in match_selector(fields, "input[type=tel]")] == [2]
    assert [f.idx for f in match_selector(fields, "label=linkedin")] == [1]
    assert [f.idx for f in match_selector(fields, "label=consent")] == [3]
    assert [f.idx for f in match_selector(fields, "input[name^=cards]")] == [3]
    assert match_selector(fields, "select[name=x]") == []


def test_fingerprint_ignores_generated_ids_and_values():
    a = [
        FormField(0, "#q1", "input", "text", id="question_37267170002", label="Where are you located?"),
        FormField(1, "#e", "input", "email", id="email", label="Email"),
    ]
    b = [
        FormField(5, "#q9", "input", "text", id="question_99999999999", label="Where are you located?"),
        FormField(6, "#e", "input", "email", id="email", label="Email"),
    ]
    c = a + [FormField(2, "#x", "textarea", "textarea", label="Why us?")]
    assert fingerprint(a) == fingerprint(b) != fingerprint(c)
    assert domain_of("https://www.Jobs.Lever.co/acme/123") == "jobs.lever.co"


def test_builtin_profiles_load_and_detect():
    profiles = load_builtin_profiles(PROFILES)
    assert set(profiles) == {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}
    for ats, url in URLS.items():
        assert profile_for_url(url, profiles).ats == ats, url
    assert profile_for_url("https://careers.example.com/apply", profiles) is None
    for spec in profiles.values():
        assert spec.submit and spec.fields


def test_mapper_filters_and_profile_fields(session, demo):
    fields = [
        FormField(0, "#first_name", "input", "text", id="first_name", label="First Name", required=True),
        FormField(1, "#q_123456789", "input", "text", id="q_123456789", label="Years of Python?", required=True),
        FormField(
            2, "#secret", "textarea", "textarea", id="secret", label="Decode this secret to prove you are not a bot"
        ),
        FormField(3, "#cap", "textarea", "textarea", name="g-recaptcha-response", hidden=True),
    ]
    out = mapper.map_fields(session, FakeLLM(), demo, fields, "https://x/apply")
    by = {m.idx: m for m in out.mappings}
    assert by[0].key == "profile.first_name" and by[1].key.startswith("answers.") and by[2].needs_human
    assert 3 not in by  # hidden fields are not sent to the mapper
    learned = mapper.mapping_to_profile_fields(fields, out)
    assert learned == {"#first_name": "profile.first_name", "label=Years of Python?": "answers.Years of Python?"}


# ------------------------------------------------------------------ runner on fixtures


def _values(page, selectors):
    return {s: page.locator(s).first.input_value() for s in selectors}


@pytest.mark.parametrize("ats", list(URLS))
def test_dry_run_fills_builtin_profiles(ats, page, session, demo, resume):
    runner = FormRunner(
        session,
        vault(resume),
        answers=AnswerSource(session, FakeLLM(), demo),
        llm=FakeLLM(),
        user=demo,
        profiles_dir=PROFILES,
    )
    shot = Path(get_settings().data_dir) / f"{ats}.png"
    result = runner.run(page, url=URLS[ats], html=(FIXTURES / f"{ats}.html").read_text(), screenshot_path=shot)
    assert result.profile_origin == "builtin", result.summary()
    assert not result.submitted and result.error == ""
    assert shot.exists()
    assert "demo.applies@example.com" in result.answers.values()
    filled = result.answers
    assert any(v in ("Demo", "Demo User") for v in filled.values())
    assert any(v == "resume.pdf" for v in filled.values()), filled
    uploaded = page.evaluate("() => Array.from(document.querySelectorAll('input[type=file]')).map(i => i.files.length)")
    assert 1 in uploaded
    assert page.locator("#thanks").count() == 0 or not page.locator("#thanks").is_visible()
    # Every answer the filler sent is visible on the Queue page
    assert all(isinstance(v, str) for v in result.answers.values())


def test_ashby_anti_bot_question_pauses_for_human(page, session, demo, resume):
    runner = FormRunner(
        session,
        vault(resume),
        answers=AnswerSource(session, FakeLLM(), demo),
        llm=FakeLLM(),
        user=demo,
        profiles_dir=PROFILES,
    )
    result = runner.run(page, url=URLS["ashby"], html=(FIXTURES / "ashby.html").read_text())
    assert result.paused_reason  # required custom questions the fake cannot answer confidently, or unfilled
    assert page.locator("input[name=_systemfield_name]").input_value() == "Demo User"


def test_captcha_pauses(page, session, demo, resume):
    html = (
        (FIXTURES / "lever.html")
        .read_text()
        .replace(
            "</form>",
            '<iframe src="https://www.google.com/recaptcha/api2/anchor" style="width:300px;height:80px"></iframe></form>',  # noqa: E501
        )
    )
    runner = FormRunner(session, vault(resume), profiles_dir=PROFILES)
    result = runner.run(page, url=URLS["lever"], html=html)
    assert result.paused_reason.startswith("captcha")
    assert page.locator("input[name=email]").input_value() == "demo.applies@example.com"


def test_submit_clicks_and_detects_success(page, session, demo, resume):
    html = (
        (FIXTURES / "workable.html")
        .read_text()
        .replace("<button", '<div id="done" style="display:none">Thank you for applying</div><button')
        .replace(
            '<form data-ui="application-form">',
            "<form data-ui=\"application-form\" onsubmit=\"event.preventDefault();document.getElementById('done').style.display='block'\">",  # noqa: E501
        )
    )
    runner = FormRunner(
        session,
        vault(resume),
        answers=AnswerSource(session, FakeLLM(), demo),
        llm=FakeLLM(),
        user=demo,
        profiles_dir=PROFILES,
    )
    result = runner.run(page, url=URLS["workable"], html=html, submit=True)
    assert result.paused_reason == "", result.summary()
    assert result.submitted is True
    assert page.locator("#done").is_visible()


def test_unknown_form_uses_ai_mapper_then_learned_profile(page, session, demo, resume):
    html = """<form><label for="fn">First name *</label><input id="fn" name="fn" required>
    <label for="ln">Last name *</label><input id="ln" name="ln" required>
    <label for="em">Email *</label><input id="em" name="em" type="email" required>
    <label for="cv">Resume</label><input id="cv" type="file">
    <label for="np">What is your notice period?</label><input id="np" name="np">
    <label for="nl">Newsletter</label><input id="nl" type="checkbox" name="newsletter">
    <button type="submit">Send</button></form>"""
    llm = FakeLLM()
    runner = FormRunner(
        session, vault(resume), answers=AnswerSource(session, llm, demo), llm=llm, user=demo, profiles_dir=PROFILES
    )
    r1 = runner.run(page, url="https://careers.example.com/apply/123", html=html)
    assert r1.profile_origin == "ai" and r1.paused_reason == "", r1.summary()
    assert page.locator("#fn").input_value() == "Demo" and page.locator("#np").input_value() == "30 days"
    assert page.locator("#nl").is_checked() is False
    row = session.exec(select(FormProfile).where(FormProfile.domain == "careers.example.com")).one()
    assert row.fields["#fn"] == "profile.first_name" and "newsletter" not in str(row.fields)
    assert not any("demo" in str(v).lower() for v in row.fields.values())  # no personal data stored
    map_calls = len([c for c in llm.calls if c.task == "form_map"])
    r2 = runner.run(page, url="https://careers.example.com/apply/456", html=html)
    assert r2.profile_origin == "learned" and r2.paused_reason == ""
    assert len([c for c in llm.calls if c.task == "form_map"]) == map_calls
    session.refresh(row)
    assert row.success_count == 1


def test_answers_go_to_bank_for_review(page, session, demo, resume):
    html = """<form><label for="em">Email *</label><input id="em" name="email" type="email" required>
    <label for="q">Why do you want to join us? *</label><textarea id="q" name="q" required></textarea></form>"""
    runner = FormRunner(
        session,
        vault(resume),
        answers=AnswerSource(session, FakeLLM(), demo),
        llm=FakeLLM(),
        user=demo,
        profiles_dir=PROFILES,
    )
    result = runner.run(page, url="https://x.example.com/apply", html=html)
    assert page.locator("#q").input_value()
    assert result.needs_human and "low confidence" in result.needs_human[0]
    bank = session.exec(select(AnswerBank).where(AnswerBank.user_id == demo.id)).all()
    assert any("join us" in b.question for b in bank) and all(not b.approved for b in bank)


def test_extract_fields_marks_hidden_and_groups(page):
    page.set_content("""<form><fieldset><legend>Do you consent?</legend>
      <label><input type="radio" name="c" value="yes"> Yes</label><label><input type="radio" name="c" value="no"> No</label></fieldset>  # noqa: E501
      <input type="text" name="honey" style="display:none"><select name="s"><option>One</option><option>Two</option></select></form>""")  # noqa: E501
    fields = extract_fields(page)
    radios = [f for f in fields if f.type == "radio"]
    assert radios[0].group == "Do you consent?" and radios[0].label == "Yes"
    assert next(f for f in fields if f.name == "honey").hidden
    assert next(f for f in fields if f.type == "select").options == ["One", "Two"]


# ------------------------------------------------------------------ apply pipeline


def _app(session, user, status=AppStatus.approved, ats="greenhouse", score=90):
    job = Job(
        dedupe_key=f"k{status}{ats}{score}",
        title="Engineer",
        company="Acme",
        ats_type=ats,
        apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
    )
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score, cover_letter="CL")
    session.add(app)
    session.commit()
    return app, job


def test_pick_applications_respects_sites(session, demo):
    a1, _ = _app(session, demo, AppStatus.approved)
    a2, _ = _app(session, demo, AppStatus.queued, score=80)
    _app(session, demo, AppStatus.queued, ats="linkedin")  # assisted site, disabled by default
    _app(session, demo, AppStatus.new)
    picked = apply_mod.pick_applications(session, demo, limit=10)
    assert [p[0].id for p in picked] == [a1.id, a2.id]


def test_run_one_status_transitions(session, demo, monkeypatch):
    sent = []
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: sent.append((kind, msg)))
    site = session.exec(select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        r = FillResult(url=url, profile_origin="builtin", screenshot_path="/shot.png")
        r.plan = []
        if scenario == "paused":
            r.paused_reason = "captcha on page"
        elif scenario == "submitted":
            assert submit is True
            r.submitted = True
        elif scenario == "nofields":
            r.error = "no form fields found on the page"
        return r

    monkeypatch.setattr(FormRunner, "run", fake_run)
    for scenario, expected in [
        ("dry", AppStatus.dry_run),
        ("paused", AppStatus.paused),
        ("nofields", AppStatus.failed),
    ]:
        app, job = _app(session, demo, score=50 + len(scenario))
        out = apply_mod.run_one(session, demo, app, job, site, page=None, llm=None, dry_run=True)
        assert out.status == expected, scenario
    assert sent and sent[0][0] == "paused"
    site.auto_submit = True
    session.add(site)
    scenario = "submitted"
    app, job = _app(session, demo, score=99)
    out = apply_mod.run_one(session, demo, app, job, site, page=None, llm=None, dry_run=False)
    assert out.status == AppStatus.applied and app.submitted_at is not None and app.follow_up_at is not None


def test_apply_user_kill_switch_and_dry_run_default(session, demo, monkeypatch):
    from jobapply.switches import set_value

    set_value(session, "kill_switch", "on")
    session.commit()
    assert apply_mod.apply_user(session, demo, None, browser=None) == []
    set_value(session, "kill_switch", "off")
    session.commit()

    class Page:
        def close(self):
            pass

    class Ctx:
        def new_page(self):
            return Page()

        def close(self):
            pass

    class Browser:
        def new_context(self, **kw):
            return Ctx()

    seen = {}

    def fake_run_one(session_, user, app, job, site, page, llm, dry_run):
        seen["dry_run"] = dry_run
        return apply_mod.ApplyOutcome(app.id, AppStatus.dry_run, "ok")

    monkeypatch.setattr(apply_mod, "run_one", fake_run_one)
    _app(session, demo)
    out = apply_mod.apply_user(session, demo, None, Browser(), dry_run=False)
    assert len(out) == 1 and seen["dry_run"] is True  # global dry-run switch is on by default


def test_vault_view_for_falls_back_to_uploaded_resume(session, demo):
    demo.profile = {**demo.profile, "resumes": [{"name": "a.pdf", "path": "/up/a.pdf"}]}
    app = Application(user_id=demo.id, job_id=1, cover_letter="x")
    v = vault_view_for(demo, app)
    assert v.resume_path == "/up/a.pdf" and v.cover_letter == "x"
    app.resume_variant = "/var/v.pdf"
    assert vault_view_for(demo, app).resume_path == "/var/v.pdf"
