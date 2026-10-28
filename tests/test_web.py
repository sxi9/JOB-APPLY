import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobapply.models import Application, AppStatus, Credential, Job, User
from jobapply.seed import seed
from jobapply.users import authenticate, create_user, hash_password, verify_password
from jobapply.web.app import create_app

PAGES = [
    "/",
    "/jobs",
    "/jobs?scope=all&hours=24&remote=1&q=engineer&sort=score",
    "/queue",
    "/applications",
    "/applications?status=interview",
    "/inbox",
    "/inbox?category=interview",
    "/profile",
    "/settings",
]


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "Jl0tE_KZf7aYyq3W_e7u6bSJ5l1W9mY2y5V7q9cN6uM=")
    from jobapply.config import get_settings

    get_settings.cache_clear()
    seed(engine)
    app = create_app(engine=engine, secret_key="test-secret")
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def login(client, username, password):
    page = client.get("/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    resp = client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})
    return resp, csrf


def test_passwords():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2_sha256$") and "secret123" not in h
    assert verify_password("secret123", h) and not verify_password("nope", h)
    assert not verify_password("x", "garbage")


def test_create_user_and_authenticate(session):
    u = create_user(session, "carol", "pw12345678")
    session.commit()
    assert authenticate(session, "carol", "pw12345678").id == u.id
    assert authenticate(session, "carol", "wrong") is None
    assert authenticate(session, "nobody", "x") is None
    with pytest.raises(ValueError):
        create_user(session, "carol", "again")


def test_anonymous_redirects_to_login(client):
    assert client.get("/jobs").status_code == 303
    assert client.get("/jobs").headers["location"].startswith("/login")
    assert client.get("/jobs", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/login"
    assert client.get("/healthz").json() == {"ok": True}


def test_login_logout(client):
    resp, csrf = login(client, "demo", "wrong")
    assert resp.status_code == 401
    resp, csrf = login(client, "demo", "demo1234")
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert client.get("/").status_code == 200
    assert client.post("/logout", data={"csrf": csrf}).status_code == 303
    assert client.get("/").status_code == 303


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_for_user(client, path):
    login(client, "demo", "demo1234")
    resp = client.get(path)
    assert resp.status_code == 200, resp.text[:500]
    assert "JOB-APPLY" in resp.text
    assert "{{" not in resp.text


def test_user_cannot_open_admin(client):
    login(client, "demo", "demo1234")
    assert client.get("/admin").status_code == 403


def test_admin_pages_and_view_as(client, engine):
    _, csrf = login(client, "admin", "admin1234")
    assert client.get("/admin").status_code == 200
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
    assert client.get("/queue").text.count("Approve") == 0  # admin has no queue of their own
    resp = client.post("/admin/view", data={"csrf": csrf, "uid": demo.id, "back": "/queue"})
    assert resp.status_code == 303
    page = client.get("/queue").text
    assert "Viewing as <strong>demo</strong>" in page and "Approve" in page


def test_csrf_required(client):
    login(client, "demo", "demo1234")
    assert client.post("/settings/filters", data={"roles": "x"}).status_code == 403


def test_job_detail_and_actions(client, engine):
    _, csrf = login(client, "demo", "demo1234")
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        job = s.exec(
            select(Job).where(Job.id.notin_(select(Application.job_id).where(Application.user_id == demo.id)))
        ).first()
    assert client.get(f"/jobs/{job.id}").status_code == 200
    resp = client.post(
        f"/jobs/{job.id}/action", data={"csrf": csrf, "action": "blocklist"}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200 and "skipped" in resp.text
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        assert job.company in demo.filters["company_blocklist"]
        app = s.exec(select(Application).where(Application.user_id == demo.id, Application.job_id == job.id)).one()
        assert app.status == AppStatus.skipped
    assert client.post(f"/jobs/{job.id}/action", data={"csrf": csrf, "action": "nope"}).status_code == 400
    assert client.get("/jobs/999999").status_code == 404


def test_queue_approve_with_edits(client, engine):
    _, csrf = login(client, "demo", "demo1234")
    with Session(engine) as s:
        app = s.exec(select(Application).where(Application.status == AppStatus.queued)).first()
    resp = client.post(
        f"/queue/{app.id}",
        data={"csrf": csrf, "decision": "approve", "cover_letter": "Edited letter", "answer__Notice period": "15 days"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200 and "approved" in resp.text
    with Session(engine) as s:
        app = s.get(Application, app.id)
        assert app.status == AppStatus.approved and app.cover_letter == "Edited letter"
        assert app.answers["Notice period"] == "15 days"


def test_other_users_rows_are_invisible(client, engine):
    # Admin's own view must not see demo's application or mail by id.
    login(client, "admin", "admin1234")
    with Session(engine) as s:
        app = s.exec(select(Application)).first()
    assert client.get(f"/applications/{app.id}").status_code == 404
    assert client.post(f"/queue/{app.id}", data={"decision": "approve"}).status_code == 403  # csrf first
    _, csrf = login(client, "admin", "admin1234")
    assert client.post(f"/queue/{app.id}", data={"csrf": csrf, "decision": "approve"}).status_code == 404


def test_settings_save_filters_sites_credentials(client, engine):
    _, csrf = login(client, "demo", "demo1234")
    resp = client.post(
        "/settings/filters",
        data={
            "csrf": csrf,
            "roles": "backend engineer\npython",
            "remote_only": "1",
            "seniority": ["senior", "lead"],
            "max_age_hours": "48",
            "salary_floor": "1000000",
            "locations": "remote, bengaluru",
        },
    )
    assert resp.status_code == 303
    resp = client.post(
        "/settings/sites",
        data={"csrf": csrf, "enabled__greenhouse": "1", "auto__greenhouse": "1", "cap__greenhouse": "7"},
    )
    assert resp.status_code == 303
    resp = client.post(
        "/settings/credentials",
        data={"csrf": csrf, "site": "Naukri", "kind": "login", "username": "demo@example.com", "secret": "hunter2"},
    )
    assert resp.status_code == 303
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        assert demo.filters["roles"] == ["backend engineer", "python"] and demo.filters["remote_only"] is True
        assert demo.filters["locations"] == ["remote", "bengaluru"]
        from jobapply.models import UserSite

        gh = s.exec(select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()
        lever = s.exec(select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "lever")).one()
        assert gh.auto_submit and gh.daily_cap == 7 and not lever.enabled
        cred = s.exec(select(Credential).where(Credential.user_id == demo.id, Credential.site == "naukri")).one()
        assert b"hunter2" not in cred.secret_blob
    page = client.get("/settings").text
    assert "hunter2" not in page and "naukri" in page


def test_profile_save_and_answers(client, engine):
    _, csrf = login(client, "demo", "demo1234")
    resp = client.post(
        "/profile",
        data={
            "csrf": csrf,
            "first_name": "Dee",
            "skills": "Python: 7\nRust",
            "resume_json": '{"basics": {"name": "Dee"}}',
        },
    )
    assert resp.status_code == 303
    resp = client.post(
        "/profile/answers", data={"csrf": csrf, "new_question": "Willing to travel?", "new_answer": "Yes"}
    )
    assert resp.status_code == 303
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        assert demo.profile["first_name"] == "Dee" and demo.profile["skills"] == {"Python": 7.0, "Rust": 0}
        assert demo.profile["resume_json"]["basics"]["name"] == "Dee"
    assert "Willing to travel?" in client.get("/profile").text
    resp = client.post("/profile", data={"csrf": csrf, "resume_json": "{not json"})
    assert resp.status_code == 303
    assert "not valid JSON" in client.get("/profile").text


def test_admin_switches_and_users(client, engine):
    _, csrf = login(client, "admin", "admin1234")
    resp = client.post(
        "/admin/switches", data={"csrf": csrf, "kill_switch": "on", "dry_run": "on", "collect_interval_minutes": "5"}
    )
    assert resp.status_code == 303
    from jobapply.switches import collect_interval, kill_switch_on

    with Session(engine) as s:
        assert kill_switch_on(s) and collect_interval(s) == 10
    resp = client.post(
        "/admin/users", data={"csrf": csrf, "username": "newbie", "password": "longenough1", "role": "user"}
    )
    assert resp.status_code == 303
    resp = client.post("/admin/users", data={"csrf": csrf, "username": "short", "password": "x"})
    page = client.get("/admin").text
    assert "at least 8" in page and "newbie created" in page
    with Session(engine) as s:
        assert s.exec(select(User).where(User.username == "newbie")).one().role == "user"


def test_seed_is_idempotent(engine):
    assert "admin" in seed(engine)
    assert seed(engine) == {"status": "already seeded"}
