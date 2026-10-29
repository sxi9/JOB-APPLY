"""Workday account step: page classification, password generation, sign-in / create-account / verification on
an inline "mini Workday" fixture (real headless Chromium), the verification mail reader over a fake IMAP server.

The fixture (``fixture_html``) is one page that renders the job page, the apply-method dialog, the sign-in /
create / verify step and the wizard from ``location.pathname`` plus a config blob, keeping its state in
``sessionStorage`` so link navigations (as on the live site) carry it along. Shared by the wizard and apply tests.
"""
# ruff: noqa: E501  (embedded JavaScript fixture, like src/jobapply/forms/fields.py)

from __future__ import annotations

import imaplib
import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

from jobapply.config import get_settings
from jobapply.mail.imap import ImapReader, MailboxConfig
from jobapply.mail.otp import CodeRegistry
from jobapply.models import utcnow
from jobapply.sites.workday import account as acc
from jobapply.sites.workday.account import (
    MAILBOX_LOGIN_FAILED,
    Verification,
    WorkdayAccount,
    active_step,
    classify_page,
    generate_password,
    page_error,
    password_ok,
    verification_from_mail,
    wait_for_verification,
)
from jobapply.sites.workday.tenant import parse_tenant_url
from jobapply.vault import Vault, generate_key

NO_PAUSE = lambda page, lo, hi: page.wait_for_timeout(30)  # noqa: E731
JOB_URL = "https://acme.wd5.myworkdayjobs.com/Acme/job/Bengaluru/Python-Developer_JR1"
TENANT = parse_tenant_url(JOB_URL)
CODE = "482913"

# ------------------------------------------------------------------ the fixture

