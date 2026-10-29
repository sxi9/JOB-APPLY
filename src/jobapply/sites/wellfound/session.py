"""Wellfound session state on top of the persistent browser profile (`jobapply.sites.browser`).

The program never types Wellfound credentials. The profile is logged in by importing a cookie export
(`import_cookies`); `check_session` opens the job board headless and classifies the page; `throttled` keeps the
apply path away for a while after a Cloudflare challenge or a block.

Page classification (recorded live 2026-10-29, logged out; logged-in markup is inferred, see docs/sites/WELLFOUND.md):

- Next.js pages (`/jobs`, `/role/...`) embed an Apollo cache in `<script id="__NEXT_DATA__">` whose
  `ROOT_QUERY.talent.viewer.currentUser` is `null` when logged out and an object when logged in.
- The Rails/React job page (`/jobs/<id>-<slug>`) has no `__NEXT_DATA__`; logged out it renders header buttons
  whose onclick goes to `/login?...` / `/jobs/signup?...` and a Save button pointing at `/jobs/signup`.
- `/login` is a plain form: `form#new_user` with `input#user_email[name=username]`.
- Cloudflare's interstitial is a 403 with `cf-mitigated: challenge`, title "Security Check | Wellfound" and a
  `challenge-platform` script; a Turnstile/captcha may also sit inside a 200 page.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)

SITE = "wellfound"
HOME_URL = "https://wellfound.com/jobs"
LOGIN_URL = "https://wellfound.com/login"
COOKIE_DOMAINS = ("wellfound.com", "angel.co")
COOLDOWN_KEY = "wellfound_cooldown_until"  # user.preferences
DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_DAILY_CAP = 15
BLOCK_STATUSES = (403, 429)

# URL paths that mean "log in first".
LOGIN_PATHS = ("/login", "/join", "/jobs/signup", "/users/sign_in", "/signup", "/candidates/signup")
# Cloudflare / captcha markers. The challenge keeps the original URL, so the DOM and title decide.
CHALLENGE_TITLES = ("security check", "just a moment", "attention required", "access denied")
CHALLENGE_SELECTORS = (
    "#challenge-form", "form#challenge-form", "#challenge-running", "#cf-challenge-running",
    "iframe[src*='challenges.cloudflare.com']", "#turnstile-wrapper", "#cf-chl-widget", ".cf-browser-verification",
)
CHALLENGE_TEXTS = (
    "verify you are human", "checking your browser", "enable javascript and cookies to continue",
    "performance & security by cloudflare", "needs to review the security of your connection",
)
# The login form itself, and the logged-out header / Save button of the job page.
LOGIN_FORM_SELECTORS = ("form#new_user", "input#user_email", "input[name='username'][type='email']")
LOGGED_OUT_SELECTORS = ("button[onclick*='/login']", "button[onclick*='/jobs/signup']", "a[href^='/login']",
                        "a[href^='/jobs/signup']")
# Logged-in markers (inferred from Wellfound's talent navigation; verify against a real session).
LOGGED_IN_SELECTORS = ("a[href='/logout']", "a[href^='/logout']", "[data-test='UserMenu']",
                       "[data-test='NavUserMenu']", "[data-test='ProfileMenu']", "img[alt='Avatar for you']")
# Content containers: the page rendered real content rather than an interstitial.
CONTENT_SELECTORS = ("[data-test='JobListing']", "[data-test='SearchBar']", "#__next [data-test]")
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 20000)"
_NEXT_DATA_JS = "() => { const s = document.getElementById('__NEXT_DATA__'); return s ? s.textContent : ''; }"
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DECISIVE_WAIT_SELECTORS = ", ".join([*LOGGED_IN_SELECTORS, *LOGIN_FORM_SELECTORS, *LOGGED_OUT_SELECTORS,
                                      "#__NEXT_DATA__", "#challenge-form"])


# ------------------------------------------------------------------ page probes (duck-typed, never navigate)


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


def _any(page: Any, selectors: tuple[str, ...]) -> bool:
    return any(_count(page, s) > 0 for s in selectors)


def _title_of(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:  # noqa: BLE001
        return ""


def _path_of(url: str) -> str:
    url = (url or "").lower()
    for host in ("wellfound.com", "angel.co"):
        if host in url:
            return url.split(host, 1)[1] or "/"
    return url


def next_data_of(page: Any) -> dict[str, Any] | None:
    """The parsed `__NEXT_DATA__` JSON of the current page, or None when the page is not a Next.js page."""
    raw = ""
    if callable(getattr(page, "evaluate", None)):
        try:
            raw = page.evaluate(_NEXT_DATA_JS) or ""
        except Exception:  # noqa: BLE001
            raw = ""
    if not raw:
        try:
            m = _NEXT_DATA_RE.search(page.content() or "")
        except Exception:  # noqa: BLE001
            m = None
        raw = m.group(1) if m else ""
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def viewer_of(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """`ROOT_QUERY.talent.viewer` from a Next.js page's Apollo cache (either `apolloState.data` or
    `apolloState` holds the normalised cache)."""
    if not data:
        return None
    props = data.get("props") or {}
    page_props = props.get("pageProps") or {}
    state = page_props.get("apolloState") or props.get("apolloState") or {}
    cache = state.get("data", state) if isinstance(state, dict) else {}
    root = cache.get("ROOT_QUERY") if isinstance(cache, dict) else None
    talent = (root or {}).get("talent") or {}
    viewer = talent.get("viewer")
    return viewer if isinstance(viewer, dict) else None


def classify_next_data(data: dict[str, Any] | None) -> str:
    """ok | login | unknown from the embedded Apollo viewer: `currentUser` set means logged in."""
    viewer = viewer_of(data)
    if viewer is None or "currentUser" not in viewer:
        return "unknown"
    if viewer.get("currentUser") or viewer.get("currentCandidate") or viewer.get("id"):
        return "ok"
    return "login"


def classify_page(page: Any) -> str:
    """ok | login | challenge | unknown from the current URL and DOM, without navigating.

    Order: a captcha / Cloudflare interstitial wins; then the login URL or form; then the Next.js viewer state
    (decisive on `/jobs` and `/role/...`); then the header markers of the Rails job page; then the challenge
    wording on a page that rendered no content.
    """
    path = _path_of(page.url)
    if _detect_captcha(page) or _any(page, CHALLENGE_SELECTORS):
        return "challenge"
    if any(t in _title_of(page).lower() for t in CHALLENGE_TITLES):
        return "challenge"
    if any(path.startswith(p) for p in LOGIN_PATHS) or _any(page, LOGIN_FORM_SELECTORS):
        return "login"
    state = classify_next_data(next_data_of(page))
    if state != "unknown":
        return state
    if _any(page, LOGGED_IN_SELECTORS):
        return "ok"
    if _any(page, LOGGED_OUT_SELECTORS):
        return "login"
    if _any(page, CONTENT_SELECTORS):
        # A rendered job page with neither a Log In button nor a known avatar marker: most likely logged in,
        # but the markers are inferred, so the caller decides (apply.py probes the Apply button next).
        return "unknown"
    text = _page_text(page)
    if any(t in text for t in CHALLENGE_TEXTS):
        return "challenge"
    return "unknown"


# ------------------------------------------------------------------ per-user cooldown (user.preferences)


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
    """True when the apply path should skip Wellfound: a challenge is pending or a cooldown is active."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        return until is None or until > now
    return False


