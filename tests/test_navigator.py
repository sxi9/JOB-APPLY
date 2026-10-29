"""Universal external-link navigator (forms/navigator.py) on inline HTML fixtures served through page.route.

No network: every URL under http://ext.test/ is answered from the dict handed to `_route`. The pages use plain
JS for client-side wizards and redirects, so the navigator sees the same DOM behaviours as a real site."""

from __future__ import annotations

import email as email_lib
import imaplib
from datetime import datetime, timedelta
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
from sqlmodel import select

from jobapply.ai.fake import FakeLLM
from jobapply.config import get_settings
from jobapply.forms.navigator import (
    MAILBOX_LOGIN_FAILED,
    STATUS_ACCOUNT_WALL,
    STATUS_FORM,
    STATUS_MAILTO,
    STATUS_NO_FORM,
    Candidate,
    Navigator,
    NavPick,
    forget_external_account,
    generate_password,
    score_candidate,
)
from jobapply.forms.runner import AnswerSource, FormRunner
from jobapply.mail.imap import MailboxConfig
from jobapply.models import Credential, utcnow
from jobapply.seed import DEMO_PROFILE
from jobapply.vault import Vault, generate_key
from tests.test_forms import PROFILES, vault

BASE = "http://ext.test"

# show(id, el): reveal one block and hide the control that was clicked; go(path): same-tab navigation
JS = ("<script>function show(id,el){document.getElementById(id).style.display='block';"
      "if(el)el.style.display='none'}function go(p){location.href=p}</script>")

FORM_HTML = f"""<html><body>{JS}<h1>Backend Engineer - application</h1><form id="application">
<label for="fn">First name *</label><input id="fn" name="fn" required>
<label for="ln">Last name *</label><input id="ln" name="ln" required>
<label for="em">Email *</label><input id="em" name="em" type="email" required>
<label for="np">What is your notice period?</label><input id="np" name="np">
<button type="button" id="final" onclick="show('done')">Submit application</button>
</form><div id="done" style="display:none">Thanks for applying</div></body></html>"""


def _route(page, pages: dict[str, str]) -> None:
    def handler(route, request):
        path = request.url.split("ext.test", 1)[1].split("?")[0] or "/"
        body = pages.get(path)
        if body is None:
            route.fulfill(status=404, body="not here", content_type="text/html")
        else:
            route.fulfill(status=200, body=body, content_type="text/html; charset=utf-8")

    page.context.route(f"{BASE}/**", handler)  # context-wide so a popup tab is served too


def _runner(session, demo, resume, llm=None, **kw) -> FormRunner:
    llm = llm if llm is not None else FakeLLM()
    return FormRunner(session, vault(resume), answers=AnswerSource(session, llm, demo), llm=llm, user=demo,
                      profiles_dir=PROFILES, **kw)


def _filled(page) -> bool:
    return page.locator("#fn").input_value() == "Demo" and page.locator("#em").input_value() == DEMO_PROFILE["email"]


# ------------------------------------------------------------------ fake mailbox


class FakeImap:
    """The imaplib subset ImapReader uses, over an in-memory mailbox."""

    def __init__(self, messages: list[bytes]):
        self.messages = {str(i + 1): m for i, m in enumerate(messages)}

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, folder):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [" ".join(self.messages).encode()]

    def fetch(self, uid, what):
        if uid not in self.messages:
            return "NO", [None]
        return "OK", [(f"{uid} (RFC822 {{{len(self.messages[uid])}}}".encode(), self.messages[uid]), b")"]

    def store(self, uid, op, flag):
        return "OK", []

    def logout(self):
        return "BYE", []


