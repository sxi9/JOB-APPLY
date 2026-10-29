"""Instahyre session classification, persistence and the uniform helpers. No real browser is launched."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from jobapply.models import Role
from jobapply.repo import UserScope
from jobapply.sites import browser
from jobapply.sites.instahyre import session as ih
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
    """Duck-typed Playwright page: url, goto, locator(...).count(), title(), evaluate(), wait_for_timeout()."""

    def __init__(self, url: str = "about:blank", present: set[str] | None = None, title: str = "",
                 redirect: dict[str, str] | None = None, captcha: str = "", fail_goto: Exception | None = None,
                 status: int = 200):
        self.url = url
        self.present = set(present or ())
        self._title = title
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
        return FakeLocator(1 if selector in self.present else 0)

    def title(self) -> str:
        return self._title

    def evaluate(self, js: str):
        return self.captcha

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


@pytest.fixture
def user(session):
    u = create_user(session, "alice", "pw12345678", role=Role.user)
    session.commit()
    return u


# classify_page --------------------------------------------------------------------
def test_classify_login_urls():
    for url in ("https://www.instahyre.com/login/?next=/candidate/opportunities/",
                "https://www.instahyre.com/candidates/register/",
                "https://www.instahyre.com/candidate/register", "https://www.instahyre.com/login"):
        assert ih.classify_page(FakePage(url=url)) == "login", url
        assert ih.is_login_url(url)
    assert not ih.is_login_url("https://www.instahyre.com/job-447614-python-developer-at-acme-bangalore/")


def test_classify_cloudflare_challenge_wins():
    cf = "https://www.instahyre.com/search-jobs?q=python&__cf_chl_rt_tk=abc"
    assert ih.classify_page(FakePage(url=cf, present={"a[href*='/logout']"})) == "challenge"
    assert ih.is_challenge_url(cf)
    assert ih.classify_page(FakePage(url=ih.HOME_URL, title="Just a moment...")) == "challenge"
    assert ih.classify_page(FakePage(url=ih.HOME_URL, present={"#challenge-form"})) == "challenge"
    assert ih.classify_page(FakePage(url=ih.LOGIN_URL, captcha='iframe[src*="turnstile"]')) == "challenge"


def test_classify_logged_in_and_out_markup():
    job = "https://www.instahyre.com/job-447614-python-developer-at-acme-bangalore/"
    # public job page: login/signup nav links and the sign-up bound Apply button
    assert ih.classify_page(FakePage(url=job, present={"#nav-user-login", "#nav-user-signup"})) == "login"
    assert ih.classify_page(FakePage(url=job, present={"[ng-click*='setPostAuthRedirectionUrl']"})) == "login"
    # signed-in job page: the apply handler of the candidate app
    assert ih.classify_page(FakePage(url=job, present={"[ng-click*='submitChoice']"})) == "ok"
    assert ih.classify_page(FakePage(url=ih.HOME_URL, present={"a[href*='/logout']"})) == "ok"
    # a page under /candidate/ that was not redirected away is served only to a signed-in candidate
    assert ih.classify_page(FakePage(url=ih.HOME_URL)) == "ok"
    # a job page with nothing decisive rendered yet
    assert ih.classify_page(FakePage(url=job)) == "unknown"
    # the login form on the home page
    home = FakePage(url="https://www.instahyre.com/", present={"form[ng-submit='loginUser()']"})
    assert ih.classify_page(home) == "login"


# check() ------------------------------------------------------------------------------
def _check(session, user, page, timeout_ms: int = 200) -> str:
    return ih.InstahyreSession(context=None, user=user, session_db=session).check(page, timeout_ms=timeout_ms)


def test_check_ok(session, user):
    page = FakePage(present={"a[href*='/logout']"})
    assert _check(session, user, page) == "ok"
    assert page.visited == [ih.HOME_URL]
    site = UserScope(session, user).site("instahyre")
    assert site.login_status == "ok" and site.last_login_at is not None


def test_check_login_redirect_is_stored_as_expired(session, user):
    page = FakePage(redirect={ih.HOME_URL: "https://www.instahyre.com/login/?next=/candidate/opportunities/"})
    assert _check(session, user, page) == "login"
    assert UserScope(session, user).site("instahyre").login_status == "expired"


@pytest.mark.parametrize("status", [403, 429, 503])
def test_check_block_status_is_a_challenge(session, user, status):
    page = FakePage(present={"a[href*='/logout']"}, status=status)
    assert _check(session, user, page) == "challenge"
    site = UserScope(session, user).site("instahyre")
    assert site.login_status == "challenge"
    assert ih.cooldown_until(user) > datetime.now(UTC)


def test_check_challenge_page_and_unknown(session, user):
    page = FakePage(redirect={ih.HOME_URL: ih.HOME_URL + "?__cf_chl_rt_tk=x"})
    assert _check(session, user, page) == "challenge"
    assert ih.throttled(user, UserScope(session, user).site("instahyre")) is True
    page = FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))
    assert _check(session, user, page) == "unknown"
    # a job page that renders nothing decisive is polled, then left unknown
    page = FakePage(redirect={ih.HOME_URL: "https://www.instahyre.com/job-1-x/"})
    assert _check(session, user, page) == "unknown"
    assert page.waited_ms >= 200


def test_check_keeps_ok_on_transient_unknown(session, user):
    assert _check(session, user, FakePage(present={"a[href*='/logout']"})) == "ok"
    site = UserScope(session, user).site("instahyre")
    assert _check(session, user, FakePage(fail_goto=TimeoutError("Timeout"))) == "unknown"
    assert site.login_status == "ok"
    # ... and a later clean check clears a stale cooldown
    user.preferences = {ih.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    assert _check(session, user, FakePage(present={"a[href*='/logout']"})) == "ok"
    assert ih.COOLDOWN_KEY not in (user.preferences or {})


def test_record_helpers_and_throttled(session, user):
    s = ih.InstahyreSession(None, user, session)
    site = UserScope(session, user).site("instahyre")
    assert ih.throttled(user, site) is False
    until = s.record_challenge()
    assert until > datetime.now(UTC) and site.login_status == "challenge" and ih.throttled(user, site) is True
    user.preferences = {ih.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert ih.throttled(user, site) is False
    user.preferences = {}
    assert ih.throttled(user, site) is True  # challenge without a timestamp: stay away
    s.record_login_needed()
    assert site.login_status == "expired"
    user.preferences = {ih.COOLDOWN_KEY: "not a date"}
    assert ih.throttled(user) is False
    assert ih.InstahyreSession.safe_classify(object()) == "unknown"


# check_session / import_cookies ----------------------------------------------------------
class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def test_check_session_opens_headless_profile(session, engine, user, monkeypatch):
    opened: list[int] = []
    page = FakePage(present={"a[href*='/logout']"})

    @contextmanager
    def fake_context(user_id):
        opened.append(user_id)
        yield FakeContext(page)

    monkeypatch.setattr(ih, "_browser_context", fake_context)
    assert ih.check_session(user.id, engine=engine) == "ok"
    assert opened == [user.id] and page.visited == [ih.HOME_URL]
    session.expire_all()
    assert UserScope(session, user).site("instahyre").login_status == "ok"
    with pytest.raises(ValueError):
        ih.check_session(999_999, engine=engine)


def test_browser_context_launches_headless(monkeypatch):
    calls: list[dict] = []

    class Ctx:
        def close(self):
            calls.append({"closed": True})

    class PW:
        def __enter__(self):
            return "pw"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: PW())
    monkeypatch.setattr(browser, "launch_persistent",
                        lambda pw, user_id, site, headless=None, **kw: calls.append(
                            {"user_id": user_id, "site": site, "headless": headless}) or Ctx())
    with ih._browser_context(7) as ctx:
        assert isinstance(ctx, Ctx)
    assert calls == [{"user_id": 7, "site": "instahyre", "headless": True}, {"closed": True}]


def test_import_cookies_filters_domain_and_rechecks(tmp_path, monkeypatch):
    export = [
        {"name": "sessionid", "value": "v1", "domain": "www.instahyre.com", "path": "/", "secure": True,
         "httpOnly": True, "sameSite": "lax", "expirationDate": 1800000000},
        {"name": "csrftoken", "value": "v2", "domain": ".instahyre.com", "path": "/"},
        {"name": "li_at", "value": "v3", "domain": ".linkedin.com", "path": "/"},
    ]
    cookies = browser.cookies_from_export(export, allowed=ih.ALLOWED_COOKIE_DOMAINS)
    assert sorted(c["name"] for c in cookies) == ["csrftoken", "sessionid"]
    assert cookies[0]["domain"] == "www.instahyre.com" and cookies[0]["expires"] == 1800000000
    with pytest.raises(ValueError):
        browser.cookies_from_export([export[2]], allowed=ih.ALLOWED_COOKIE_DOMAINS)

    path = tmp_path / "cookies.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    calls: list[tuple] = []
    monkeypatch.setattr(ih, "_import_cookies_file", lambda user_id, p: calls.append(("import", user_id, p)) or 2)
    monkeypatch.setattr(ih, "check_session", lambda user_id, engine=None, timeout_ms=30000: calls.append(
        ("check", user_id)) or "ok")
    assert ih.import_cookies(3, str(path)) == "ok"
    assert calls == [("import", 3, path), ("check", 3)]


def test_import_cookies_file_hook_uses_site_domains(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(browser, "import_cookies_file",
                        lambda user_id, site, path, allowed=("linkedin.com",): seen.update(
                            user_id=user_id, site=site, path=path, allowed=allowed) or 1)
    assert ih._import_cookies_file(4, ih.Path("x.json")) == 1
    assert seen == {"user_id": 4, "site": "instahyre", "path": ih.Path("x.json"), "allowed": ("instahyre.com",)}
