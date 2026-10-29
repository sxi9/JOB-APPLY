"""Cutshort session classification and bookkeeping. No real browser is launched."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobapply.models import Role
from jobapply.repo import UserScope
from jobapply.sites import browser
from jobapply.sites.cutshort import session as css
from jobapply.users import create_user


class FakeLocator:
    def __init__(self, n: int):
        self.n = n

    def count(self) -> int:
        return self.n


class FakeResponse:
    def __init__(self, status: int):
        self.status = status


class FakePage:
    """Duck-typed Playwright page: url, goto, locator(...).count(), title(), content(), evaluate()."""

    def __init__(self, url: str = "about:blank", html: str = "", title: str = "Cutshort", logged_in: bool = False,
                 login_form: bool = False, redirect: dict[str, str] | None = None, captcha: str = "",
                 fail_goto: Exception | None = None, status: int = 200):
        self.url, self.html, self._title = url, html, title
        self.logged_in, self.login_form = logged_in, login_form
        self.redirect = redirect or {}
        self.captcha, self.fail_goto, self.status = captcha, fail_goto, status
        self.visited: list[str] = []
        self.waited_ms = 0

    def goto(self, url: str, **kw) -> FakeResponse:
        self.visited.append(url)
        if self.fail_goto:
            raise self.fail_goto
        self.url = self.redirect.get(url, url)
        return FakeResponse(self.status)

    def locator(self, selector: str) -> FakeLocator:
        if self.logged_in and selector in css.LOGGED_IN_SELECTORS:
            return FakeLocator(1)
        if self.login_form and selector in css.LOGIN_SELECTORS:
            return FakeLocator(1)
        return FakeLocator(0)

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self.html

    def evaluate(self, js: str):
        if "innerText" in js:
            return re.sub(r"<[^>]+>", " ", self.html)
        return self.captcha

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


@pytest.fixture
def user(session):
    u = create_user(session, "alice", "pw12345678", role=Role.user)
    session.commit()
    return u


# classify_page -------------------------------------------------------------------------
def test_classify_login_redirect_and_markers():
    assert css.classify_page(FakePage("https://cutshort.io/?redirect_url=%2Fprofile%2Fall-jobs%3Fjobid%3D1")) == "login"
    assert css.is_login_url("https://cutshort.io/?redirect_url=/profile/all-jobs")
    assert not css.is_login_url("https://cutshort.io/profile/all-jobs?redirect_url=x")  # still inside the app
    assert not css.is_login_url("https://example.com/?redirect_url=x")
    # the unauth header / auth modal on any page
    assert css.classify_page(FakePage("https://cutshort.io/job/Dev-Acme-x1", login_form=True)) == "login"
    assert css.classify_page(FakePage("file:///tmp/fixture.html", login_form=True)) == "login"


def test_classify_ok_in_candidate_area():
    assert css.classify_page(FakePage(css.PROFILE_URL, logged_in=True)) == "ok"
    # stayed on /profile/ with content but without the shell markers: still logged in (no redirect happened)
    assert css.classify_page(FakePage(css.DASHBOARD_URL, html="<main>Recommended jobs for you</main>")) == "ok"
    # an empty shell on /profile/ (client still hydrating, redirect pending): undecided
    assert css.classify_page(FakePage(css.PROFILE_URL)) == "unknown"
    # a logged-in marker on a fixture page outside cutshort.io is trusted too
    assert css.classify_page(FakePage("file:///tmp/profile.html", logged_in=True)) == "ok"
    # a public job page with neither marker is not a session signal
    assert css.classify_page(FakePage("https://cutshort.io/job/Dev-Acme-x1", html="<h1>Dev</h1>")) == "unknown"


def test_classify_challenge():
    assert css.classify_page(FakePage("https://cutshort.io/cdn-cgi/challenge-platform/x")) == "challenge"
    captcha = FakePage(css.PROFILE_URL, logged_in=True, captcha='iframe[src*="recaptcha"]')
    assert css.classify_page(captcha) == "challenge"
    assert css.classify_page(FakePage("https://cutshort.io/", title="Just a moment...")) == "challenge"
    otp = FakePage("https://cutshort.io/verify", html="<p>Enter the OTP sent to your phone</p>")
    assert css.classify_page(otp) == "challenge"
    # a job description mentioning the words, on a logged-in page, is content
    page = FakePage(css.PROFILE_URL, logged_in=True, html="<p>You will investigate unusual activity reports</p>")
    assert css.classify_page(page) == "ok"
    assert css.status_for("login") == "expired" and css.status_for("ok") == "ok"
    assert css.status_for("challenge") == "challenge" and css.status_for("weird") == "unknown"


# CutshortSession.check ---------------------------------------------------------------------
def _check(session, user, page, timeout_ms: int = 200) -> str:
    return css.CutshortSession(context=None, user=user, session_db=session).check(page, timeout_ms=timeout_ms)


def test_check_ok_records_login(session, user):
    page = FakePage(logged_in=True)
    assert _check(session, user, page) == "ok"
    assert page.visited == [css.PROFILE_URL]
    site = UserScope(session, user).site("cutshort")
    assert site.login_status == "ok" and site.last_login_at is not None


def test_check_login_redirect_is_expired(session, user):
    page = FakePage(redirect={css.PROFILE_URL: "https://cutshort.io/?redirect_url=%2Fprofile%2Fall-jobs"})
    assert _check(session, user, page) == "login"
    assert UserScope(session, user).site("cutshort").login_status == "expired"


def test_check_challenge_sets_cooldown(session, user):
    page = FakePage(redirect={css.PROFILE_URL: "https://cutshort.io/profile/all-jobs"}, title="Just a moment...")
    assert _check(session, user, page) == "challenge"
    site = UserScope(session, user).site("cutshort")
    assert site.login_status == "challenge" and css.cooldown_until(user) > datetime.now(UTC)
    assert css.throttled(user, site) is True
    # a blocked status code is a challenge without reading the page
    page = FakePage(logged_in=True, status=403)
    assert _check(session, user, page) == "challenge"


def test_check_unknown_keeps_ok(session, user):
    assert _check(session, user, FakePage(logged_in=True)) == "ok"
    site = UserScope(session, user).site("cutshort")
    assert _check(session, user, FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))) == "unknown"
    assert site.login_status == "ok"
    page = FakePage()  # empty shell that never hydrates
    assert _check(session, user, page) == "unknown" and page.waited_ms >= 200
    assert site.login_status == "ok"
    # and without a verified session the unknown is recorded
    site.login_status = "expired"
    session.add(site)
    session.commit()
    assert _check(session, user, FakePage()) == "unknown"
    assert site.login_status == "unknown"


def test_throttled(session, user):
    site = UserScope(session, user).site("cutshort")
    assert css.throttled(user, site) is False
    user.preferences = {css.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert css.throttled(user, site) is True
    user.preferences = {css.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert css.throttled(user, site) is False
    user.preferences = {}
    site.login_status = "challenge"
    assert css.throttled(user, site) is True
    user.preferences = {css.COOLDOWN_KEY: "not a date"}
    site.login_status = "ok"
    assert css.throttled(user) is False
    cs = css.CutshortSession(None, user, session)
    cs.record_challenge()
    assert css.throttled(user, site) is True
    assert cs.record("ok") == "ok" and css.COOLDOWN_KEY not in (user.preferences or {})


# module-level helpers ---------------------------------------------------------------------
class _Ctx:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _PW:
    def __enter__(self):
        return "pw"

    def __exit__(self, *a):
        return False


def test_check_session_and_import_cookies_use_headless_profile(session, engine, user, monkeypatch, tmp_path):
    launches: list[tuple] = []
    page = FakePage(logged_in=True)
    ctx = _Ctx(page)

    def fake_launch(pw, user_id, site, headless=None, viewport=(1280, 900)):
        launches.append((user_id, site, headless))
        return ctx

    monkeypatch.setattr(browser, "launch_persistent", fake_launch)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())
    assert css.check_session(user.id, engine=engine) == "ok"
    assert launches == [(user.id, "cutshort", True)] and ctx.closed is True
    assert page.visited == [css.PROFILE_URL]
    assert css.check_session(999_999, engine=engine) == "unknown"  # no such user, no browser
    assert len(launches) == 1

    imported: list[tuple] = []

    def fake_import(user_id, site, path, allowed=("linkedin.com",)):
        imported.append((user_id, site, Path(path), allowed))
        return 3

    monkeypatch.setattr(browser, "import_cookies_file", fake_import)
    cookie_file = tmp_path / "cutshort-cookies.json"
    cookie_file.write_text("[]", encoding="utf-8")
    assert css.import_cookies(user.id, cookie_file, engine=engine) == "ok"
    assert imported == [(user.id, "cutshort", cookie_file, ("cutshort.io",))]
    assert len(launches) == 2


def test_cookie_export_filter_keeps_only_cutshort(tmp_path):
    rows = [{"name": "a", "value": "1", "domain": ".cutshort.io", "path": "/", "secure": True, "httpOnly": True},
            {"name": "b", "value": "2", "domain": "www.linkedin.com", "path": "/"},
            {"name": "c", "value": "3", "domain": "cutshort.io", "path": "/", "expirationDate": 1893456000}]
    out = browser.cookies_from_export(rows, allowed=css.COOKIE_DOMAINS)
    assert [c["name"] for c in out] == ["a", "c"] and out[1]["expires"] == 1893456000
    with pytest.raises(ValueError):
        browser.cookies_from_export(rows[1:2], allowed=css.COOKIE_DOMAINS)
