"""Naukri apply: the real runner in headless Chromium against inline job-page / chatbot / profile fixtures,
the pure apply-control classifier, the question map, and the gated per-user orchestrator (stubbed browser)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from jobapply.forms.profiles import VaultView
from jobapply.models import Application, AppStatus, Job, UserSite
from jobapply.repo import UserScope
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.naukri import apply as na
from jobapply.sites.naukri import session as ns
from jobapply.sites.naukri.apply import (
    ApplyResult,
    NaukriApplyRunner,
    apply_job,
    apply_naukri_user,
    classify_apply_control,
    key_for_question,
    load_question_map,
    pick_naukri_applications,
    refresh_profile,
)
from jobapply.switches import set_value

NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(30)  # noqa: E731

# ------------------------------------------------------------------ inline fixtures

JOB_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Senior Python Developer - Acme | Naukri.com</title>
<style>.hidden{display:none}
.chatbot_Drawer{position:fixed;right:0;top:0;width:360px;background:#fff;border:1px solid #999;padding:12px}</style>
</head><body>
<header><div class="nI-gNb-drawer__icon"><img class="nI-gNb-icon-img" alt="me"></div></header>
<main>
  <h1 class="styles_jd-header-title__rZwM1">Senior Python Developer</h1>
  <div class="styles_jd-header-comp-name__MMX5Y">Acme · Bengaluru</div>
  <div id="state" data-clicked="0" data-answers="[]" data-finished="0"></div>
  <div class="styles_jhc__apply-button-container__5Bqnb">
    <!--CONTROL-->
    <button id="apply-button" class="styles_apply-button__uJI3x">Apply</button>
    <!--/CONTROL-->
  </div>
  <div class="styles_JDC__dang-inner-html__h0K4t">Backend engineer: Python, FastAPI, Postgres. Security check on every
  login is part of the SOC duties; verify you are human is not. Unusual activity detection experience welcome.</div>
</main>
<div class="chatbot_Drawer chatbot_right hidden" id="drawer">
  <div class="chatbot_MessageContainer" id="msgs"></div>
  <div id="options"></div>
  <div class="textArea hidden" contenteditable="true" id="userInput__abc"></div>
  <div class="sendMsg" id="sendMsg__abc">Send</div>
</div>
<script>
const QUESTIONS = /*QUESTIONS*/[
  {q: "What is your notice period?", type: "text"},
  {q: "Are you willing to relocate to Bengaluru?", type: "radio", options: ["Yes", "No"]},
  {q: "How many years of experience do you have with Python?", type: "text"}
]/*/QUESTIONS*/;
const state = document.getElementById('state');
const drawer = document.getElementById('drawer');
const msgs = document.getElementById('msgs');
const opts = document.getElementById('options');
const input = document.getElementById('userInput__abc');
let idx = -1; const answers = [];
function render() {
  const q = QUESTIONS[idx];
  const m = document.createElement('div'); m.className = 'botMsg msg'; m.innerHTML = '<span>' + q.q + '</span>';
  msgs.appendChild(m);
  opts.innerHTML = '';
  if (q.type === 'radio') {
    input.classList.add('hidden');
    const c = document.createElement('div'); c.className = 'singleselect-radiobutton-container';
    q.options.forEach((o, i) => {
      const w = document.createElement('div'); w.className = 'ssrc__radio-btn-container';
      w.innerHTML = '<input type="radio" id="r' + idx + '_' + i + '" name="q' + idx + '" value="' + o + '">' +
                    '<label for="r' + idx + '_' + i + '">' + o + '</label>';
      c.appendChild(w);
    });
    opts.appendChild(c);
  } else {
    input.classList.remove('hidden'); input.textContent = '';
  }
}
function finish() {
  drawer.classList.add('hidden');
  const b = document.getElementById('apply-button');
  if (b) { b.id = 'already-applied'; b.className = 'styles_already-applied__4KDhw'; b.textContent = 'Applied'; }
  const d = document.createElement('div'); d.className = 'styles_apply-message__x1';
  d.textContent = 'You have successfully applied to this job'; document.body.appendChild(d);
  state.dataset.finished = '1';
}
const applyBtn = document.getElementById('apply-button');
if (applyBtn && !applyBtn.dataset.noop) applyBtn.addEventListener('click', () => {
  state.dataset.clicked = String(Number(state.dataset.clicked) + 1);
  if (!QUESTIONS.length) { finish(); return; }
  drawer.classList.remove('hidden'); idx = 0; render();
});
document.getElementById('sendMsg__abc').addEventListener('click', () => {
  const q = QUESTIONS[idx]; let a = '';
  if (q.type === 'radio') { const r = opts.querySelector('input:checked'); if (!r) return; a = r.value; }
  else { a = input.textContent.trim(); if (!a) return; }
  answers.push([q.q, a]); state.dataset.answers = JSON.stringify(answers);
  const u = document.createElement('div'); u.className = 'userMsg msg'; u.textContent = a; msgs.appendChild(u);
  idx += 1;
  if (idx >= QUESTIONS.length) finish(); else render();
});
</script>
</body></html>
"""

