"""Instahyre session management on top of the persistent browser profile.

The program never types Instahyre credentials. A user logs in by exporting the cookies of a browser where they
are signed in (``import_cookies``), which loads them into the headless persistent profile; ``check_session``
opens the logged-in opportunities page and classifies the saved profile; ``throttled`` tells the apply path to
stay away for a while after a Cloudflare challenge or a block.

Observed 2026-10-29 (logged out): ``/candidate/opportunities/`` answers ``302 /login/?next=...``; the login page
is an AngularJS form (``form[ng-submit="loginUser()"]`` with ``#email`` / ``#password``) and references
reCAPTCHA; the public nav carries ``#nav-user-login`` and ``#nav-user-signup``; a public job page's
"Apply now" button is bound to ``setPostAuthRedirectionUrl()`` (it only sends a visitor to sign-up). Plain
headless Chromium was answered with a Cloudflare interstitial (HTTP 403, title "Just a moment...") on the
very first visit, so a challenge is detected first and always pauses the run.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)

SITE = "instahyre"
BASE_URL = "https://www.instahyre.com"
# Logged-in landing page: a logged-out visitor is redirected to /login/?next=... (observed live).
HOME_URL = f"{BASE_URL}/candidate/opportunities/"
LOGIN_URL = f"{BASE_URL}/login/"
ALLOWED_COOKIE_DOMAINS = ("instahyre.com",)
COOLDOWN_KEY = "instahyre_cooldown_until"
DEFAULT_COOLDOWN_HOURS = 6
# Cloudflare answers its interstitial with 403 / 503; 429 is a plain rate limit. All of them pause the run.
BLOCK_STATUSES = (403, 429, 503)

# URL paths that mean the session is gone (the server redirects there, or the SPA sends the visitor there).
LOGIN_PATHS = ("/login", "/candidates/register", "/candidate/register", "/accounts/login", "/signup", "/join")
# Cloudflare challenge markers: query token on the redirected URL, interstitial markup, page titles.
CHALLENGE_URL_MARKERS = ("__cf_chl", "/cdn-cgi/challenge-platform")
CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#cf-challenge-running",
    "#challenge-running",
    "#challenge-stage",
    ".cf-turnstile",
    "#turnstile-wrapper",
    "iframe[src*='challenges.cloudflare.com']",
    "#cf-error-details",
)
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "security check")
# Rendered only for a signed-in candidate: the apply handlers of the opportunities app, candidate nav links.
LOGGED_IN_SELECTORS = (
    "[ng-click*='submitChoice']",
    "a[href*='/logout']",
    "#nav-user-logout",
    "#nav-candidate-profile",
    "a[href*='/candidate/profile']",
    "a[href*='/candidate/opportunities']",
)
# Rendered only for a visitor: the public nav's login / signup links, the login form, the sign-up redirect
# bound to the public "Apply now" button.
LOGGED_OUT_SELECTORS = (
    "#nav-user-login",
    "#nav-user-signup",
    "form[ng-submit='loginUser()']",
    "input#password[name='password']",
    "[ng-click*='setPostAuthRedirectionUrl']",
)
# Paths the server only serves to a signed-in candidate (a visitor is redirected to /login/).
CANDIDATE_PATHS = ("/candidate/",)
_DECISIVE_WAIT_SELECTORS = ", ".join([*LOGGED_IN_SELECTORS, *LOGGED_OUT_SELECTORS, *CHALLENGE_SELECTORS])


def _detect_captcha(page: Any) -> str:
    if not callable(getattr(page, "evaluate", None)):
        return ""
    try:
        from jobapply.forms.fields import detect_captcha

        return detect_captcha(page) or ""
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        return ""


def _count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:  # noqa: BLE001
        return 0


def _path_of(url: str) -> str:
    url = (url or "").lower()
    return url.split("instahyre.com", 1)[1] if "instahyre.com" in url else url


def _title_of(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        return ""


def is_login_url(url: str) -> bool:
    path = _path_of(url).split("?", 1)[0]
    return any(path.startswith(p) for p in LOGIN_PATHS)


def is_challenge_url(url: str) -> bool:
    return any(m in (url or "").lower() for m in CHALLENGE_URL_MARKERS)


def classify_page(page: Any) -> str:
    """ok | login | challenge | unknown from the current URL and DOM, without navigating.

    A Cloudflare challenge (URL token, interstitial markup, captcha widget, "Just a moment" title) wins over
    everything. A login / sign-up URL means logged out. Then the DOM decides: the signed-in apply handlers or
    candidate nav mean ok; the public login / signup nav or the login form mean logged out; a page under
    /candidate/ that was not redirected away is ok even before its nav renders. Anything else is unknown.
    """
    url = page.url
    path = _path_of(url).split("?", 1)[0]
    if is_challenge_url(url):
        return "challenge"
    if any(_count(page, sel) > 0 for sel in CHALLENGE_SELECTORS):
        return "challenge"
    if _detect_captcha(page):
        return "challenge"
    title = _title_of(page).lower()
    if any(t in title for t in CHALLENGE_TITLES):
        return "challenge"
    if any(path.startswith(p) for p in LOGIN_PATHS):
        return "login"
    if any(_count(page, sel) > 0 for sel in LOGGED_IN_SELECTORS):
        return "ok"
    if any(_count(page, sel) > 0 for sel in LOGGED_OUT_SELECTORS):
        return "login"
    if any(path.startswith(p) for p in CANDIDATE_PATHS):
        return "ok"
    return "unknown"


def cooldown_until(user: User) -> datetime | None:
    raw = (user.preferences or {}).get(COOLDOWN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# login_status values stored on the UserSite row follow the dashboard vocabulary (ok | expired | challenge |
# unknown); classify_page's "login" is stored as "expired".
_STORED_STATUS = {"login": "expired"}


class InstahyreSession:
    def __init__(self, context: Any, user: User, session_db: Any):
        self.context = context
        self.user = user
        self.db = session_db
        self.scope = UserScope(session_db, user)

    # state ------------------------------------------------------------------
    def _site(self) -> UserSite:
        site = self.scope.site(SITE)
        if site is None:
            site = UserSite(user_id=self.user.id, site=SITE, daily_cap=15, enabled=False, login_status="unknown")
            self.db.add(site)
        return site

    def _record(self, status: str) -> None:
        site = self._site()
        site.login_status = _STORED_STATUS.get(status, status)
        if status == "ok":
            site.last_login_at = utcnow()
            self.clear_cooldown()
        self.db.add(site)
        if status == "challenge":
            self.set_cooldown()
        self.db.commit()
        log.info("instahyre session", user=self.user.username, status=status)

    def record_challenge(self) -> datetime:
        """A challenge / block seen mid-run: status `challenge` plus the cooldown, committed."""
        self._record("challenge")
        return cooldown_until(self.user) or utcnow()

    def record_login_needed(self) -> None:
        """A login page seen mid-run: the stored session is gone."""
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
        """Poll classify_page until it is decisive or the time is up: the SPA renders its nav client-side."""
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

    def check(self, page: Any, timeout_ms: int = 30_000) -> str:
        """Open the opportunities page and classify the session: ok | login | challenge | unknown. Persists it.

        `unknown` (timeout, half-rendered page) never overwrites a stored `ok`: one slow load is not a logout.
        """
        try:
            resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - TimeoutError or navigation error
            log.warning("instahyre opportunities page did not load", user=self.user.username,
                        error=str(exc).splitlines()[0])
            return self._record_unknown()
        status_code = getattr(resp, "status", None)
        if status_code in BLOCK_STATUSES:
            log.warning("instahyre blocked the opportunities request", user=self.user.username, status=status_code)
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
            log.info("instahyre session state unclear, keeping ok", user=self.user.username)
        return "unknown"

    @staticmethod
    def safe_classify(page: Any) -> str:
        """classify_page, or 'unknown' when the page / browser is gone."""
        try:
            return classify_page(page)
        except Exception as exc:  # noqa: BLE001 - "Target page, context or browser has been closed"
            log.debug("classify failed", error=str(exc)[:120])
            return "unknown"


def throttled(user: User, site: UserSite | None = None, now: datetime | None = None) -> bool:
    """True when the apply path should skip Instahyre: a challenge is pending or a cooldown is active."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        # A challenge without a cooldown timestamp (older rows): stay away for the default window.
        return until is None or until > now
    return False


