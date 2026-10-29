"""Naukri session management on top of the persistent browser profile (``jobapply.sites.browser``).

The program never types Naukri credentials. A user logs in once in their own browser, exports the
``naukri.com`` cookies (any "cookie editor" extension, JSON) and ``import_cookies`` loads them into the
tool's persistent profile; ``check_session`` opens the logged-in homepage headless and classifies it;
``throttled`` keeps the apply pipeline away for a while after a CAPTCHA / OTP / block.

Page states (``classify_page``):

- ``ok``         the logged-in nav rendered (profile drawer, "My Naukri" links) on a ``/mnjuser/`` page
- ``login``      the login form / "Login" header link is shown, or the URL is the ``/nlogin/login`` page
- ``challenge``  a CAPTCHA, an OTP prompt, "recaptcha required" / "access denied" / unusual-activity text
- ``unknown``    nothing decisive yet (SPA shell still rendering, interstitial)
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)

SITE = "naukri"
COOKIE_DOMAINS: tuple[str, ...] = ("naukri.com",)
HOME_URL = "https://www.naukri.com/mnjuser/homepage"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
LOGIN_URL = "https://www.naukri.com/nlogin/login"
COOLDOWN_KEY = "naukri_cooldown_until"
DEFAULT_COOLDOWN_HOURS = 6
# 406 is what the search API answers with {"message": "recaptcha required"}; 403/429 are plain blocks.
BLOCK_STATUSES = (403, 406, 429)

# URL fragments that mean the session is gone (Naukri redirects a logged-out /mnjuser/* visit here).
LOGIN_PATHS = ("/nlogin/login", "/nlogin/", "/registration/", "/login")
# URL fragments for security checks.
CHALLENGE_PATHS = ("/captcha", "/verify", "/otp", "/challenge", "/blocked")
CHALLENGE_TEXTS = (
    "recaptcha required",
    "access denied",
    "unusual activity",
    "verify you are human",
    "verify that you are human",
    "are you a human",
    "enter otp",
    "enter the otp",
    "one time password",
    "verification code",
    "too many requests",
    "security check",
)
# Any of these present means the logged-in header rendered.
LOGGED_IN_SELECTORS = (
    "div.nI-gNb-drawer__icon",
    "img.nI-gNb-icon-img",
    "div.nI-gNb-drawer__bars",
    "a[href*='/mnjuser/profile']",
    ".view-profile-wrapper",
    "div.nI-gNb-info__sub-link",
)
# The login form (nlogin page) or the logged-out header's Login / Register links.
LOGIN_FORM_SELECTORS = (
    "input#usernameField",
    "input#passwordField",
    "form[name='login']",
    "a#login_Layer",
    "#login_Layer",
    "a#register_Layer",
)
CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='captcha']",
    ".g-recaptcha",
    "iframe[src*='hcaptcha']",
    "#captcha",
)
# Paths where the page text is content (job descriptions, the user's own profile): never scanned for
# challenge wording, so a JD saying "security check" cannot flag a challenge.
CONTENT_PATHS = ("/mnjuser/", "/job-listings-", "/jobs-in-", "-jobs")
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 20000)"
_DECISIVE_WAIT_SELECTORS = ", ".join([*LOGGED_IN_SELECTORS, *LOGIN_FORM_SELECTORS, *CAPTCHA_SELECTORS])


# ------------------------------------------------------------------ page classification (pure-ish)


def _detect_captcha(page: Any) -> str:
    if any(_count(page, sel) > 0 for sel in CAPTCHA_SELECTORS):
        return "captcha"
    if not callable(getattr(page, "evaluate", None)):
        return ""
    try:
        from jobapply.forms.fields import detect_captcha

        return detect_captcha(page) or ""
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        return ""


def _page_text(page: Any) -> str:
    """Visible body text (first 20 kB), falling back to the raw HTML for pages without `evaluate`."""
    if callable(getattr(page, "evaluate", None)):
        try:
            text = page.evaluate(_BODY_TEXT_JS)
            if isinstance(text, str) and text.strip():
                return text.lower()
        except Exception:  # noqa: BLE001
            pass
    try:
        return (page.content() or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:  # noqa: BLE001
        return 0


def _path_of(url: str) -> str:
    url = (url or "").lower()
    return url.split("naukri.com", 1)[1] if "naukri.com" in url else url


def classify_page(page: Any) -> str:
    """ok | login | challenge | unknown from the current URL and DOM, without navigating.

    Order matters: a challenge URL or captcha wins; on a content page the rendered header means logged
    in before any text is looked at; a login URL or login form means logged out.
    """
    path = _path_of(page.url)
    if any(p in path for p in CHALLENGE_PATHS):
        return "challenge"
    if _detect_captcha(page):
        return "challenge"
    if any(p in path for p in LOGIN_PATHS):
        return "login"
    content_page = any(p in path for p in CONTENT_PATHS)
    if any(_count(page, sel) > 0 for sel in LOGGED_IN_SELECTORS):
        return "ok"
    if any(_count(page, sel) > 0 for sel in LOGIN_FORM_SELECTORS):
        return "login"
    if not content_page:
        text = _page_text(page)
        if any(t in text for t in CHALLENGE_TEXTS):
            return "challenge"
    return "unknown"


# ------------------------------------------------------------------ cooldown


def cooldown_until(user: User) -> datetime | None:
    raw = (user.preferences or {}).get(COOLDOWN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def throttled(user: User, site: UserSite | None = None, now: datetime | None = None) -> bool:
    """True when the apply pipeline should skip Naukri: a challenge is pending or a cooldown is active."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        return until is None or until > now
    return False


