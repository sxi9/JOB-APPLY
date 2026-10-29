from datetime import timedelta

from sqlmodel import Session, select

from jobapply.collectors.ats import GreenhouseCollector
from jobapply.collectors.base import NormalizedJob
from jobapply.models import Application, AppStatus, CollectorRun, Job, JobSource, LlmUsage, User, UserSite, utcnow
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


# ----------------------------------------------------------------- minute-level recency


def test_minutes_filter_on_exact_timestamp_sources():
    fresh = _job(source="greenhouse", posted_at=utcnow() - timedelta(minutes=5))
    stale = _job(source="greenhouse", posted_at=utcnow() - timedelta(minutes=90))
    assert match(fresh, UserFilters(max_age_minutes=30))[0]
    assert match(stale, UserFilters(max_age_minutes=30))[1] == "too old"
    # minutes wins over hours when both are set
    assert match(stale, UserFilters(max_age_minutes=30, max_age_hours=72))[1] == "too old"
    assert match(fresh, UserFilters(max_age_minutes=30, max_age_hours=0))[0]


def test_hours_filter_back_compat_when_only_hours_set():
    j = _job(source="greenhouse", posted_at=utcnow() - timedelta(hours=2))
    assert match(j, UserFilters(max_age_hours=24))[0]
    assert match(j, UserFilters(max_age_hours=1))[1] == "too old"


def test_minutes_filter_does_not_hide_coarse_timestamp_jobs():
    # Aggregator posting 20 min old: a 10-min filter falls back to whole-hour granularity for coarse
    # sources, so it passes rather than being wrongly hidden.
    coarse = _job(source="indeed", posted_at=utcnow() - timedelta(minutes=20))
    assert match(coarse, UserFilters(max_age_minutes=10))[0]
    # The same age on an exact-timestamp source IS dropped by a 10-min filter.
    exact = _job(source="linkedin", posted_at=utcnow() - timedelta(minutes=20))
    assert match(exact, UserFilters(max_age_minutes=10))[1] == "too old"
    # A coarse job that is genuinely old is still dropped (hours granularity, not hidden entirely).
    old_coarse = _job(source="indeed", posted_at=utcnow() - timedelta(days=2))
    assert match(old_coarse, UserFilters(max_age_minutes=10))[1] == "too old"


def test_assign_jobs_respects_minutes_window(session):
    alice = User(username="alice", password_hash="x", filters={"max_age_minutes": 30})
    session.add(alice)
    session.commit()
    ids = store_jobs(session, [
        nj(title="Fresh Engineer", source="greenhouse", source_id="fresh",
           posted_at=utcnow() - timedelta(minutes=5)),
        nj(title="Stale Engineer", source="greenhouse", source_id="stale",
           posted_at=utcnow() - timedelta(minutes=90)),
    ])
    assert assign_jobs(session, ids) == {"alice": 1}
    kept = [session.get(Job, a.job_id).title
            for a in session.exec(select(Application).where(Application.user_id == alice.id)).all()]
    assert kept == ["Fresh Engineer"]


def test_scheduler_builds_hot_collect_task_with_configured_interval(monkeypatch, engine):
    import jobapply.scheduler as sch
    from jobapply.config import get_settings

    monkeypatch.setattr(sch, "session_scope", lambda: __import__("jobapply.db", fromlist=["x"]).session_scope(engine))
    monkeypatch.setenv("JOBAPPLY_HOT_INTERVAL_MINUTES", "3")
    get_settings.cache_clear()
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        s = build_scheduler(["collector"], scheduler=BackgroundScheduler())
        jobs = {j.id: j for j in s.get_jobs()}
        assert "collect_hot" in jobs
        assert jobs["collect_hot"].trigger.interval.total_seconds() == 180
    finally:
        get_settings.cache_clear()


def test_hot_collectors_selects_only_hot_sources():
    from jobapply.collectors.registry import Company, SearchConfig, hot_collectors

    companies = [
        Company(name="Stripe", ats="greenhouse", slug="stripe"),
        Company(name="Ramp", ats="ashby", slug="ramp", hot=True),
    ]
    searches = SearchConfig(queries=["python developer"], linkedin={"enabled": True, "minutes_old": 60},
                            hot={"companies": ["stripe"], "linkedin": True, "linkedin_minutes_old": 15})
    out = hot_collectors(companies=companies, searches=searches)
    keys = {c.key for c in out}
    assert "greenhouse:stripe" in keys  # named in hot.companies
    assert "ashby:ramp" in keys  # per-company hot flag
    assert any(c.source == "linkedin" for c in out)  # guest search included
    # Nothing hot -> no collectors.
    cold = [Company(name="Stripe", ats="greenhouse", slug="stripe")]
    assert hot_collectors(companies=cold, searches=SearchConfig(queries=["x"])) == []


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


