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


def test_score_user_records_failures_and_switches_to_heuristics_on_retryable(session, demo):
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

    llm = Flaky()
    counts = score_user(session, demo, llm)
    # job 1: bad request, recorded and left `new`; job 2: chain exhausted, so it and job 3 are scored
    # by the heuristics instead of waiting for a provider (docs/NO_AI_MODE.md).
    assert counts["failed"] == 1 and counts["scored"] == 2
    apps = session.exec(select(Application)).all()
    assert [a.status for a in apps].count(AppStatus.new) == 1
    assert sum(1 for a in apps if (a.score_reason or "").startswith("[heuristic]")) == 2
    assert len([c for c in llm.calls if c.task == "score"]) == 2  # no third LLM attempt this run


def test_score_user_keeps_live_chain_after_a_non_retryable_job(session, demo):
    """Regression: FailoverLLM used to raise `all providers failed` with retryable=True even when every
    provider failed with a per-request error, so one bad job downgraded the rest of the run to heuristics."""
    from jobapply.ai import providers
    from jobapply.ai.base import LLM
    from jobapply.ai.providers import CATALOG, FailoverLLM, ProviderConfig

    providers.reset_health()
    jobs = [_job(session, key=f"k{i}", title=f"Backend Engineer {i}") for i in range(3)]
    for j in jobs:
        session.add(Application(user_id=demo.id, job_id=j.id))
    session.commit()

    class Stub(LLM):
        name = "stub"

        def __init__(self):
            self.calls = []
            self.inner = FakeLLM()

        def complete(self, request):
            self.calls.append(request)
            if request.task == "score" and "Backend Engineer 0" in request.prompt:
                raise LLMError("reply did not match ScoreResult", retryable=False)
            return self.inner.complete(request)

    stub = Stub()
    cfg = ProviderConfig(CATALOG["openai"], "k", "gpt", CATALOG["openai"].base_url)
    chain = FailoverLLM.from_configs([cfg], cooldown_minutes=10)
    chain.entries[0].llm = stub
    try:
        counts = score_user(session, demo, chain, prepare=False)
    finally:
        providers.reset_health()
    assert counts["failed"] == 1 and counts["scored"] == 2
    apps = {a.job_id: a for a in session.exec(select(Application)).all()}
    assert apps[jobs[0].id].status == AppStatus.new and "scoring failed" in apps[jobs[0].id].error
    # Jobs 2 and 3 were still scored by the chain, not by the heuristics.
    assert all(not (apps[j.id].score_reason or "").startswith("[heuristic]") for j in jobs[1:])
    assert len([c for c in stub.calls if c.task == "score"]) == 3


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
    assert len(llm.calls) == 3  # cover letter + resume + application message, once each
    assert app.message  # the per-application note was populated and reused (not regenerated)
    assert {c.task for c in llm.calls} == {"cover_letter", "resume", "application_message"}


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


# ----------------------------------------------------------- no-AI mode (docs/NO_AI_MODE.md)