# ------------------------------------------------------------------ session object


class NaukriSession:
    """The user's persistent Naukri browser context plus the DB rows that track its login state.

    ``page`` is created lazily from ``context`` (or injected by tests / callers that already hold one).
    """

    def __init__(self, context: Any, user: User, session_db: Any, page: Any = None):
        self.context = context
        self.user = user
        self.db = session_db
        self.scope = UserScope(session_db, user)
        self._page = page

    @property
    def page(self) -> Any:
        if self._page is None:
            if self.context is None:
                raise RuntimeError("NaukriSession has no browser context and no page")
            self._page = self.context.new_page()
        return self._page

    # state ------------------------------------------------------------------
    def _site(self) -> UserSite:
        site = self.scope.site(SITE)
        if site is None:
            site = UserSite(user_id=self.user.id, site=SITE, daily_cap=25, enabled=False, login_status="unknown")
            self.db.add(site)
        return site

    def _record(self, status: str) -> None:
        site = self._site()
        site.login_status = status
        if status == "ok":
            site.last_login_at = utcnow()
            self.clear_cooldown()
        self.db.add(site)
        if status == "challenge":
            self.set_cooldown()
        self.db.commit()
        log.info("naukri session", user=self.user.username, status=status)

    def record_challenge(self) -> datetime:
        """A challenge / block seen mid-run: status `challenge` plus the cooldown, committed."""
        self._record("challenge")
        return cooldown_until(self.user) or utcnow()

    def record_login_required(self) -> None:
        self._record("login")

    def set_cooldown(self, hours: float = DEFAULT_COOLDOWN_HOURS) -> datetime:
        until = utcnow() + timedelta(hours=hours)
        prefs = dict(self.user.preferences or {})
        prefs[COOLDOWN_KEY] = until.isoformat()
        self.user.preferences = prefs
        self.db.add(self.user)
        return until

    def clear_cooldown(self) -> None:
        prefs = dict(self.user.preferences or {})
        if prefs.pop(COOLDOWN_KEY, None) is not None:
            self.user.preferences = prefs
            self.db.add(self.user)

    # checks -------------------------------------------------------------------
    def _wait_decisive(self, page: Any, timeout_ms: int, poll_ms: int = 500) -> str:
        """Poll classify_page until it is decisive or the time is up: the header renders client-side."""
        status = classify_page(page)
        if status != "unknown":
            return status
        waiter = getattr(page, "wait_for_selector", None)
        if callable(waiter):
            try:
                waiter(_DECISIVE_WAIT_SELECTORS, timeout=timeout_ms, state="attached")
            except Exception:  # noqa: BLE001 - timeout: fall through to a last classification
                pass
            return classify_page(page)
        deadline = time.monotonic() + timeout_ms / 1000
        sleeper = getattr(page, "wait_for_timeout", None)
        while status == "unknown" and time.monotonic() < deadline:
            if callable(sleeper):
                sleeper(poll_ms)
            else:
                time.sleep(poll_ms / 1000)
            status = classify_page(page)
        return status

    def check(self, page: Any | None = None, timeout_ms: int = 30_000) -> str:
        """Open the logged-in homepage and classify: ok | login | challenge | unknown. Persists the result.

        `unknown` (timeout, half-rendered page) never overwrites a stored `ok`: one slow load is not a logout.
        """
        page = page if page is not None else self.page
        try:
            resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - TimeoutError or navigation error
            log.warning("naukri homepage did not load", user=self.user.username, error=str(exc).splitlines()[0])
            return self._record_unknown()
        status_code = getattr(resp, "status", None)
        if status_code in BLOCK_STATUSES:
            log.warning("naukri blocked the homepage request", user=self.user.username, status=status_code)
            self._record("challenge")
            return "challenge"
        status = self._wait_decisive(page, timeout_ms)
        if status == "unknown":
            return self._record_unknown()
        self._record(status)
        return status

    def _record_unknown(self) -> str:
        site = self._site()
        if site.login_status != "ok":
            self._record("unknown")
        else:
            log.info("naukri session state unclear, keeping ok", user=self.user.username)
        return "unknown"


