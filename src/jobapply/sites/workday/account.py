"""The Workday *Create Account / Sign In* step (step 1 of the apply flow) and page classification.

Live on 2026-10-29 (revantage.wd1, logged out): the job page's ``adventureButton`` ("Apply") opens a dialog
with ``autofillWithResume`` / ``applyManually`` / ``useMyLastApplication``; the apply flow page
(``applyFlowPage``, ``progressBar`` with ``progressBarActiveStep``) starts on "Create Account/Sign In". Some
tenants front the email form with social buttons plus ``SignInWithEmailButton``; the form itself is
``signInFormo`` with ``email`` / ``password`` inputs, ``signInSubmitButton``, ``createAccountLink`` and
``forgotPasswordLink``. The create form adds ``verifyPassword``, the single required ``createAccountCheckbox``
("I understand and acknowledge the terms of use") and ``createAccountSubmitButton``; the password rules are
alphabetic + numeric + uppercase + lowercase + special, at least 8 characters. Both forms carry a honeypot
(``beecatcher``, ``name=website``, "for robots only") that is never touched.

What happens after *Create Account* (the verification mail) could not be observed without creating an
account, so the ``verify`` state and its code input are matched broadly (fixture-only) and the mail is
read with ``mail.otp`` (a code, or a verification link) from the user's mailbox.

Safety: ``create_account`` is the only call that creates anything and ``apply.apply_job`` refuses it under
the global dry run; ``sign_in`` only types the password this program generated and stored in the vault,
never the mailbox password; passwords are never logged or returned in results.
"""

from __future__ import annotations

import imaplib
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jobapply.forms.fields import detect_captcha
from jobapply.logs import get_logger
from jobapply.mail.imap import ImapReader, Mail
from jobapply.mail.otp import CodeRegistry, extract_code, mail_filter, verification_link
from jobapply.models import User, utcnow
from jobapply.sites.workday.tenant import WorkdayTenant
from jobapply.vault import Vault

log = get_logger(__name__)

STATE_LOGIN = "login"
STATE_CREATE = "create"
STATE_VERIFY = "verify"
STATE_WIZARD = "wizard"
STATE_SUBMITTED = "submitted"
STATE_CHALLENGE = "challenge"
STATE_UNKNOWN = "unknown"
STATES = (STATE_LOGIN, STATE_CREATE, STATE_VERIFY, STATE_WIZARD, STATE_SUBMITTED, STATE_CHALLENGE, STATE_UNKNOWN)

# data-automation-id selectors. Marked (live) when seen on the public pages on 2026-10-29.
SELECTORS: dict[str, str] = {
    "apply_button": '[data-automation-id="adventureButton"]',  # live: <a role=button href=.../apply>
    "apply_manually": '[data-automation-id="applyManually"]',  # live
    "autofill": '[data-automation-id="autofillWithResume"]',  # live
    "use_last": '[data-automation-id="useMyLastApplication"]',  # live
    "dialog_close": '[data-automation-id="closeButton"]',  # live
    "flow_page": '[data-automation-id="applyFlowPage"]',  # live
    "active_step": '[data-automation-id="progressBarActiveStep"]',  # live
    "sign_in_with_email": '[data-automation-id="SignInWithEmailButton"]',  # live (optional pre-step)
    "sign_in_content": '[data-automation-id="signInContent"]',  # live
    "email": 'input[data-automation-id="email"]',  # live
    "password": 'input[data-automation-id="password"]',  # live
    "verify_password": 'input[data-automation-id="verifyPassword"]',  # live (create form only)
    "create_checkbox": 'input[data-automation-id="createAccountCheckbox"]',  # live (the required one)
    "create_submit": '[data-automation-id="createAccountSubmitButton"]',  # live
    "create_link": '[data-automation-id="createAccountLink"]',  # live
    "sign_in_submit": '[data-automation-id="signInSubmitButton"]',  # live
    "sign_in_link": '[data-automation-id="signInLink"]',  # live
    "honeypot": 'input[data-automation-id="beecatcher"], input[name="website"]',  # live: never filled
    "next_button": '[data-automation-id="bottom-navigation-next-button"], [data-automation-id="pageFooterNextButton"]',
    "verify_code": ('input[data-automation-id="verificationCode"], input[data-automation-id="verifyCode"], '
                    'input[data-automation-id="code"], input[name="verificationCode"], input[name="code"], '
                    'input[autocomplete="one-time-code"]'),
    "verify_submit": ('[data-automation-id="verifySubmitButton"], [data-automation-id="verifyEmailSubmitButton"], '
                      '[data-automation-id="verifyCodeSubmitButton"], [data-automation-id="submitButton"], '
                      'form button[type="submit"]'),
    "verify_content": '[data-automation-id="verifyEmailContent"], [data-automation-id="verificationContent"]',
    "submitted": ('[data-automation-id="applySuccessPage"], [data-automation-id="successPage"], '
                  '[data-automation-id="applicationSubmitted"]'),
    "error": ('[data-automation-id="errorMessage"], [data-automation-id="alertMessage"], '
              '[data-automation-id="errorBanner"], [role="alert"]'),
    "already_applied": '[data-automation-id="alreadyApplied"]',
}