def test_heuristic_score_reads_filters_and_recency(session, demo):
    from datetime import timedelta

    from jobapply.ai.heuristics import heuristic_answer, score_job_heuristic
    from jobapply.models import utcnow

    filters = {"roles": ["backend engineer"], "seniority": ["mid", "senior"], "locations": ["remote"],
               "keywords_exclude": ["manager"]}
    fresh = Job(dedupe_key="f", title="Senior Backend Engineer", company="A", description=DESC, remote=True,
                posted_at=utcnow() - timedelta(hours=2))
    stale = Job(dedupe_key="s", title="Senior Backend Engineer", company="A", description=DESC, remote=True,
                posted_at=utcnow() - timedelta(days=20))
    r_fresh = score_job_heuristic(demo, filters, fresh)
    r_stale = score_job_heuristic(demo, filters, stale)
    assert r_fresh.matched_skills == ["aws", "go", "postgres", "python"] and r_fresh.missing_skills == ["kafka"]
    assert r_fresh.score > r_stale.score >= 70
    assert r_fresh == score_job_heuristic(demo, filters, fresh)  # deterministic
    # Same function the fake uses.
    fake = FakeLLM()._score({"profile": {**DEMO_PROFILE, "target_roles": ["backend engineer"]},
                             "job": {"title": stale.title, "description": DESC}})
    assert fake.matched_skills == r_fresh.matched_skills
    mgr = Job(dedupe_key="m", title="Engineering Manager, Backend", company="A", description=DESC, remote=False,
              location="Berlin")
    r_mgr = score_job_heuristic(demo, filters, mgr)
    assert r_mgr.score < r_stale.score - 30
    assert "excluded word" in r_mgr.reason and "seniority manager" in r_mgr.reason and "location" in r_mgr.reason
    assert 0 <= score_job_heuristic({}, {}, Job(dedupe_key="e", title="", company="")).score <= 100

    # Closed questions from the vault; unknown questions stay unanswered so the LLM (if any) gets them.
    assert heuristic_answer("How many years of Python experience do you have?", None, demo) == ("6", 95)
    assert heuristic_answer("Years of experience with Kubernetes", None, DEMO_PROFILE) == ("2", 95)
    assert heuristic_answer("Are you willing to relocate?", ["Yes", "No"], demo) == ("Yes", 90)
    assert heuristic_answer("Do you require visa sponsorship?", ["Yes", "No"], demo) == ("No", 90)
    assert heuristic_answer("Are you legally authorized to work in India?", ["Yes", "No"], demo)[0] == "Yes"
    assert heuristic_answer("What is your notice period?", None, demo) == ("30 days", 85)
    assert heuristic_answer("Expected CTC", None, demo) == ("40 LPA", 85)
    assert heuristic_answer("When can you start?", None, demo)[0] == "30 days"
    assert heuristic_answer("Total years of experience", None, demo) == ("6", 85)
    assert heuristic_answer("Why do you want to join us?", None, demo) == (None, 0)
    assert heuristic_answer("Preferred office", ["Paris", "Tokyo"], demo) == (None, 0)
    assert heuristic_answer("LinkedIn profile", None, {}) == (None, 0)


def test_heuristic_answer_rules_are_anchored(demo):
    """Loose matches used to fill the wrong profile value at 85-95 confidence (above the review threshold)."""
    from jobapply.ai.heuristics import heuristic_answer

    # Not salary questions, not contact questions, not a start date: nobody answers, the LLM gets them.
    for q in ("Expected graduation date", "Desired job location", "Describe your experience with mobile development",
              "What is your availability for interviews this week?", "Tell us about your email marketing work",
              "Which phone platforms have you shipped on?"):
        assert heuristic_answer(q, None, demo) == (None, 0), q
    # The anchored forms still answer.
    assert heuristic_answer("Expected CTC", None, demo) == ("40 LPA", 85)
    assert heuristic_answer("What is your desired salary?", None, demo) == ("40 LPA", 85)
    assert heuristic_answer("Salary expectation (annual)", None, demo) == ("40 LPA", 85)
    assert heuristic_answer("Phone number", None, demo) == ("+91 98765 43210", 95)
    assert heuristic_answer("Phone", None, demo) == ("+91 98765 43210", 95)
    assert heuristic_answer("Mobile no.", None, demo) == ("+91 98765 43210", 95)
    assert heuristic_answer("Email address", None, demo) == ("demo.applies@example.com", 95)
    assert heuristic_answer("Email", None, demo) == ("demo.applies@example.com", 95)
    assert heuristic_answer("When are you available to start?", None, demo) == ("30 days", 85)