def _mail(sender: str, subject: str, body: str, when: datetime | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, DEMO_PROFILE["email"], subject
    msg["Date"] = format_datetime(when or utcnow())
    msg["Message-ID"] = f"<{abs(hash(subject + body))}@ext.test>"
    msg.set_content(body)
    return msg.as_bytes()


IMAP_CFG = MailboxConfig(host="imap.example.com", username=DEMO_PROFILE["email"], password="test-only")


def _mail_opts(messages: list[bytes]) -> dict:
    fake = FakeImap(messages)
    return {"imap_config": IMAP_CFG, "imap_factory": lambda cfg: fake, "otp_timeout": 1.0, "otp_poll": 0.1}


@pytest.fixture
def vault_key(demo, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ unit: scoring


def test_score_candidate_prefers_apply_and_excludes_final_controls():
    c = lambda text, **kw: Candidate(idx=0, tag=kw.pop("tag", "a"), text=text, **kw)  # noqa: E731
    assert score_candidate(c("Apply now", href="/apply")) > score_candidate(c("Continue"))
    assert score_candidate(c("Apply for this job")) >= 100
    assert score_candidate(c("Continue")) >= 50 > score_candidate(c("Learn more"))
    for final in ("Submit", "Submit application", "Send application", "Finish", "Confirm and submit"):
        assert score_candidate(c(final, tag="button", type="button")) == 0
    for noise in ("Share this job", "Save job", "Print", "Privacy policy", "Search jobs", "Back"):
        assert score_candidate(c(noise)) == 0
    # a form's own submit button saying "Apply" is the submit, never a link to the form
    assert score_candidate(c("Apply", tag="button", type="submit", in_form=True)) == 0
    assert score_candidate(c("Apply", tag="button", type="submit", in_form=True), allow_form_submit=True) > 0
    assert score_candidate(c("Sign in to apply")) == 0  # account links go through the wall handling
    assert len(generate_password()) >= 20 and generate_password() != generate_password()


# ------------------------------------------------------------------ landing page -> form


def test_apply_button_leads_to_form(page, session, demo, resume):
    landing = ("<html><body><h1>Backend Engineer</h1><p>Great role.</p>"
               "<a href='/share'>Share this job</a> <button type='button'>Save job</button>"
               "<button type='button' onclick=\"location.href='/apply'\">Apply now</button>"
               "<a href='/jobs'>View all jobs</a></body></html>")
    _route(page, {"/job": landing, "/apply": FORM_HTML, "/share": "<p>share</p>", "/jobs": "<p>jobs</p>"})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.url == f"{BASE}/apply" and page.url == f"{BASE}/apply"
    assert result.error == "" and result.paused_reason == "", result.summary()
    assert "Apply now" in result.hops and _filled(page)
    assert not result.submitted and not page.locator("#done").is_visible()


def test_apply_link_opening_a_popup_is_taken_over(page, session, demo, resume):
    landing = ("<html><body><h1>Role</h1>"
               "<button type='button' onclick=\"window.open('/apply', '_blank')\">Apply</button></body></html>")
    _route(page, {"/job": landing, "/apply": FORM_HTML})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert page.url == f"{BASE}/apply" and _filled(page), result.summary()
    assert len(page.context.pages) == 1  # the popup was closed


def test_cookie_banner_is_dismissed_before_navigating(page, session, demo, resume):
    landing = ("<html><body><div id='cookie-consent' style='position:fixed;inset:0;background:#fff;z-index:9'>"
               "We use cookies <button type='button' onclick=\"this.parentNode.style.display='none'\">Accept all"
               "</button></div><h1>Role</h1><a href='/apply' id='go'>Apply now</a></body></html>")
    _route(page, {"/job": landing, "/apply": FORM_HTML})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.hops.startswith("banner"), result.hops
    assert page.url == f"{BASE}/apply" and _filled(page)


def test_iframe_form_is_opened_directly(page, session, demo, resume):
    outer = ("<html><body><h1>Role</h1><p>Apply below.</p>"
             f"<iframe src='{BASE}/embed' style='width:800px;height:600px'></iframe></body></html>")
    _route(page, {"/job": outer, "/embed": FORM_HTML})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.url == f"{BASE}/embed" and result.hops.startswith("frame") and _filled(page)


def test_wizard_next_steps_are_walked_and_final_never_clicked(page, session, demo, resume):
    # Landing: "Start application" reveals step 1 (JS). Step 1 "Continue" (type=button) reveals the form fields;
    # the form's "Next" reveals step 3 with more fields and a "Submit application" button that is never clicked.
    wizard = f"""<html><body>{JS}<h1>Role</h1>
<button type="button" id="start" onclick="show('s1',this)">Start application</button>
<div id="s1" style="display:none"><p>Welcome. Have your resume ready.</p>
 <button type="button" onclick="show('s2',this)">Continue</button></div>
<div id="s2" style="display:none"><form onsubmit="event.preventDefault()">
 <label for="fn">First name *</label><input id="fn" name="fn" required>
 <label for="ln">Last name *</label><input id="ln" name="ln" required>
 <label for="em">Email *</label><input id="em" name="em" type="email" required>
 <button type="button" id="next" onclick="show('s3',this)">Next</button>
 <div id="s3" style="display:none">
  <label for="ph">Phone *</label><input id="ph" name="ph" type="tel" required>
  <label for="np">What is your notice period?</label><input id="np" name="np">
  <button type="button" id="final" onclick="show('done')">Submit application</button>
 </div></form></div><div id="done" style="display:none">Submitted</div></body></html>"""
    _route(page, {"/job": wizard})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.paused_reason == "" and result.error == "", result.summary()
    assert "Start application" in result.hops and "Continue" in result.hops and "wizard 'Next'" in result.hops
    assert _filled(page) and page.locator("#ph").input_value() == DEMO_PROFILE["phone"]
    assert page.locator("#np").input_value() == "30 days"
    assert not page.locator("#done").is_visible() and not result.submitted
    assert len([p for p in result.plan if p.done]) >= 5


def test_wizard_never_clicks_the_forms_own_apply_button(page, session, demo, resume):
    # A quick-apply form whose final control posts from a JS click handler (no native submit event, so the dry-run
    # submit guard cannot intercept it) and says "Apply now": the wizard walker must never click it.
    js_form = f"""<html><body>{JS}<h1>Role</h1>
<script>window.sent = 0; function sendApplication() {{ window.sent += 1; show('done'); }}</script>
<form id="application">
<label for="fn">First name *</label><input id="fn" name="fn" required>
<label for="ln">Last name *</label><input id="ln" name="ln" required>
<label for="em">Email *</label><input id="em" name="em" type="email" required>
<button type="button" id="final" onclick="sendApplication()">Apply now</button>
</form><div id="done" style="display:none">Sent</div></body></html>"""
    # Recruitee-style: the form's own type=submit says "Apply for this job". Only `_submit` may click it, once.
    native = f"""<html><body>{JS}<h1>Role</h1><script>window.sent = 0;</script>
<form id="application" onsubmit="event.preventDefault(); window.sent += 1; show('done')">
<label for="fn">First name *</label><input id="fn" name="fn">
<label for="ln">Last name *</label><input id="ln" name="ln">
<label for="em">Email *</label><input id="em" name="em" type="email">
<button type="submit" id="final">Apply for this job</button>
</form><div id="done" style="display:none">Thanks for applying</div></body></html>"""
    _route(page, {"/js": js_form, "/native": native})
    for path in ("/js", "/native"):
        page.goto(f"{BASE}{path}")
        assert Navigator(page).next_control() is None, path
        result = _runner(session, demo, resume).run(page, url=f"{BASE}{path}")  # dry run
        assert _filled(page) and result.paused_reason == "" and result.error == "" and not result.submitted, path
        assert page.evaluate("() => window.sent") == 0 and not page.locator("#done").is_visible(), path
    # real run on the native form: submitted exactly once, by `_submit`, and booked as submitted
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/native", submit=True)
    assert result.submitted and result.error == "", result.summary()
    assert page.evaluate("() => window.sent") == 1 and page.locator("#done").is_visible()


def test_no_form_after_hops_pauses_with_reason(page, session, demo, resume):
    pages = {"/job": "<html><body><a href='/a'>Apply</a></body></html>",
             "/a": "<html><body><p>Step</p><a href='/b'>Continue</a></body></html>",
             "/b": "<html><body><p>Nothing here but marketing.</p><a href='/about'>About us</a></body></html>",
             "/about": "<p>about</p>"}
    _route(page, pages)
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.paused_reason.startswith("no application form found after"), result.summary()
    assert result.plan == [] and result.error == "" and page.url == f"{BASE}/b"


def test_captcha_on_landing_pauses(page, session, demo, resume):
    landing = ("<html><body><h1>Role</h1><iframe src='https://www.google.com/recaptcha/api2/anchor' "
               "style='width:300px;height:80px'></iframe><a href='/apply'>Apply</a></body></html>")
    _route(page, {"/job": landing, "/apply": FORM_HTML})
    page.route("https://www.google.com/**", lambda route, req: route.fulfill(status=200, body="<p>c</p>"))
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.paused_reason.startswith("captcha on page") and page.url == f"{BASE}/job"


def test_dry_run_never_clicks_a_final_control(page, session, demo, resume):
    # The only visible control is final-looking: neither the heuristics nor the LLM list may offer it.
    landing = ("<html><body><h1>Role</h1><p>Ready?</p>"
               "<button type='button' id='final' onclick=\"document.getElementById('done').style.display='block'\">"
               "Submit application</button><div id='done' style='display:none'>Sent</div></body></html>")
    _route(page, {"/job": landing})
    llm = FakeLLM()
    llm.queue("navigate", NavPick(choice=1, reason="only option"))  # would pick #1 if anything were offered
    result = _runner(session, demo, resume, llm=llm).run(page, url=f"{BASE}/job")
    assert result.paused_reason.startswith("no apply control found"), result.summary()
    assert not page.locator("#done").is_visible()
    assert not [c for c in llm.calls if c.task == "navigate"]  # nothing safe to offer: the model was not asked


# ------------------------------------------------------------------ LLM pick from a numbered list


AMBIGUOUS = ("<html><body><h1>Ingénieur backend</h1><nav><a href='/about'>About</a> <a href='/team'>Team</a></nav>"
             "<a href='/jobs/8812/postuler' id='p'>Postuler</a><a href='/jobs'>Toutes les offres</a></body></html>")


def test_llm_picks_from_numbered_list_when_heuristics_fail(page, session, demo, resume):
    _route(page, {"/job": AMBIGUOUS, "/jobs/8812/postuler": FORM_HTML, "/about": "<p>a</p>", "/team": "<p>t</p>",
                  "/jobs": "<p>j</p>"})
    llm = FakeLLM()
    llm.queue("navigate", NavPick(choice=2, reason="postuler = apply"))
    result = _runner(session, demo, resume, llm=llm).run(page, url=f"{BASE}/job")
    assert page.url == f"{BASE}/jobs/8812/postuler" and _filled(page), result.summary()
    call = next(c for c in llm.calls if c.task == "navigate")
    # "About" is filtered out as navigation noise before the list is numbered; the rest is offered in order
    assert "1. [a] 'Team'" in call.prompt and "2. [a] 'Postuler' -> /jobs/8812/postuler" in call.prompt
    assert "About" not in call.prompt.split("Controls", 1)[1]
    assert call.context["candidates"][1].startswith("2.")


def test_llm_out_of_range_or_absent_means_heuristics_only(page, session, demo, resume):
    _route(page, {"/job": AMBIGUOUS, "/jobs/8812/postuler": FORM_HTML})
    llm = FakeLLM()
    llm.queue("navigate", NavPick(choice=9))  # not in the list: ignored
    result = _runner(session, demo, resume, llm=llm).run(page, url=f"{BASE}/job")
    assert result.paused_reason.startswith("no apply control found") and page.url == f"{BASE}/job"
    # FakeLLM without a canned answer has no handler for "navigate": heuristics only, no crash
    result = _runner(session, demo, resume, llm=FakeLLM()).run(page, url=f"{BASE}/job")
    assert result.paused_reason.startswith("no apply control found")
    # no LLM at all
    runner = FormRunner(session, vault(resume), answers=None, llm=None, user=demo, profiles_dir=PROFILES)
    assert runner.run(page, url=f"{BASE}/job").paused_reason.startswith("no apply control found")


# ------------------------------------------------------------------ account walls


LOGIN = f"""<html><body>{JS}<h2>Sign in to apply</h2>
<script>function login(e){{e.preventDefault();
 if (document.querySelector('[name=password]').value === 'pw-stored-test-only') go('/apply-form')}}</script>
<form onsubmit="login(event)">
<label>Email <input type="email" name="email"></label>
<label>Password <input type="password" name="password"></label>
<button type="submit">Sign in</button></form>
<a href="/signup">Create account</a></body></html>"""

SIGNUP = f"""<html><body>{JS}<h2>Create your account</h2>
<script>function signup(e){{e.preventDefault();
 sessionStorage.setItem('pw', document.querySelector('[name=password]').value); go('/verify')}}</script>
<form onsubmit="signup(event)">
<label>First name <input name="first_name"></label>
<label>Last name <input name="last_name"></label>
<label>Email <input type="email" name="email"></label>
<label>Password <input type="password" name="password"></label>
<label>Confirm password <input type="password" name="confirm"></label>
<label><input type="checkbox" name="tos" required> I agree to the terms</label>
<button type="submit">Create account</button></form></body></html>"""

VERIFY = f"""<html><body>{JS}<p>Enter the code we emailed you</p>
<script>function verify(e){{e.preventDefault();
 if (document.querySelector('[name=code]').value === '482913') go('/apply-form')}}</script>
<form onsubmit="verify(event)">
<label>Verification code <input name="code"></label><button type="submit">Verify</button></form></body></html>"""

WALL_PAGES = {"/job": "<html><body><h1>Role</h1><a href='/login'>Apply</a></body></html>",
              "/login": LOGIN, "/signup": SIGNUP, "/verify": VERIFY, "/apply-form": FORM_HTML}


def _cred(session, demo):
    return session.exec(select(Credential).where(Credential.user_id == demo.id, Credential.site == "ext:ext.test",
                                                 Credential.kind == "login")).first()


def test_signup_wall_creates_account_and_verifies_with_mailbox_code(page, session, demo, vault_key):
    _route(page, WALL_PAGES)
    mails = [_mail("no-reply@ext.test", "Verify your email", "Your verification code is 482913. Welcome!")]
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200, **_mail_opts(mails))
    out = nav.find_form()
    assert out.status == STATUS_FORM, out.reason
    assert page.url == f"{BASE}/apply-form" and any(h.startswith("account ext.test") for h in out.hops)
    cred = Vault().load(session, demo.id, "ext:ext.test", "login")
    assert cred["username"] == DEMO_PROFILE["email"] and cred["verified"] is True
    assert len(cred["password"]) >= 20 and cred["password"] != "pw-stored-test-only"
    # the password typed into the sign-up form is the one in the vault, never the mailbox password
    assert page.evaluate("() => sessionStorage.getItem('pw')") == cred["password"]


def test_signup_wall_pauses_when_no_code_arrives(page, session, demo, vault_key):
    _route(page, WALL_PAGES)
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200, **_mail_opts([]))
    out = nav.find_form()
    assert out.status == STATUS_ACCOUNT_WALL and "no verification code arrived" in out.reason
    assert page.url == f"{BASE}/verify"
    cred = Vault().load(session, demo.id, "ext:ext.test", "login")
    assert cred and cred["verified"] is False  # stored before the form was sent, so it is never lost


