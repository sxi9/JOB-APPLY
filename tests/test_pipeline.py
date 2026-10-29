from datetime import timedelta

from sqlmodel import Session, select

from jobapply.collectors.ats import GreenhouseCollector
from jobapply.collectors.base import NormalizedJob
from jobapply.models import Application, AppStatus, CollectorRun, Job, JobSource, LlmUsage, User, utcnow
from jobapply.pipeline.collect import run_all
from jobapply.pipeline.dedupe import dedupe_key, norm_company, norm_title, store_jobs
from jobapply.pipeline.filters import UserFilters, assign_jobs, detect_seniority, match
from jobapply.scheduler import build_scheduler

from .helpers import fixture_client

DESC = "We build payments infrastructure. You will write Python and Go services, own on-call, and mentor engineers."


def nj(**kw):
    base = dict(title="Senior Backend Engineer", company="Acme Inc.", location="Bengaluru, India",
                posted_at=utcnow() - timedelta(hours=3), url="https://x/1", apply_url="https://x/1",
                description=DESC, source="indeed", source_id="1")
    base.update(kw)
    return NormalizedJob(**base)


def test_normalizers():
    assert norm_company("Acme Technologies Pvt. Ltd.") == "acme"
    assert norm_title("Backend Engineer (m/w/d)") == "backend engineer"
    assert norm_title("Engineer - Payments") != norm_title("Engineer - Billing")
    assert dedupe_key(nj()) == dedupe_key(nj(company="ACME", source="linkedin", source_id="9"))


def test_same_job_across_sources_prefers_ats(session):
    first = store_jobs(session, [nj()])
    assert len(first) == 1
    ats_posted = utcnow() - timedelta(hours=5)
    again = store_jobs(session, [nj(source="greenhouse", source_id="acme:77", ats_type="greenhouse",
                                    apply_url="https://boards.greenhouse.io/acme/jobs/77", posted_at=ats_posted,
                                    title="Senior Backend Engineer ", location="Bengaluru")])
    assert again == []
    job = session.exec(select(Job)).one()
    assert job.source == "greenhouse" and job.ats_type == "greenhouse"
    assert job.apply_url.startswith("https://boards.greenhouse.io")
    assert job.posted_at == ats_posted
    assert len(session.exec(select(JobSource)).all()) == 2


def test_fuzzy_match_on_slightly_different_title(session):
    store_jobs(session, [nj()])
    new = store_jobs(session, [nj(title="Senior Back-end Engineer", source="glassdoor", source_id="g1",
                                  description=DESC + " Apply now!")])
    assert new == [] and len(session.exec(select(Job)).all()) == 1


def test_different_jobs_stay_separate(session):
    store_jobs(session, [nj()])
    new = store_jobs(session, [nj(title="Senior Frontend Engineer", source_id="2",
                                  description="React and TypeScript design systems, accessibility, CSS.")])
    assert len(new) == 1


def test_resubmitted_source_row_is_idempotent(session):
    store_jobs(session, [nj()])
    assert store_jobs(session, [nj()]) == []
    assert len(session.exec(select(JobSource)).all()) == 1


def _job(**kw):
    base = dict(dedupe_key="k", title="Senior Python Developer", company="Acme", location="Pune, India",
                remote=False, posted_at=utcnow() - timedelta(hours=2), description="Django, AWS, Postgres",
                source="lever")
    base.update(kw)
    return Job(**base)


def test_filter_rules():
    j = _job()
    assert match(j, UserFilters()) == (True, "ok")
    assert match(_job(posted_at=utcnow() - timedelta(days=5)), UserFilters(max_age_hours=24))[1] == "too old"
    assert match(j, UserFilters(roles=["python developer"]))[0]
    assert match(j, UserFilters(roles=["java developer"]))[1] == "role not matched"
    assert match(j, UserFilters(keywords_include=["django"]))[0]
    assert match(j, UserFilters(keywords_include=["go"]))[1] == "no include keyword"
    assert match(j, UserFilters(keywords_exclude=["senior"]))[1] == "exclude keyword in title"
    assert match(j, UserFilters(remote_only=True))[1] == "not remote"
    assert match(j, UserFilters(locations=["pune"]))[0]
    assert match(j, UserFilters(locations=["remote"]))[1] == "location not matched"
    assert match(_job(remote=True), UserFilters(locations=["remote"]))[0]
    assert match(_job(salary_max=900000), UserFilters(salary_floor=1500000))[1] == "salary below floor"
    assert match(j, UserFilters(salary_floor=1500000))[0]  # unknown salary passes
    assert match(j, UserFilters(seniority=["mid", "junior"]))[1] == "seniority senior"
    assert match(j, UserFilters(company_blocklist=["ACME Ltd"]))[1] == "company blocklisted"
    assert match(j, UserFilters(sources=["greenhouse"]))[1] == "source not selected"


