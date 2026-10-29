"""Profile page: the completeness card and the single uploaded resume file."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from jobapply.ai.service import master_resume_pdf
from jobapply.config import get_settings
from jobapply.models import AnswerBank, User
from jobapply.seed import DEMO_PROFILE
from jobapply.users import create_user
from jobapply.web.app import create_app
from jobapply.web.routes import profile as profile_routes

PDF_BYTES = b"%PDF-1.4\n%uploaded by the user\n"


@pytest.fixture
def client(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "Jl0tE_KZf7aYyq3W_e7u6bSJ5l1W9mY2y5V7q9cN6uM=")
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    app = create_app(engine=engine, secret_key="test-secret")
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def _make_user(engine, username, profile):
    with Session(engine) as s:
        user = create_user(s, username, "pw12345678", profile=profile)
        s.commit()
        return user.id


def _login(client, username, password="pw12345678"):
    page = client.get("/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})
    return csrf


def _reload(engine, uid):
    with Session(engine) as s:
        return s.get(User, uid)


# ------------------------------------------------------------------ completeness card


def test_completeness_card_lists_only_missing_fields(client, engine, monkeypatch):
    # Pin the required set so the test does not depend on the forms module's real table.
    monkeypatch.setattr(profile_routes, "_required_profile_keys", lambda: [
        ("first_name", "essential"), ("email", "essential"), ("phone", "essential"),
        ("linkedin", "optional"), ("github", "optional"),
    ])
    uid = _make_user(engine, "half", {"first_name": "Ann", "email": "", "linkedin": "https://x/ann"})
    with Session(engine) as s:
        s.add(AnswerBank(user_id=uid, question_hash="qh1", question="Why us?", answer="", approved=False))
        s.add(AnswerBank(user_id=uid, question_hash="qh2", question="Relocate?", answer="Yes", approved=True))
        s.commit()
    _login(client, "half")
    page = client.get("/profile").text

    # Missing essentials flagged red, missing optionals amber, filled ones absent from the list.
    card = page.split('id="needed"', 1)[1].split("</section>", 1)[0]
    assert 'href="#email"' in card and 'href="#phone"' in card  # empty + absent essentials
    assert 'href="#github"' in card  # absent optional
    assert 'href="#first_name"' not in card and 'href="#linkedin"' not in card  # filled, not listed
    assert card.count("chip-red") >= 2 and "chip-amber" in card
    assert "1 answer awaiting your approval" in card  # one unapproved, one approved


def test_completeness_card_clear_when_everything_filled(client, engine):
    _make_user(engine, "full", dict(DEMO_PROFILE))
    _login(client, "full")
    card = client.get("/profile").text.split('id="needed"', 1)[1].split("</section>", 1)[0]
    assert "ready to apply" in card and "chip-red" not in card


def test_completeness_card_uses_real_catalog_with_live_anchors(client, engine):
    """Regression for the high finding: the real REQUIRED_PROFILE_KEYS (frozen RequiredKey dataclasses with
    namespaced keys) must actually drive the card. Before the fix _normalize_required dropped every dataclass
    entry, so a brand-new empty profile was told it was 'ready to apply'. Also guards finding #5: each rendered
    anchor must resolve to an input id on the page (no `#profile.first_name` dead links)."""
    _make_user(engine, "empty", {})
    _login(client, "empty")
    page = client.get("/profile").text
    card = page.split('id="needed"', 1)[1].split("</section>", 1)[0]
    assert "ready to apply" not in card  # an empty profile is not ready
    assert "chip-red" in card  # at least one essential field flagged
    hrefs = re.findall(r'href="#([^"]+)"', card)
    assert "first_name" in hrefs and "email" in hrefs  # essentials listed
    input_ids = set(re.findall(r'id="([^"]+)"', page))
    for anchor in hrefs:
        if anchor == "answers":
            continue
        assert anchor in input_ids, f"dead anchor #{anchor}: no matching input on the page"


# ------------------------------------------------------------------ uploaded resume file


def test_upload_resume_stores_file_and_master_prefers_it(client, engine):
    uid = _make_user(engine, "up", dict(DEMO_PROFILE))
    csrf = _login(client, "up")
    resp = client.post("/profile/resume-file",
                       data={"csrf": csrf}, files={"file": ("cv.pdf", PDF_BYTES, "application/pdf")})
    assert resp.status_code == 303

    expected = get_settings().resumes_dir / str(uid) / "uploaded.pdf"
    assert expected.exists() and expected.read_bytes() == PDF_BYTES
    user = _reload(engine, uid)
    assert user.profile["resume_file"] == str(expected)
    assert master_resume_pdf(user) == expected  # the apply path sends the upload, not the generated PDF
    assert "cv.pdf" not in client.get("/profile").text  # stored name is uploaded.pdf
    assert "uploaded.pdf" in client.get("/profile").text


def test_replace_keeps_one_file_and_remove_reverts_to_generated(client, engine):
    uid = _make_user(engine, "rep", dict(DEMO_PROFILE))
    csrf = _login(client, "rep")
    client.post("/profile/resume-file", data={"csrf": csrf},
                files={"file": ("a.pdf", PDF_BYTES, "application/pdf")})
    client.post("/profile/resume-file", data={"csrf": csrf},
                files={"file": ("b.docx", b"PK\x03\x04 docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})  # noqa: E501
    folder = get_settings().resumes_dir / str(uid)
    assert sorted(p.name for p in folder.glob("uploaded.*")) == ["uploaded.docx"]  # only one kept
    assert _reload(engine, uid).profile["resume_file"].endswith("uploaded.docx")

    resp = client.post("/profile/resume-file/remove", data={"csrf": csrf})
    assert resp.status_code == 303
    assert list(folder.glob("uploaded.*")) == []
    user = _reload(engine, uid)
    assert "resume_file" not in user.profile
    assert master_resume_pdf(user).name == "master.pdf"  # back to the generated resume


def test_upload_rejects_other_file_types(client, engine):
    _make_user(engine, "bad", dict(DEMO_PROFILE))
    csrf = _login(client, "bad")
    resp = client.post("/profile/resume-file", data={"csrf": csrf},
                       files={"file": ("notes.txt", b"hi", "text/plain")})
    assert resp.status_code == 303
    assert "PDF or DOCX" in client.get("/profile").text


def test_resume_file_is_isolated_per_user(client, engine):
    a_id = _make_user(engine, "alice", dict(DEMO_PROFILE))
    b_id = _make_user(engine, "bob", dict(DEMO_PROFILE))

    csrf_b = _login(client, "bob")
    client.post("/profile/resume-file", data={"csrf": csrf_b},
                files={"file": ("bob.pdf", b"%PDF-1.4 bob", "application/pdf")})

    # Alice logs in: she neither sees nor can touch Bob's file.
    csrf_a = _login(client, "alice")
    assert _reload(engine, a_id).profile.get("resume_file") is None
    client.post("/profile/resume-file", data={"csrf": csrf_a},
                files={"file": ("alice.pdf", b"%PDF-1.4 alice", "application/pdf")})
    client.post("/profile/resume-file/remove", data={"csrf": csrf_a})

    # Bob's file and path are untouched, under his own directory.
    bob = _reload(engine, b_id)
    assert bob.profile["resume_file"] == str(get_settings().resumes_dir / str(b_id) / "uploaded.pdf")
    assert (get_settings().resumes_dir / str(b_id) / "uploaded.pdf").exists()
    assert not (get_settings().resumes_dir / str(a_id) / "uploaded.pdf").exists()


def test_resume_file_upload_requires_csrf(client, engine):
    _make_user(engine, "nocsrf", dict(DEMO_PROFILE))
    _login(client, "nocsrf")
    resp = client.post("/profile/resume-file", files={"file": ("x.pdf", PDF_BYTES, "application/pdf")})
    assert resp.status_code == 403