# ------------------------------------------------------------------ uniform helpers (headless, no credentials)


@contextmanager
def _browser_context(user_id: int) -> Iterator[Any]:
    """The user's persistent Instahyre profile in HEADLESS Chromium (this machine cannot spawn a headed one)."""
    from playwright.sync_api import sync_playwright

    from jobapply.sites.browser import launch_persistent

    with sync_playwright() as pw:
        context = launch_persistent(pw, user_id, SITE, headless=True)
        try:
            yield context
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


def _import_cookies_file(user_id: int, path: Path) -> int:
    from jobapply.sites.browser import import_cookies_file

    return import_cookies_file(user_id, SITE, path, allowed=ALLOWED_COOKIE_DOMAINS)


def check_session(user_id: int, engine: Any = None, timeout_ms: int = 30_000) -> str:
    """Open the user's persistent profile headless, visit the opportunities page and classify it.

    Returns ok | login | challenge | unknown and records login_status on the user's Instahyre site row.
    """
    from jobapply.db import session_scope

    with session_scope(engine) as db:
        user = db.get(User, int(user_id))
        if user is None:
            raise ValueError(f"no user with id {user_id}")
        with _browser_context(user.id) as context:
            page = context.new_page()
            status = InstahyreSession(context, user, db).check(page, timeout_ms=timeout_ms)
    log.info("instahyre session checked", user_id=user_id, status=status)
    return status


def import_cookies(user_id: int, path: str | Path, engine: Any = None) -> str:
    """Load a cookie export (browser-extension JSON) into the persistent profile, then check the session.

    Only cookies for instahyre.com are kept; values are never logged. Raises ValueError when the file holds no
    Instahyre cookies. Returns the session status after the import (ok | login | challenge | unknown).
    """
    count = _import_cookies_file(int(user_id), Path(path))
    log.info("instahyre cookies imported", user_id=user_id, count=count)
    return check_session(user_id, engine=engine)


__all__ = ["InstahyreSession", "classify_page", "check_session", "import_cookies", "throttled", "cooldown_until",
           "is_login_url", "is_challenge_url", "SITE", "BASE_URL", "HOME_URL", "LOGIN_URL", "COOLDOWN_KEY",
           "BLOCK_STATUSES", "ALLOWED_COOKIE_DOMAINS"]
