"""LinkedIn session management and browser profile helpers. No real browser is launched."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from jobapply.config import get_settings
from jobapply.models import Role
from jobapply.repo import UserScope
from jobapply.sites import browser
from jobapply.sites.linkedin import session as ls
from jobapply.users import create_user


# fakes ------------------------------------------------------------------------
class FakeLocator:
    def __init__(self, n: int):
        self.n = n

    def count(self) -> int:
        return self.n


class FakeResponse:
    def __init__(self, status: int):
        self.status = status


class FakePage:
    """Duck-typed Playwright page: url, goto, locator(...).count(), content(), evaluate(), wait_for_timeout()."""

    def __init__(self, url: str = "about:blank", html: str = "", nav_present: bool = False,
                 redirect: dict[str, str] | None = None, captcha: str = "", fail_goto: Exception | None = None,
                 login_form: bool = False, status: int = 200):
        self.url = url
        self.html = html
        self.nav_present = nav_present
        self.login_form = login_form
        self.redirect = redirect or {}
        self.captcha = captcha
        self.fail_goto = fail_goto
        self.status = status
        self.visited: list[str] = []
        self.polls = 0
        self.waited_ms = 0

    def goto(self, url: str, **kw) -> FakeResponse:
        self.visited.append(url)
        if self.fail_goto:
            raise self.fail_goto
        self.url = self.redirect.get(url, url)
        return FakeResponse(self.status)

    def locator(self, selector: str) -> FakeLocator:
        if self.nav_present and selector in ls.LOGGED_IN_SELECTORS:
            return FakeLocator(1)
        if self.login_form and selector in ls.LOGIN_FORM_SELECTORS:
            return FakeLocator(1)
        return FakeLocator(0)

    def content(self) -> str:
        return self.html

    def evaluate(self, js: str):
        if "innerText" in js:  # visible text, as the real page would give it
            return re.sub(r"<[^>]+>", " ", self.html)
        return self.captcha

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


@pytest.fixture
def user(session):
    u = create_user(session, "alice", "pw12345678", role=Role.user)
    session.commit()
    return u


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    return tmp_path / "data" / "browser-profiles"


# browser helpers ------------------------------------------------------------------
def test_profile_dir_layout(profiles_dir):
    p = browser.profile_dir(7, "linkedin")
    assert p == profiles_dir / "7" / "linkedin"
    assert p.is_dir()
    assert browser.profile_dir(7, "naukri").parent == p.parent
    with pytest.raises(ValueError):
        browser.profile_dir(7, "../evil")


@pytest.mark.parametrize("hhmm,spec,expected", [
    ("08:00", "08:00-23:00", True),
    ("07:59", "08:00-23:00", False),
    ("22:59", "08:00-23:00", True),
    ("23:00", "08:00-23:00", False),
    ("02:00", "22:00-06:00", True),   # wraps midnight
    ("12:00", "22:00-06:00", False),
    ("12:00", "00:00-00:00", True),   # empty window means always
])
def test_is_active_hours(hhmm, spec, expected):
    h, m = map(int, hhmm.split(":"))
    assert browser.is_active_hours(datetime(2026, 10, 29, h, m), spec) is expected


def test_is_active_hours_rejects_garbage():
    with pytest.raises(ValueError):
        browser.is_active_hours(datetime.now(), "morning")


def test_human_pause_uses_page_timer_and_jitter():
    class P:
        waited: list[int] = []

        def wait_for_timeout(self, ms):
            self.waited.append(ms)

    p = P()
    slept = {browser.human_pause(p, 100, 300) for _ in range(20)}
    assert all(100 <= s <= 300 for s in slept)
    assert len(p.waited) == 20


def test_launch_persistent_builds_persistent_context(profiles_dir, monkeypatch):
    calls: list[dict] = []

    class Ctx:
        def add_init_script(self, js):
            self.js = js

    class Chromium:
        def launch_persistent_context(self, **kw):
            calls.append(kw)
            return Ctx()

    class PW:
        chromium = Chromium()

    monkeypatch.setattr(get_settings(), "chromium_executable", None)
    browser.launch_persistent(PW(), 3, "linkedin", headless=True)
    kw = calls[0]
    assert kw["user_data_dir"] == str(profiles_dir / "3" / "linkedin")
    assert kw["headless"] is True
    # no user_agent override: the bundled build's own UA matches its client hints / userAgentData
    assert "user_agent" not in kw
    assert kw["locale"] == "en-US" and kw["timezone_id"]
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert kw["ignore_default_args"] == ["--enable-automation"]
    # No channel and no executable_path: Playwright's bundled full Chromium drives the persistent profile
    # (the "chromium" channel is not always installed and spawns unreliably on Windows).
    assert "executable_path" not in kw and "channel" not in kw
    browser.launch_persistent(PW(), 3, "linkedin", headless=False)
    assert "channel" not in calls[1]


def test_launch_persistent_falls_back_to_system_chrome(profiles_dir, monkeypatch):
    calls: list[dict] = []

    class Ctx:
        def add_init_script(self, js):
            pass

    class Chromium:
        def launch_persistent_context(self, **kw):
            calls.append(kw)
            if "channel" not in kw:
                raise RuntimeError("Executable doesn't exist at C:\\...\\chrome.exe")
            return Ctx()

    class PW:
        chromium = Chromium()

    monkeypatch.setattr(get_settings(), "chromium_executable", None)
    browser.launch_persistent(PW(), 3, "linkedin", headless=False)
    assert len(calls) == 2 and calls[1]["channel"] == "chrome"


# check() ------------------------------------------------------------------------------
def _check(session, user, page, timeout_ms: int = 200) -> str:
    return ls.LinkedInSession(context=None, user=user, session_db=session).check(page, timeout_ms=timeout_ms)


def test_check_ok(session, user):
    page = FakePage(nav_present=True)
    assert _check(session, user, page) == "ok"
    assert page.visited == [ls.FEED_URL]
    site = UserScope(session, user).site("linkedin")
    assert site.login_status == "ok" and site.last_login_at is not None


@pytest.mark.parametrize("target", [
    "https://www.linkedin.com/login?fromSignIn=true",
    "https://www.linkedin.com/uas/login?session_redirect=%2Ffeed%2F",
    "https://www.linkedin.com/authwall?trk=qf",
    "https://www.linkedin.com/checkpoint/lg/login-submit",
])
def test_check_expired(session, user, target):
    page = FakePage(redirect={ls.FEED_URL: target})
    assert _check(session, user, page) == "expired"
    assert UserScope(session, user).site("linkedin").login_status == "expired"


def test_check_challenge_by_url(session, user):
    page = FakePage(redirect={ls.FEED_URL: "https://www.linkedin.com/checkpoint/challenge/AgF3?x=1"})
    assert _check(session, user, page) == "challenge"


def test_check_challenge_by_text_and_captcha(session, user):
    page = FakePage(redirect={ls.FEED_URL: "https://www.linkedin.com/checkpoint/rm/sign-in"},
                    html="<h1>Let's do a quick security check</h1>")
    assert _check(session, user, page) == "challenge"
    page = FakePage(nav_present=True, captcha='iframe[src*="recaptcha"]')
    assert _check(session, user, page) == "challenge"
    site = UserScope(session, user).site("linkedin")
    assert site.login_status == "challenge"
    assert ls.cooldown_until(user) > datetime.now(UTC)


def test_check_unknown_on_timeout(session, user):
    page = FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))
    assert _check(session, user, page) == "unknown"
    assert UserScope(session, user).site("linkedin").login_status == "unknown"


def test_check_feed_without_nav_is_unknown(session, user):
    page = FakePage(nav_present=False)
    assert _check(session, user, page) == "unknown"
    assert page.waited_ms >= 200  # polled for the client-side nav before giving up


def test_check_keeps_ok_on_transient_unknown(session, user):
    assert _check(session, user, FakePage(nav_present=True)) == "ok"
    site = UserScope(session, user).site("linkedin")
    # a timeout or a half-rendered feed does not downgrade a verified session
    assert _check(session, user, FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))) == "unknown"
    assert site.login_status == "ok"
    assert _check(session, user, FakePage(nav_present=False)) == "unknown"
    assert site.login_status == "ok"


class LatePage(FakePage):
    """The nav appears only after a few polls (SPA shell painting after domcontentloaded)."""

    def __init__(self, after: int):
        super().__init__()
        self.after = after

    def locator(self, selector):
        self.polls += 1
        return FakeLocator(1 if self.polls > self.after and selector in ls.LOGGED_IN_SELECTORS else 0)


def test_check_waits_for_the_nav_to_render(session, user):
    assert _check(session, user, LatePage(after=6), timeout_ms=5000) == "ok"


def test_feed_post_text_is_not_a_challenge(session, user):
    html = ("<nav id='global-nav'></nav><div class='feed-post'>LinkedIn flagged unusual activity on my account "
            "and asked me to verify your identity, enter the code...</div>")
    page = FakePage(url=ls.FEED_URL, nav_present=True, html=html)
    assert ls.classify_page(page) == "ok"
    assert _check(session, user, page) == "ok"
    assert ls.cooldown_until(user) is None
    # the same wording on a non-content page (checkpoint interstitial without the URL hint) still counts
    page = FakePage(url="https://www.linkedin.com/some-interstitial", html=html)
    assert ls.classify_page(page) == "challenge"


def test_home_page_with_login_form_is_expired(session, user):
    page = FakePage(redirect={ls.FEED_URL: "https://www.linkedin.com/"}, login_form=True)
    assert _check(session, user, page) == "expired"
    assert UserScope(session, user).site("linkedin").login_status == "expired"


def test_check_treats_999_as_a_challenge(session, user):
    page = FakePage(nav_present=True, status=999)
    assert _check(session, user, page) == "challenge"
    assert UserScope(session, user).site("linkedin").login_status == "challenge"
    assert ls.cooldown_until(user) > datetime.now(UTC)


# login_interactive() -----------------------------------------------------------------
class FlipPage(FakePage):
    """Lands on /login, then after N polls the URL flips to /feed with the nav present."""

    def __init__(self, flip_after: int):
        super().__init__()
        self.flip_after = flip_after
        self._url = ls.LOGIN_URL

    @property
    def url(self) -> str:  # each classify_page() reads the URL once: that is one poll
        self.polls += 1
        if self.polls > self.flip_after:
            self._url = ls.FEED_URL
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    def goto(self, url, **kw):
        self.visited.append(url)
        self._url = url

    def locator(self, selector):
        return FakeLocator(1 if self._url == ls.FEED_URL and selector in ls.LOGGED_IN_SELECTORS else 0)


def test_login_interactive_success(session, user, monkeypatch, capsys):
    sleeps: list[float] = []
    monkeypatch.setattr(ls.time, "sleep", lambda s: sleeps.append(s))
    page = FlipPage(flip_after=3)
    user.preferences = {ls.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    ok = ls.LinkedInSession(None, user, session).login_interactive(page, timeout_s=60)
    assert ok is True
    assert page.visited[0] == ls.LOGIN_URL
    assert sleeps and all(s == 2.0 for s in sleeps)
    site = UserScope(session, user).site("linkedin")
    assert site.login_status == "ok" and site.last_login_at is not None
    assert ls.COOLDOWN_KEY not in user.preferences
    out = capsys.readouterr().out
    assert "Log in to LinkedIn in the browser window" in out


def test_login_interactive_already_logged_in_clears_cooldown(session, user):
    page = FakePage(redirect={ls.LOGIN_URL: ls.FEED_URL}, nav_present=True)
    user.preferences = {ls.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=5)).isoformat()}
    site = UserScope(session, user).site("linkedin")
    site.login_status = "challenge"
    session.add(site)
    session.commit()
    assert ls.LinkedInSession(None, user, session).login_interactive(page, timeout_s=5) is True
    assert site.login_status == "ok" and ls.COOLDOWN_KEY not in (user.preferences or {})
    assert ls.throttled(user, site) is False


def test_login_interactive_survives_a_closed_window(session, user, monkeypatch):
    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    page = FlipPage(flip_after=10_000)
    calls = {"n": 0}
    original = type(page).url

    def closing_url(self):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("Target page, context or browser has been closed")
        return original.fget(self)

    monkeypatch.setattr(FlipPage, "url", property(closing_url, original.fset))
    ok = ls.LinkedInSession(None, user, session).login_interactive(page, timeout_s=60)
    assert ok is False
    assert UserScope(session, user).site("linkedin").login_status == "expired"
    # a login page that never loads is reported, not raised
    page = FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))
    assert ls.LinkedInSession(None, user, session).login_interactive(page, timeout_s=1) is False


def test_login_interactive_timeout(session, user, monkeypatch):
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(ls.time, "sleep", lambda s: None)
    monkeypatch.setattr(ls.time, "monotonic", fake_monotonic)
    page = FlipPage(flip_after=10_000)
    ok = ls.LinkedInSession(None, user, session).login_interactive(page, timeout_s=5)
    assert ok is False
    assert UserScope(session, user).site("linkedin").login_status == "expired"


# throttled() -------------------------------------------------------------------------
def test_throttled_cooldown(session, user):
    site = UserScope(session, user).site("linkedin")
    assert ls.throttled(user, site) is False
    user.preferences = {ls.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert ls.throttled(user, site) is True
    user.preferences = {ls.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert ls.throttled(user, site) is False
    # challenge recorded without a timestamp: stay away
    user.preferences = {}
    site.login_status = "challenge"
    assert ls.throttled(user, site) is True
    # expired cooldown after a challenge lets the next run retry
    user.preferences = {ls.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    assert ls.throttled(user, site) is False
    user.preferences = {ls.COOLDOWN_KEY: "not a date"}
    site.login_status = "ok"
    assert ls.throttled(user) is False


# CLI -------------------------------------------------------------------------------
def test_cli_linkedin_help():
    import typer.main

    from jobapply.cli import app
    from jobapply.cli_linkedin import linkedin_app

    # Registered on the root CLI (resolved through click so the root callback, which reconfigures
    # logging for the whole process, does not run inside the test).
    assert "linkedin" in typer.main.get_command(app).commands
    result = CliRunner().invoke(linkedin_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "check", "search", "apply"):
        assert cmd in result.output