FIXTURE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Python Developer - Acme</title>
<style>.hidden{display:none} label{display:block;margin-top:6px} .err{color:#b00}
.dialog{position:fixed;top:80px;left:300px;width:420px;background:#fff;border:1px solid #333;padding:16px}</style>
</head><body>
<header data-automation-id="header"><button data-automation-id="utilityButtonSignIn">Sign In</button></header>
<div id="app"></div>
<script>
const CFG = /*CFG*/{}/*/CFG*/;
const BASE = 'https://acme.wd5.myworkdayjobs.com/Acme/job/Bengaluru/Python-Developer_JR1';
const STEPS = CFG.steps || ["My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Review"];
const st = JSON.parse(sessionStorage.getItem('wd_state') ||
  '{"values":{},"next_clicks":0,"submit_clicks":0,"step":0,"sign_in_attempts":0,"create_attempts":0,"verify_attempts":0}');
function save() { sessionStorage.setItem('wd_state', JSON.stringify(st)); }
const app = document.getElementById('app');
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
function phase() {
  const p = location.pathname;
  if (CFG.phase) return CFG.phase;
  if (CFG.captcha) return 'captcha';
  if (/\/apply\/(applyManually|autofillWithResume)$/.test(p)) {
    if (CFG.already_applied) return 'already';
    if (st.submitted) return 'submitted';
    if (st.signed_in || CFG.signed_in) return 'wizard';
    if (st.pending_verify) return 'verify';
    return 'login';
  }
  if (/\/apply$/.test(p)) return 'dialog';
  return 'job';
}
function field(label, inner, auto) {
  return `<div data-automation-id="formField-${auto}"><label for="in-${auto}">${label}</label>${inner}</div>`;
}
function input(label, auto, o) {
  o = o || {};
  const req = o.required ? ' required' : '';
  const type = o.type || 'text';
  const tag = type === 'textarea' ? `<textarea id="in-${auto}" data-automation-id="${auto}"${req}></textarea>`
    : `<input id="in-${auto}" type="${type}" data-automation-id="${auto}"${req}>`;
  return field(label + (o.required ? '*' : ''), tag, auto);
}
function select(label, auto, options, required) {
  const o = ['Select One'].concat(options).map(x => `<option>${x}</option>`).join('');
  return field(label + (required ? '*' : ''),
    `<select id="in-${auto}" data-automation-id="${auto}"${required ? ' required' : ''}>${o}</select>`, auto);
}
function radio(legend, name, options, required) {
  const r = options.map((o, i) => `<label><input type="radio" name="${name}" value="${o}" id="${name}-${i}"` +
    `${required ? ' required' : ''}> ${o}</label>`).join('');
  return `<fieldset data-automation-id="formField-${name}"><legend>${legend}${required ? '*' : ''}</legend>${r}</fieldset>`;
}
function checkbox(label, auto, required) {
  return `<div data-automation-id="formField-${auto}"><input type="checkbox" id="in-${auto}" data-automation-id="${auto}"` +
    `${required ? ' required' : ''}><label for="in-${auto}">${label}${required ? '*' : ''}</label></div>`;
}
const HONEYPOT = `<div style="position:absolute;left:-9999px"><label for="hp">Enter website. This input is for robots only,` +
  ` do not enter if you're human.</label><input id="hp" type="text" name="website" data-automation-id="beecatcher"></div>`;
const ERR = `<div id="err" class="err hidden" data-automation-id="errorMessage"></div>`;
function error(msg) { const e = document.getElementById('err'); e.textContent = msg; e.classList.remove('hidden'); }
function progress(active) {
  const items = ['Create Account/Sign In'].concat(STEPS).map((s, i) => {
    const on = s === active;
    return `<li data-automation-id="${on ? 'progressBarActiveStep' : 'progressBarInactiveStep'}">` +
      `<span>${on ? 'current ' : ''}step ${i + 1} of ${STEPS.length + 1}</span><label>${s}</label></li>`;
  }).join('');
  return `<div data-automation-id="applyFlowPage"><button type="button" data-automation-id="backToJobPosting">Back to Job Posting</button>` +
    `<h2 data-automation-id="jobTitleHeading">Python Developer</h2><ol data-automation-id="progressBar">${items}</ol>` +
    `<div id="content"></div></div>`;
}
function renderJob(dialog) {
  app.innerHTML = `<div data-automation-id="jobPostingHeader"><h1>Python Developer</h1></div>` +
    `<a data-automation-id="adventureButton" role="button" href="${BASE}/apply">Apply</a>` +
    `<p>Backend engineer: Python, FastAPI, Postgres. You will run the security review of every release.</p>` +
    (dialog ? `<div class="dialog" role="dialog"><button data-automation-id="closeButton">x</button>` +
      `<a data-automation-id="autofillWithResume" role="button" href="${BASE}/apply/autofillWithResume">Autofill with Resume</a>` +
      `<a data-automation-id="applyManually" role="button" href="${BASE}/apply/applyManually">Apply Manually</a>` +
      `<a data-automation-id="useMyLastApplication" role="button" href="${BASE}/apply/useMyLastApplication">Use My Last Application</a>` +
      `</div>` : '');
}
function renderLogin(mode) {
  app.innerHTML = progress('Create Account/Sign In');
  const c = document.getElementById('content');
  if (mode === 'cover') {
    c.innerHTML = `<div data-automation-id="signInContent"><h3>Sign In</h3><div data-automation-id="richText">By signing in with` +
      ` your social account, you consent to creating a candidate profile.</div>` +
      `<button type="button" data-automation-id="GoogleSignInButton">Sign in with Google</button>` +
      `<button type="button" data-automation-id="SignInWithEmailButton">Sign in with email</button></div>`;
    document.querySelector('[data-automation-id="SignInWithEmailButton"]').onclick = () => renderLogin('form');
    return;
  }
  if (mode === 'form') {
    c.innerHTML = `<div data-automation-id="signInContent"><h3>Sign In</h3>${ERR}<form data-automation-id="signInFormo">` +
      input('Email Address', 'email', {required: true}) + input('Password', 'password', {required: true, type: 'password'}) +
      `<div data-automation-id="noCaptchaWrapper"><button type="submit" data-automation-id="signInSubmitButton">Sign In</button></div>` +
      `<p>Don't have an account yet?<button type="button" data-automation-id="createAccountLink">Create Account</button></p>` +
      `<button type="button" data-automation-id="forgotPasswordLink">Forgot your password?</button>${HONEYPOT}</form></div>`;
    document.querySelector('[data-automation-id="createAccountLink"]').onclick = () => renderLogin('create');
    document.querySelector('form').onsubmit = (e) => {
      e.preventDefault();
      const email = document.getElementById('in-email').value, pw = document.getElementById('in-password').value;
      st.honeypot = document.getElementById('hp').value; st.sign_in_attempts += 1; save();
      if (st.honeypot) return error('Robot detected.');
      if (CFG.account_email && email === CFG.account_email && pw === CFG.account_password) {
        st.signed_in = 1; st.signed_in_email = email; save();
        if (CFG.verify_on_sign_in) { st.pending_verify = 1; st.created_email = email; save(); return renderVerify(); }
        return render();
      }
      error('The email address or password you entered is incorrect.');
    };
    return;
  }
  c.innerHTML = `<div data-automation-id="signInContent"><h3>Create Account</h3><div data-automation-id="passwordRulesList">` +
    `Password Requirements: An alphabetic character, A numeric character, An uppercase character, A special character,` +
    ` A lowercase character, A minimum of 8 characters</div>${ERR}<form data-automation-id="signInFormo">` +
    input('Email Address', 'email', {required: true}) + input('Password', 'password', {required: true, type: 'password'}) +
    input('Verify New Password', 'verifyPassword', {required: true, type: 'password'}) +
    `<div data-automation-id="richText">Welcome to our application process!</div>` +
    checkbox('I understand and acknowledge the terms of use.', 'createAccountCheckbox', true) +
    checkbox('Send me job alerts and marketing email.', 'marketingCheckbox', false) +
    `<div data-automation-id="noCaptchaWrapper"><button type="submit" data-automation-id="createAccountSubmitButton">Create Account</button></div>` +
    `<button type="button" data-automation-id="signInLink">Sign In</button>` +
    `<button type="button" data-automation-id="forgotPasswordLink">Forgot your password?</button>${HONEYPOT}</form></div>`;
  document.querySelector('[data-automation-id="signInLink"]').onclick = () => renderLogin('form');
  document.querySelector('form').onsubmit = (e) => {
    e.preventDefault();
    const email = document.getElementById('in-email').value, pw = document.getElementById('in-password').value;
    const v = document.getElementById('in-verifyPassword').value;
    const terms = document.getElementById('in-createAccountCheckbox').checked;
    st.marketing_checked = document.getElementById('in-marketingCheckbox').checked;
    st.honeypot = document.getElementById('hp').value; st.create_attempts += 1; save();
    if (st.honeypot) return error('Robot detected.');
    if (!terms) return error('You must acknowledge the terms of use.');
    if (pw !== v) return error('The passwords do not match.');
    if (!/[A-Z]/.test(pw) || !/[a-z]/.test(pw) || !/\d/.test(pw) || !/[^A-Za-z0-9]/.test(pw) || pw.length < 8)
      return error('The password does not meet the requirements.');
    if (CFG.existing_email && email === CFG.existing_email) return error('An account with this email address already exists.');
    st.created = 1; st.created_email = email; st.created_password = pw; save();
    if (CFG.verify) { st.pending_verify = 1; save(); return renderVerify(); }
    st.signed_in = 1; save(); render();
  };
}
function renderVerify() {
  app.innerHTML = progress('Create Account/Sign In');
  document.getElementById('content').innerHTML = `<div data-automation-id="verifyEmailContent"><h3>Verify Your Email</h3>` +
    `<p>We sent a verification code to ${esc(st.created_email || '')}. Enter the verification code below.</p>${ERR}` +
    `<form>${input('Verification Code', 'verificationCode', {required: true})}` +
    `<button type="submit" data-automation-id="verifySubmitButton">Verify</button></form></div>`;
  document.querySelector('form').onsubmit = (e) => {
    e.preventDefault();
    st.verify_attempts += 1; save();
    const code = document.getElementById('in-verificationCode').value.trim();
    if (code === String(CFG.code || '')) { st.verified = 1; st.signed_in = 1; delete st.pending_verify; save(); return render(); }
    error('The code you entered is invalid.');
  };
}
function renderWizard() {
  const step = STEPS[st.step];
  app.innerHTML = progress(step);
  let body = '';
  if (step === 'My Information') body =
    input('First Name', 'legalNameSection_firstName', {required: true}) + input('Last Name', 'legalNameSection_lastName', {required: true}) +
    input('Address Line 1', 'addressSection_addressLine1') + input('City', 'addressSection_city') +
    input('Postal Code', 'addressSection_postalCode') + input('Email Address', 'email', {required: true}) +
    input('Country Phone Code', 'phone-country-code') + select('Phone Device Type', 'phone-device-type', ['Mobile', 'Home', 'Work'], true) +
    input('Phone Number', 'phone-number', {required: true}) + input('How Did You Hear About Us?', 'source') +
    radio('Have you previously worked for Acme?', 'previousWorker', ['Yes', 'No'], true);
  else if (step === 'My Experience') body =
    `<div data-automation-id="workExperienceSection"><h3>Work Experience</h3>` + input('Job Title', 'jobTitle', {required: true}) +
    input('Company', 'company', {required: true}) + input('Location', 'location') + input('Role Description', 'description', {type: 'textarea'}) +
    `</div><div data-automation-id="educationSection"><h3>Education</h3>` + input('School or University', 'school') +
    select('Degree', 'degree', ["Bachelor's Degree", "Master's Degree"], false) + input('Field of Study', 'fieldOfStudy') + `</div>` +
    input('Skills', 'skills') + input('LinkedIn', 'linkedin') + input('Website', 'website') +
    input('Resume/CV', 'file-upload-input-ref', {required: true, type: 'file'});
  else if (step === 'Application Questions') body =
    select('Are you legally authorized to work in India?', 'q1', ['Yes', 'No'], true) +
    select('Will you now or in the future require sponsorship?', 'q2', ['Yes', 'No'], true) +
    input('What is your notice period?', 'q3', {required: true}) + input('Expected annual salary (INR)', 'q4');
  else if (step === 'Voluntary Disclosures') body =
    select('Gender', 'gender', ['Male', 'Female', 'Decline to answer'], false) +
    checkbox('I have read and consent to the terms and conditions.', 'termsAndConditions', true);
  else if (step === 'Review') body = `<h3>Review</h3><pre id="review">${esc(JSON.stringify(st.values))}</pre>`;
  const label = step === 'Review' ? 'Submit' : 'Save and Continue';
  // Like the live SPA, Next is a plain button whose click handler advances the step (no native submit event,
  // so nothing a submit-event guard could intercept); Enter in a field goes through the same handler.
  document.getElementById('content').innerHTML = `${ERR}<form id="wizard-form">${body}<div>` +
    `<button type="button" data-automation-id="bottom-navigation-back-button">Back</button>` +
    `<button type="button" data-automation-id="bottom-navigation-next-button">${label}</button></div></form>`;
  const form = document.getElementById('wizard-form');
  document.querySelector('[data-automation-id="bottom-navigation-back-button"]').onclick = () => {
    st.step = Math.max(0, st.step - 1); save(); render(); };
  document.querySelector('[data-automation-id="bottom-navigation-next-button"]').onclick = () => advance();
  form.onsubmit = (e) => { e.preventDefault(); advance(); };
  function advance() {
    if (step === 'Review') { st.submitted = 1; st.submit_clicks += 1; save(); return render(); }
    const vals = {}; const missing = [];
    for (const f of form.querySelectorAll('input, select, textarea')) {
      if (f.type === 'submit' || f.type === 'button') continue;
      let lab = f.labels && f.labels[0] ? f.labels[0].innerText.trim() : f.name;
      if (f.type === 'radio') {
        lab = f.closest('fieldset').querySelector('legend').innerText.trim();
        if (f.checked) vals[lab] = f.value;
        else if (f.required && !form.querySelector(`input[name="${f.name}"]:checked`) && !missing.includes(lab)) missing.push(lab);
        continue;
      }
      if (f.type === 'checkbox') { vals[lab] = f.checked; if (f.required && !f.checked) missing.push(lab); continue; }
      if (f.type === 'file') { vals[lab] = f.files.length ? f.files[0].name : ''; if (f.required && !f.files.length) missing.push(lab); continue; }
      const v = f.tagName === 'SELECT' ? (f.value === 'Select One' ? '' : f.value) : f.value;
      vals[lab] = v; if (f.required && !v) missing.push(lab);
    }
    if (missing.length) return error('Required fields are missing: ' + missing.join(', '));
    if (CFG.reject_step === step) return error('Something went wrong on this page. Please try again.');
    st.values[step] = vals; st.next_clicks += 1; st.step += 1; save(); render();
  }
}
function render() {
  const p = phase();
  if (p === 'job') return renderJob(false);
  if (p === 'dialog') return renderJob(true);
  if (p === 'login') return renderLogin(CFG.social ? 'cover' : 'form');
  if (p === 'create') return renderLogin('create');
  if (p === 'verify') return renderVerify();
  if (p === 'wizard') return renderWizard();
  if (p === 'submitted') { app.innerHTML = `<div data-automation-id="applySuccessPage"><h2>Thank you for applying</h2>` +
    `<p>Your application has been submitted.</p></div>`; return; }
  if (p === 'already') { app.innerHTML = `<div><h2>You have already applied to this job.</h2></div>`; return; }
  if (p === 'captcha') { app.innerHTML = `<div class="g-recaptcha" data-sitekey="x" style="width:304px;height:78px;background:#eee"></div>` +
    `<p>Please verify you are human.</p>`; return; }
  app.innerHTML = '<p>Loading</p>';
}
render();
</script></body></html>
"""


def fixture_html(**cfg) -> str:
    start, end = FIXTURE.index("/*CFG*/") + len("/*CFG*/"), FIXTURE.index("/*/CFG*/")
    return FIXTURE[:start] + json.dumps(cfg) + FIXTURE[end:]


def route_tenant(page, html: str) -> None:
    """Serve ``html`` for every path of the fake tenant so link navigations (Apply, Apply Manually) work."""

    def handler(route, request):
        route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")

    page.route("https://acme.wd5.myworkdayjobs.com/**", handler)


def wd_state(page) -> dict:
    return page.evaluate("() => JSON.parse(sessionStorage.getItem('wd_state') || '{}')")


def open_phase(page, **cfg) -> None:
    """Load the fixture on the apply-manually URL (so sessionStorage works) in the given phase."""
    route_tenant(page, fixture_html(**cfg))
    page.goto(TENANT.apply_manually_url, wait_until="domcontentloaded")
    page.wait_for_timeout(50)


# ------------------------------------------------------------------ fake mail


class FakeImap:
    def __init__(self, messages: list[bytes]):
        self.messages = {str(i + 1): m for i, m in enumerate(messages)}

    def login(self, user, password):
        return "OK", [b"Logged in"]

    def select(self, folder):
        return "OK", [str(len(self.messages)).encode()]

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


def raw_mail(sender: str, subject: str, body: str, html: str | None = None, when: datetime | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "demo.applies@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when or utcnow())
    msg["Message-ID"] = f"<{abs(hash(subject + body))}@example.com>"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def code_mail(code: str = CODE) -> bytes:
    return raw_mail("Acme Careers <acme@myworkday.com>", "Verify your email address",
                    f"Thanks for creating an account. Your verification code is {code}. It expires in 15 minutes.")


def link_mail(url: str) -> bytes:
    return raw_mail("acme@myworkday.com", "Confirm your email", f"Click the link to verify your email: {url}",
                    html=f'<a href="{url}">Verify email</a>')


def fake_reader(messages: list[bytes]) -> tuple[ImapReader, FakeImap]:
    fake = FakeImap(messages)
    cfg = MailboxConfig(host="imap.example.com", username="demo.applies@example.com", password="mailbox-pw")
    return ImapReader(cfg, client_factory=lambda c: fake), fake


def client_factory_for(messages: list[bytes]):
    fake = FakeImap(messages)
    return lambda cfg: fake


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    yield Vault()
    get_settings.cache_clear()


def make_account(page, user, session, vault=None, **kw) -> WorkdayAccount:
    return WorkdayAccount(page, user, session, TENANT, vault=vault, pause=NO_PAUSE, settle_ms=60, **kw)


# ------------------------------------------------------------------ pure units


def test_generate_password_meets_workday_rules():
    seen = {generate_password() for _ in range(50)}
    assert len(seen) == 50
    for pw in seen:
        assert len(pw) == 20 and password_ok(pw), "rules: alphabetic, numeric, upper, lower, special, >= 8"
        assert " " not in pw and "\n" not in pw
    assert len(generate_password(8)) == 12  # never shorter than 12
    assert not password_ok("short1!") and not password_ok("alllowercase1!") and not password_ok("NoDigits!!")


def test_verification_from_mail_prefers_code_then_link():
    from jobapply.mail.imap import parse_message

    assert verification_from_mail(parse_message("1", code_mail())) == Verification("code", CODE)
    link = "https://acme.wd5.myworkdayjobs.com/Acme/verifyEmail?token=abc123"
    assert verification_from_mail(parse_message("2", link_mail(link))) == Verification("link", link)
    assert verification_from_mail(parse_message("3", raw_mail("a@b.com", "Hi", "no code here"))) is None


def test_wait_for_verification_filters_on_the_workday_sender():
    r, _ = fake_reader([raw_mail("noreply@linkedin.com", "Your code", "Your verification code is 111111"),
                        code_mail()])
    assert wait_for_verification(r, timeout=1, poll=0.1) == Verification("code", CODE)
    r2, _ = fake_reader([raw_mail("noreply@linkedin.com", "Your code", "Your verification code is 111111")])
    assert wait_for_verification(r2, timeout=0.3, poll=0.1) is None


# ------------------------------------------------------------------ classification (real Chromium on the fixture)


@pytest.mark.parametrize(("cfg", "expected"), [
    ({"phase": "job"}, "unknown"),
    ({"phase": "login", "social": True}, "login"),  # the "Sign in with email" cover, form not shown yet
    ({"phase": "login"}, "login"),
    ({"phase": "create"}, "create"),
    ({"phase": "verify"}, "verify"),
    ({"phase": "wizard"}, "wizard"),
    ({"phase": "submitted"}, "submitted"),
    ({"phase": "already"}, "submitted"),
    ({"phase": "captcha"}, "challenge"),
])
def test_classify_page_states(page, cfg, expected):
    open_phase(page, **cfg)
    assert classify_page(page) == expected


def test_classify_blank_page_is_unknown_and_active_step_reads_the_progress_bar(page):
    page.set_content("<html><body><p>Loading</p></body></html>")
    assert classify_page(page) == "unknown" and active_step(page) == ""
    open_phase(page, phase="wizard")
    assert active_step(page) == "My Information"
    open_phase(page, phase="login")
    assert active_step(page) == "Create Account/Sign In"


def test_job_description_wording_is_not_a_challenge(page):
    open_phase(page, phase="job")  # the JD mentions a "security review"; only bot-wall wording counts
    assert classify_page(page) == "unknown"
    page.set_content("<html><body><h1>Access Denied</h1><p>Your request has been blocked.</p></body></html>")
    assert classify_page(page) == "challenge"


# ------------------------------------------------------------------ open_apply


def test_open_apply_walks_job_page_dialog_and_apply_manually(page, session, demo):
    route_tenant(page, fixture_html(social=True))
    account = make_account(page, demo, session)
    assert account.open_apply(JOB_URL) == "login"
    assert page.url.endswith("/apply/applyManually")
    assert account.email == "demo.applies@example.com"


def test_open_apply_reports_a_block_status(session, demo):
    class Resp:
        status = 403

    class P:
        url = JOB_URL

        def goto(self, *a, **kw):
            return Resp()

    assert make_account(P(), demo, session).open_apply(JOB_URL) == "challenge"


# ------------------------------------------------------------------ sign in


def test_sign_in_with_stored_credentials_reaches_the_wizard(page, session, demo):
    open_phase(page, social=True, account_email="demo.applies@example.com", account_password="Stored-Pw-1")
    account = make_account(page, demo, session)
    assert account.sign_in("demo.applies@example.com", "Stored-Pw-1") == "wizard"
    st = wd_state(page)
    assert st["signed_in"] == 1 and st["sign_in_attempts"] == 1 and st["honeypot"] == ""  # robots-only box untouched
    assert active_step(page) == "My Information"


def test_sign_in_rejected_stays_on_login_with_the_error(page, session, demo):
    open_phase(page, account_email="demo.applies@example.com", account_password="Right-Pw-1")
    account = make_account(page, demo, session)
    assert account.sign_in("demo.applies@example.com", "Wrong-Pw-1") == "login"
    assert "incorrect" in page_error(page)
    assert wd_state(page).get("signed_in") is None


# ------------------------------------------------------------------ create account + verification


def test_create_account_fills_only_the_required_checkbox_and_lands_on_verify(page, session, demo, vault):
    open_phase(page, verify=True, code=CODE)
    account = make_account(page, demo, session, vault)
    creds = account.new_credentials()
    assert creds["username"] == "demo.applies@example.com" and password_ok(creds["password"])
    assert account.create_account(account.email, creds["password"]) == "verify"
    st = wd_state(page)
    assert st["created"] == 1 and st["created_email"] == "demo.applies@example.com"
    assert st["created_password"] == creds["password"]  # what the site got is what the vault holds
    assert st["marketing_checked"] is False and st["honeypot"] == ""
    assert account.created
    # the vault row: site workday:acme, kind login, never the mailbox password
    stored = vault.load(session, demo.id, "workday:acme", "login")
    assert stored["username"] == "demo.applies@example.com" and stored["password"] == creds["password"]
    assert stored["tenant"] == "acme" and stored["site"] == "Acme" and stored["verified"] is False
    # the mailed code finishes the step
    r, _ = fake_reader([code_mail()])
    assert account.verify_email(r, timeout=1, poll=0.1) == ("wizard", "")
    assert wd_state(page)["verified"] == 1 and active_step(page) == "My Information"
    assert vault.load(session, demo.id, "workday:acme", "login")["verified"] is True


def test_create_account_from_the_sign_in_cover(page, session, demo, vault):
    """Starting on the social cover: Sign in with email -> Create Account link -> form; no verification mail."""
    open_phase(page, social=True)
    account = make_account(page, demo, session, vault)
    creds = account.new_credentials()
    assert account.create_account(account.email, creds["password"]) == "wizard"
    assert wd_state(page)["created"] == 1


def test_create_account_existing_email_is_reported(page, session, demo, vault):
    open_phase(page, existing_email="demo.applies@example.com")
    account = make_account(page, demo, session, vault)
    creds = account.new_credentials()
    assert account.create_account(account.email, creds["password"]) == "create"
    assert "already exists" in page_error(page)


def test_verify_email_times_out_without_a_mail(page, session, demo):
    open_phase(page, phase="verify", code=CODE)
    account = make_account(page, demo, session)
    r, _ = fake_reader([raw_mail("x@y.com", "Hello", "nothing")])
    state, reason = account.verify_email(r, timeout=0.3, poll=0.1)
    assert state == "verify" and reason.startswith("verification code not received")
    assert wd_state(page).get("verify_attempts", 0) == 0


def test_wrong_code_is_not_retried(page, session, demo):
    open_phase(page, phase="verify", code=CODE)
    account = make_account(page, demo, session)
    r, _ = fake_reader([code_mail("999999")])
    state, reason = account.verify_email(r, timeout=1, poll=0.1)
    assert state == "verify" and reason == "verification code not accepted"
    assert wd_state(page)["verify_attempts"] == 1


def test_verify_email_ignores_mail_from_before_the_click_and_never_reuses_a_code(page, session, demo):
    open_phase(page, phase="verify", code=CODE)
    account = make_account(page, demo, session)
    account.started_at = utcnow()  # set by create_account / sign_in right before their click
    # the previous tenant's mail from the same run: same sender pattern, the very code this fixture accepts
    stale = raw_mail("globex@myworkday.com", "Verify your email address", f"Your verification code is {CODE}.",
                     when=utcnow() - timedelta(minutes=8))
    r, _ = fake_reader([stale])
    state, reason = account.verify_email(r, timeout=0.3, poll=0.1)
    assert state == "verify" and reason.startswith("verification code not received")
    assert wd_state(page).get("verify_attempts", 0) == 0  # never typed into this tenant's box
    # a fresh mail is used once ...
    reg = CodeRegistry(session, demo.id)
    r, _ = fake_reader([code_mail()])
    assert wait_for_verification(r, timeout=1, poll=0.1, after=account.started_at, registry=reg) == \
        Verification("code", CODE)
    # ... and the same code is refused for the next tenant of the run
    r, _ = fake_reader([code_mail()])
    assert wait_for_verification(r, timeout=0.3, poll=0.1, registry=reg) is None
    assert wait_for_verification(r, timeout=0.3, poll=0.1) == Verification("code", CODE)  # no registry: as before


def test_create_account_marks_the_row_created_only_after_the_click(page, session, demo, vault):
    open_phase(page, verify=True, code=CODE)
    broken = make_account(page, demo, session, vault, selectors={"verify_password": 'input[data-automation-id="nope"]'})
    creds = broken.new_credentials()
    assert vault.load(session, demo.id, "workday:acme", "login")["created"] is False
    assert broken.create_account(broken.email, creds["password"]) in ("login", "create")  # form not detected
    assert not broken.created and broken.started_at is None and wd_state(page).get("create_attempts", 0) == 0
    assert vault.load(session, demo.id, "workday:acme", "login")["created"] is False  # still "no account"
    account = make_account(page, demo, session, vault)
    assert account.create_account(account.email, creds["password"]) == "verify"
    assert account.created and account.started_at is not None
    stored = vault.load(session, demo.id, "workday:acme", "login")
    assert stored["created"] is True and stored["verified"] is False and stored["password"] == creds["password"]


def test_mailbox_login_refused_is_reported_once(page, session, demo):
    open_phase(page, phase="verify", code=CODE)
    account = make_account(page, demo, session)
    attempts: list[int] = []

    def refused(cfg):
        attempts.append(1)
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    cfg = MailboxConfig(host="imap.example.com", username="demo.applies@example.com", password="mailbox-pw")
    state, reason = account.verify_email(ImapReader(cfg, client_factory=refused), timeout=1, poll=0.1)
    assert state == "verify" and reason == MAILBOX_LOGIN_FAILED and len(attempts) == 1


def test_verification_link_is_opened_in_the_same_page(page, session, demo):
    route_tenant(page, fixture_html(signed_in=True))
    page.goto(TENANT.apply_manually_url, wait_until="domcontentloaded")
    account = make_account(page, demo, session)
    link = "https://acme.wd5.myworkdayjobs.com/Acme/job/Bengaluru/Python-Developer_JR1/apply/applyManually?token=abc"
    assert account.enter_verification(Verification("link", link)) == "wizard"
    assert page.url.startswith(link.split("?")[0])


def test_credentials_without_a_vault(page, session, demo):
    account = make_account(page, demo, session, None)
    assert account.credentials() is None
    with pytest.raises(RuntimeError):
        account.new_credentials()


def test_selectors_are_documented_and_the_honeypot_is_never_a_target():
    assert acc.SELECTORS["honeypot"].startswith('input[data-automation-id="beecatcher"]')
    for key in ("email", "password", "verify_password", "create_checkbox"):
        assert "beecatcher" not in acc.SELECTORS[key] and "website" not in acc.SELECTORS[key]
    assert Path(acc.__file__).read_text(encoding="utf-8").count("beecatcher") >= 2