# ----------------------------------------------------------------- LinkedIn offsite routing (apply.py)


def _li_job(session, user, *, status=AppStatus.queued, easy_apply=False, score=80, apply_id="100"):
    """A non-Easy-Apply LinkedIn job (ats_type None, /jobs/view link) with its application."""
    job = Job(dedupe_key=f"li-{apply_id}-{easy_apply}-{score}", title="Engineer", company="Acme",
              source="linkedin", ats_type=None,
              apply_url=f"https://www.linkedin.com/jobs/view/{apply_id}/")
    session.add(job)
    session.flush()
    session.add(JobSource(job_id=job.id, source="linkedin", source_id=apply_id, raw={"easy_apply": easy_apply}))
    app = Application(user_id=user.id, job_id=job.id, status=status, score=score)
    session.add(app)
    session.commit()
    return app, job


def _enable_linkedin(session, user, **kw):
    site = session.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == "linkedin")).one()
    site.enabled = True
    for k, v in kw.items():
        setattr(site, k, v)
    session.add(site)
    session.commit()
    return site


class _FakePage:
    url = "https://www.linkedin.com/feed/"

    def new_page(self):
        return self

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, status="ok"):
        self.status = status

    def check(self, page):
        return self.status


def _wire_external(monkeypatch, resolutions, *, status="ok", throttled=False):
    """Monkeypatch the LinkedIn hooks and FormRunner so apply_external_linkedin_user never opens a browser.

    `resolutions` maps a job apply_url to the ApplyResolution the resolver should return.
    """
    from jobapply.forms.runner import FillResult, FormRunner
    from jobapply.pipeline import apply as apply_mod

    contexts: list[_FakeContext] = []
    runner_calls: list[dict] = []
    notified: list[tuple[str, str]] = []

    def launch(pw, user_id):
        ctx = _FakeContext()
        contexts.append(ctx)
        return ctx

    def resolve(page, job_url):
        return resolutions[job_url]

    def fake_run(self, page, url=None, html=None, ats_hint=None, submit=False, screenshot_path=None):
        runner_calls.append({"url": url, "ats_hint": ats_hint, "submit": submit})
        return FillResult(url=url or "", profile_origin="builtin", submitted=submit)

    monkeypatch.setattr(apply_mod, "_launch_persistent_linkedin", launch)
    monkeypatch.setattr(apply_mod, "_linkedin_session", lambda ctx, u, s: _FakeSession(status))
    monkeypatch.setattr(apply_mod, "_linkedin_throttled", lambda u, s: throttled)
    monkeypatch.setattr(apply_mod, "_resolve_external_url", resolve)
    monkeypatch.setattr(apply_mod, "notify", lambda s, u, kind, msg: notified.append((kind, msg)))
    monkeypatch.setattr(FormRunner, "run", fake_run)
    return contexts, runner_calls, notified


def test_external_linkedin_picker_excludes_easy_apply_and_resolved(session, demo):
    _enable_linkedin(session, demo)
    from jobapply.pipeline.apply import pick_external_linkedin_applications, site_for_job

    offsite_app, offsite_job = _li_job(session, demo, apply_id="201")
    _li_job(session, demo, easy_apply=True, apply_id="202")  # easy apply -> excluded
    # a job already resolved to an ATS no longer routes through the LinkedIn picker
    resolved = Job(dedupe_key="gh-1", title="E", company="Acme", source="linkedin", ats_type="greenhouse",
                   apply_url="https://boards.greenhouse.io/acme/jobs/9")
    session.add(resolved)
    session.flush()
    session.add(Application(user_id=demo.id, job_id=resolved.id, status=AppStatus.queued, score=70))
    session.commit()
    assert site_for_job(resolved) == "greenhouse"
    picked = pick_external_linkedin_applications(session, demo, limit=10)
    assert [a.id for a, _ in picked] == [offsite_app.id]


