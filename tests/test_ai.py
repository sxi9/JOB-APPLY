from types import SimpleNamespace

import anthropic
import pytest
from pypdf import PdfReader
from sqlmodel import select

from jobapply.ai.base import AnswerResult, LLMError, LLMRefused, LLMRequest, ResumeVariant, ScoreResult
from jobapply.ai.claude import FALLBACK_BETA, ClaudeLLM
from jobapply.ai.fake import FakeLLM
from jobapply.ai.resume import render_resume_pdf
from jobapply.ai.service import answer_question, ensure_resume_variant, get_llm, prepare_application, score_job
from jobapply.config import get_settings
from jobapply.models import AnswerBank, Application, AppStatus, Job, LlmUsage, User
from jobapply.pipeline.score import score_user
from jobapply.seed import DEMO_PROFILE

DESC = "We need Python and Postgres on AWS. Kafka experience is a plus. Go is required."


@pytest.fixture
def demo(session, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    user = User(
        username="demo",
        password_hash="x",
        profile=DEMO_PROFILE,
        filters={"roles": ["backend engineer"]},
        preferences={"score_threshold": 70},
    )
    session.add(user)
    session.commit()
    yield user
    get_settings.cache_clear()


def _job(session, title="Backend Engineer", company="Acme", desc=DESC, key="k1"):
    job = Job(dedupe_key=key, title=title, company=company, description=desc, location="Remote", remote=True)
    session.add(job)
    session.commit()
    return job


def test_fake_score_is_deterministic(session, demo):
    job = _job(session)
    llm = FakeLLM()
    r1 = score_job(session, llm, demo, job)
    r2 = score_job(session, llm, demo, job)
    assert r1 == r2
    assert r1.matched_skills == ["aws", "go", "postgres", "python"]  # demo profile lists Go: 1 year
    assert r1.missing_skills == ["kafka"]
    assert 0 <= r1.score <= 100
    assert llm.calls[0].task == "score" and "Candidate profile" in llm.calls[0].system
    assert session.exec(select(LlmUsage)).first().task == "score"


def test_fake_handles_canned_and_unknown_tasks():
    llm = FakeLLM()
    llm.queue("score", ScoreResult(score=99, reason="canned"))
    out = llm.complete(LLMRequest(task="score", system="", prompt="", schema=ScoreResult))
    assert out.parsed.score == 99
    with pytest.raises(NotImplementedError):
        llm.complete(LLMRequest(task="nope", system="", prompt=""))


def test_score_user_queues_above_threshold(session, demo):
    good = _job(session, key="good")
    bad = _job(session, title="Frontend Designer", company="Beta", desc="Figma, CSS, Webflow", key="bad")
    for j in (good, bad):
        session.add(Application(user_id=demo.id, job_id=j.id))
    session.commit()
    counts = score_user(session, demo, FakeLLM())
    assert counts == {"scored": 2, "queued": 1, "below": 1, "failed": 0}
    apps = {a.job_id: a for a in session.exec(select(Application)).all()}
    assert apps[good.id].status == AppStatus.queued and apps[good.id].score >= 70
    assert apps[good.id].cover_letter.startswith("Dear Acme team") and apps[good.id].resume_variant.endswith(".pdf")
    assert apps[bad.id].status == AppStatus.scored and not apps[bad.id].cover_letter
    assert "Postgres" not in apps[bad.id].score_reason


def test_score_user_records_failures_and_stops_on_retryable(session, demo):
    jobs = [_job(session, key=f"k{i}") for i in range(3)]
    for j in jobs:
        session.add(Application(user_id=demo.id, job_id=j.id))
    session.commit()

    class Flaky(FakeLLM):
        def complete(self, request):
            if request.task == "score":
                n = len([c for c in self.calls if c.task == "score"])
                self.calls.append(request)
                if n == 0:
                    raise LLMError("bad", retryable=False)
                if n == 1:
                    raise LLMError("429", retryable=True)
            return super().complete(request)

    counts = score_user(session, demo, Flaky())
    assert counts["failed"] == 2 and counts["scored"] == 0
    statuses = [a.status for a in session.exec(select(Application)).all()]
    assert statuses.count(AppStatus.new) == 3  # nothing changed status; rerun picks them up


def test_resume_variant_cached_per_company(session, demo):
    a = _job(session, key="a")
    b = _job(session, title="Platform Engineer", key="b")  # same company
    c = _job(session, company="Other Co", key="c")
    llm = FakeLLM()
    p1 = ensure_resume_variant(session, llm, demo, a)
    p2 = ensure_resume_variant(session, llm, demo, b)
    p3 = ensure_resume_variant(session, llm, demo, c)
    assert p1 == p2 != p3 and p1.exists() and p3.exists()
    assert len([c for c in llm.calls if c.task == "resume"]) == 2
    text = "\n".join(page.extract_text() for page in PdfReader(str(p1)).pages)
    assert "Demo User" in text and "Backend Engineer" in text and "Example Systems" in text
    assert "demo.applies@example.com" in text
    # The LLM never sees contact details.
    assert "demo.applies@example.com" not in llm.calls[0].system


def test_render_pdf_unicode(tmp_path):
    variant = ResumeVariant(
        label="Ingénieur · 工程师",
        summary="Résumé with ümlauts and – dashes.",
        skills=[{"name": "Langs", "keywords": ["Python", "C++"]}],
        work=[{"name": "Zoë GmbH", "position": "Dev", "startDate": "2020", "highlights": ["Did ✓"]}],
    )
    path = render_resume_pdf({"name": "Ana Müller", "email": "a@x.io"}, variant, tmp_path / "r.pdf")
    text = PdfReader(str(path)).pages[0].extract_text()
    assert "Müller" in text and "Zoë" in text


def test_answer_question_uses_bank_then_llm(session, demo):
    llm = FakeLLM()
    r = answer_question(session, llm, demo, "What is your notice period?")
    assert r.answer == "30 days" and r.confidence >= 80
    row = session.exec(select(AnswerBank)).one()
    assert row.approved is False and row.source == "ai"
    row.answer, row.approved = "45 days", True
    session.add(row)
    session.commit()
    r2 = answer_question(session, llm, demo, "what is your Notice Period ?")  # normalizes to the same hash
    assert r2.answer == "45 days" and r2.confidence == 100
    assert len([c for c in llm.calls if c.task == "answer"]) == 1
    r3 = answer_question(session, llm, demo, "How many years of Python experience do you have?")
    assert r3.answer == "6"


def test_prepare_application_is_idempotent(session, demo):
    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()
    llm = FakeLLM()
    prepare_application(session, llm, demo, app, job)
    prepare_application(session, llm, demo, app, job)
    assert len(llm.calls) == 2  # cover letter + resume, once


def test_get_llm_fake_backend(session, demo):
    assert isinstance(get_llm(session, demo, backend="fake"), FakeLLM)
    with pytest.raises(ValueError):
        get_llm(session, demo, backend="nope")


# ----------------------------------------------------------- Claude backend with a stub client


class _StubMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def parse(self, **kw):
        self.calls.append(("parse", kw))
        return self.response

    def create(self, **kw):
        self.calls.append(("create", kw))
        return self.response


def _stub(response):
    messages = _StubMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _response(
    text='{"score": 81, "reason": "ok", "matched_skills": [], "missing_skills": []}',
    parsed=None,
    stop="end_turn",
    details=None,
):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=details,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="text", text=text)],
        parsed_output=parsed,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=40, cache_read_input_tokens=1000),
    )


