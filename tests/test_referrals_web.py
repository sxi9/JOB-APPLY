"""Referrals review page tests.

The ReferralDraft model is built by another agent. These tests skip gracefully when it is not
importable yet, so they never depend on that model's timing. The page itself must always render
(empty-safe) even without the model.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobapply.models import Job, User
from jobapply.seed import seed
from jobapply.web.app import create_app


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


def _referral_model():
    models = pytest.importorskip("jobapply.models")
    model = getattr(models, "ReferralDraft", None)
    if model is None:
        pytest.skip("ReferralDraft model not available yet (built by another agent)")
    return model


def _make_draft(engine, model, user_id, job_id, *, status="draft", body="Hi, I'd love a referral.",
                contact_name="Jane Recruiter"):
    with Session(engine) as s:
        draft = model(
            user_id=user_id,
            job_id=job_id,
            contact_name=contact_name,
            contact_headline="Engineering Manager at Acme",
            contact_profile_url="https://www.linkedin.com/in/jane",
            channel="linkedin",
            body=body,
            status=status,
        )
        s.add(draft)
        s.commit()
        s.refresh(draft)
        return draft.id


def _user_and_job(engine, username="demo"):
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == username)).one()
        job = s.exec(select(Job)).first()
        return user.id, job.id


# ---------------------------------------------------------------- always-on (no model needed)


def test_referrals_page_renders_empty_safe(client):
    """The page renders for a logged-in user even with no drafts / no model."""
    login(client, "demo", "demo1234")
    resp = client.get("/referrals")
    assert resp.status_code == 200, resp.text[:500]
    assert "Referral drafts" in resp.text
    assert "{{" not in resp.text
    # Safety explainer is present.
    assert "off by default" in resp.text
    assert "Approving does not send" in resp.text


def test_referrals_nav_link_present(client):
    login(client, "demo", "demo1234")
    assert 'href="/referrals"' in client.get("/").text


def test_referrals_requires_login(client):
    assert client.get("/referrals").status_code == 303


# ---------------------------------------------------------------- with the model


def test_referrals_page_lists_drafts(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    _make_draft(engine, model, uid, jid, contact_name="Jane Recruiter")
    login(client, "demo", "demo1234")
    page = client.get("/referrals").text
    assert "Jane Recruiter" in page
    assert "https://www.linkedin.com/in/jane" in page
    assert "Approve" in page


def test_referrals_save_edits_body(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid)
    _, csrf = login(client, "demo", "demo1234")
    resp = client.post(f"/referrals/{did}/save", data={"csrf": csrf, "body": "Edited message body."})
    assert resp.status_code == 303
    with Session(engine) as s:
        draft = s.get(model, did)
        assert draft.body == "Edited message body."
        assert draft.status == "draft"  # saving never changes status or sends


def test_referrals_approve_and_unapprove(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid)
    _, csrf = login(client, "demo", "demo1234")
    assert client.post(f"/referrals/{did}/approve", data={"csrf": csrf}).status_code == 303
    with Session(engine) as s:
        assert s.get(model, did).status == "approved"
    assert client.post(f"/referrals/{did}/unapprove", data={"csrf": csrf}).status_code == 303
    with Session(engine) as s:
        assert s.get(model, did).status == "draft"


def test_referrals_skip(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid)
    _, csrf = login(client, "demo", "demo1234")
    assert client.post(f"/referrals/{did}/skip", data={"csrf": csrf}).status_code == 303
    with Session(engine) as s:
        assert s.get(model, did).status == "skipped"


def test_referrals_csrf_required(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid)
    login(client, "demo", "demo1234")
    assert client.post(f"/referrals/{did}/save", data={"body": "x"}).status_code == 403
    assert client.post(f"/referrals/{did}/approve", data={}).status_code == 403
    assert client.post(f"/referrals/{did}/skip", data={}).status_code == 403


def test_referrals_per_user_isolation(client, engine):
    model = _referral_model()
    admin_uid, jid = _user_and_job(engine, "admin")
    did = _make_draft(engine, model, admin_uid, jid)  # owned by admin
    _, csrf = login(client, "demo", "demo1234")  # acting as demo
    # Another user's draft is invisible and not editable.
    assert client.post(f"/referrals/{did}/save", data={"csrf": csrf, "body": "x"}).status_code == 404
    assert client.post(f"/referrals/{did}/approve", data={"csrf": csrf}).status_code == 404
    assert client.post(f"/referrals/{did}/skip", data={"csrf": csrf}).status_code == 404
    with Session(engine) as s:
        assert s.get(model, did).status == "draft"  # untouched


def test_referrals_approve_does_not_clobber_body(client, engine):
    """Approve only changes status; it must never overwrite the body, even if a stale body field is posted
    (the approve form no longer carries one). Guards against losing unsaved edits on approve."""
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid, body="original body")
    _, csrf = login(client, "demo", "demo1234")
    resp = client.post(f"/referrals/{did}/approve", data={"csrf": csrf, "body": "stale value"})
    assert resp.status_code == 303
    with Session(engine) as s:
        draft = s.get(model, did)
        assert draft.status == "approved"
        assert draft.body == "original body"  # the stale posted body was ignored


def test_referrals_approve_only_from_draft(client, engine):
    model = _referral_model()
    uid, jid = _user_and_job(engine, "demo")
    did = _make_draft(engine, model, uid, jid, status="skipped")
    _, csrf = login(client, "demo", "demo1234")
    # Approving a skipped draft is a no-op (does not send, does not transition).
    assert client.post(f"/referrals/{did}/approve", data={"csrf": csrf}).status_code == 303
    with Session(engine) as s:
        assert s.get(model, did).status == "skipped"