def test_heuristic_work_authorization_needs_profile_support(demo):
    from jobapply.ai.heuristics import heuristic_answer

    q = "Are you legally authorized to work in the United States?"
    # An Indian citizen is not declared authorized for the US...
    assert heuristic_answer(q, ["Yes", "No"], demo) == (None, 0)
    # ...but is for India, and for a question that names no country.
    assert heuristic_answer("Are you legally authorized to work in India?", ["Yes", "No"], demo) == ("Yes", 70)
    assert heuristic_answer("Are you authorized to work in your current location?", ["Yes", "No"], demo)[0] == "Yes"
    assert heuristic_answer("Do you have the right to work without sponsorship?", ["Yes", "No"], demo) == ("Yes", 70)
    # Without an explicit needs_sponsorship there is no boolean to give: no default "Yes".
    no_sponsor = {**DEMO_PROFILE}
    del no_sponsor["needs_sponsorship"]
    assert heuristic_answer("Are you legally authorized to work in India?", ["Yes", "No"], no_sponsor) == (None, 0)
    assert heuristic_answer("Are you eligible to work here?", ["Yes", "No"], no_sponsor) == (None, 0)
    # Free-text authorization keeps the profile wording, country permitting.
    assert heuristic_answer("Work authorization status", None, demo) == ("Indian citizen", 85)
    us = {**DEMO_PROFILE, "location": "Austin, TX", "work_authorization": "US citizen"}
    assert heuristic_answer(q, ["Yes", "No"], us) == ("Yes", 70)


def test_heuristic_score_ignores_go_in_everyday_phrases(demo):
    from jobapply.ai.heuristics import score_job_heuristic

    no_go = {**DEMO_PROFILE, "skills": {"Python": 6}}
    phrase = Job(dedupe_key="g1", title="Backend Engineer", company="A",
                 description="Join our go-to-market engineering team working in Python. Ready to go? Let's go!")
    r = score_job_heuristic(no_go, {}, phrase)
    assert r.missing_skills == [] and r.matched_skills == ["python"]
    lang = Job(dedupe_key="g2", title="Backend Engineer", company="A",
               description="Services in Python and Go; Go experience required.")
    r = score_job_heuristic(no_go, {}, lang)
    assert r.missing_skills == ["go"]
    golang = Job(dedupe_key="g3", title="Dev", company="A", description="Golang and Python")
    assert score_job_heuristic(DEMO_PROFILE, {}, golang).matched_skills == ["go", "python"]


def test_heuristic_guards_blank_profile_values():
    from jobapply.ai.heuristics import cover_letter_template, score_job_heuristic

    profile = {"first_name": None, "last_name": None, "skills": {"": 2, " ": 1, "AWS": 4, "FastAPI": 3,
                                                                 "PostgreSQL": 5}}
    job = Job(dedupe_key="b", title="Backend Engineer", company="Acme", description="AWS, FastAPI and PostgreSQL")
    r = score_job_heuristic(profile, {}, job)
    assert r.matched_skills == ["aws", "fastapi", "postgresql"] and "Matches ," not in r.reason
    letter = cover_letter_template(profile, job)
    assert "AWS, FastAPI, PostgreSQL" in letter  # brand casing kept
    assert "None" not in letter and letter.endswith("Candidate")
    assert "Python, FastAPI, Postgres" in cover_letter_template(DEMO_PROFILE, job)


def test_answer_source_banks_heuristic_answers_for_review(session, demo):
    from jobapply.forms.runner import AnswerSource

    src = AnswerSource(session, None, demo)
    assert src.answer("What is your notice period?") == ("30 days", 85)
    row = session.exec(select(AnswerBank).where(AnswerBank.user_id == demo.id)).one()
    assert row.source == "heuristic" and row.approved is False and row.answer == "30 days"
    # The user corrects it on the Answers page; the corrected value wins next time.
    row.answer, row.approved = "45 days", True
    session.add(row)
    session.commit()
    assert src.answer("What is your notice period?") == ("45 days", 100)
    assert src.answer("Why do you want to join us?") == (None, 0)  # nothing banked for an unanswered question
    assert session.exec(select(AnswerBank).where(AnswerBank.user_id == demo.id)).one().id == row.id