# ------------------------------------------------------------------ module-level helpers (uniform interface)


def _load_user(session_db: Any, user_id: int) -> User:
    user = session_db.get(User, int(user_id))
    if user is None:
        raise ValueError(f"no user with id {user_id}")
    return user


def check_session(user_id: int, session_db: Any = None, *, timeout_ms: int = 30_000) -> str:
    """Open the persistent profile of `user_id` HEADLESS, load the homepage and classify the login state.

    Returns ``ok`` | ``login`` | ``challenge`` | ``unknown`` and records it on the user's ``naukri``
    UserSite row. Without `session_db` a short-lived DB session is opened.
    """
    from playwright.sync_api import sync_playwright

    from jobapply.sites.browser import launch_persistent

    def _run(db: Any) -> str:
        user = _load_user(db, user_id)
        with sync_playwright() as pw:
            context = launch_persistent(pw, user.id, SITE, headless=True)
            try:
                return NaukriSession(context, user, db).check(timeout_ms=timeout_ms)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass

    if session_db is not None:
        return _run(session_db)
    from jobapply.db import session_scope

    with session_scope() as db:
        return _run(db)


def import_cookies(user_id: int, path: str | Path, session_db: Any = None) -> str:
    """Load a ``naukri.com`` cookie export into the user's persistent profile, then verify the session.

    The cookie file is read once from disk, filtered to the Naukri domains and set in a headless
    context (``jobapply.sites.browser.import_cookies_file``); values are never logged or returned.
    Returns the ``check_session`` result (``ok`` when the import produced a logged-in profile).
    """
    from jobapply.sites.browser import import_cookies_file

    count = import_cookies_file(int(user_id), SITE, Path(path), allowed=COOKIE_DOMAINS)
    log.info("naukri cookies imported", user_id=user_id, count=count)
    return check_session(user_id, session_db)


__all__ = [
    "SITE", "HOME_URL", "PROFILE_URL", "LOGIN_URL", "COOKIE_DOMAINS", "COOLDOWN_KEY", "BLOCK_STATUSES",
    "NaukriSession", "classify_page", "check_session", "import_cookies", "throttled", "cooldown_until",
]