# ------------------------------------------------------------------ session object


class WellfoundSession:
    """Browser context + user + db session, mirroring `LinkedInSession`. `login_status` on the user's
    `wellfound` site row is kept in the dashboard vocabulary: ok | expired | challenge | unknown."""

    def __init__(self, context: Any, user: User, session_db: Any, llm: Any = None, answers: Any = None):
        self.context = context
        self.user = user
        self.db = session_db
        self.llm = llm
        self.answers = answers
        self.scope = UserScope(session_db, user)

    # state ------------------------------------------------------------------
    def _site(self) -> UserSite:
        site = self.scope.site(SITE)
        if site is None:
            site = UserSite(user_id=self.user.id, site=SITE, daily_cap=DEFAULT_DAILY_CAP, enabled=False,
                            login_status="unknown")
            self.db.add(site)
        return site

    @property
    def site(self) -> UserSite:
        return self._site()

    def _record(self, status: str) -> None:
        stored = "expired" if status == "login" else status
        site = self._site()
        site.login_status = stored
        if stored == "ok":
            site.last_login_at = utcnow()
            self.clear_cooldown()
        self.db.add(site)
        if stored == "challenge":
            self.set_cooldown()
        self.db.commit()
        log.info("wellfound session", user=self.user.username, status=stored)

    def record(self, status: str) -> None:
        """Persist a classification (ok | login | challenge | unknown) on the site row."""
        if status == "unknown":
            self._record_unknown()
        else:
            self._record(status)

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

    def _record_unknown(self) -> str:
        site = self._site()
        if site.login_status != "ok":
            self._record("unknown")
        else:
            log.info("wellfound session state unclear, keeping ok", user=self.user.username)
        return "unknown"

    # checks -------------------------------------------------------------------
    def _wait_decisive(self, page: Any, timeout_ms: int, poll_ms: int = 500) -> str:
        status = classify_page(page)
        if status != "unknown":
            return status
        waiter = getattr(page, "wait_for_selector", None)
        if callable(waiter):
            try:
                waiter(_DECISIVE_WAIT_SELECTORS, timeout=timeout_ms, state="attached")
            except Exception:  # noqa: BLE001 - timeout: one last classification below
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
        """Open the job board and classify: ok | login | challenge | unknown. Persists the result.

        `unknown` (timeout, half-rendered page) never overwrites a stored `ok`.
        """
        try:
            resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - TimeoutError or navigation error
            log.warning("wellfound job board did not load", user=self.user.username,
                        error=str(exc).splitlines()[0])
            return self._record_unknown()
        status_code = getattr(resp, "status", None)
        if status_code in BLOCK_STATUSES:
            log.warning("wellfound blocked the job board request", user=self.user.username, status=status_code)
            self._record("challenge")
            return "challenge"
        status = self._wait_decisive(page, timeout_ms)
        if status == "unknown":
            return self._record_unknown()
        self._record(status)
        return status

    def page(self) -> Any:
        return self.context.new_page()