def test_external_linkedin_resolved_greenhouse_goes_to_form_runner(session, demo, monkeypatch):
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="301")
    gh = "https://boards.greenhouse.io/acme/jobs/777"
    contexts, runner_calls, notified = _wire_external(
        monkeypatch, {job.apply_url: ApplyResolution("offsite", gh, "greenhouse")})
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=True)

    assert len(out) == 1 and out[0].status == AppStatus.dry_run
    assert len(runner_calls) == 1
    assert runner_calls[0]["url"] == gh and runner_calls[0]["ats_hint"] == "greenhouse"
    assert runner_calls[0]["submit"] is False  # dry run never submits
    session.refresh(job)
    assert job.apply_url == gh and job.ats_type == "greenhouse"  # written back for lazy future runs
    assert contexts and contexts[0].closed


def test_external_linkedin_unresolvable_is_paused(session, demo, monkeypatch):
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="401")
    contexts, runner_calls, notified = _wire_external(
        monkeypatch, {job.apply_url: ApplyResolution("signin", None, None)})
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=True)

    assert len(out) == 1 and out[0].status == AppStatus.paused
    assert runner_calls == []  # never handed to the runner
    assert notified and notified[-1][0] == "paused"
    session.refresh(job)
    assert job.ats_type is None and "/jobs/view/" in job.apply_url  # left untouched for a manual apply


def test_external_linkedin_easy_apply_is_flagged_not_applied(session, demo, monkeypatch):
    from jobapply.pipeline.apply import apply_external_linkedin_user, pick_external_linkedin_applications
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="501")
    contexts, runner_calls, notified = _wire_external(
        monkeypatch, {job.apply_url: ApplyResolution("easy_apply", None, None)})
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=True)

    assert out == [] and runner_calls == []  # no outcome recorded, left for the Easy Apply filler
    src = session.exec(select(JobSource).where(JobSource.job_id == job.id)).one()
    assert (src.raw or {}).get("easy_apply") is True
    # now excluded from the external picker
    assert pick_external_linkedin_applications(session, demo, limit=5) == []


def _allow_real_submits(session, monkeypatch):
    """Turn off both the env dry-run flag and the admin dry-run toggle so real submits are possible."""
    from jobapply.config import get_settings
    from jobapply.switches import set_value

    monkeypatch.setenv("JOBAPPLY_DRY_RUN", "false")
    get_settings.cache_clear()
    set_value(session, "dry_run", "off")
    session.commit()


def test_external_linkedin_honours_user_cap(session, demo, monkeypatch):
    """Real submits through the offsite-LinkedIn path stop at the per-user daily cap. Regression: the path
    had no cap check and could exceed the cap on every run."""
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _allow_real_submits(session, monkeypatch)
    demo.preferences = {**(demo.preferences or {}), "daily_cap": 1}
    session.add(demo)
    _enable_linkedin(session, demo)
    gh_site = session.exec(
        select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()
    gh_site.auto_submit = True
    session.add(gh_site)
    session.commit()

    _, j1 = _li_job(session, demo, apply_id="701")
    _, j2 = _li_job(session, demo, apply_id="702")
    _contexts, runner_calls, _notified = _wire_external(monkeypatch, {
        j1.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        j2.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/acme/jobs/2", "greenhouse"),
    })
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=False)

    assert len([c for c in runner_calls if c["submit"]]) == 1  # one real submit, then the cap stops the run
    assert len([o for o in out if o.status == AppStatus.applied]) == 1


def test_external_linkedin_site_cap_fills_for_review(session, demo, monkeypatch):
    """Once the resolved site is at its own daily cap, the posting is filled for review (dry run), never
    submitted for real, so the per-site cap cannot be exceeded."""
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _allow_real_submits(session, monkeypatch)
    _enable_linkedin(session, demo)
    gh_site = session.exec(
        select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()
    gh_site.auto_submit = True
    gh_site.daily_cap = 1
    session.add(gh_site)
    session.commit()

    _, j1 = _li_job(session, demo, apply_id="711")
    _, j2 = _li_job(session, demo, apply_id="712")
    _contexts, runner_calls, _notified = _wire_external(monkeypatch, {
        j1.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        j2.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/acme/jobs/2", "greenhouse"),
    })
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=False)

    # First submits for real (applied); second is over the site cap -> filled only (dry_run), not submitted.
    assert [c["submit"] for c in runner_calls] == [True, False]
    assert [o.status for o in out] == [AppStatus.applied, AppStatus.dry_run]