SUBMITTED_TEXT = re.compile(
    r"thank you for applying|application (?:has been|was) (?:submitted|received)|successfully (?:submitted|applied)|"
    r"you(?:'ve| have) (?:successfully )?applied|your application is complete", re.I)
ALREADY_APPLIED_TEXT = re.compile(r"already applied|already submitted an application|you have applied to this", re.I)
VERIFY_TEXT = re.compile(
    r"verif(?:y|ication) (?:code|your email|email)|check your (?:email|inbox)|code (?:we )?(?:sent|emailed)|"
    r"enter the (?:verification )?code|we(?:'ve| have) sent (?:you )?an? (?:email|code|link)", re.I)
# Deliberately narrow: a job description (an Information Security role says "security check") is on the apply
# pages too, so only wording a bot wall itself uses counts.
CHALLENGE_TEXT = re.compile(
    r"verify (?:that )?you are (?:a )?human|are you a human|access denied|unusual activity|too many requests|"
    r"request blocked|your request has been blocked", re.I)
SIGN_IN_STEP = re.compile(r"create account|sign in", re.I)
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 20000)"
WORKDAY_SENDER = r"myworkday\.com|workday\.com|myworkdayjobs\.com"
MAILBOX_LOGIN_FAILED = "mailbox login failed: fix the app password in Settings"

PasswordRules = tuple[int, str]


