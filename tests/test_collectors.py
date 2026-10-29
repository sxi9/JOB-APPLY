from datetime import UTC, datetime

import pytest

from jobapply.collectors.apis import (
    AdzunaCollector,
    ArbeitnowCollector,
    JobicyCollector,
    JSearchCollector,
    RemoteOKCollector,
    RemotiveCollector,
    TheMuseCollector,
)
from jobapply.collectors.ats import (
    AshbyCollector,
    GreenhouseCollector,
    LeverCollector,
    SmartRecruitersCollector,
    WorkableCollector,
)
from jobapply.collectors.base import CollectorError, html_to_text, parse_dt, parse_salary
from jobapply.collectors.detector import detect, detect_from_url
from jobapply.collectors.hn import HNWhoIsHiringCollector
from jobapply.collectors.jobspy_adapter import JobSpyCollector
from jobapply.collectors.registry import Company, SearchConfig, build_collectors, detect_companies
from jobapply.config import Settings

from .helpers import fixture_client


def _check(jobs, source, n):
    assert len(jobs) == n
    for j in jobs:
        assert j.source == source
        assert j.title and j.company and j.source_id
        assert j.url.startswith("http") and j.apply_url.startswith("http")
        assert j.posted_at is None or j.posted_at.tzinfo is not None
        assert "<p>" not in j.description


def test_greenhouse():
    jobs = GreenhouseCollector("stripe", client=fixture_client({"greenhouse": "greenhouse_stripe.json"})).collect()
    _check(jobs, "greenhouse", 3)
    j = jobs[0]
    assert j.company == "Stripe" and j.ats_type == "greenhouse"
    assert j.source_id.startswith("stripe:")
    assert j.posted_at == datetime(2026, 9, 3, 17, 32, 53, tzinfo=UTC)
    assert "&lt;" not in j.description and "<h2>" not in j.description


def test_lever():
    jobs = LeverCollector("palantir", "Palantir", fixture_client({"lever.co": "lever_palantir.json"})).collect()
    _check(jobs, "lever", 3)
    assert jobs[0].location == "Singapore, Singapore"
    assert jobs[0].apply_url.endswith("/apply")


def test_ashby_salary_and_remote():
    jobs = AshbyCollector("ramp", "Ramp", fixture_client({"ashbyhq": "ashby_ramp.json"})).collect()
    _check(jobs, "ashby", 3)
    j = jobs[0]
    assert j.remote is True
    assert (j.salary_min, j.salary_max, j.currency) == (211400.0, 290600.0, "USD")


def test_workable():
    jobs = WorkableCollector("huggingface", client=fixture_client({"workable": "workable_huggingface.json"})).collect()
    _check(jobs, "workable", 3)
    assert jobs[0].company == "Hugging Face" and jobs[0].remote is True


def test_smartrecruiters():
    jobs = SmartRecruitersCollector("Experian", client=fixture_client(
        {"smartrecruiters": "smartrecruiters_experian.json"})).collect()
    _check(jobs, "smartrecruiters", 2)
    assert "Costa Rica" in jobs[0].location


@pytest.mark.parametrize("cls,routes,source,n", [
    (RemotiveCollector, {"remotive": "remotive.json"}, "remotive", 3),
    (RemoteOKCollector, {"remoteok": "remoteok.json"}, "remoteok", 3),
    (JobicyCollector, {"jobicy": "jobicy.json"}, "jobicy", 3),
    (ArbeitnowCollector, {"arbeitnow": "arbeitnow.json"}, "arbeitnow", 3),
    (TheMuseCollector, {"themuse": "themuse.json"}, "themuse", 3),
])
def test_feeds(cls, routes, source, n):
    _check(cls(client=fixture_client(routes)).collect(), source, n)


def test_hn_who_is_hiring():
    c = HNWhoIsHiringCollector(client=fixture_client({"search_by_date": "hn_search.json",
                                                      "items/49922569": "hn_items.json"}))
    assert c.latest_story_id() == 49922569
    jobs = c.collect()
    assert len(jobs) == 4
    by_company = {j.company: j for j in jobs}
    sidekick = by_company["Sidekick Labs"]
    assert sidekick.title.startswith("Senior SWE") and sidekick.remote
    assert sidekick.location == "REMOTE (US Timezones)"
    shepherd = by_company["Shepherd (Series B)"]
    assert shepherd.location.startswith("San Francisco") and shepherd.title == "Multiple roles (see post)"
    assert by_company["PrairieLearn (Remote US)"].title == "Full-Stack Software Engineer"
    assert sidekick.url.startswith("https://news.ycombinator.com/item?id=")


def test_adzuna_and_jsearch():
    seen = []
    jobs = AdzunaCollector("id", "key", ["python"], client=fixture_client(
        {"adzuna": "adzuna_synthetic.json"}, seen)).collect()
    _check(jobs, "adzuna", 2)
    assert jobs[0].title == "Senior Python Developer" and jobs[1].remote
    assert "app_key=key" in str(seen[0].url) and "what=python" in str(seen[0].url)

    seen.clear()
    jobs = JSearchCollector("rk", ["platform engineer"], client=fixture_client(
        {"jsearch": "jsearch_synthetic.json"}, seen)).collect()
    _check(jobs, "jsearch", 1)
    assert jobs[0].ats_type == "greenhouse"
    assert seen[0].headers["X-RapidAPI-Key"] == "rk"
    with pytest.raises(CollectorError):
        JSearchCollector("", ["x"])