def test_external_linkedin_disabled_site_not_submitted(session, demo, monkeypatch):
    """A disabled resolved ATS site is filled for review, never submitted for real, even with auto_submit on.
    Regression: the submit gate ignored site.enabled, so a disabled site could still get a real submit."""
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _allow_real_submits(session, monkeypatch)
    _enable_linkedin(session, demo)
    gh_site = session.exec(
        select(UserSite).where(UserSite.user_id == demo.id, UserSite.site == "greenhouse")).one()
    gh_site.enabled = False
    gh_site.auto_submit = True
    session.add(gh_site)
    session.commit()

    _, j1 = _li_job(session, demo, apply_id="801")
    _contexts, runner_calls, _notified = _wire_external(monkeypatch, {
        j1.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/acme/jobs/5", "greenhouse")})
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=False)

    assert len(runner_calls) == 1 and runner_calls[0]["submit"] is False
    assert out[0].status == AppStatus.dry_run


def test_external_linkedin_skips_when_session_expired(session, demo, monkeypatch):
    from jobapply.pipeline.apply import apply_external_linkedin_user
    from jobapply.sites.linkedin.resolve import ApplyResolution

    _enable_linkedin(session, demo)
    app, job = _li_job(session, demo, apply_id="601")
    contexts, runner_calls, notified = _wire_external(
        monkeypatch, {job.apply_url: ApplyResolution("offsite", "https://boards.greenhouse.io/x/jobs/1",
                                                     "greenhouse")}, status="expired")
    out = apply_external_linkedin_user(None, session, demo, limit=5, dry_run=True)
    assert out == [] and runner_calls == []
    assert notified and notified[-1][0] == "paused" and "login" in notified[-1][1]


# ------------------------------------------------------------------ role taxonomy integration


def test_role_selection_gates_matching_like_roles():
    from jobapply.ai.heuristics import score_job_heuristic

    cyber = _job(title="Cyber Threat Intelligence Analyst", description="SIEM, threat intel, SOC work")
    frontend = _job(title="Frontend Engineer", description="React and CSS")

    f = UserFilters(role_selection=["cybersecurity:*"])
    assert match(cyber, f)[0]
    assert match(frontend, f)[1] == "role not matched"

    # A narrow sub-role selection only matches its own titles.
    f2 = UserFilters(role_selection=["cybersecurity:pentest"])
    assert match(_job(title="Senior Penetration Tester", description="x"), f2)[0]
    assert match(cyber, f2)[1] == "role not matched"

    # Free-text roles keep working alongside a taxonomy selection (either may satisfy the gate).
    f3 = UserFilters(roles=["frontend engineer"], role_selection=["cybersecurity:soc"])
    assert match(frontend, f3)[0]
    assert match(_job(title="SOC Analyst", description="x"), f3)[0]

    # The expansion is folded into keywords_include on serialisation, so the heuristic scorer (which reads
    # the stored dict) treats the synonyms as title/keyword hits.
    profile = {"skills": {"python": 3}}
    stored = UserFilters(role_selection=["cybersecurity:*"]).model_dump()
    assert "threat intelligence" in stored["keywords_include"]
    with_sel = score_job_heuristic(profile, stored, cyber).score
    without = score_job_heuristic(profile, UserFilters().model_dump(), cyber).score
    assert with_sel > without


def test_assign_jobs_matches_cyber_user_by_role_selection(session):
    sel = UserFilters(role_selection=["cybersecurity:*"]).model_dump()
    cat = User(username="cat", password_hash="x", filters=sel)
    session.add(cat)
    session.commit()
    ids = store_jobs(session, [
        nj(title="Security Operations Center Analyst", source_id="soc1", description="SOC monitoring"),
        nj(title="Frontend Engineer", source_id="fe1", description="React and CSS"),
    ])
    assert assign_jobs(session, ids)["cat"] == 1
    got = [session.get(Job, a.job_id).title
           for a in session.exec(select(Application).where(Application.user_id == cat.id)).all()]
    assert got == ["Security Operations Center Analyst"]
