"""Wellfound session classification and the persistent-profile helpers. No real browser is launched."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from jobapply.models import Role
from jobapply.repo import UserScope
from jobapply.sites.wellfound import session as ws
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


def next_data(logged_in: bool | None) -> str:
    viewer: dict = {"__typename": "TalentViewer"}
    if logged_in is not None:
        viewer["currentUser"] = {"__ref": "User:1"} if logged_in else None
        viewer["id"] = "1" if logged_in else None
    cache = {"ROOT_QUERY": {"talent": {"viewer": viewer}}}
    return ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"apolloState": {"data": cache}}}}) + "</script>")


class FakePage:
    """Duck-typed Playwright page: url, title, goto, locator(...).count(), content(), evaluate(), wait_for_timeout()."""

    def __init__(self, url: str = "https://wellfound.com/jobs", html: str = "", title: str = "Wellfound",
                 present: tuple[str, ...] = (), redirect: dict[str, str] | None = None, captcha: str = "",
                 fail_goto: Exception | None = None, status: int = 200):
        self.url, self.html, self._title = url, html, title
        self.present = set(present)
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

    def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(1 if selector in self.present else 0)

    def content(self) -> str:
        return self.html

    def evaluate(self, js: str):
        if "__NEXT_DATA__" in js:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', self.html, re.S)
            return m.group(1) if m else ""
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


# classify_page --------------------------------------------------------------------
def test_classify_challenge_wins():
    assert ws.classify_page(FakePage(title="Security Check | Wellfound", html=next_data(True))) == "challenge"
    assert ws.classify_page(FakePage(title="Just a moment...", html=next_data(True))) == "challenge"
    assert ws.classify_page(FakePage(present=("#challenge-form",), html=next_data(True))) == "challenge"
    assert ws.classify_page(FakePage(captcha='iframe[src*="turnstile"]', html=next_data(True))) == "challenge"
    # challenge wording on a page that rendered nothing else
    assert ws.classify_page(FakePage(html="<p>Checking your browser before accessing wellfound.com</p>")) == "challenge"
    # the same wording inside a rendered job page is content
    page = FakePage(url="https://wellfound.com/jobs/1-x", present=("[data-test='JobListing']",),
                    html="<div>We are checking your browser skills, verify you are human-centred</div>")
    assert ws.classify_page(page) == "unknown"


def test_classify_login_by_url_form_and_header():
    assert ws.classify_page(FakePage(url="https://wellfound.com/login?after_sign_in=%2Fjobs")) == "login"
    assert ws.classify_page(FakePage(url="https://wellfound.com/jobs/signup?source=x")) == "login"
    assert ws.classify_page(FakePage(url="https://wellfound.com/join")) == "login"
    assert ws.classify_page(FakePage(present=("form#new_user",))) == "login"
    # the Rails job page, logged out: header buttons whose onclick goes to /login
    page = FakePage(url="https://wellfound.com/jobs/1053169-python-developer",
                    present=("button[onclick*='/login']", "[data-test='JobListing']"))
    assert ws.classify_page(page) == "login"


def test_classify_next_data_viewer_is_decisive():
    assert ws.classify_page(FakePage(html=next_data(False))) == "login"
    assert ws.classify_page(FakePage(html=next_data(True))) == "ok"
    # nothing in the viewer: fall through to the DOM markers
    assert ws.classify_page(FakePage(html=next_data(None))) == "unknown"
    assert ws.classify_page(FakePage(html=next_data(None), present=("a[href='/logout']",))) == "ok"
    assert ws.classify_page(FakePage(html=next_data(None), present=("[data-test='UserMenu']",))) == "ok"
    # the `evaluate`-less path reads __NEXT_DATA__ from content()
    page = FakePage(html=next_data(True))
    page.evaluate = None  # type: ignore[assignment]
    assert ws.next_data_of(page) and ws.classify_next_data(ws.next_data_of(page)) == "ok"
    assert ws.classify_next_data(None) == "unknown" and ws.viewer_of({"props": {}}) is None
    # apolloState without the `data` wrapper
    flat = {"props": {"pageProps": {"apolloState": {"ROOT_QUERY": {"talent": {"viewer": {"currentUser": None}}}}}}}
    assert ws.classify_next_data(flat) == "login"


def test_classify_unknown_when_nothing_rendered():
    assert ws.classify_page(FakePage(html="")) == "unknown"
    page = FakePage(url="https://wellfound.com/jobs/1-x", present=("[data-test='JobListing']",))
    assert ws.classify_page(page) == "unknown"


# WellfoundSession.check() ---------------------------------------------------------------
def _check(session, user, page, timeout_ms: int = 200) -> str:
    return ws.WellfoundSession(context=None, user=user, session_db=session).check(page, timeout_ms=timeout_ms)


def test_check_ok(session, user):
    page = FakePage(html=next_data(True))
    assert _check(session, user, page) == "ok"
    assert page.visited == [ws.HOME_URL]
    site = UserScope(session, user).site("wellfound")
    assert site.login_status == "ok" and site.last_login_at is not None


def test_check_login_is_stored_as_expired(session, user):
    assert _check(session, user, FakePage(html=next_data(False))) == "login"
    assert UserScope(session, user).site("wellfound").login_status == "expired"
    page = FakePage(redirect={ws.HOME_URL: "https://wellfound.com/login?after_sign_in=%2Fjobs"})
    assert _check(session, user, page) == "login"


def test_check_challenge_sets_cooldown(session, user):
    assert _check(session, user, FakePage(title="Security Check | Wellfound")) == "challenge"
    site = UserScope(session, user).site("wellfound")
    assert site.login_status == "challenge"
    assert ws.cooldown_until(user) > datetime.now(UTC)
    assert ws.throttled(user, site) is True
    # a 403 / 429 on the navigation itself counts as a block
    user.preferences = {}
    assert _check(session, user, FakePage(html=next_data(True), status=403)) == "challenge"
    assert ws.cooldown_until(user) is not None


def test_check_unknown_keeps_ok(session, user):
    assert _check(session, user, FakePage(fail_goto=TimeoutError("Timeout 30000ms exceeded"))) == "unknown"
    assert UserScope(session, user).site("wellfound").login_status == "unknown"
    assert _check(session, user, FakePage(html=next_data(True))) == "ok"
    page = FakePage(html="")
    assert _check(session, user, page) == "unknown"
    assert page.waited_ms >= 200  # polled before giving up
    assert UserScope(session, user).site("wellfound").login_status == "ok"


def test_session_creates_missing_site_row(session, user):
    site = UserScope(session, user).site("wellfound")
    session.delete(site)
    session.commit()
    s = ws.WellfoundSession(None, user, session)
    s.record("ok")
    row = UserScope(session, user).site("wellfound")
    assert row is not None and row.login_status == "ok" and row.enabled is False and row.daily_cap == 15
    s.record("unknown")
    assert row.login_status == "ok"  # unknown never downgrades ok


def test_throttled():
    from jobapply.models import User, UserSite

    u = User(username="x", password_hash="h")
    site = UserSite(user_id=1, site="wellfound")
    assert ws.throttled(u, site) is False
    u.preferences = {ws.COOLDOWN_KEY: (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    assert ws.throttled(u, site) is True
    u.preferences = {ws.COOLDOWN_KEY: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert ws.throttled(u, site) is False
    u.preferences = {}
    site.login_status = "challenge"
    assert ws.throttled(u, site) is True
    u.preferences = {ws.COOLDOWN_KEY: "not a date"}
    site.login_status = "ok"
    assert ws.throttled(u) is False


# check_session / import_cookies ------------------------------------------------------
class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakePW:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_session_opens_headless_profile(session, engine, user, monkeypatch):
    page = FakePage(html=next_data(True))
    ctx = FakeContext(page)
    launched: list[tuple] = []

    def fake_launch(pw, user_id):
        launched.append((user_id,))
        return ctx

    monkeypatch.setattr(ws, "_sync_playwright", lambda: FakePW())
    monkeypatch.setattr(ws, "_launch", fake_launch)
    assert ws.check_session(user.id, engine=engine) == "ok"
    assert launched == [(user.id,)] and ctx.closed is True and page.visited == [ws.HOME_URL]
    session.expire_all()
    assert UserScope(session, user).site("wellfound").login_status == "ok"
    with pytest.raises(ValueError):
        ws.check_session(9999, engine=engine)


def test_launch_is_headless(monkeypatch):
    calls: list[dict] = []

    def fake_launch_persistent(pw, user_id, site, headless=None, **kw):
        calls.append({"user_id": user_id, "site": site, "headless": headless})
        return "ctx"

    monkeypatch.setattr("jobapply.sites.browser.launch_persistent", fake_launch_persistent)
    assert ws._launch(None, 3) == "ctx"
    assert calls == [{"user_id": 3, "site": "wellfound", "headless": True}]


def test_import_cookies_then_checks(session, engine, user, monkeypatch, tmp_path):
    imported: list[tuple] = []

    def fake_import(user_id, site, path, allowed=()):
        imported.append((user_id, site, path.name, allowed))
        return 3

    monkeypatch.setattr("jobapply.sites.browser.import_cookies_file", fake_import)
    monkeypatch.setattr(ws, "check_session", lambda uid, engine=None: "ok")
    cookie_file = tmp_path / "wellfound-cookies.json"
    cookie_file.write_text("[]", encoding="utf-8")
    assert ws.import_cookies(user.id, cookie_file, engine=engine) == "ok"
    assert imported == [(user.id, "wellfound", "wellfound-cookies.json", ("wellfound.com", "angel.co"))]


def test_cookie_domain_filter_matches_wellfound_and_angel():
    from jobapply.sites.browser import cookies_from_export

    rows = [{"name": "_wellfound_session", "value": "v", "domain": "wellfound.com", "path": "/"},
            {"name": "legacy", "value": "v", "domain": ".angel.co"},
            {"name": "other", "value": "v", "domain": "linkedin.com"}]
    kept = cookies_from_export(rows, ws.COOKIE_DOMAINS)
    assert sorted(c["name"] for c in kept) == ["_wellfound_session", "legacy"]
