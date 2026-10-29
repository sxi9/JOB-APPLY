"""Self-service sign-up and the guided onboarding wizard."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.models import Role, User, UserSite
from jobapply.seed import seed
from jobapply.web.app import create_app


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "Jl0tE_KZf7aYyq3W_e7u6bSJ5l1W9mY2y5V7q9cN6uM=")
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(engine.url.database) + "-data")
    get_settings.cache_clear()
    seed(engine)
    app = create_app(engine=engine, secret_key="test-secret")
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def _csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def signup(client, username, password, confirm=None, email=""):
    csrf = _csrf(client.get("/signup").text)
    return client.post(
        "/signup",
        data={
            "username": username,
            "password": password,
            "confirm": password if confirm is None else confirm,
            "email": email,
            "csrf": csrf,
        },
    )


def login(client, username, password):
    csrf = _csrf(client.get("/login").text)
    return client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})


# ----------------------------------------------------------------- sign-up


def test_login_page_shows_create_account_link(client):
    assert 'href="/signup"' in client.get("/login").text


def test_signup_creates_plain_user_and_logs_in(client, engine):
    resp = signup(client, "newcomer", "longpassword1", email="new@example.com")
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding"
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "newcomer")).one()
        assert u.role == Role.user and u.email == "new@example.com"
        assert u.password_hash.startswith("pbkdf2_sha256$") and "longpassword1" not in u.password_hash
    # already logged in: a protected page renders, and the setup nudge shows.
    page = client.get("/")
    assert page.status_code == 200
    assert "Finish setup" in page.text and 'href="/onboarding"' in page.text


def test_signup_rejects_duplicate_username(client):
    assert signup(client, "dupe", "longpassword1").status_code == 303
    client.post("/logout", data={"csrf": _csrf(client.get("/login").text)})
    resp = signup(client, "dupe", "longpassword1")
    assert resp.status_code == 400 and "already taken" in resp.text


def test_signup_validation_errors(client):
    assert "at least 8" in signup(client, "shorty", "x").text
    assert "do not match" in signup(client, "mismatch", "longpassword1", confirm="longpassword2").text
    assert "3-32 characters" in signup(client, "a b", "longpassword1").text


def test_signup_disabled_404s_and_hides_link(client, monkeypatch):
    csrf = _csrf(client.get("/login").text)  # grab a valid token before disabling signup
    monkeypatch.setenv("JOBAPPLY_ALLOW_SIGNUP", "false")
    get_settings.cache_clear()
    assert client.get("/signup").status_code == 404
    resp = client.post("/signup", data={"username": "blocked", "password": "longpassword1",
                                        "confirm": "longpassword1", "csrf": csrf})
    assert resp.status_code == 404
    assert 'href="/signup"' not in client.get("/login").text


# ----------------------------------------------------------------- the wizard


def test_onboarding_renders_and_steps_round_trip(client, engine):
    signup(client, "wiz", "longpassword1")
    page = client.get("/onboarding").text
    assert "Finish setting up your account" in page and "{{" not in page
    for step in ("basics", "eligibility", "roles", "resume", "answers", "linkedin"):
        assert client.get(f"/onboarding?step={step}").status_code == 200
    csrf = _csrf(client.get("/onboarding").text)

    # basics -> profile
    resp = client.post(
        "/onboarding/basics",
        data={"csrf": csrf, "first_name": "Wiz", "last_name": "Ard", "email": "wiz@example.com",
              "phone": "+1 555 0100", "location": "Remote"},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=eligibility"

    # eligibility -> profile
    client.post(
        "/onboarding/eligibility",
        data={"csrf": csrf, "work_authorization": "US citizen", "needs_sponsorship": "no",
              "willing_to_relocate": "yes"},
    )
    # answers -> profile
    client.post(
        "/onboarding/answers",
        data={"csrf": csrf, "notice_period": "2 weeks", "current_ctc": "100k", "expected_ctc": "130k",
              "years_experience": "8"},
    )
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "wiz")).one()
        assert u.profile["first_name"] == "Wiz" and u.profile["location"] == "Remote"
        assert u.profile["work_authorization"] == "US citizen" and u.profile["needs_sponsorship"] == "no"
        assert u.profile["notice_period"] == "2 weeks" and u.profile["years_experience"] == "8"
        assert u.profile.get("onboarded") is not True  # not finished yet


def test_onboarding_roles_persist_to_filters(client, engine):
    signup(client, "roler", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=roles").text)
    resp = client.post(
        "/onboarding/roles",
        data={"csrf": csrf, "role_selection": ["cybersecurity:*", "software_engineering:backend"],
              "locations": "remote, bengaluru", "remote_only": "1", "salary_floor": "1500000"},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=resume"
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "roler")).one()
        assert set(u.filters["role_selection"]) == {"cybersecurity:*", "software_engineering:backend"}
        assert u.filters["locations"] == ["remote", "bengaluru"] and u.filters["remote_only"] is True
        assert u.filters["salary_floor"] == 1500000
    # round-trip: the saved checkbox comes back checked
    assert 'value="cybersecurity:*" checked' in client.get("/onboarding?step=roles").text


def test_onboarding_resume_upload_stores_file_and_path(client, engine):
    signup(client, "uploader", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=resume").text)
    resp = client.post(
        "/onboarding/resume",
        data={"csrf": csrf},
        files={"file": ("cv.pdf", b"%PDF-1.4\n%fake\n", "application/pdf")},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=answers"
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "uploader")).one()
        path = u.profile["resume_file"]
    from pathlib import Path

    assert path.endswith("uploaded.pdf") and Path(path).read_bytes().startswith(b"%PDF")


def test_onboarding_resume_json_paste(client, engine):
    signup(client, "paster", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=resume").text)
    client.post(
        "/onboarding/resume",
        data={"csrf": csrf, "resume_json": '{"basics": {"name": "Paster"}}'},
    )
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "paster")).one()
        assert u.profile["resume_json"]["basics"]["name"] == "Paster"


def test_onboarding_linkedin_connect_calls_import_and_reports(client, engine, monkeypatch):
    from jobapply.sites import browser

    calls = {}

    def fake_import(user_id, site, path, allowed=("linkedin.com",)):
        calls["args"] = (user_id, site, str(path))
        return 7

    monkeypatch.setattr(browser, "import_cookies_file", fake_import)
    signup(client, "linker", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=linkedin").text)
    resp = client.post(
        "/onboarding/linkedin",
        data={"csrf": csrf, "cookies_json": '[{"name": "li_at", "value": "x", "domain": ".linkedin.com"}]'},
    )
    assert resp.status_code == 303
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "linker")).one()
        site = s.exec(select(UserSite).where(UserSite.user_id == u.id, UserSite.site == "linkedin")).one()
        assert site.login_status == "ok"
    assert calls["args"][1] == "linkedin" and str(u.id) in calls["args"][2]
    assert "Imported 7 cookies" in client.get("/onboarding?step=linkedin").text


def test_onboarding_linkedin_connect_reports_failure(client, engine, monkeypatch):
    from jobapply.sites import browser

    def boom(*a, **k):
        raise ValueError("no cookies for ('linkedin.com',) found in the export")

    monkeypatch.setattr(browser, "import_cookies_file", boom)
    signup(client, "badlinker", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=linkedin").text)
    resp = client.post("/onboarding/linkedin", data={"csrf": csrf, "cookies_json": "[]"})
    assert resp.status_code == 303
    assert "Could not connect LinkedIn" in client.get("/onboarding?step=linkedin").text
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "badlinker")).one()
        site = s.exec(select(UserSite).where(UserSite.user_id == u.id, UserSite.site == "linkedin")).one()
        assert site.login_status == "expired"


def test_onboarding_done_flips_flag_and_stops_nudge(client, engine):
    signup(client, "finisher", "longpassword1")
    assert "Finish setup" in client.get("/").text
    csrf = _csrf(client.get("/onboarding").text)
    resp = client.post("/onboarding/done", data={"csrf": csrf})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "finisher")).one()
        assert u.profile["onboarded"] is True
    assert "Finish setup" not in client.get("/").text


def test_existing_user_and_admin_not_nagged(client):
    # demo has a non-empty profile; admin is admin. Neither should see the setup nudge.
    login(client, "demo", "demo1234")
    assert "Finish setup" not in client.get("/").text
    login(client, "admin", "admin1234")
    assert "Finish setup" not in client.get("/").text


def test_onboarding_roles_merges_and_preserves_other_filters(client, engine):
    # Regression: the roles step used to call settings.save_filters, which rebuilds UserFilters from its
    # (partial) form and wiped every filter the wizard does not post. It must now merge only its own fields.
    signup(client, "keeper", "longpassword1")
    with Session(engine) as s:
        u = s.exec(select(User).where(User.username == "keeper")).one()
        u.filters = {
            "keywords_include": ["python", "rust"],
            "keywords_exclude": ["intern"],
            "seniority": ["senior", "lead"],
            "company_blocklist": ["acme"],
            "max_age_hours": 24,
        }
        s.add(u)
        s.commit()
    csrf = _csrf(client.get("/onboarding?step=roles").text)
    resp = client.post(
        "/onboarding/roles",
        data={"csrf": csrf, "role_selection": ["cybersecurity:*"], "locations": "remote",
              "remote_only": "1", "salary_floor": "1000000"},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=resume"
    with Session(engine) as s:
        f = s.exec(select(User).where(User.username == "keeper")).one().filters
        # wizard-owned fields were updated
        assert f["role_selection"] == ["cybersecurity:*"]
        assert f["locations"] == ["remote"] and f["remote_only"] is True
        assert f["salary_floor"] == 1000000
        # everything the wizard does not own is preserved (the bug reset these to defaults)
        assert f["keywords_include_text"] == ["python", "rust"]  # raw free-text kept
        assert "python" in f["keywords_include"]
        assert f["keywords_exclude"] == ["intern"]
        assert set(f["seniority"]) == {"senior", "lead"}
        assert f["company_blocklist"] == ["acme"]
        assert f["max_age_hours"] == 24


def test_onboarding_resume_invalid_json_stays_on_step(client, engine):
    # Regression: bad pasted JSON was silently discarded while the user was advanced to the answers step.
    signup(client, "badjson", "longpassword1")
    csrf = _csrf(client.get("/onboarding?step=resume").text)
    resp = client.post("/onboarding/resume", data={"csrf": csrf, "resume_json": "{not valid"})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=resume"
    with Session(engine) as s:
        assert "resume_json" not in s.exec(select(User).where(User.username == "badjson")).one().profile
    assert "not valid JSON" in client.get("/onboarding?step=resume").text


def test_safe_next_rejects_open_redirect_vectors():
    from jobapply.web.routes.auth import _safe_next

    assert _safe_next("/dashboard") == "/dashboard"
    assert _safe_next(None) == "/"
    assert _safe_next("//evil.com") == "/"
    assert _safe_next("/\\evil.com") == "/"      # browsers normalise "\" to "/" -> //evil.com
    assert _safe_next("/\\/evil.com") == "/"
    assert _safe_next("/foo\r\nSet-Cookie: x=y") == "/"
    assert _safe_next("https://evil.com") == "/"


def test_login_ignores_backslash_open_redirect(client):
    csrf = _csrf(client.get("/login").text)
    resp = client.post("/login", data={"username": "demo", "password": "demo1234",
                                       "csrf": csrf, "next": "/\\evil.com"})
    assert resp.status_code == 303 and resp.headers["location"] == "/"


def test_onboarding_per_user_isolation(client, engine):
    signup(client, "alice", "longpassword1")
    client.post("/logout", data={"csrf": _csrf(client.get("/login").text)})
    signup(client, "bob", "longpassword1")
    # Logged in as bob; saving basics must only touch bob, never alice.
    csrf = _csrf(client.get("/onboarding").text)
    client.post("/onboarding/basics", data={"csrf": csrf, "first_name": "Bob"})
    with Session(engine) as s:
        alice = s.exec(select(User).where(User.username == "alice")).one()
        bob = s.exec(select(User).where(User.username == "bob")).one()
        assert bob.profile.get("first_name") == "Bob"
        assert alice.profile == {}  # untouched