def test_signup_verification_ignores_codes_from_before_the_signup(page, session, demo, vault_key):
    # A Naukri OTP from 6 minutes ago carries the very code the site would accept: it is from before the sign-up
    # was sent, so it is never typed; the run pauses instead of consuming another site's code.
    stale = _mail("noreply@naukri.com", "Your Naukri OTP", "Your Naukri login OTP is 482913",
                  when=utcnow() - timedelta(minutes=6))
    _route(page, WALL_PAGES)
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200, **_mail_opts([stale]))
    out = nav.find_form()
    assert out.status == STATUS_ACCOUNT_WALL and "no verification code arrived" in out.reason, out.reason
    assert page.url == f"{BASE}/verify"
    cred = Vault().load(session, demo.id, "ext:ext.test", "login")
    assert cred["created"] is True and cred["verified"] is False  # the sign-up was sent, the address not confirmed


def test_signup_rejected_code_is_not_marked_verified(page, session, demo, vault_key):
    fresh_but_wrong = _mail("no-reply@ext.test", "Verify your email", "Your verification code is 111111.")
    _route(page, WALL_PAGES)
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200,
                    **_mail_opts([fresh_but_wrong]))
    out = nav.find_form()
    assert out.status == STATUS_ACCOUNT_WALL and "not accepted" in out.reason, out.reason
    assert page.url == f"{BASE}/verify"
    assert Vault().load(session, demo.id, "ext:ext.test", "login")["verified"] is False