def test_prepare_application_without_llm_uses_master_resume_and_template(session, demo):
    from jobapply.ai.heuristics import cover_letter_template

    job = _job(session)
    app = Application(user_id=demo.id, job_id=job.id)
    session.add(app)
    session.commit()
    prepare_application(session, None, demo, app, job)
    assert app.cover_letter == cover_letter_template(demo, job, "professional")
    assert app.cover_letter.startswith("Dear Acme team") and "Demo User" in app.cover_letter
    assert app.resume_variant and app.resume_variant.endswith("master.pdf")
    text = "\n".join(p.extract_text() for p in PdfReader(app.resume_variant).pages)
    assert "Demo User" in text and "Example Systems" in text and "Startup Labs" in text
    assert "Senior Software Engineer" in text  # the master label, not the posting title
    assert session.exec(select(LlmUsage)).first() is None
    # An uploaded PDF wins over the rendered master.
    uploaded = get_settings().resumes_dir / "u.pdf"
    uploaded.parent.mkdir(parents=True, exist_ok=True)
    uploaded.write_bytes(b"%PDF-1.4 stub")
    demo.profile = {**demo.profile, "resumes": [{"name": "u.pdf", "path": str(uploaded)}]}
    app2 = Application(user_id=demo.id, job_id=job.id)
    prepare_application(session, None, demo, app2, job)
    assert app2.resume_variant == str(uploaded)

    # And an LLM that fails never raises either.
    class Broken(FakeLLM):
        def complete(self, request):
            raise LLMError("down", retryable=True)

    app3 = Application(user_id=demo.id, job_id=job.id)
    prepare_application(session, Broken(), demo, app3, job)
    assert app3.cover_letter == app.cover_letter and app3.resume_variant == str(uploaded)


def test_score_user_without_llm_marks_heuristic(session, demo):
    good = _job(session, key="good")
    bad = _job(session, title="Frontend Designer", company="Beta", desc="Figma, CSS, Webflow", key="bad")
    for j in (good, bad):
        session.add(Application(user_id=demo.id, job_id=j.id))
    session.commit()
    counts = score_user(session, demo, None)
    assert counts == {"scored": 2, "queued": 1, "below": 1, "failed": 0}
    apps = {a.job_id: a for a in session.exec(select(Application)).all()}
    assert apps[good.id].status == AppStatus.queued and apps[good.id].score_reason.startswith("[heuristic] ")
    assert apps[good.id].cover_letter.startswith("Dear Acme team") and apps[good.id].resume_variant.endswith(".pdf")
    assert apps[bad.id].status == AppStatus.scored and apps[bad.id].score_reason.startswith("[heuristic] ")
    assert session.exec(select(LlmUsage)).first() is None


def test_ai_mode_off_never_builds_an_llm(session, demo):
    from jobapply.ai.service import ai_mode

    assert ai_mode(demo) == "auto" and ai_mode(None) == "auto"
    demo.preferences = {**demo.preferences, "ai_mode": "off"}
    assert ai_mode(demo) == "off"
    with pytest.raises(LLMError):
        get_llm(session, demo, backend="fake")  # even a configured backend is skipped
    demo.preferences = {**demo.preferences, "ai_mode": "nonsense"}
    assert ai_mode(demo) == "auto"


def test_answer_question_without_llm_returns_empty(session, demo):
    from jobapply.forms.answers import question_hash

    r = answer_question(session, None, demo, "Why us?")
    assert r.answer == "" and r.confidence == 0
    session.add(AnswerBank(user_id=demo.id, question_hash=question_hash("Why us?"), question="Why us?",
                           answer="Because.", approved=True))
    session.commit()
    assert answer_question(session, None, demo, "Why us?").answer == "Because."