def test_claude_structured_request_shape():
    client, messages = _stub(_response(parsed=ScoreResult(score=81, reason="ok")))
    llm = ClaudeLLM(client=client, model="claude-opus-5-5", effort="low")
    out = llm.complete(LLMRequest(task="score", system="SYS", prompt="P", schema=ScoreResult, max_tokens=4096))
    assert out.parsed.score == 81 and out.model == "claude:claude-opus-5-5"
    assert out.input_tokens == 2200 and out.output_tokens == 40
    kind, kw = messages.calls[0]
    assert kind == "parse" and kw["output_format"] is ScoreResult
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 4096
    assert kw["output_config"] == {"effort": "low"}
    assert kw["fallbacks"] == "default" and kw["betas"] == [FALLBACK_BETA]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["system"][0]["text"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "P"}]


def test_claude_text_request_and_errors():
    client, messages = _stub(_response(text="Dear team"))
    llm = ClaudeLLM(client=client)
    assert llm.complete(LLMRequest(task="cover_letter", system="S", prompt="P")).text == "Dear team"
    assert messages.calls[0][0] == "create"

    client, _ = _stub(_response(stop="refusal", details=SimpleNamespace(category="cyber", explanation="no")))
    with pytest.raises(LLMRefused) as exc:
        ClaudeLLM(client=client).complete(LLMRequest(task="x", system="S", prompt="P"))
    assert exc.value.category == "cyber"

    client, _ = _stub(_response(stop="max_tokens"))
    with pytest.raises(LLMError, match="truncated"):
        ClaudeLLM(client=client).complete(LLMRequest(task="x", system="S", prompt="P"))

    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def create(self, **kw):
            raise self.exc

        parse = create

    def raising(exc):
        return ClaudeLLM(client=SimpleNamespace(beta=SimpleNamespace(messages=Boom(exc))))

    fake_resp = SimpleNamespace(status_code=429, headers={}, request=None)
    with pytest.raises(LLMError) as e:
        raising(anthropic.RateLimitError("slow down", response=fake_resp, body=None)).complete(
            LLMRequest(task="x", system="S", prompt="P")
        )
    assert e.value.retryable
    with pytest.raises(LLMError) as e:
        raising(anthropic.APIConnectionError(request=None)).complete(LLMRequest(task="x", system="S", prompt="P"))
    assert e.value.retryable


def test_claude_parses_text_when_sdk_gives_no_parsed_output():
    client, _ = _stub(_response(parsed=None))
    out = ClaudeLLM(client=client).complete(LLMRequest(task="score", system="S", prompt="P", schema=ScoreResult))
    assert isinstance(out.parsed, ScoreResult) and out.parsed.score == 81
    client, _ = _stub(_response(text="not json", parsed=None))
    with pytest.raises(LLMError, match="did not match"):
        ClaudeLLM(client=client).complete(LLMRequest(task="score", system="S", prompt="P", schema=ScoreResult))


def test_answer_result_schema_roundtrip():
    assert AnswerResult.model_validate_json('{"answer": "yes", "confidence": 80}').confidence == 80
