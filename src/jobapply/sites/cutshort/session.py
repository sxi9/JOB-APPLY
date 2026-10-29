"""Cutshort session management on top of the persistent browser profile.

The program never types Cutshort credentials. The user exports the site's cookies from a browser they are
logged in to and `import_cookies` loads them into the profile for `user_id`; `check_session` classifies
whether that profile is still logged in; `throttled` tells the apply path to stay away for a while after a
challenge. Everything runs headless (this machine cannot spawn a headed Chromium).

Logged-out behaviour recorded 2026-10-29: the candidate area (``/profile/...``) server-renders an empty shell
and the client then redirects to ``https://cutshort.io/?redirect_url=<path>``, the home page with the auth
modal; ``/login`` is a 404 (login is a modal, "Candidate login" in the unauth header). Logged in, the page
stays on ``/profile/...``. Cloudflare fronts the site, so its interstitial ("Just a moment...") counts as a
challenge.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)

SITE = "cutshort"
HOST = "cutshort.io"
HOME_URL = "https://cutshort.io/"
PROFILE_URL = "https://cutshort.io/profile/all-jobs"
DASHBOARD_URL = "https://cutshort.io/profile/candidate-dashboard"
COOKIE_DOMAINS = (HOST,)
COOLDOWN_KEY = "cutshort_cooldown_until"
DEFAULT_COOLDOWN_HOURS = 6
BLOCK_STATUSES = (403, 429, 503)

# A logged-out visit to the candidate area lands here.
LOGIN_QUERY = "redirect_url="
# Path prefixes of the logged-in candidate app.
PROFILE_PATHS = ("/profile/",)
# Challenge pages: Cloudflare's interstitial, or the site's own verification flows.
CHALLENGE_PATHS = ("/cdn-cgi/challenge", "/challenge", "/verify-otp", "/otp")
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "security check", "verify you are human")
# Body wording of interstitials only: a job description may say "security check" or "unusual activity".
CHALLENGE_TEXTS = ("verify you are human", "checking your browser before accessing", "enter the otp",
                   "enter the code we sent", "we sent a code to", "one-time password")
# The unauth header / auth modal. Any of these visible means the profile is not logged in.
LOGIN_SELECTORS = (
    'button:has-text("Candidate login")',
    'input[type="password"]',
    '[id*="auth-modal"]',
    'button:has-text("Get started")',
)
# The logged-in candidate shell. Present only after the client hydrated with a user.
LOGGED_IN_SELECTORS = (
    'a[href*="/profile/candidate-dashboard"]',
    "#auth-page-wrapper",
    'a[href="/logout"], a[href*="/logout"]',
)
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 20000)"


def _detect_captcha(page: Any) -> str:
    if not callable(getattr(page, "evaluate", None)):
        return ""
    try:
        from jobapply.forms.fields import detect_captcha

        return detect_captcha(page) or ""
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        return ""


def _page_text(page: Any) -> str:
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


def _title_of(page: Any) -> str:
    try:
        return str(page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _parts(url: str) -> tuple[str, str, str]:
    p = urlsplit((url or "").strip())
    return p.netloc.lower(), p.path.lower() or "/", p.query.lower()


def is_login_url(url: str) -> bool:
    """The home page with ``redirect_url=``: where a logged-out visit to the candidate area ends up."""
    host, path, query = _parts(url)
    return HOST in host and LOGIN_QUERY in query and not path.startswith(PROFILE_PATHS)


def classify_page(page: Any) -> str:
    """ok | login | challenge | unknown from the current URL and DOM, without navigating.

    Order: a challenge URL, captcha or Cloudflare title wins; the login redirect / auth modal next; a page in
    the candidate area with no login markers is logged in. Pages outside cutshort.io (fixtures) are judged by
    their DOM only.
    """
    host, path, _query = _parts(page.url)
    on_site = HOST in host
    if on_site and any(path.startswith(p) for p in CHALLENGE_PATHS):
        return "challenge"
    if _detect_captcha(page):
        return "challenge"
    title = _title_of(page)
    if any(t in title for t in CHALLENGE_TITLES):
        return "challenge"
    if is_login_url(page.url):
        return "login"
    if any(_count(page, sel) > 0 for sel in LOGIN_SELECTORS):
        return "login"
    in_profile = on_site and path.startswith(PROFILE_PATHS)
    if in_profile and any(_count(page, sel) > 0 for sel in LOGGED_IN_SELECTORS):
        return "ok"
    if not on_site and any(_count(page, sel) > 0 for sel in LOGGED_IN_SELECTORS):
        return "ok"
    text = _page_text(page)
    if any(t in text for t in CHALLENGE_TEXTS):
        return "challenge"
    if in_profile and text.strip():
        # The candidate area kept its URL and rendered content without any login marker: logged in.
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


def status_for(classification: str) -> str:
    """classify_page vocabulary -> UserSite.login_status vocabulary (unknown | ok | expired | challenge)."""
    return {"ok": "ok", "login": "expired", "challenge": "challenge"}.get(classification, "unknown")


class CutshortSession:
    """One user's Cutshort browser profile plus the bookkeeping on `user_sites`."""

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
        site.login_status = status
        if status == "ok":
            site.last_login_at = utcnow()
            self.clear_cooldown()
        self.db.add(site)
        if status == "challenge":
            self.set_cooldown()
        self.db.commit()
        log.info("cutshort session", user=self.user.username, status=status)

    def record(self, classification: str) -> str:
        """Persist a classify_page result (login -> expired). Returns the stored login_status."""
        status = status_for(classification)
        if status == "unknown":
            return self._record_unknown()
        self._record(status)
        return status

    def record_challenge(self) -> datetime:
        self._record("challenge")
        return cooldown_until(self.user) or utcnow()

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

    def _record_unknown(self) -> str:
        site = self._site()
        if site.login_status != "ok":
            self._record("unknown")
        else:
            log.info("cutshort session state unclear, keeping ok", user=self.user.username)
        return "unknown"

    # checks -------------------------------------------------------------------
    def _wait_decisive(self, page: Any, timeout_ms: int, poll_ms: int = 500) -> str:
        """Poll classify_page until it is decisive or the time is up: the redirect and the shell are client-side."""
        status = classify_page(page)
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
        """Open the candidate area and classify: ok | login | challenge | unknown. Persists the result.

        `unknown` never overwrites a stored `ok`: one slow load is not a logout.
        """
        try:
            resp = page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - TimeoutError or navigation error
            log.warning("cutshort profile page did not load", user=self.user.username, error=str(exc).splitlines()[0])
            self._record_unknown()
            return "unknown"
        status_code = getattr(resp, "status", None)
        if status_code in BLOCK_STATUSES:
            log.warning("cutshort blocked the profile request", user=self.user.username, status=status_code)
            self._record("challenge")
            return "challenge"
        status = self._wait_decisive(page, timeout_ms)
        self.record(status)
        return status


