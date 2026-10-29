"""Naukri session management: page classification, the session check, cookie import, throttling.
No real browser is launched."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobapply.models import Role
from jobapply.repo import UserScope
from jobapply.sites import browser
from jobapply.sites.naukri import session as ns
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
                 redirect: dict[str, str] | None = None, captcha: bool = False, fail_goto: Exception | None = None,
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
        self.waited_ms = 0

    def goto(self, url: str, **kw) -> FakeResponse:
        self.visited.append(url)
        if self.fail_goto:
            raise self.fail_goto
        self.url = self.redirect.get(url, url)
        return FakeResponse(self.status)

    def locator(self, selector: str) -> FakeLocator:
        if self.nav_present and selector in ns.LOGGED_IN_SELECTORS:
            return FakeLocator(1)
        if self.login_form and selector in ns.LOGIN_FORM_SELECTORS:
            return FakeLocator(1)
        if self.captcha and selector in ns.CAPTCHA_SELECTORS:
            return FakeLocator(1)
        return FakeLocator(0)

    def content(self) -> str:
        return self.html

    def evaluate(self, js: str):
        if "innerText" in js:
            return re.sub(r"<[^>]+>", " ", self.html)
        return ""

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


@pytest.fixture
def user(session):
    u = create_user(session, "alice", "pw12345678", role=Role.user)
    session.commit()
    return u


# classify_page ---------------------------------------------------------------
def test_classify_login_by_url_and_form():
    assert ns.classify_page(FakePage(url="https://www.naukri.com/nlogin/login?URL=/mnjuser/homepage")) == "login"
    assert ns.classify_page(FakePage(url="https://www.naukri.com/", login_form=True)) == "login"
    assert ns.classify_page(FakePage(url="https://www.naukri.com/python-developer-jobs", login_form=True)) == "login"


def test_classify_ok_needs_the_logged_in_header():
    assert ns.classify_page(FakePage(url=ns.HOME_URL, nav_present=True)) == "ok"
    assert ns.classify_page(FakePage(url=ns.HOME_URL)) == "unknown"  # SPA still rendering
    # the header wins over login-ish wording in the page body
    page = FakePage(url=ns.HOME_URL, nav_present=True, html="<p>Login to apply faster</p>")
    assert ns.classify_page(page) == "ok"


def test_classify_challenge_by_url_captcha_and_text():
    assert ns.classify_page(FakePage(url="https://www.naukri.com/captcha?x=1")) == "challenge"
    assert ns.classify_page(FakePage(url=ns.HOME_URL, nav_present=True, captcha=True)) == "challenge"
    page = FakePage(url="https://www.naukri.com/some-interstitial", html="<h1>recaptcha required</h1>")
    assert ns.classify_page(page) == "challenge"
    page = FakePage(url="https://www.naukri.com/err", html="<p>Please enter OTP sent to your mobile</p>")
    assert ns.classify_page(page) == "challenge"


def test_job_description_text_is_not_a_challenge():
    html = "<div>SOC analyst: you will run a security check and flag unusual activity; verify you are human.</div>"
    page = FakePage(url="https://www.naukri.com/job-listings-soc-analyst-acme-291025501234", html=html)
    assert ns.classify_page(page) == "unknown"
    page = FakePage(url="https://www.naukri.com/mnjuser/profile", html=html, nav_present=True)
    assert ns.classify_page(page) == "ok"


# NaukriSession.check ----------------------------------------------------------------
def _check(session, user, page, timeout_ms: int = 200) -> str:
    return ns.NaukriSession(context=None, user=user, session_db=session, page=page).check(timeout_ms=timeout_ms)


def test_check_ok_records_login(session, user):
    page = FakePage(nav_present=True)
    assert _check(session, user, page) == "ok"
    assert page.visited == [ns.HOME_URL]
    site = UserScope(session, user).site("naukri")
    assert site.login_status == "ok" and site.last_login_at is not None


def test_check_login_redirect(session, user):
    page = FakePage(redirect={ns.HOME_URL: "https://www.naukri.com/nlogin/login?URL=%2Fmnjuser%2Fhomepage"})
    assert _check(session, user, page) == "login"
    assert UserScope(session, user).site("naukri").login_status == "login"


def test_check_challenge_sets_cooldown(session, user):
    page = FakePage(nav_present=True, captcha=True)
    assert _check(session, user, page) == "challenge"
    site = UserScope(session, user).site("naukri")
    assert site.login_status == "challenge"
    assert ns.cooldown_until(user) > datetime.now(UTC)
    assert ns.throttled(user, site) is True


@pytest.mark.parametrize("status", ns.BLOCK_STATUSES)
def test_check_treats_blocks_as_challenge(session, user, status):
    page = FakePage(nav_present=True, status=status)
    assert _check(session, user, page) == "challenge"


def test_check_unknown_keeps_a_verified_ok(session, user):
    assert _check(session, user, FakePage(nav_present=True)) == "ok"
    site = UserScope(session, user).site("naukri")
    assert _check(session, user, FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))) == "unknown"
    assert site.login_status == "ok"
    page = FakePage(nav_present=False)
    assert _check(session, user, page) == "unknown"
    assert site.login_status == "ok" and page.waited_ms >= 200


def test_check_unknown_first_time_is_recorded(session, user):
    assert _check(session, user, FakePage(fail_goto=TimeoutError("x"))) == "unknown"
    assert UserScope(session, user).site("naukri").login_status == "unknown"


def test_session_page_is_lazy():
    class Ctx:
        def new_page(self):
            return "page"

    class U:
        id, username, preferences = 1, "u", {}

    nk = ns.NaukriSession(Ctx(), U(), session_db=None)
    assert nk.page == "page"
    with pytest.raises(RuntimeError):
        _ = ns.NaukriSession(None, U(), session_db=None).page


# throttled / cooldown ----------------------------------------------------------------
def test_throttled_cooldown(session, user):
    site = UserScope(session, user).site("naukri")
    assert ns.throttled(user, site) is False
    user.preferences = {ns.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert ns.throttled(user, site) is True
    user.preferences = {ns.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert ns.throttled(user, site) is False
    user.preferences = {}
    site.login_status = "challenge"
    assert ns.throttled(user, site) is True
    user.preferences = {ns.COOLDOWN_KEY: "not a date"}
    site.login_status = "ok"
    assert ns.throttled(user) is False


def test_record_login_required_and_ok_clears_cooldown(session, user):
    nk = ns.NaukriSession(None, user, session, page=FakePage())
    nk.record_challenge()
    assert ns.COOLDOWN_KEY in user.preferences
    nk.record_login_required()
    assert UserScope(session, user).site("naukri").login_status == "login"
    assert _check(session, user, FakePage(nav_present=True)) == "ok"
    assert ns.COOLDOWN_KEY not in (user.preferences or {})


# cookie import ------------------------------------------------------------------------
def test_cookies_from_export_keeps_only_naukri_domains():
    rows = [
        {"name": "nauk_at", "value": "x", "domain": ".naukri.com", "path": "/", "secure": True,
         "httpOnly": True, "sameSite": "lax", "expirationDate": 1e10},
        {"name": "li_at", "value": "y", "domain": ".linkedin.com", "path": "/"},
        {"name": "sid", "value": "z", "domain": "www.naukri.com", "path": "/", "session": True},
    ]
    out = browser.cookies_from_export(rows, ns.COOKIE_DOMAINS)
    assert sorted(c["name"] for c in out) == ["nauk_at", "sid"]
    assert out[0]["domain"] == ".naukri.com" and out[0]["expires"] == 10_000_000_000
    assert "expires" not in out[1]
    with pytest.raises(ValueError):
        browser.cookies_from_export([rows[1]], ns.COOKIE_DOMAINS)


def test_import_cookies_uses_the_shared_importer_with_naukri_domains(monkeypatch, tmp_path):
    calls: dict = {}

    def fake_import(user_id, site, path, allowed=("linkedin.com",)):
        calls.update(user_id=user_id, site=site, path=Path(path), allowed=allowed)
        return 3

    monkeypatch.setattr(browser, "import_cookies_file", fake_import)
    monkeypatch.setattr(ns, "check_session", lambda user_id, db=None, **kw: "ok")
    cookie_file = tmp_path / "naukri.json"
    cookie_file.write_text(json.dumps([{"name": "a", "value": "b", "domain": ".naukri.com"}]), encoding="utf-8")
    assert ns.import_cookies(7, cookie_file) == "ok"
    assert calls == {"user_id": 7, "site": "naukri", "path": cookie_file, "allowed": ("naukri.com",)}


def test_check_session_opens_the_profile_headless(session, user, monkeypatch):
    launched: list[dict] = []

    class Ctx:
        def __init__(self):
            self.closed = False

        def new_page(self):
            return FakePage(nav_present=True)

        def close(self):
            self.closed = True

    ctx = Ctx()

    def fake_launch(pw, user_id, site, headless=None, **kw):
        launched.append({"user_id": user_id, "site": site, "headless": headless})
        return ctx

    class PW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(browser, "launch_persistent", fake_launch)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: PW())
    assert ns.check_session(user.id, session) == "ok"
    assert launched == [{"user_id": user.id, "site": "naukri", "headless": True}]
    assert ctx.closed is True
    assert UserScope(session, user).site("naukri").login_status == "ok"
    with pytest.raises(ValueError):
        ns.check_session(9999, session)