def test_jobspy_with_fake_scraper():
    calls = []

    def fake_scrape(**kw):
        calls.append(kw)
        return [
            {"id": "in-1", "site": "indeed", "title": "Data Engineer", "company": "Acme",
             "location": "Remote, IN", "date_posted": "2026-10-28", "job_url": "https://in.indeed.com/viewjob?jk=1",
             "job_url_direct": "https://jobs.lever.co/acme/123", "description": "Spark", "is_remote": True,
             "min_amount": float("nan"), "max_amount": float("nan"), "currency": None},
            {"title": None, "job_url": None},
        ]

    jobs = JobSpyCollector("indeed", ["data engineer"], scrape=fake_scrape).collect()
    assert len(jobs) == 1 and jobs[0].ats_type == "lever" and jobs[0].salary_min is None
    assert calls[0]["site_name"] == ["indeed"] and calls[0]["hours_old"] == 24


def test_non_200_raises():
    with pytest.raises(CollectorError):
        GreenhouseCollector("nope", client=fixture_client({})).collect()


@pytest.mark.parametrize("url,ats,slug", [
    ("https://boards.greenhouse.io/stripe", "greenhouse", "stripe"),
    ("https://job-boards.greenhouse.io/embed/job_board?for=figma", "greenhouse", "figma"),
    ("https://jobs.lever.co/palantir/abc", "lever", "palantir"),
    ("https://jobs.ashbyhq.com/ramp", "ashby", "ramp"),
    ("https://apply.workable.com/huggingface/", "workable", "huggingface"),
    ("https://jobs.smartrecruiters.com/Visa/123", "smartrecruiters", "Visa"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "workday", "nvidia"),
])
def test_detect_from_url(url, ats, slug):
    hit = detect_from_url(url)
    assert hit and (hit.ats, hit.slug) == (ats, slug)


def test_detect_follows_page_content():
    import httpx

    def handler(req):
        return httpx.Response(200, text='<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">')

    hit = detect("https://acme.example/careers", httpx.Client(transport=httpx.MockTransport(handler)))
    assert (hit.ats, hit.slug) == ("greenhouse", "acme")
    assert detect_from_url("https://example.com/careers") is None


def test_registry_builds_and_detects(tmp_path):
    companies = [Company("Stripe", "greenhouse", "stripe"), Company("Notion", careers_url="https://jobs.ashbyhq.com/notion"),
                 Company("Big Co", "workday", "bigco")]
    assert detect_companies(companies) == 1
    s = Settings(_env_file=None, rapidapi_key="k")
    cols = build_collectors(s, companies, SearchConfig(queries=["python"], feeds=["remotive"]))
    keys = [c.key for c in cols]
    assert keys == ["greenhouse:stripe", "ashby:notion", "remotive", "jsearch"]
    assert [c.key for c in build_collectors(s, companies, SearchConfig(feeds=[]), only=["ashby"])] == ["ashby:notion"]


def test_lever_flags_stale_evergreen_posting_without_faking_date():
    from datetime import UTC, datetime, timedelta

    from jobapply.collectors.ats import LeverCollector, is_stale_posting

    c = LeverCollector("acme", "Acme")
    old_ms = int((datetime.now(UTC) - timedelta(days=2906)).timestamp() * 1000)  # "2906 d ago" evergreen req
    fresh_ms = int((datetime.now(UTC) - timedelta(days=3)).timestamp() * 1000)

    stale = c.parse({"id": "1", "text": "Staff Engineer", "createdAt": old_ms,
                     "categories": {"location": "Remote"}})
    fresh = c.parse({"id": "2", "text": "Staff Engineer", "createdAt": fresh_ms,
                     "categories": {"location": "Remote"}})

    # The real date is preserved (not faked), but the stale one is flagged in the stored source raw.
    assert stale.posted_at is not None and stale.posted_at.year < datetime.now(UTC).year
    assert stale.raw.get("stale_posting") is True
    assert "stale_posting" not in fresh.raw and fresh.posted_at is not None
    # The helper matches the default window (365d) and is overridable.
    assert is_stale_posting(stale.posted_at) is True
    assert is_stale_posting(fresh.posted_at) is False
    assert is_stale_posting(stale.posted_at, max_days=10000) is False
    assert is_stale_posting(None) is False


def test_helpers():
    assert parse_dt(1786469891368).year == 2026
    assert parse_dt("2026-05-29").tzinfo is not None
    assert parse_dt("garbage") is None
    assert parse_salary("₹12,00,000 - ₹18,00,000")[2] == "INR"
    assert parse_salary("no money here") == (None, None, None)
    assert html_to_text("<p>a</p><ul><li>b</li></ul>") == "a\n\n- b"