def test_uncreated_stored_row_retries_the_signup(page, session, demo, vault_key):
    # Last time the sign-up form could not be filled: the row holds a password the site never saw. The next run
    # must create the account, not sign in with it forever.
    Vault().store(session, demo.id, "ext:ext.test", "login", DEMO_PROFILE["email"],
                  {"password": "never-sent-test-only", "verified": False, "created": False})
    session.flush()
    _route(page, WALL_PAGES)
    mails = [_mail("no-reply@ext.test", "Verify your email", "Your verification code is 482913.")]
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200, **_mail_opts(mails))
    out = nav.find_form()
    assert out.status == STATUS_FORM and page.url == f"{BASE}/apply-form", out.reason
    cred = Vault().load(session, demo.id, "ext:ext.test", "login")
    assert cred["created"] is True and cred["verified"] is True and cred["password"] != "never-sent-test-only"
    assert page.evaluate("() => sessionStorage.getItem('pw')") == cred["password"]
    assert forget_external_account(session, demo.id, "https://ext.test/login") and _cred(session, demo) is None
    assert not forget_external_account(session, demo.id, "ext.test")


def test_login_wall_with_three_inputs_goes_through_the_navigator(page, session, demo, resume, vault_key):
    # email + password + "remember me" is three visible fields: the runner must not take it for the application
    # form and fill it; it is an account wall for the stored-account sign-in.
    login3 = LOGIN.replace('<button type="submit">Sign in</button>',
                           '<label><input type="checkbox" name="remember"> Remember me</label>'
                           '<button type="submit">Sign in</button>')
    Vault().store(session, demo.id, "ext:ext.test", "login", DEMO_PROFILE["email"],
                  {"password": "pw-stored-test-only", "verified": True})
    session.flush()
    _route(page, {**WALL_PAGES, "/login": login3})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/login")
    assert page.url == f"{BASE}/apply-form" and _filled(page), result.summary()
    assert result.paused_reason == "" and "account ext.test" in result.hops


