"""LinkedIn session management on top of the persistent browser profile.

The program never types LinkedIn credentials. `login_interactive` opens the login page in the visible
window and waits for the user to sign in (including any email code); `check` classifies whether the
saved profile is still logged in; `throttled` tells the apply pipeline to stay away for a while after
a security challenge.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)

SITE = "linkedin"
FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"
COOLDOWN_KEY = "linkedin_cooldown_until"
DEFAULT_COOLDOWN_HOURS = 6
BLOCK_STATUSES = (429, 999)

# URL fragments that mean the session is gone.
EXPIRED_PATHS = ("/login", "/uas/login", "/authwall", "/checkpoint/lg/login", "/signup")
# URL fragments for security checks (challenge pages share the /checkpoint prefix with the login redirect).
CHALLENGE_PATHS = ("/checkpoint/challenge", "/checkpoint/rm/", "/checkpoint/rp/", "/challenge")
CHALLENGE_TEXTS = (
    "let's do a quick security check",
    "let’s do a quick security check",
    "quick security check",
    "unusual activity",
    "verify your identity",
    "enter the code",
    "we sent a code",
    "security verification",
    "are you a human",
)
# Any of these present means the global nav rendered, i.e. logged in.
LOGGED_IN_SELECTORS = (
    "#global-nav",
    "img.global-nav__me-photo",
    ".global-nav__me",
    "a[href*='/in/'] img[alt*='Photo']",
)
# The inline sign-in form LinkedIn renders on its home page when the cookies are gone.
LOGIN_FORM_SELECTORS = ("#session_key", "form.login__form", "input[name='session_key']")
# Paths where the page text is the user's own content (feed posts, job descriptions), never scanned for
# challenge wording: a post saying "unusual activity" must not flag a challenge.
CONTENT_PATHS = ("/feed", "/jobs", "/in/", "/messaging", "/mynetwork", "/notifications")
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 20000)"
_DECISIVE_WAIT_JS_SELECTORS = ", ".join([*LOGGED_IN_SELECTORS, *LOGIN_FORM_SELECTORS, "#captcha-internal"])


def _detect_captcha(page: Any) -> str:
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
    return url.split("linkedin.com", 1)[1] if "linkedin.com" in url else url


def _title_of(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        return ""


def classify_page(page: Any) -> str:
    """ok | expired | challenge | unknown from the current URL and DOM, without navigating.

    Order matters: a challenge URL or captcha wins; on a content page (feed, job) the rendered nav means
    logged in before any text is looked at, so feed posts cannot trigger a false challenge.
    """
    path = _path_of(page.url)
    if any(p in path for p in CHALLENGE_PATHS):
        return "challenge"
    if _detect_captcha(page):
        return "challenge"
    if any(p in path for p in EXPIRED_PATHS):
        return "expired"
    content_page = any(path.startswith(p) for p in CONTENT_PATHS)
    if content_page and any(_count(page, sel) > 0 for sel in LOGGED_IN_SELECTORS):
        return "ok"
    if any(_count(page, sel) > 0 for sel in LOGIN_FORM_SELECTORS):
        return "expired"
    # LinkedIn redirects a logged-out visitor off /feed to the authwall, so landing on /feed with no
    # login form means logged in even when the nav markup has not rendered yet (common in headless).
    if path.startswith("/feed") and "feed" in (_title_of(page) or "").lower():
        return "ok"
    if not content_page:
        text = _page_text(page)
        if any(t in text for t in CHALLENGE_TEXTS):
            return "challenge"
    if "/checkpoint" in path:
        return "expired"
    # On a content page with no nav yet: likely still rendering or an interstitial.
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


class LinkedInSession:
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
        log.info("linkedin session", user=self.user.username, status=status)

    def record_challenge(self) -> datetime:
        """A challenge / block seen mid-run: status `challenge` plus the cooldown, committed."""
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

    # checks -------------------------------------------------------------------
    def _wait_decisive(self, page: Any, timeout_ms: int, poll_ms: int = 500) -> str:
        """Poll classify_page until it is decisive or the time is up: the feed renders its nav client-side."""
        status = classify_page(page)
        if status != "unknown":
            return status
        waiter = getattr(page, "wait_for_selector", None)
        if callable(waiter):
            try:
                waiter(_DECISIVE_WAIT_JS_SELECTORS, timeout=timeout_ms, state="attached")
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
        """Open the feed and classify the session: ok | expired | challenge | unknown. Persists the result.

        `unknown` (timeout, half-rendered page) never overwrites a stored `ok`: one slow load is not a logout.
        """
        try:
            resp = page.goto(FEED_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - TimeoutError or navigation error
            log.warning("linkedin feed did not load", user=self.user.username, error=str(exc).splitlines()[0])
            return self._record_unknown()
        status_code = getattr(resp, "status", None)
        if status_code in BLOCK_STATUSES:
            log.warning("linkedin blocked the feed request", user=self.user.username, status=status_code)
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
            log.info("linkedin session state unclear, keeping ok", user=self.user.username)
        return "unknown"

    def login_interactive(self, page: Any, timeout_s: int = 600, poll_s: float = 2.0) -> bool:
        """Open the login page and wait for the user to sign in by hand. Never types credentials."""
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - window closed, timeout
            log.warning("linkedin login page did not load", user=self.user.username, error=str(exc).splitlines()[0])
            self._record("unknown")
            return False
        if self._safe_classify(page) == "ok":
            self._record("ok")  # also clears a challenge cooldown
            return True
        msg = (f"Log in to LinkedIn in the browser window as {self.user.username}. "
               f"Type any email/SMS code yourself. Waiting up to {timeout_s} s.")
        print(msg, flush=True)
        log.info("linkedin login: waiting for user", user=self.user.username, timeout_s=timeout_s)
        self._site().login_status = "relogin_requested"
        self.db.commit()
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            status = self._safe_classify(page)
            if status == "closed":
                log.warning("linkedin login window closed", user=self.user.username)
                self._record("expired")
                return False
            if status == "ok":
                self._record("ok")
                print("LinkedIn login detected, session saved.", flush=True)
                return True
            if status == "challenge" and last != "challenge":
                print("LinkedIn is asking for a verification code or security check: complete it in the window.",
                      flush=True)
            last = status
            time.sleep(poll_s)
        log.warning("linkedin login timed out", user=self.user.username)
        self._record("expired")
        return False

    @staticmethod
    def _safe_classify(page: Any) -> str:
        """classify_page, or 'closed' when the page / browser is gone."""
        try:
            return classify_page(page)
        except Exception as exc:  # noqa: BLE001 - "Target page, context or browser has been closed"
            log.debug("classify failed", error=str(exc)[:120])
            return "closed"


def throttled(user: User, site: UserSite | None = None, now: datetime | None = None) -> bool:
    """True when the apply pipeline should skip LinkedIn: a challenge is pending or a cooldown is active."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        # A challenge without a cooldown timestamp (older rows): stay away for the default window.
        return until is None or until > now
    return False


__all__ = ["LinkedInSession", "classify_page", "throttled", "cooldown_until", "SITE", "FEED_URL", "LOGIN_URL",
           "COOLDOWN_KEY", "BLOCK_STATUSES"]
