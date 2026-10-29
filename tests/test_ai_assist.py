"""AI-assisted applying: per-field recovery and the per-application message, plus the message migration."""

from __future__ import annotations

from sqlmodel import Session, select

from jobapply.ai.base import LLMError, RecoverResult
from jobapply.ai.fake import FakeLLM
from jobapply.ai.heuristics import application_message_template
from jobapply.ai.service import application_message, recover_field
from jobapply.config import get_settings
from jobapply.db import make_engine, upgrade
from jobapply.models import Application, Job, LlmUsage, User
from jobapply.seed import DEMO_PROFILE


def _job(session, title="Backend Engineer", company="Acme", key="rk1"):
    job = Job(dedupe_key=key, title=title, company=company,
              description="Python, Postgres and AWS backend role.", location="Remote", remote=True)
    session.add(job)
    session.commit()
    return job


# ------------------------------------------------------------------ recover_field


def test_recover_field_reformats_phone_with_stub_llm(session, demo):
    job = _job(session)
    llm = FakeLLM()
    llm.queue("recover_field", RecoverResult(action="reformat", value="+91 9876543210",
                                             reason="added country code"))
    rec = recover_field(session, llm, demo, question="Phone", value_tried="9876543210", field_type="tel",
                        options=[], error_text="Enter a valid phone number", job=job)
    assert rec.action == "reformat" and rec.value.startswith("+") and "country code" in rec.reason
    assert llm.calls[0].task == "recover_field"
    assert session.exec(select(LlmUsage).where(LlmUsage.task == "recover_field")).first() is not None


def test_recover_field_picks_a_listed_option(session, demo):
    # FakeLLM's default handler: with options it returns a matching one (pick_option).
    llm = FakeLLM()
    rec = recover_field(session, llm, demo, question="Country code", value_tried="India", field_type="select",
                        options=["United States +1", "India +91"], error_text="no option matches 'India'")
    assert rec.action == "pick_option" and rec.value == "India +91"


def test_recover_field_normalizes_bad_action_to_skip(session, demo):
    llm = FakeLLM()
    llm.queue("recover_field", RecoverResult(action="frobnicate", value="x"))
    rec = recover_field(session, llm, demo, question="Q", value_tried="v", field_type="text")
    assert rec.action == "skip"


# ------------------------------------------------------------------ application_message


def test_application_message_with_fakellm(session, demo):
    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()
    llm = FakeLLM()
    msg = application_message(session, llm, demo, job, app)
    assert msg and llm.calls[0].task == "application_message"
    assert job.company in msg or job.title in msg


def test_application_message_falls_back_to_template_without_llm(session, demo):
    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()
    msg = application_message(session, None, demo, job, app)
    assert msg == application_message_template(demo, job)
    # 2-4 sentences, no greeting/sign-off, grounded in this job.
    assert 2 <= msg.count(".") <= 4
    assert "Dear" not in msg and "Best regards" not in msg
    assert job.company in msg and job.title in msg


def test_application_message_falls_back_when_llm_raises(session, demo):
    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()

    class Raising(FakeLLM):
        def complete(self, request):
            raise LLMError("boom", retryable=False)

    msg = application_message(session, Raising(), demo, job, app)
    assert msg == application_message_template(demo, job)


def test_application_message_template_is_profile_grounded(session, demo):
    job = _job(session, title="Platform Engineer", company="Globex", key="rk2")
    msg = application_message_template(demo, job)
    assert "Platform Engineer" in msg and "Globex" in msg
    # one of the profile's own skills shows up
    assert any(s in msg for s in ("Python", "Postgres", "AWS", "FastAPI"))


# ------------------------------------------------------------------ AnswerSource message routing


def test_answer_source_message_only_for_generic_boxes(session, demo):
    # Regression: the per-application message must fill only genuine "anything else / message to the team"
    # boxes, not specific free-text questions (those belong to the tailored LLM answer).
    from jobapply.forms.runner import AnswerSource

    job = _job(session)
    msg = "I fit this role because of X."
    src = AnswerSource(session, llm=None, user=demo, job=job, message=msg)
    for q in ("Is there anything else you'd like us to know?", "Additional information",
              "Message to the hiring team", "Why do you want to work here?", "Tell us about yourself"):
        ans, conf = src.answer(q)
        assert ans == msg and conf == 85, q
    # Specific free-text prompts must NOT receive the generic message; they fall through to the
    # heuristic / LLM path instead (here (None, 0) or an unrelated heuristic answer, never `msg`).
    for q in ("Tell us about a time you disagreed with a manager",
              "Tell us about your experience with Kubernetes",
              "Why would you like to relocate to Berlin?"):
        ans, _ = src.answer(q)
        assert ans != msg, q


def test_application_message_survives_non_llm_error(session, demo):
    # Regression: a provider raising a non-LLMError must not propagate out of application_message
    # (prepare_application promises it never raises).
    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()

    class Boom(FakeLLM):
        def complete(self, request):
            raise TimeoutError("socket timed out")

    msg = application_message(session, Boom(), demo, job, app)
    assert msg == application_message_template(demo, job)


# ------------------------------------------------------------------ migration + round-trip


def test_message_migration_applies_and_round_trips(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    upgrade(url=url)  # exactly what `jobapply db upgrade` runs: all revisions incl. 0003 on a fresh db
    engine = make_engine(url)
    with Session(engine, expire_on_commit=False) as s:
        user = User(username="mig", password_hash="x", profile=DEMO_PROFILE)
        s.add(user)
        s.commit()
        job = Job(dedupe_key="mk1", title="Eng", company="Acme")
        s.add(job)
        s.commit()
        app = Application(user_id=user.id, job_id=job.id, message="Why I fit this role.")
        s.add(app)
        s.commit()
        app_id = app.id
    with Session(engine, expire_on_commit=False) as s:
        got = s.get(Application, app_id)
        assert got.message == "Why I fit this role."
    get_settings.cache_clear()