def test_mailbox_login_failure_is_reported_once(page, session, demo, vault_key):
    attempts: list[int] = []

    def refused(cfg):
        attempts.append(1)
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    _route(page, WALL_PAGES)
    page.goto(f"{BASE}/job")
    nav = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200, imap_config=IMAP_CFG,
                    imap_factory=refused, otp_timeout=1.0, otp_poll=0.1)
    out = nav.find_form()
    assert out.status == STATUS_ACCOUNT_WALL and MAILBOX_LOGIN_FAILED in out.reason, out.reason
    assert len(attempts) == 1  # not one refused login per poll until the deadline


def test_wall_without_mailbox_pauses_mailbox_not_configured(page, session, demo, vault_key):
    _route(page, WALL_PAGES)
    page.goto(f"{BASE}/job")
    out = Navigator(page, session=session, user=demo, allow_final=True, settle_ms=200).find_form()
    assert out.status == STATUS_ACCOUNT_WALL and "mailbox not configured" in out.reason
    assert _cred(session, demo) is None  # nothing created


def test_wall_in_dry_run_never_creates_an_account(page, session, demo, resume, vault_key):
    _route(page, WALL_PAGES)
    shot = get_settings().screenshots_dir / "wall.png"
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job", screenshot_path=shot)
    assert "needs an account" in result.paused_reason and "not created" in result.paused_reason
    assert page.url == f"{BASE}/login" and _cred(session, demo) is None and shot.exists()
    assert result.plan == [] and result.error == ""