# ------------------------------------------------------------------ module-level helpers for the CLI / wiring


def _sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _launch(pw: Any, user_id: int):
    from jobapply.sites.browser import launch_persistent

    # This machine only runs the bundled Chromium headless; the login comes from imported cookies anyway.
    return launch_persistent(pw, user_id, SITE, headless=True)


def check_session(user_id: int, engine: Any = None, timeout_ms: int = 30_000) -> str:
    """Open the user's persistent Wellfound profile headless and classify the session.

    Returns ok | login | challenge | unknown and records it on the user's `wellfound` site row
    (`login` is stored as `expired`, the dashboard's vocabulary).
    """
    from jobapply.db import session_scope

    with session_scope(engine) as db:
        user = db.get(User, int(user_id))
        if user is None:
            raise ValueError(f"no user with id {user_id}")
        with _sync_playwright() as pw:
            context = _launch(pw, user.id)
            try:
                page = context.new_page()
                return WellfoundSession(context, user, db).check(page, timeout_ms=timeout_ms)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass


def import_cookies(user_id: int, path: Any, engine: Any = None) -> str:
    """Load a browser cookie export (JSON) for wellfound.com into the user's persistent profile, then verify
    the session. Returns the `check_session` status. Cookie values are never logged or returned."""
    from pathlib import Path

    from jobapply.sites.browser import import_cookies_file

    count = import_cookies_file(int(user_id), SITE, Path(path), allowed=COOKIE_DOMAINS)
    log.info("wellfound cookies imported", user_id=user_id, count=count)
    return check_session(int(user_id), engine=engine)


__all__ = ["SITE", "HOME_URL", "LOGIN_URL", "COOKIE_DOMAINS", "COOLDOWN_KEY", "BLOCK_STATUSES", "WellfoundSession",
           "classify_page", "classify_next_data", "next_data_of", "viewer_of", "cooldown_until", "throttled",
           "check_session", "import_cookies"]