def test_seniority():
    assert detect_seniority("Sr. Engineer") == "senior"
    assert detect_seniority("Staff Software Engineer") == "principal"
    assert detect_seniority("Software Engineer") is None
    assert detect_seniority("Engineering Intern") == "intern"


def test_assign_jobs_per_user(session):
    alice = User(username="alice", password_hash="x", filters={"roles": ["python"]})
    bob = User(username="bob", password_hash="x", filters={"roles": ["java"]})
    session.add_all([alice, bob])
    session.commit()
    ids = store_jobs(session, [nj(title="Python Engineer", source_id="p"), nj(title="Java Engineer", source_id="j")])
    assert assign_jobs(session, ids) == {"alice": 1, "bob": 1}
    assert assign_jobs(session, ids) == {"alice": 0, "bob": 0}  # never twice
    titles = {u: [session.get(Job, a.job_id).title for a in session.exec(
        select(Application).where(Application.user_id == uid)).all()] for u, uid in (("a", alice.id), ("b", bob.id))}
    assert titles == {"a": ["Python Engineer"], "b": ["Java Engineer"]}


def test_run_all_records_runs(engine, session):
    session.add(User(username="alice", password_hash="x", filters={"max_age_hours": 0}))
    session.commit()

    class Broken(GreenhouseCollector):
        def collect(self):
            raise RuntimeError("boom")

    good = GreenhouseCollector("stripe", client=fixture_client({"greenhouse": "greenhouse_stripe.json"}))
    result = run_all(collectors=[good, Broken("x")], engine=engine)
    # The fixture has two "Abuse Investigator, Dublin" postings; they merge into one job.
    assert result["new_jobs"] == 2 and result["assigned"] == {"alice": 2}
    assert len(session.exec(select(JobSource)).all()) == 3
    runs = {r.source: r for r in session.exec(select(CollectorRun)).all()}
    assert runs["greenhouse:stripe"].ok and runs["greenhouse:stripe"].fetched == 3
    assert not runs["greenhouse:x"].ok and "boom" in runs["greenhouse:x"].error


def test_scheduler_registers_tasks(monkeypatch, engine):
    import jobapply.scheduler as sch

    monkeypatch.setattr(sch, "session_scope", lambda: __import__("jobapply.db", fromlist=["x"]).session_scope(engine))
    from apscheduler.schedulers.background import BackgroundScheduler

    s = build_scheduler(scheduler=BackgroundScheduler())
    assert "collect" in {j.id for j in s.get_jobs()}
    assert build_scheduler(["nope"], scheduler=BackgroundScheduler()).get_jobs() == []


# ----------------------------------------------------------------- scoring with no AI at all


def _no_ai_env(monkeypatch, tmp_path):
    """No `.env`, no JOBAPPLY_LLM_BACKEND, no provider keys: what a fresh checkout looks like."""
    import os

    from jobapply.config import get_settings

    monkeypatch.chdir(tmp_path)  # the repo .env must not leak into the test
    for name in list(os.environ):
        if name.startswith("JOBAPPLY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()


def test_score_all_on_seeded_demo_with_backend_unset(engine, monkeypatch, tmp_path):
    from jobapply.config import get_settings
    from jobapply.pipeline.score import score_all
    from jobapply.seed import seed

    _no_ai_env(monkeypatch, tmp_path)
    try:
        assert get_settings().llm_backend == "fake"
        seed(engine)
        out = score_all(engine=engine)
        assert out["demo"]["scored"] >= 1 and out["demo"]["failed"] == 0
        with Session(engine) as s:
            assert not s.exec(select(Application).where(Application.status == AppStatus.new)).all()
            scored = s.exec(select(Application).where(Application.score.isnot(None))).all()
            assert all(a.score_reason for a in scored)
    finally:
        get_settings.cache_clear()


def test_score_all_live_backend_without_keys_falls_back_to_heuristics(engine, monkeypatch, tmp_path):
    from jobapply.config import get_settings
    from jobapply.pipeline.score import score_all
    from jobapply.seed import seed

    _no_ai_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JOBAPPLY_LLM_BACKEND", "live")
    monkeypatch.setenv("JOBAPPLY_LLM_PROVIDERS", "claude,openai,gemini")  # all keyless: no chain at all
    get_settings.cache_clear()
    try:
        seed(engine)
        with Session(engine) as s:
            before = len(s.exec(select(Application).where(Application.status == AppStatus.new)).all())
        assert before >= 1
        out = score_all(engine=engine)
        assert out["demo"]["scored"] == before and out["demo"]["failed"] == 0
        with Session(engine) as s:
            rows = s.exec(select(Application).where(Application.score_reason.like("[heuristic]%"))).all()
            assert len(rows) == before
            assert all(a.status in (AppStatus.queued, AppStatus.scored) for a in rows)
            assert not s.exec(select(LlmUsage)).all()
    finally:
        get_settings.cache_clear()