def test_wall_signs_in_with_the_stored_account(page, session, demo, resume, vault_key):
    Vault().store(session, demo.id, "ext:ext.test", "login", DEMO_PROFILE["email"],
                  {"password": "pw-stored-test-only", "verified": True})
    session.flush()
    _route(page, WALL_PAGES)
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")  # dry run: sign-in is allowed
    assert result.paused_reason == "" and page.url == f"{BASE}/apply-form" and _filled(page), result.summary()
    assert "account ext.test" in result.hops


def test_wall_without_vault_pauses(page, session, demo, resume, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "")  # the repo .env may carry a key; an empty env var wins over it
    get_settings.cache_clear()
    try:
        _route(page, WALL_PAGES)
        result = _runner(session, demo, resume).run(page, url=f"{BASE}/job", submit=True)
        assert "vault not configured" in result.paused_reason and not result.submitted
        assert _cred(session, demo) is None
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ mailto apply links


def test_mailto_apply_link_is_reported_not_followed(page, session, demo, resume):
    landing = ("<html><body><h1>Role</h1>"
               "<a href='mailto:jobs@acme.example?subject=Backend'>Apply by email</a></body></html>")
    _route(page, {"/job": landing})
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert result.mailto == "mailto:jobs@acme.example?subject=Backend"
    assert result.paused_reason.startswith("apply by email") and page.url == f"{BASE}/job"
    page.goto(f"{BASE}/job")
    out = Navigator(page).find_form()
    assert out.status == STATUS_MAILTO and out.mailto.startswith("mailto:")