def generate_password(length: int = 20) -> str:
    """A fresh account password meeting Workday's published rules (alphabetic, numeric, uppercase, lowercase,
    special, >= 8). Ambiguous glyphs are left out; the result is never logged."""
    length = max(12, int(length))
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnpqrstuvwxyz"
    digits = "23456789"
    special = "!@#$%^&*-_=+?"
    rng = secrets.SystemRandom()
    chars = [rng.choice(upper), rng.choice(lower), rng.choice(digits), rng.choice(special)]
    pool = upper + lower + digits + special
    chars += [rng.choice(pool) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def password_ok(password: str) -> bool:
    return (len(password) >= 8 and any(c.isupper() for c in password) and any(c.islower() for c in password)
            and any(c.isdigit() for c in password) and any(c in string.punctuation for c in password))


# ------------------------------------------------------------------ page helpers


def _visible(page: Any, selector: str) -> Any:
    """The first visible element matching ``selector`` (a comma list), or None."""
    try:
        loc = page.locator(selector)
        n = min(loc.count(), 12)
        for i in range(n):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    except Exception as exc:  # noqa: BLE001 - detached nodes mid-transition / fake page
        log.debug("locator failed", selector=selector[:60], error=str(exc)[:120])
    return None


def _text(page: Any) -> str:
    try:
        text = page.evaluate(_BODY_TEXT_JS)
        if isinstance(text, str):
            return text
    except Exception:  # noqa: BLE001
        pass
    try:
        return re.sub(r"<[^>]+>", " ", page.content() or "")
    except Exception:  # noqa: BLE001
        return ""


def active_step(page: Any, selectors: dict[str, str] | None = None) -> str:
    """The label of the progress bar's active step ("My Information"), "" when no wizard is on the page."""
    sel = {**SELECTORS, **(selectors or {})}
    loc = _visible(page, sel["active_step"])
    if loc is None:
        return ""
    try:
        text = loc.inner_text() or ""
    except Exception:  # noqa: BLE001
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^(current )?step \d+ of \d+$", ln, re.I)]
    return lines[-1] if lines else ""


def classify_page(page: Any, selectors: dict[str, str] | None = None) -> str:
    """login | create | verify | wizard | submitted | challenge | unknown, from the DOM without navigating.

    Order: a CAPTCHA / block wins; a confirmation page; the email verification prompt; the create form
    (``verifyPassword`` visible); the sign-in form or its "Sign in with email" cover; the wizard (progress bar
    past step 1, or the wizard's Next button); otherwise unknown.
    """
    sel = {**SELECTORS, **(selectors or {})}
    try:
        if detect_captcha(page):
            return STATE_CHALLENGE
    except Exception:  # noqa: BLE001 - fake page without evaluate
        pass
    text = _text(page)
    if CHALLENGE_TEXT.search(text):
        return STATE_CHALLENGE
    if (_visible(page, sel["submitted"]) is not None or _visible(page, sel["already_applied"]) is not None
            or SUBMITTED_TEXT.search(text) or ALREADY_APPLIED_TEXT.search(text)):
        return STATE_SUBMITTED
    has_password = _visible(page, sel["password"]) is not None
    if not has_password and (_visible(page, sel["verify_code"]) is not None
                             or _visible(page, sel["verify_content"]) is not None or VERIFY_TEXT.search(text)):
        return STATE_VERIFY
    if _visible(page, sel["verify_password"]) is not None or _visible(page, sel["create_submit"]) is not None:
        return STATE_CREATE
    if has_password or _visible(page, sel["sign_in_submit"]) is not None \
            or _visible(page, sel["sign_in_with_email"]) is not None:
        return STATE_LOGIN
    step = active_step(page, sel)
    if step and not SIGN_IN_STEP.search(step):
        return STATE_WIZARD
    if not step and _visible(page, sel["next_button"]) is not None and _visible(page, sel["flow_page"]) is not None:
        return STATE_WIZARD
    if step and SIGN_IN_STEP.search(step):
        return STATE_LOGIN  # the sign-in step is still rendering its form
    return STATE_UNKNOWN


def page_error(page: Any, selectors: dict[str, str] | None = None) -> str:
    """The visible error banner text, if any ("The email address is already in use")."""
    sel = {**SELECTORS, **(selectors or {})}
    loc = _visible(page, sel["error"])
    if loc is None:
        return ""
    try:
        return re.sub(r"\s+", " ", loc.inner_text() or "").strip()[:300]
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ verification mail


@dataclass(frozen=True)
class Verification:
    kind: str  # code | link
    value: str


def verification_from_mail(mail: Mail) -> Verification | None:
    """A code (preferred) or a verification link out of one mail."""
    code = extract_code(mail.subject + "\n" + mail.body)
    if code:
        return Verification("code", code)
    link = verification_link(mail)
    return Verification("link", link) if link else None


def wait_for_verification(reader: ImapReader, sender: str = WORKDAY_SENDER, timeout: float = 180,
                          poll: float = 5, after: datetime | None = None,
                          registry: CodeRegistry | None = None) -> Verification | None:
    """Poll the mailbox for Workday's verification mail: a code or a link, whichever arrives. ``sender`` is a
    regex (``mail.imap.sender_matches``); the default covers the ``<tenant>@myworkday.com`` senders.

    Every tenant mails from the same pattern, so ``after`` (the moment Create Account / Sign In was clicked)
    rejects the previous tenant's mail from the same run, and ``registry`` skips a code already typed once and
    records the one returned. Without them any Workday mail of the last ten minutes would do."""
    base = mail_filter(sender, after=after)

    def pred(m: Mail) -> bool:
        if not base(m):
            return False
        v = verification_from_mail(m)
        if v is None:
            return False
        return not (v.kind == "code" and registry is not None and registry.is_used(v.value))

    since = after - timedelta(minutes=5) if after is not None else None
    mail = reader.wait_for(pred, timeout=timeout, poll=poll, since=since)
    if not mail:
        return None
    v = verification_from_mail(mail)
    if v is not None and v.kind == "code" and registry is not None:
        registry.mark_used(v.value)
    return v


# ------------------------------------------------------------------ the account flow


PauseFn = Any


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


class WorkdayAccount:
    """Drives step 1 of the apply flow for one tenant inside ``page``.

    ``vault`` is a ``jobapply.vault.Vault`` (None: credentials cannot be read or stored); ``db`` the SQLModel
    session the credentials live in. Every method returns the ``classify_page`` state after it ran.
    """

    def __init__(self, page: Any, user: User, db: Any, tenant: WorkdayTenant, *, vault: Vault | None = None,
                 selectors: dict[str, str] | None = None, pause: PauseFn | None = None,
                 nav_timeout_ms: int = 60_000, settle_ms: int = 2_500):
        self.page = page
        self.user = user
        self.db = db
        self.tenant = tenant
        self.vault = vault
        self.sel = {**SELECTORS, **(selectors or {})}
        self._pause = pause or _human_pause
        self.nav_timeout_ms = nav_timeout_ms
        self.settle_ms = settle_ms
        self.click_timeout_ms = 10_000
        self.created = False  # True once Create Account was clicked in this run
        self.started_at: datetime | None = None  # when Create Account / Sign In was clicked: mail before it is stale

    # -- credentials

    @property
    def email(self) -> str:
        profile = self.user.profile or {}
        mailbox = profile.get("mailbox") or {}
        return str(profile.get("email") or self.user.email or mailbox.get("email") or "").strip()

    def credentials(self) -> dict[str, Any] | None:
        """``{"username", "password", ...}`` for this tenant from the vault, or None."""
        if self.vault is None:
            return None
        try:
            return self.vault.load(self.db, self.user.id, self.tenant.vault_site, "login")
        except Exception as exc:  # noqa: BLE001 - wrong vault key: treat as no credentials, say why
            log.warning("workday credentials unreadable", tenant=self.tenant.tenant, error=str(exc)[:120])
            return None

    def new_credentials(self) -> dict[str, Any]:
        """Generate a password for this tenant and store it in the vault (site ``workday:<tenant>``, kind
        ``login``). Stored before the account is created so a crash after the click cannot lose it, with
        ``created`` False until Create Account was actually clicked (``apply_job`` retries the creation on
        such a row instead of signing in with credentials the tenant never saw). The mailbox password is
        never reused; the value is never logged."""
        if self.vault is None:
            raise RuntimeError("vault not configured: cannot store a Workday account password")
        password = generate_password()
        secret = {"password": password, "tenant": self.tenant.tenant, "site": self.tenant.site,
                  "created_at": utcnow().isoformat(), "verified": False, "created": False}
        self.vault.store(self.db, self.user.id, self.tenant.vault_site, "login", self.email, secret)
        self.db.commit()
        log.info("workday account password generated", user=self.user.username, tenant=self.tenant.tenant)
        return {"username": self.email, **secret}

    def _update_secret(self, **flags: Any) -> None:
        creds = self.credentials()
        if self.vault is None or not creds:
            return
        secret = {k: v for k, v in creds.items() if k != "username"}
        secret.update(flags)
        self.vault.store(self.db, self.user.id, self.tenant.vault_site, "login", creds.get("username") or self.email,
                         secret)
        self.db.commit()

    def mark_created(self) -> None:
        self._update_secret(created=True)

    def mark_verified(self) -> None:
        self._update_secret(verified=True)

    # -- page helpers

    def state(self) -> str:
        return classify_page(self.page, self.sel)

    def _settle(self) -> None:
        self._pause(self.page, self.settle_ms // 2, self.settle_ms)

    def _wait_state(self, timeout_ms: int = 20_000, poll_ms: int = 500) -> str:
        """Poll ``classify_page`` until it is decisive (the SPA renders client-side)."""
        waited = 0
        state = self.state()
        while state == STATE_UNKNOWN and waited < timeout_ms:
            self.page.wait_for_timeout(poll_ms)
            waited += poll_ms
            state = self.state()
        return state

    def _wait_controls(self, keys: tuple[str, ...], timeout_ms: int = 20_000, poll_ms: int = 500) -> bool:
        """Wait until any of the named controls is visible. The sign-in step reports ``login`` from its progress
        bar label while its panel is still a loading skeleton, so the form controls must be awaited before
        anything is read, clicked or typed."""
        waited = 0
        while waited <= timeout_ms:
            if any(_visible(self.page, self.sel[k]) is not None for k in keys):
                return True
            self.page.wait_for_timeout(poll_ms)
            waited += poll_ms
        return False

    def _click(self, key: str, timeout: int | None = None) -> bool:
        """Click a control. Workday's submit buttons sit under a click-filter cover (``aria-hidden``,
        ``tabindex=-2``), so a pointer click can time out on a visible, enabled button; then the element is
        clicked from the DOM, and as a last resort Enter is pressed in the active field."""
        loc = _visible(self.page, self.sel[key])
        if loc is None:
            return False
        timeout = self.click_timeout_ms if timeout is None else timeout
        try:
            loc.click(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001 - intercepted / covered control
            log.info("workday pointer click fell back to a DOM click", control=key, error=str(exc)[:80])
        try:
            loc.evaluate("e => e.click()")
            return True
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.keyboard.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _fill(self, key: str, value: str) -> bool:
        loc = _visible(self.page, self.sel[key])
        if loc is None:
            return False
        loc.click(timeout=5_000)
        loc.fill(value, timeout=5_000)
        return True

    def screenshot(self, path: Path | None) -> str | None:
        if not path:
            return None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])
            return None

    # -- navigation

    def open_apply(self, job_url: str | None = None, *, method: str = "manual") -> str:
        """Open the job's apply flow (``method``: ``manual`` or ``autofill``) and return the page state.

        Goes to the job page, clicks *Apply* (``adventureButton``) and the chosen method in the dialog; when the
        dialog does not show up the method URL is opened directly (both seen live as plain links).
        """
        url = job_url or self.tenant.job_url or self.tenant.base_url
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        status = getattr(resp, "status", None)
        if status in (403, 429):
            return STATE_CHALLENGE
        if status is not None and status >= 400:
            return STATE_UNKNOWN
        self._settle()
        state = self.state()
        if state in (STATE_CHALLENGE, STATE_SUBMITTED):
            return state
        key = "autofill" if method == "autofill" else "apply_manually"
        if _visible(self.page, self.sel[key]) is None and _visible(self.page, self.sel["apply_button"]) is not None:
            self._click("apply_button")
            self._settle()
        if not self._click(key):
            target = self.tenant.autofill_url if method == "autofill" else self.tenant.apply_manually_url
            if target and self.page.url.rstrip("/") != target.rstrip("/"):
                self.page.goto(target, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        self._settle()
        return self._wait_state()

    # -- sign in / create

    def sign_in(self, email: str, password: str) -> str:
        """Type the stored credentials into the sign-in form and submit it. Returns the resulting state
        (``wizard`` on success, ``login`` when Workday rejected them, ``verify`` when it wants the email
        confirmed first)."""
        self._wait_controls(("email", "sign_in_with_email", "sign_in_link"))
        if _visible(self.page, self.sel["email"]) is None:
            if self._click("sign_in_with_email"):
                self._settle()
            elif self._click("sign_in_link"):
                self._settle()
        if not self._fill("email", email) or not self._fill("password", password):
            return self.state()
        self._pause(self.page, 300, 800)
        self.started_at = utcnow()
        if not self._click("sign_in_submit"):
            _visible(self.page, self.sel["password"]).press("Enter")
        self._settle()
        state = self._wait_state()
        if state == STATE_LOGIN:
            err = page_error(self.page, self.sel)
            log.warning("workday sign-in rejected", tenant=self.tenant.tenant, error=err[:120])
        return state

    def create_account(self, email: str, password: str) -> str:
        """Fill the create-account form (email, password twice, the one required checkbox) and submit it.
        The caller decides whether creating is allowed (never under the global dry run)."""
        self._wait_controls(("verify_password", "email", "sign_in_with_email", "create_link"))
        if _visible(self.page, self.sel["verify_password"]) is None:
            if _visible(self.page, self.sel["email"]) is None:
                if self._click("sign_in_with_email"):
                    self._settle()
            if self._click("create_link"):
                self._settle()
        if _visible(self.page, self.sel["verify_password"]) is None:
            return self.state()
        if not (self._fill("email", email) and self._fill("password", password)
                and self._fill("verify_password", password)):
            return self.state()
        box = _visible(self.page, self.sel["create_checkbox"])
        if box is not None:
            try:
                box.check(timeout=5_000)
            except Exception:  # noqa: BLE001 - a styled checkbox: click its label
                box.evaluate("e => { const l = e.closest('label') || e.labels[0]; (l || e).click(); }")
        self._pause(self.page, 300, 800)
        self.started_at = utcnow()
        if not self._click("create_submit"):
            return self.state()
        self.created = True
        self.mark_created()
        self._settle()
        state = self._wait_state()
        if state in (STATE_CREATE, STATE_LOGIN):
            err = page_error(self.page, self.sel)
            log.warning("workday create account rejected", tenant=self.tenant.tenant, error=err[:120])
        return state

    # -- verification

    def enter_verification(self, verification: Verification) -> str:
        """Apply the mailed code (typed into the code input) or link (opened in the same page)."""
        if verification.kind == "link":
            self.page.goto(verification.value, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            self._settle()
            state = self._wait_state()
            if state in (STATE_LOGIN, STATE_UNKNOWN) and self.tenant.apply_manually_url:
                # The link confirmed the address on a separate page: return to the apply flow.
                self.page.goto(self.tenant.apply_manually_url, wait_until="domcontentloaded",
                               timeout=self.nav_timeout_ms)
                self._settle()
                state = self._wait_state()
            return state
        if not self._fill("verify_code", verification.value):
            return self.state()
        self._pause(self.page, 300, 800)
        if not self._click("verify_submit"):
            _visible(self.page, self.sel["verify_code"]).press("Enter")
        self._settle()
        return self._wait_state()

    def verify_email(self, reader: ImapReader, *, timeout: float = 180, poll: float = 5,
                     sender: str = WORKDAY_SENDER, after: datetime | None = None) -> tuple[str, str]:
        """Wait for Workday's verification mail and apply it. Returns ``(state, reason)``; ``reason`` is set
        only when nothing usable arrived ("verification code not received"). Only mail newer than ``after``
        (default: when this account's Create Account / Sign In was clicked) counts, and a code typed once for
        this user is never reused (``CodeRegistry``)."""
        after = after if after is not None else self.started_at
        registry = CodeRegistry(self.db, self.user.id) if self.db is not None and self.user.id is not None else None
        try:
            verification = wait_for_verification(reader, sender=sender, timeout=timeout, poll=poll, after=after,
                                                 registry=registry)
        except imaplib.IMAP4.error as exc:  # the server refused the mailbox login: every job would hit this
            log.warning("mailbox login failed", user=self.user.username, error=str(exc)[:160])
            return self.state(), MAILBOX_LOGIN_FAILED
        except Exception as exc:  # noqa: BLE001 - network failure
            log.warning("mailbox read failed", user=self.user.username, error=str(exc)[:160])
            return self.state(), f"mailbox read failed: {str(exc)[:120]}"
        finally:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass
        if verification is None:
            return self.state(), f"verification code not received within {int(timeout)} s"
        log.info("workday verification mail received", tenant=self.tenant.tenant, kind=verification.kind)
        state = self.enter_verification(verification)
        if state == STATE_VERIFY:
            return state, "verification code not accepted"
        self.mark_verified()
        return state, ""


# ------------------------------------------------------------------ stored accounts (Settings card, CLI)

VAULT_PREFIX = "workday:"


def tenant_accounts(db: Any, user_id: int, vault: Vault | None = None) -> list[dict[str, Any]]:
    """The tenants this user has a stored Workday account for (vault site ``workday:<tenant>``, kind ``login``):
    ``[{"tenant", "username", "verified", "created_at", "updated_at"}]``. Never the password. ``verified`` /
    ``created_at`` need the vault to decrypt the secret; without one (or with the wrong key) they are None."""
    from sqlmodel import select

    from jobapply.models import Credential

    rows = db.exec(select(Credential).where(Credential.user_id == int(user_id), Credential.kind == "login",
                                            Credential.site.like(f"{VAULT_PREFIX}%"))
                   .order_by(Credential.site)).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {"tenant": row.site[len(VAULT_PREFIX):], "username": row.username,
                                 "verified": None, "created_at": None, "updated_at": row.updated_at}
        if vault is not None:
            try:
                secret = vault.decrypt(row.secret_blob)
                entry["verified"] = bool(secret.get("verified"))
                entry["created_at"] = secret.get("created_at")
            except Exception as exc:  # noqa: BLE001 - wrong key: list the tenant, say nothing else
                log.warning("workday account unreadable", tenant=entry["tenant"], error=str(exc)[:120])
        out.append(entry)
    return out


def count_accounts(db: Any, user_id: int) -> int:
    return len(tenant_accounts(db, user_id))


def forget_account(db: Any, user_id: int, tenant: str) -> bool:
    """Delete the stored account for ``tenant`` (the vault row only; the Workday account itself stays and can be
    reset on the tenant's Candidate Home). Returns True when a row was removed."""
    from jobapply.repo import UserScope

    key = f"{VAULT_PREFIX}{(tenant or '').strip().lower()}"
    cred = UserScope(db, int(user_id)).credential(key, "login")
    if cred is None:
        return False
    db.delete(cred)
    db.commit()
    log.info("workday account forgotten", user_id=user_id, tenant=tenant)
    return True


__all__ = [
    "STATES", "STATE_LOGIN", "STATE_CREATE", "STATE_VERIFY", "STATE_WIZARD", "STATE_SUBMITTED", "STATE_CHALLENGE",
    "STATE_UNKNOWN", "SELECTORS", "WORKDAY_SENDER", "MAILBOX_LOGIN_FAILED", "VAULT_PREFIX", "classify_page",
    "active_step", "page_error",
    "generate_password", "password_ok", "Verification", "verification_from_mail", "wait_for_verification",
    "WorkdayAccount", "tenant_accounts", "count_accounts", "forget_account",
]