def throttled(user: User, site: UserSite | None = None, now: datetime | None = None) -> bool:
    """True when the apply path should skip Cutshort: a challenge is pending or a cooldown is active."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        return until is None or until > now
    return False


# ------------------------------------------------------------------ module-level helpers (uniform interface)


def _launch(pw: Any, user_id: int):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, SITE, headless=True)


def check_session(user_id: int, timeout_ms: int = 30_000, engine: Any = None) -> str:
    """Open the user's persistent Cutshort profile headless and classify it: ok | login | challenge | unknown.

    Records the result on `user_sites.login_status` (login is stored as `expired`). An unknown user is `unknown`.
    """
    from playwright.sync_api import sync_playwright

    from jobapply.db import session_scope

    with session_scope(engine) as db:
        user = db.get(User, int(user_id))
        if user is None:
            log.warning("cutshort check: no such user", user_id=user_id)
            return "unknown"
        with sync_playwright() as pw:
            context = _launch(pw, user.id)
            try:
                page = context.new_page()
                return CutshortSession(context, user, db).check(page, timeout_ms=timeout_ms)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass


def import_cookies(user_id: int, path: str | Path, engine: Any = None) -> str:
    """Load a browser cookie export (JSON) for cutshort.io into the user's profile, then check the session.

    Only cookies for `cutshort.io` are kept; values are never logged. Returns the classify result.
    """
    from jobapply.sites.browser import import_cookies_file

    count = import_cookies_file(int(user_id), SITE, Path(path), allowed=COOKIE_DOMAINS)
    log.info("cutshort cookies imported", user_id=user_id, count=count)
    return check_session(int(user_id), engine=engine)


__all__ = [
    "BLOCK_STATUSES", "COOKIE_DOMAINS", "COOLDOWN_KEY", "CutshortSession", "DASHBOARD_URL", "HOME_URL",
    "LOGGED_IN_SELECTORS", "LOGIN_SELECTORS", "PROFILE_URL", "SITE", "check_session", "classify_page",
    "cooldown_until", "import_cookies", "is_login_url", "status_for", "throttled",
]