def test_quick_apply_form_with_few_fields_is_still_filled(page, session, demo, resume):
    quick = (f"<html><body>{JS}<h1>Role</h1><form onsubmit=\"event.preventDefault();show('t')\">"
             "<label for='em'>Email *</label><input id='em' name='email' type='email' required>"
             "<label for='cv'>Resume</label><input id='cv' type='file' name='resume'>"
             "<button type='submit'>Apply</button></form><div id='t' style='display:none'>Thanks</div></body></html>")
    _route(page, {"/job": quick})
    page.goto(f"{BASE}/job")
    out = Navigator(page).find_form()
    assert out.status == STATUS_NO_FORM and len(out.fields) == 2  # the form's own "Apply" submit is not a hop
    result = _runner(session, demo, resume).run(page, url=f"{BASE}/job")
    assert page.locator("#em").input_value() == DEMO_PROFILE["email"] and not page.locator("#t").is_visible()
    assert result.paused_reason == "" and result.filled == 2


def test_eml_roundtrip_helper():
    # sanity for the pipeline test's parser: an EmailMessage with an attachment parses back
    msg = EmailMessage()
    msg["To"], msg["Subject"] = "hr@acme.example", "x"
    msg.set_content("body")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="resume.pdf")
    back = email_lib.message_from_bytes(msg.as_bytes(), policy=email_policy.default)
    assert [p.get_filename() for p in back.iter_attachments()] == ["resume.pdf"]