PROFILE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>My Naukri profile</title><style>.hidden{display:none}</style></head><body>
<header><div class="nI-gNb-drawer__icon"></div></header>
<div id="state" data-saved="0"></div>
<div class="resumeHeadline" id="lazyResumeHead">
  <span class="widgetTitle">Resume headline</span><span class="edit icon" id="edit-headline">edit</span>
  <p id="headline-text">Senior Python developer, 6 years</p>
  <form id="headline-form" class="hidden" onsubmit="return false;">
    <textarea id="resumeHeadlineTxt" name="resumeHeadline">Senior Python developer, 6 years</textarea>
    <button type="submit" class="btn-dark-ot" id="save-headline">Save</button>
  </form>
</div>
<input id="attachCV" type="file" accept=".doc,.docx,.pdf" class="hidden">
<script>
document.getElementById('edit-headline').onclick =
  () => document.getElementById('headline-form').classList.remove('hidden');
document.getElementById('save-headline').onclick = () => {
  document.getElementById('headline-form').classList.add('hidden');
  document.getElementById('state').dataset.saved = '1';
  const t = document.createElement('div'); t.className = 'toast-msg'; t.textContent = 'Details saved';
  document.body.appendChild(t);
};
</script></body></html>
"""


def _write(tmp_path: Path, name: str, html: str) -> str:
    p = tmp_path / f"{name}.html"
    p.write_text(html, encoding="utf-8")
    return p.resolve().as_uri()


def job_page(tmp_path: Path, name: str = "job", control: str | None = None, questions: list[dict] | None = None,
             extra: str = "", logged_in: bool = True) -> str:
    html = JOB_PAGE
    if control is not None:
        start, end = html.index("<!--CONTROL-->"), html.index("<!--/CONTROL-->")
        html = html[:start] + control + html[end:]
    if questions is not None:
        start, end = html.index("/*QUESTIONS*/") + len("/*QUESTIONS*/"), html.index("/*/QUESTIONS*/")
        html = html[:start] + json.dumps(questions) + html[end:]
    if not logged_in:
        html = html.replace('<div class="nI-gNb-drawer__icon"><img class="nI-gNb-icon-img" alt="me"></div>',
                            '<a id="login_Layer" href="https://www.naukri.com/nlogin/login">Login</a>')
    html = html.replace("</main>", extra + "</main>")
    return _write(tmp_path, name, html)


def state(page) -> dict[str, str]:
    return page.evaluate(
        "() => Object.fromEntries(Array.from(document.getElementById('state').attributes)"
        ".filter(a => a.name.startsWith('data-')).map(a => [a.name.slice(5), a.value]))"
    )


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
        return "I would be glad to discuss this.", 30


def make_runner(answers=None, profile=None, **kw) -> NaukriApplyRunner:
    vault = VaultView(profile=dict(profile or DEMO_PROFILE), resume_path=None)
    return NaukriApplyRunner(vault, answers, qmap=load_question_map(), pause=NO_PAUSE, **kw)


# ------------------------------------------------------------------ pure units


def test_classify_apply_control():
    c = classify_apply_control
    assert c('<button id="apply-button" class="styles_apply-button__x">Apply</button>') == ("apply", None)
    assert c('<button id="apply-button">Login to apply</button>') == ("login", None)
    assert c('<button id="already-applied" class="styles_already-applied__x">Applied</button>') == ("applied", None)
    assert c('<button id="apply-button">Applied</button>') == ("applied", None)
    assert c('<a id="company-site-button" class="styles_company-site-button__C" '
             'href="https://boards.greenhouse.io/acme/jobs/1">Apply on company site</a>'
             ) == ("external", "https://boards.greenhouse.io/acme/jobs/1")
    assert c('<button id="company-site-button">Apply on company site</button>') == ("external", None)
    assert c('<button id="apply-button">Apply on company site</button>') == ("external", None)
    assert classify_apply_control('<a id="login_Layer">Login</a><p>Register to apply</p>') == ("login", None)
    assert classify_apply_control("<div>nothing here</div>") == ("none", None)


def test_question_map_and_label_rules(tmp_path):
    qmap = load_question_map()
    assert qmap.keys and na.QUESTIONS_FILE.exists()
    assert key_for_question("What is your notice period?", qmap.keys) == "profile.notice_period"
    assert key_for_question("What is your current CTC (in LPA)?", qmap.keys) == "profile.current_ctc"
    assert key_for_question("Are you willing to relocate to Pune?", qmap.keys) == "derived.willing_to_relocate"
    assert key_for_question("Total experience in years?", qmap.keys) == "profile.years_experience"
    assert key_for_question("Email address", qmap.keys) == "profile.email"  # shared LinkedIn label rules
    assert key_for_question("Why do you want to join us?", qmap.keys) is None
    custom = tmp_path / "q.yaml"
    custom.write_text(yaml.safe_dump({"questions": {"team size": "const:12", "x": "skip"},
                                      "selectors": {"apply_button": "#go", "bad": 3}}), encoding="utf-8")
    q = load_question_map(custom)
    assert [k for _, k in q.keys] == ["const:12"] and q.selectors == {"apply_button": "#go"}
    assert load_question_map(tmp_path / "missing.yaml").keys == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("questions: [unclosed", encoding="utf-8")
    assert load_question_map(broken).keys == []


def test_question_answerer_prefers_vault_then_answer_source():
    stub = StubAnswers({"team": ("Payments", 90), "animal": ("Cat", 40)})
    qa = na.QuestionAnswerer(VaultView(profile=dict(DEMO_PROFILE)), stub, load_question_map())
    assert qa.answer("What is your notice period?") == ("30 days", "builtin", 100)
    assert qa.answer("How many years of experience with FastAPI?") == ("3", "builtin", 100)
    assert qa.answer("Are you willing to relocate?", ["Yes", "No"]) == ("Yes", "builtin", 100)
    assert qa.answer("Which team?", ["Payments", "Risk"]) == ("Payments", "ai", 90)
    assert qa.answer("Favourite animal?") == (None, "none", 0)  # below the confidence floor
    assert qa.answer("Which colour?", ["Red", "Blue"]) == (None, "none", 0)  # Red at 25: not confident
    assert not [q for q, _ in stub.asked if "notice" in q]  # vault answers never hit the answer source
    assert na.QuestionAnswerer(VaultView(profile={}), None).answer("Anything?") == (None, "none", 0)


def test_apply_result_maps_onto_fill_result():
    r = ApplyResult(url="u", status="applied", reason="applied on Naukri", screenshot="s.png")
    f = r.to_fill_result()
    assert f.submitted and f.screenshot_path == "s.png" and f.profile_origin == na.PROFILE_ORIGIN
    assert ApplyResult(url="u", status="dry_run").to_fill_result().paused_reason == ""
    assert ApplyResult(url="u", status="needs_human", reason="q").to_fill_result().paused_reason == "q"
    assert ApplyResult(url="u", status="paused", reason="captcha").to_fill_result().paused_reason == "captcha"
    assert ApplyResult(url="u", status="skipped", reason="already").to_fill_result().paused_reason == "already"
    assert ApplyResult(url="u", status="external").to_fill_result().paused_reason == "apply on company site"
    assert ApplyResult(url="u", status="failed", reason="boom").to_fill_result().error == "boom"
    assert set(ApplyResult(url="u").as_dict()) >= {"status", "screenshot", "reason", "apply_url", "ats_type"}


# ------------------------------------------------------------------ runner on fixtures (headless Chromium)


def test_dry_run_never_clicks_apply(page, tmp_path):
    runner = make_runner(StubAnswers())
    shot = tmp_path / "shots" / "nk.png"
    result = runner.run(page, job_page(tmp_path), submit=False, screenshot_path=shot, dry_reason="global dry run")
    assert result.status == "dry_run" and result.reason == "global dry run: Apply not clicked"
    assert result.clicked_apply is False and result.submitted is False
    assert shot.exists() and result.screenshot == str(shot)
    assert state(page)["clicked"] == "0"


def test_submit_answers_the_chatbot_and_confirms(page, tmp_path):
    stub = StubAnswers()
    runner = make_runner(stub)
    shot = tmp_path / "shots" / "applied.png"
    result = runner.run(page, job_page(tmp_path), submit=True, screenshot_path=shot)
    assert result.status == "applied" and result.submitted and result.clicked_apply, result.reason
    st = state(page)
    assert st["clicked"] == "1" and st["finished"] == "1"
    assert json.loads(st["answers"]) == [
        ["What is your notice period?", "30 days"],
        ["Are you willing to relocate to Bengaluru?", "Yes"],
        ["How many years of experience do you have with Python?", "6"],
    ]
    assert result.answers["What is your notice period?"] == "30 days"
    assert [q["source"] for q in result.questions] == ["builtin", "builtin", "builtin"]
    assert not stub.asked and shot.exists()


def test_submit_without_a_chatbot_is_applied(page, tmp_path):
    result = make_runner().run(page, job_page(tmp_path, "noq", questions=[]), submit=True)
    assert result.status == "applied" and state(page)["finished"] == "1"


def test_unanswerable_question_pauses_for_a_human(page, tmp_path):
    questions = [
        {"q": "What is your notice period?", "type": "text"},
        {"q": "Which team are you applying for?", "type": "radio", "options": ["Payments", "Risk"]},
        {"q": "Expected CTC?", "type": "text"},
    ]
    stub = StubAnswers()  # answers "Payments" at 25: below the floor, never guessed
    shot = tmp_path / "shots" / "human.png"
    result = make_runner(stub).run(page, job_page(tmp_path, "team", questions=questions), submit=True,
                                   screenshot_path=shot)
    assert result.status == "needs_human" and result.clicked_apply
    assert result.needs_human == ["Which team are you applying for?"]
    assert "Which team" in result.reason and "finish the chat by hand" in result.reason
    assert json.loads(state(page)["answers"]) == [["What is your notice period?", "30 days"]]
    assert state(page)["finished"] == "0" and shot.exists()
    assert stub.asked == [("Which team are you applying for?", ["Payments", "Risk"])]
    assert result.to_fill_result().paused_reason.startswith("chatbot question needs you")


def test_ai_answer_is_used_when_confident(page, tmp_path):
    questions = [{"q": "Which team are you applying for?", "type": "radio", "options": ["Payments", "Risk"]},
                 {"q": "Tell us about yourself", "type": "text"}]
    stub = StubAnswers({"team": ("risk", 90), "about yourself": ("Backend engineer, six years of Python.", 80)})
    result = make_runner(stub).run(page, job_page(tmp_path, "ai", questions=questions), submit=True)
    assert result.status == "applied", result.reason
    assert json.loads(state(page)["answers"]) == [["Which team are you applying for?", "Risk"],
                                                  ["Tell us about yourself", "Backend engineer, six years of Python."]]
    assert [q["source"] for q in result.questions] == ["ai", "ai"]


def test_external_with_href_is_routed_not_clicked(page, tmp_path):
    control = ('<a id="company-site-button" class="styles_company-site-button__C_2YK" '
               'href="https://boards.greenhouse.io/acme/jobs/4001" target="_blank">Apply on company site</a>')
    result = make_runner().run(page, job_page(tmp_path, "ext", control=control), submit=True)
    assert result.status == "external" and result.clicked_apply is False
    assert result.apply_url == "https://boards.greenhouse.io/acme/jobs/4001" and result.ats_type == "greenhouse"
    assert state(page)["clicked"] == "0"


def test_external_button_without_href_is_not_clicked_in_dry_run(page, tmp_path):
    control = ('<button id="company-site-button" class="styles_company-site-button__C_2YK">'
               "Apply on company site</button>")
    result = make_runner().run(page, job_page(tmp_path, "ext2", control=control), submit=False)
    assert result.status == "external" and result.apply_url is None and result.ats_type is None
    assert "not clicked" in result.reason


def test_external_button_click_captures_the_popup_when_allowed(page, tmp_path):
    target = _write(tmp_path, "company", "<html><body><h1>Careers at Acme</h1></body></html>")
    control = (f'<button id="company-site-button" onclick="window.open(\'{target}\', \'_blank\')">'
               "Apply on company site</button>")
    result = make_runner().run(page, job_page(tmp_path, "ext3", control=control), submit=True)
    assert result.status == "external" and result.apply_url == target and result.ats_type is None
    assert len(page.context.pages) == 1  # the popup was closed again


def test_already_applied_is_skipped(page, tmp_path):
    control = '<button id="already-applied" class="styles_already-applied__4KDhw" data-noop="1">Applied</button>'
    result = make_runner().run(page, job_page(tmp_path, "done", control=control), submit=True)
    assert result.status == "skipped" and result.reason == "already applied on Naukri"


def test_logged_out_page_pauses_with_login_status(page, tmp_path):
    control = '<button id="apply-button" data-noop="1">Login to apply</button>'
    result = make_runner().run(page, job_page(tmp_path, "out", control=control, logged_in=False), submit=True)
    assert result.status == "paused" and result.login_status == "login" and result.reason == "naukri login required"
    assert state(page)["clicked"] == "0"


def test_captcha_pauses_before_and_after_the_click(page, tmp_path):
    captcha = '<div class="g-recaptcha" data-sitekey="x"></div>'
    result = make_runner().run(page, job_page(tmp_path, "cap", extra=captcha), submit=True)
    assert result.status == "paused" and result.login_status == "challenge" and state(page)["clicked"] == "0"
    # a captcha that appears inside the chatbot stops the questionnaire, nothing is retried
    questions = [{"q": "What is your notice period?", "type": "text"}]
    late = ('<script>document.getElementById("apply-button").addEventListener("click", () => {'
            'const d = document.createElement("div"); d.className = "g-recaptcha"; d.dataset.sitekey = "y";'
            'document.body.appendChild(d); });</script>')
    result = make_runner().run(page, job_page(tmp_path, "cap2", questions=questions, extra=late), submit=True)
    assert result.status == "paused" and result.login_status == "challenge" and result.clicked_apply
    assert json.loads(state(page)["answers"]) == []


def test_no_apply_control_fails(page, tmp_path):
    result = make_runner().run(page, job_page(tmp_path, "none", control="<p>This job has expired.</p>"), submit=True)
    assert result.status == "failed" and "no Apply control" in result.reason


def test_job_description_wording_is_not_a_challenge(page, tmp_path):
    result = make_runner().run(page, job_page(tmp_path, "jd"), submit=False)
    assert result.status == "dry_run"


def test_rate_limit_status_is_a_challenge(tmp_path):
    class Resp:
        status = 406

    class P:
        def goto(self, *a, **kw):
            return Resp()

    result = make_runner().run(P(), "https://www.naukri.com/job-listings-x-1", submit=True)
    assert result.status == "paused" and result.login_status == "challenge" and result.reason == "naukri answered 406"


# ------------------------------------------------------------------ apply_job (uniform entry point)


def _job_and_app(session, user, url: str) -> tuple[Job, Application]:
    job = Job(dedupe_key=f"naukri:{url[-12:]}", title="Senior Python Developer", company="Acme",
              url=url, apply_url=url, source="naukri", ats_type="naukri")
    session.add(job)
    session.flush()
    app = Application(user_id=user.id, job_id=job.id, status=AppStatus.approved, score=80)
    session.add(app)
    session.commit()
    return job, app


def test_apply_job_gates_on_dry_run_and_auto_submit(page, session, demo, tmp_path):
    url = job_page(tmp_path)
    job, app = _job_and_app(session, demo, url)
    nk = ns.NaukriSession(None, demo, session, page=page)
    shot = tmp_path / "s1.png"
    out = apply_job(nk, app, dry_run=True, auto_submit=True, answers=StubAnswers(), screenshot_path=shot,
                    pause=NO_PAUSE)
    assert out["status"] == "dry_run" and out["reason"].startswith("global dry run") and out["screenshot"] == str(shot)
    out = apply_job(nk, app, dry_run=False, auto_submit=False, answers=StubAnswers(),
                    screenshot_path=tmp_path / "s2.png", pause=NO_PAUSE)
    assert out["status"] == "dry_run" and out["reason"].startswith("auto_submit off")
    assert state(page)["clicked"] == "0"
    out = apply_job(nk, app, dry_run=False, auto_submit=True, answers=StubAnswers(),
                    screenshot_path=tmp_path / "s3.png", pause=NO_PAUSE)
    assert out["status"] == "applied" and state(page)["clicked"] == "1"
    assert set(out) >= {"status", "screenshot", "reason", "apply_url", "ats_type", "answers", "questions"}


def test_apply_job_default_screenshot_lives_under_data_screenshots(page, session, demo, tmp_path, monkeypatch):
    from jobapply.config import get_settings

    url = job_page(tmp_path)
    _, app = _job_and_app(session, demo, url)
    nk = ns.NaukriSession(None, demo, session, page=page)
    out = apply_job(nk, app, dry_run=True, auto_submit=False, answers=StubAnswers(), pause=NO_PAUSE)
    assert out["status"] == "dry_run"
    assert Path(out["screenshot"]).is_relative_to(get_settings().screenshots_dir / str(demo.id))
    assert Path(out["screenshot"]).exists()


# ------------------------------------------------------------------ refresh_profile


def _allow_real_runs(session, monkeypatch):
    """Both halves of the global dry-run switch off (env flag and admin toggle), as a real apply run needs."""
    from jobapply.config import get_settings

    monkeypatch.setenv("JOBAPPLY_DRY_RUN", "false")
    get_settings.cache_clear()
    set_value(session, "dry_run", "off")
    session.commit()


def test_refresh_profile_dry_run_only_navigates(page, session, tmp_path, monkeypatch):
    monkeypatch.setattr(na, "PROFILE_URL", _write(tmp_path, "profile", PROFILE_PAGE))
    shot = tmp_path / "p.png"
    out = refresh_profile(1, dry_run=True, session_db=session, page=page, screenshot_path=shot, pause=NO_PAUSE)
    assert out["status"] == "dry_run" and out["headline_saved"] is False and shot.exists()
    assert state(page)["saved"] == "0"


def test_refresh_profile_resaves_the_headline(page, session, tmp_path, monkeypatch):
    from jobapply.config import get_settings

    _allow_real_runs(session, monkeypatch)
    monkeypatch.setattr(na, "PROFILE_URL", _write(tmp_path, "profile", PROFILE_PAGE))
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    out = refresh_profile(1, dry_run=False, session_db=session, page=page, screenshot_path=tmp_path / "p2.png",
                          resume_path=str(resume), pause=NO_PAUSE)
    assert out["status"] == "refreshed" and out["headline_saved"] and out["resume_uploaded"]
    assert state(page)["saved"] == "1"
    headline = page.evaluate("() => document.getElementById('resumeHeadlineTxt').value")
    assert headline == "Senior Python developer, 6 years"
    get_settings.cache_clear()


def test_refresh_profile_logged_out_pauses(page, session, tmp_path, monkeypatch):
    from jobapply.config import get_settings

    _allow_real_runs(session, monkeypatch)
    html = PROFILE_PAGE.replace('<div class="nI-gNb-drawer__icon"></div>', '<a id="login_Layer">Login</a>')
    monkeypatch.setattr(na, "PROFILE_URL", _write(tmp_path, "profile_out", html))
    out = refresh_profile(1, dry_run=False, session_db=session, page=page, screenshot_path=tmp_path / "p3.png",
                          pause=NO_PAUSE)
    assert out["status"] == "paused" and out["login_status"] == "login" and state(page)["saved"] == "0"
    get_settings.cache_clear()


def test_refresh_profile_honours_kill_switch_and_global_dry_run(page, session, tmp_path, monkeypatch):
    """The only account-mutating path outside an apply run sits behind the same switches."""
    from jobapply.config import get_settings

    monkeypatch.setattr(na, "PROFILE_URL", _write(tmp_path, "profile", PROFILE_PAGE))
    # global dry-run switch on (the default): a --refresh run is downgraded to a dry run, nothing is clicked
    out = refresh_profile(1, dry_run=False, session_db=session, page=page, screenshot_path=tmp_path / "p4.png",
                          pause=NO_PAUSE)
    assert out["status"] == "dry_run" and out["headline_saved"] is False and state(page)["saved"] == "0"
    # kill switch on: skipped before the profile page is even opened
    _allow_real_runs(session, monkeypatch)
    set_value(session, "kill_switch", "on")
    session.commit()
    class NeverOpened:
        def goto(self, url, **kw):
            raise AssertionError("the profile page must not be opened while the kill switch is on")

    out = refresh_profile(1, dry_run=False, session_db=session, page=NeverOpened(),
                          screenshot_path=tmp_path / "p5.png", pause=NO_PAUSE)
    assert out["status"] == "skipped" and out["reason"] == "kill switch on"
    assert out["headline_saved"] is False and out["resume_uploaded"] is False
    get_settings.cache_clear()


# ------------------------------------------------------------------ orchestrator (browser stubbed)


class FakeHomePage:
    """Enough of a page for NaukriSession.check(): lands on the homepage with the header rendered."""

    def __init__(self, status: str = "ok"):
        self.status = status
        self.url = "about:blank"

    def goto(self, url, **kw):
        self.url = url if self.status != "login" else ns.LOGIN_URL
        return None

    def locator(self, selector):
        n = 1 if (self.status == "ok" and selector in ns.LOGGED_IN_SELECTORS) else 0

        class L:
            def count(self_inner):
                return n

        return L()

    def content(self):
        return ""

    def evaluate(self, js):
        return ""

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture
def naukri_site(session, demo) -> UserSite:
    site = UserScope(session, demo).site("naukri")
    site.enabled = True
    session.add(site)
    session.commit()
    return site


def _stub_apply(monkeypatch, results: list[dict]):
    calls: list[dict] = []

    def fake(nk, app, *, dry_run, auto_submit, **kw):
        calls.append({"app": app.id, "dry_run": dry_run, "auto_submit": auto_submit})
        r = results.pop(0)
        return {"status": "dry_run", "screenshot": None, "reason": "", "apply_url": None, "ats_type": None,
                "answers": {}, "questions": [], "needs_human": [], "clicked_apply": False, "submitted": False,
                "login_status": None, "url": "u", **r}

    monkeypatch.setattr(na, "apply_job", fake)
    return calls


def test_pick_naukri_applications(session, demo):
    j1, a1 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    j2, a2 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-b-291025500002")
    a2.status = AppStatus.queued
    j3 = Job(dedupe_key="gh", title="x", company="y", url="https://boards.greenhouse.io/a/jobs/1",
             apply_url="https://boards.greenhouse.io/a/jobs/1", source="greenhouse", ats_type="greenhouse")
    session.add(j3)
    session.flush()
    session.add(Application(user_id=demo.id, job_id=j3.id, status=AppStatus.approved))
    session.commit()
    picks = pick_naukri_applications(session, demo, 10)
    assert [a.id for a, _ in picks] == [a1.id, a2.id]
    assert len(pick_naukri_applications(session, demo, 1)) == 1


def test_orchestrator_kill_switch_and_disabled_site(session, demo, monkeypatch):
    _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    calls = _stub_apply(monkeypatch, [])
    assert apply_naukri_user(None, session, demo, page=FakeHomePage()) == []  # site disabled by default
    site = UserScope(session, demo).site("naukri")
    site.enabled = True
    session.commit()
    set_value(session, "kill_switch", "on")
    session.commit()
    assert apply_naukri_user(None, session, demo, page=FakeHomePage()) == []
    assert calls == []


def test_orchestrator_global_dry_run_wins_and_auto_submit_is_passed(session, demo, naukri_site, monkeypatch):
    _, app = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    calls = _stub_apply(monkeypatch, [{"status": "dry_run", "reason": "global dry run: Apply not clicked"}])
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), dry_run=False, between=lambda p: None)
    assert calls == [{"app": app.id, "dry_run": True, "auto_submit": False}]  # admin dry_run switch defaults to on
    assert [o.status for o in outcomes] == [AppStatus.dry_run]
    assert session.get(Application, app.id).status == AppStatus.dry_run


def test_orchestrator_real_submit_needs_every_switch_off(session, demo, naukri_site, monkeypatch):
    from jobapply.config import get_settings

    _, app = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    naukri_site.auto_submit = True
    session.add(naukri_site)
    set_value(session, "dry_run", "off")
    session.commit()
    monkeypatch.setattr(get_settings(), "dry_run", False)
    calls = _stub_apply(monkeypatch, [{"status": "applied", "reason": "applied on Naukri", "submitted": True}])
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), dry_run=False, between=lambda p: None)
    assert calls == [{"app": app.id, "dry_run": False, "auto_submit": True}]
    assert [o.status for o in outcomes] == [AppStatus.applied]
    a = session.get(Application, app.id)
    assert a.status == AppStatus.applied and a.submitted_at is not None


def test_orchestrator_session_problems_stop_the_run(session, demo, naukri_site, monkeypatch):
    _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    calls = _stub_apply(monkeypatch, [])
    assert apply_naukri_user(None, session, demo, page=FakeHomePage("login")) == []
    assert naukri_site.login_status == "login" and calls == []
    assert apply_naukri_user(None, session, demo, page=FakeHomePage("unknown")) == []
    # a cooldown after a challenge keeps the run away entirely (no session check)
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    demo.preferences = {**(demo.preferences or {}), ns.COOLDOWN_KEY: until}
    session.add(demo)
    session.commit()
    assert apply_naukri_user(None, session, demo, page=FakeHomePage()) == []


def test_orchestrator_challenge_mid_run_records_cooldown_and_stops(session, demo, naukri_site, monkeypatch):
    _, a1 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    _job_and_app(session, demo, "https://www.naukri.com/job-listings-b-291025500002")
    calls = _stub_apply(monkeypatch, [{"status": "paused", "reason": "captcha on page", "login_status": "challenge"},
                                      {"status": "applied"}])
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), limit=5, between=lambda p: None)
    assert len(outcomes) == 1 and len(calls) == 1 and outcomes[0].status == AppStatus.paused
    assert naukri_site.login_status == "challenge" and ns.cooldown_until(demo) > datetime.now(UTC)


def test_orchestrator_routes_external_jobs_to_the_ats_filler(session, demo, naukri_site, monkeypatch):
    job, app = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    job2, app2 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-b-291025500002")
    job3, app3 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-c-291025500003")
    by_id = {app.id: {"status": "external", "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                      "ats_type": "greenhouse", "reason": "apply on company site"},
             app2.id: {"status": "external", "apply_url": "https://careers.acme.example/apply", "ats_type": None},
             app3.id: {"status": "external", "apply_url": None, "ats_type": None, "reason": "url only on click"}}
    calls: list[int] = []

    def fake(nk, a, **kw):
        calls.append(a.id)
        return {"screenshot": None, "answers": {}, "questions": [], "needs_human": [], "clicked_apply": False,
                "submitted": False, "login_status": None, "url": "u", "reason": "", **by_id[a.id]}

    monkeypatch.setattr(na, "apply_job", fake)
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), limit=5, between=lambda p: None)
    assert sorted(calls) == [app.id, app2.id, app3.id]
    session.refresh(job)
    assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/1" and job.ats_type == "greenhouse"
    assert session.get(Application, app.id).status == AppStatus.approved  # left for the ATS filler
    session.refresh(job2)
    assert job2.apply_url == "https://careers.acme.example/apply" and job2.ats_type is None
    assert session.get(Application, app2.id).status == AppStatus.paused
    assert session.get(Application, app3.id).status == AppStatus.paused
    assert {o.app_id for o in outcomes} == {app2.id, app3.id}


def test_orchestrator_needs_human_is_paused(session, demo, naukri_site, monkeypatch):
    _, app = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    _stub_apply(monkeypatch, [{"status": "needs_human", "reason": "chatbot question needs you: Which team?",
                               "needs_human": ["Which team?"], "clicked_apply": True}])
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), between=lambda p: None)
    assert outcomes[0].status == AppStatus.paused
    a = session.get(Application, app.id)
    assert a.status == AppStatus.paused and "Which team" in (a.error or "")


def test_orchestrator_daily_caps(session, demo, naukri_site, monkeypatch):
    from jobapply.config import get_settings

    _, a1 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-a-291025500001")
    _, a2 = _job_and_app(session, demo, "https://www.naukri.com/job-listings-b-291025500002")
    naukri_site.auto_submit = True
    naukri_site.daily_cap = 1
    session.add(naukri_site)
    set_value(session, "dry_run", "off")
    session.commit()
    monkeypatch.setattr(get_settings(), "dry_run", False)
    calls = _stub_apply(monkeypatch, [{"status": "applied", "submitted": True},
                                      {"status": "applied", "submitted": True}])
    outcomes = apply_naukri_user(None, session, demo, page=FakeHomePage(), limit=5, dry_run=False,
                                 between=lambda p: None)
    assert len(outcomes) == 1 and len(calls) == 1 and outcomes[0].status == AppStatus.applied
    # the next run stops before any browser work because the site cap is reached
    assert apply_naukri_user(None, session, demo, page=FakeHomePage(), limit=5, dry_run=False,
                             between=lambda p: None) == []


def test_active_hours_gate(monkeypatch):
    from jobapply.config import get_settings

    monkeypatch.setattr(get_settings(), "linkedin_active_hours", "00:00-00:01")
    import jobapply.sites.browser as b

    monkeypatch.setattr(b, "is_active_hours", lambda **kw: False)
    assert na.apply_naukri() == []
