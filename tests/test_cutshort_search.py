"""Cutshort public collector: synthetic payloads shaped like the live `backend-api/webpage/jobs/<slug>` records
(recorded 2026-10-29, field names kept, values invented) served through an httpx MockTransport. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobapply.sites.cutshort import search as cs
from jobapply.sites.cutshort.search import (
    HUB_URL,
    JOB_API_URL,
    PAGE_SIZE,
    CutshortCollector,
    city_slug,
    hub_candidates,
    is_hub_slug,
    job_slug_from_url,
    parse_hub_payload,
    parse_job,
    parse_job_payload,
    public_cooldown_active,
    search_once,
    set_public_cooldown,
    slugify,
)
from jobapply.switches import get_value

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _job(job_id: str, title: str = "Backend Engineer", hours_ago: float = 1.0, **kw) -> dict:
    posted = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rec = {
        "_id": job_id,
        "publicUrl": f"https://cutshort.io/job/{slugify(title).title()}-Acme-{job_id[-8:]}",
        "headline": title,
        "hiringForClient": False,
        "allSkills": ["Python", "FastAPI"],
        "allSkillsObj": {"00360": "Python", "00001": "FastAPI"},
        "salaryRange": {"currency": "INR", "min": 1500000, "max": 3000000, "userMin": 1500000, "userMax": 3000000,
                        "hideSalary": True},
        "locations": [],
        "companyDetails": {"_id": "c1", "name": "Acme Labs", "type": "Product", "size": "20-100",
                           "stage": "Raised funding"},
        "expRange": {"min": 3, "max": 6, "maxInternal": 6},
        "sanitizedComment": "<p><strong>Role</strong></p><ul><li>Build APIs</li><li>Own services</li></ul>",
        "roleTypes": ["full_time"],
        "remoteType": "onsite",
        "companyId": {"alias": "acme-labs-x1", "name": "Acme Labs", "_id": "c1"},
        "authApplyUrl": f"https://cutshort.io/profile/view/j/{job_id}",
        "isJobSaved": False,
        "jobFactSummary": {"roleTitle": title, "companyName": "Acme Labs", "locations": "Pune",
                           "companyStage": "Raised funding", "remotePolicy": "Onsite", "experience": "3 - 6 years",
                           "postedDate": posted, "postedDateLabel": "Posted 29 Oct 2026"},
        "salaryRangeText": "Best in industry",
        "locationsText": "Pune",
    }
    rec.update(kw)
    return rec


def _hub(jobs: list[dict], count: int | None = None, slug: str = "python-jobs") -> dict:
    page_data = {"jobs": jobs, "title": "Python Jobs", "liveJobCount": count or len(jobs)}
    return {"success": True, "data": {"seoData": {"canonical": f"https://cutshort.io/jobs/{slug}"},
                                      "pageContext": {"optimizeForRole": "candidate"}, "pageData": page_data},
            "meta": {"source": "live"}}


UNKNOWN = {"success": False,
           "error": {"errorMsg": "x page config not found", "errorCode": "jobs_page_config_not_found"}}


class Routes:
    """MockTransport: hub pages keyed by (slug, page); unknown slugs answer the live 400 body; `status`
    keys ("slug", "slug:2", job slug) answer with that HTTP status."""

    def __init__(self, hubs: dict[tuple[str, int], dict] | None = None, jobs: dict[str, dict] | None = None,
                 status: dict[str, int] | None = None):
        self.hubs, self.jobs, self.status = hubs or {}, jobs or {}, status or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path.startswith("/backend-api/webpage/jobs/"):
            slug = path.rsplit("/", 1)[-1]
            page = int(req.url.params.get("page", 1))
            key = slug if page == 1 else f"{slug}:{page}"
            if key in self.status:
                return httpx.Response(self.status[key], text="slow down", headers={"retry-after": "7200"})
            if (slug, page) in self.hubs:
                return httpx.Response(200, json=self.hubs[(slug, page)])
            if any(s == slug for s, _p in self.hubs):
                return httpx.Response(200, json=_hub([], 0, slug))  # past the last page
            return httpx.Response(400, json=UNKNOWN)
        if path.startswith("/backend-api/webpage/job/"):
            slug = path.rsplit("/", 1)[-1]
            if slug in self.status:
                return httpx.Response(self.status[slug], text="")
            if slug in self.jobs:
                return httpx.Response(200, json={"success": True, "data": {"pageData": self.jobs[slug]}})
            return httpx.Response(404, json={"success": False})
        return httpx.Response(404, text="")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)

    def slugs(self) -> list[str]:
        return [f"{r.url.path.rsplit('/', 1)[-1]}:{r.url.params.get('page', 1)}" for r in self.requests]


def collector(routes: Routes, queries=("python developer",), **kw) -> CutshortCollector:
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("now", NOW)
    return CutshortCollector(list(queries), kw.pop("location", ""), kw.pop("hours_old", 24),
                             client=routes.client(), **kw)


# ----------------------------------------------------------------- parsing


def test_parse_job_record():
    rec = _job("6a69d90f4613efecf4d13392", "Senior Python Developer", hours_ago=2)
    j = parse_job(rec, hub_slug="python-jobs")
    assert j is not None
    assert (j.title, j.company, j.location) == ("Senior Python Developer", "Acme Labs", "Pune")
    assert j.source == "cutshort" and j.ats_type == "cutshort" and j.source_id == "6a69d90f4613efecf4d13392"
    assert j.url == j.apply_url == rec["publicUrl"]
    assert j.posted_at == NOW - timedelta(hours=2)
    assert j.remote is False
    assert "Build APIs" in j.description and "<" not in j.description and "Skills: Python, FastAPI" in j.description
    assert j.salary_min is None and j.salary_max is None and j.currency is None  # hideSalary
    assert j.raw["salary_text"] == "Best in industry"
    assert j.raw["slug"] == rec["publicUrl"].rsplit("/", 1)[-1] and j.raw["hub_slug"] == "python-jobs"
    assert j.raw["auth_apply_url"].endswith("/profile/view/j/6a69d90f4613efecf4d13392")
    assert j.raw["skills"] == ["Python", "FastAPI"] and (j.raw["exp_min"], j.raw["exp_max"]) == (3, 6)
    assert j.raw["one_click_apply"] is True and j.raw["company_type"] == "Product"
    # a listing with a visible salary band and a remote policy
    rec = _job("abc", "Platform Engineer", remoteType="remote_only", locationsText="Remote only",
               salaryRange={"currency": "USD", "min": 50000, "max": 90000, "userMin": 60000, "userMax": 90000,
                            "hideSalary": False})
    j = parse_job(rec)
    assert j.remote is True and j.location == "Remote only"
    assert (j.salary_min, j.salary_max, j.currency) == (60000.0, 90000.0, "USD")
    # the job endpoint's shape: locations as a string, salary as text, no listing-only fields
    detail = _job("def", "Data Engineer", locations="Bengaluru (Bangalore)", salaryRange="Best in industry")
    detail.pop("locationsText")
    detail.pop("salaryRangeText")
    j = parse_job(detail)
    assert j.location == "Bengaluru (Bangalore)" and j.raw["salary_text"] == "Best in industry"
    assert j.salary_min is None


def test_parse_job_skips_junk_and_closed_postings():
    assert parse_job({}) is None
    assert parse_job(_job("x", title="")) is None
    assert parse_job(_job("x", jobValid=False)) is None
    assert parse_job("nope") is None
    jobs, count = parse_hub_payload(_hub([_job("a"), {"_id": "b"}, _job("c")], 4283))
    assert [j.source_id for j in jobs] == ["a", "c"] and count == 4283
    assert parse_hub_payload(UNKNOWN) == ([], None) and parse_hub_payload("x") == ([], None)
    assert parse_job_payload({"success": True, "data": {"pageData": _job("z")}}).source_id == "z"
    assert parse_job_payload(UNKNOWN) is None
    assert job_slug_from_url("https://cutshort.io/job/Senior-Dev-Acme-koBBDTQ1") == "Senior-Dev-Acme-koBBDTQ1"
    assert job_slug_from_url("https://cutshort.io/jobs/python-jobs") == ""


def test_hub_candidates_and_city_slugs():
    assert hub_candidates("python developer", "Bengaluru, India") == [
        "python-developer-jobs-in-bangalore-bengaluru", "python-jobs-in-bangalore-bengaluru",
        "python-developer-jobs", "python-jobs"]
    assert hub_candidates("python", "India") == ["python-jobs"]
    assert hub_candidates("Senior Backend Engineer") == ["senior-backend-engineer-jobs", "backend-engineer-jobs",
                                                         "backend-jobs"]
    assert hub_candidates("python", remote_only=True) == ["remote-python-jobs"]
    assert hub_candidates("remote react jobs") == ["remote-react-jobs"]
    assert hub_candidates("Python-Jobs-In-Pune") == ["python-jobs-in-pune"]  # explicit hub slug, as is
    assert hub_candidates("devops engineer", "Gurgaon") == ["devops-engineer-jobs-in-delhi-ncr-gurgaon-noida",
                                                            "devops-jobs-in-delhi-ncr-gurgaon-noida",
                                                            "devops-engineer-jobs", "devops-jobs"]
    assert hub_candidates("", "Pune") == [] and hub_candidates("jobs") == []
    assert hub_candidates("c++ developer") == ["cplusplus-developer-jobs", "cplusplus-jobs"]
    assert len(hub_candidates("senior staff principal backend platform engineer", "Pune")) == cs.MAX_SLUG_TRIES
    assert city_slug("Bengaluru, India") == "bangalore-bengaluru" and city_slug("Kochi") == "kochi-cochin"
    assert city_slug("Noida") == "delhi-ncr-gurgaon-noida" and city_slug("Pune, Maharashtra") == "pune"
    assert city_slug("India") == "" and city_slug("Remote") == "" and city_slug("Berlin") == "" and city_slug("") == ""
    assert is_hub_slug("remote-python-jobs") and is_hub_slug("python-jobs-in-pune") and not is_hub_slug("python")
    assert slugify("Node.js / React") == "node-js-react" and slugify("C#") == "csharp"


# ----------------------------------------------------------------- collector


def test_collector_resolves_hub_with_fallback_and_shapes_jobs():
    routes = Routes(hubs={("python-jobs", 1): _hub([_job("a", hours_ago=1), _job("b", hours_ago=3)])})
    c = collector(routes, location="Bengaluru")
    jobs = c.collect()
    # the city hubs and python-developer-jobs do not exist (400): the India-wide python hub answers
    assert routes.slugs() == ["python-developer-jobs-in-bangalore-bengaluru:1", "python-jobs-in-bangalore-bengaluru:1",
                              "python-developer-jobs:1", "python-jobs:1"]
    assert [j.source_id for j in jobs] == ["a", "b"] and c.resolved_hubs == {"python developer": "python-jobs"}
    assert all(j.source == "cutshort" and j.ats_type == "cutshort" for j in jobs)
    assert all(j.apply_url.startswith("https://cutshort.io/job/") for j in jobs)
    assert c.rate_limited is False and c.requests_made == 4
    assert c.key == "cutshort:public" and c.name == "cutshort" and c.source == "cutshort"
    h = CutshortCollector(["x"]).client.headers  # the real client: browser UA, JSON accept, site referer
    assert "Mozilla" in h["User-Agent"] and h["Accept"].startswith("application/json")
    assert h["Referer"].startswith("https://cutshort.io/")
    # no hub at all: an empty result, one cheap 400 per candidate, no block
    routes = Routes()
    c = collector(routes, queries=["cobol"])
    assert c.collect() == [] and routes.slugs() == ["cobol-jobs:1"] and c.rate_limited is False


def test_pagination_stops_on_age_window_short_page_and_max_results():
    assert PAGE_SIZE == 50
    page1 = [_job(f"p1-{i}", hours_ago=i / 10) for i in range(PAGE_SIZE)]
    page2 = [_job(f"p2-{i}", hours_ago=10 + i) for i in range(PAGE_SIZE)]  # the tail is older than 24 h
    page3 = [_job(f"p3-{i}", hours_ago=100) for i in range(PAGE_SIZE)]
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500), ("python-jobs", 2): _hub(page2, 500),
                          ("python-jobs", 3): _hub(page3, 500)})
    c = collector(routes, queries=["python"], max_results=500, hours_old=24)
    jobs = c.collect()
    assert routes.slugs() == ["python-jobs:1", "python-jobs:2"]  # page 2 crossed the window: page 3 never fetched
    assert len(jobs) == PAGE_SIZE + 15  # p2-0 .. p2-14 are within 24 h (10 + 14 = 24 h, the boundary is inclusive)
    assert all(j.posted_at >= NOW - timedelta(hours=24) for j in jobs)

    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500), ("python-jobs", 2): _hub(page2, 500)})
    jobs = collector(routes, queries=["python"], max_results=20, hours_old=48).collect()
    assert len(jobs) == 20 and routes.slugs() == ["python-jobs:1"]

    short = [_job(f"s-{i}") for i in range(7)]
    routes = Routes(hubs={("python-jobs", 1): _hub(short, 7)})
    jobs = collector(routes, queries=["python"], max_results=500).collect()
    assert len(jobs) == 7 and routes.slugs() == ["python-jobs:1"]  # a short page ends the hub

    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 50)})
    jobs = collector(routes, queries=["python"], max_results=500, hours_old=48).collect()
    assert len(jobs) == 50 and routes.slugs() == ["python-jobs:1", "python-jobs:2"]  # empty page 2 ends it


def test_rate_limit_stops_run_keeps_results_and_records_cooldown():
    page1 = [_job(f"p1-{i}") for i in range(PAGE_SIZE)]
    for status in (429, 403, 503):
        routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500)}, status={"python-jobs:2": status})
        cooldowns: list[float] = []
        c = collector(routes, queries=["python", "django"], max_results=500, on_rate_limited=cooldowns.append)
        jobs = c.collect()
        assert len(jobs) == PAGE_SIZE and c.rate_limited is True
        assert routes.slugs() == ["python-jobs:1", "python-jobs:2"]  # the second query never started
        assert cooldowns == [2.0]  # Retry-After 7200 s == the 2 h default
    # any other 4xx/5xx on the public endpoint also ends the run, without a cooldown
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500)}, status={"python-jobs:2": 500})
    cooldowns = []
    c = collector(routes, queries=["python", "django"], max_results=500, on_rate_limited=cooldowns.append)
    assert len(c.collect()) == PAGE_SIZE and c.rate_limited is True and cooldowns == []
    # a transport error is logged and the query ends; nothing is recorded
    def boom(req):
        raise httpx.ConnectError("down", request=req)

    c = CutshortCollector(["python"], client=httpx.Client(transport=httpx.MockTransport(boom)), sleep=lambda s: None)
    assert c.collect() == [] and c.rate_limited is False


def test_budget_skip_if_and_dedupe_across_queries():
    page1 = [_job(f"p1-{i}") for i in range(PAGE_SIZE)]
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500), ("python-jobs", 2): _hub(page1, 500),
                          ("django-jobs", 1): _hub(page1[:5], 5)})
    c = collector(routes, queries=["python", "django"], max_results=500, max_requests=2, hours_old=48)
    jobs = c.collect()
    assert len(routes.requests) == 2 and c.budget_exhausted is True and c.rate_limited is False
    assert len(jobs) == PAGE_SIZE  # page 2 repeated the ids; django never requested
    routes = Routes(hubs={("python-jobs", 1): _hub(page1[:3], 3), ("django-jobs", 1): _hub(page1[1:4], 3)})
    jobs = collector(routes, queries=["python", "django", "python"], max_results=500).collect()
    assert [j.source_id for j in jobs] == ["p1-0", "p1-1", "p1-2", "p1-3"]  # merged once
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 500)})
    c = collector(routes, skip_if=lambda: True)
    assert c.collect() == [] and routes.requests == []
    # explicit hub slugs ride along with the queries; user_id and unknown options are accepted and ignored
    routes = Routes(hubs={("remote-python-jobs", 1): _hub(page1[:2], 2)})
    c = collector(routes, queries=[], hub_slugs=["remote-python-jobs", "not a slug"], user_id=7, foo="bar")
    assert [j.source_id for j in c.collect()] == ["p1-0", "p1-1"] and c.user_id == 7


def test_fetch_job_and_politeness_delay():
    rec = _job("6a69d90f4613efecf4d13392", "Sr Backend Engineering Manager")
    routes = Routes(jobs={"Sr-Backend-Engineering-Manager-Egnyte-koBBDTQ1": rec})
    slept: list[float] = []
    c = CutshortCollector(["x"], client=routes.client(), delay=2.0, sleep=slept.append)
    j = c.fetch_job("Sr-Backend-Engineering-Manager-Egnyte-koBBDTQ1")
    assert j and j.title == "Sr Backend Engineering Manager" and j.raw["job_id"] == "6a69d90f4613efecf4d13392"
    assert slept == []  # no sleep before the first request
    assert c.fetch_job("missing") is None and c.rate_limited is True  # a 404 ends the run
    assert len(slept) == 1 and 1.4 <= slept[0] <= 2.6
    assert str(routes.requests[0].url) == JOB_API_URL.format(slug="Sr-Backend-Engineering-Manager-Egnyte-koBBDTQ1")
    assert HUB_URL.format(slug="python-jobs").endswith("/backend-api/webpage/jobs/python-jobs")


def test_search_once_helper():
    page1 = [_job(f"p1-{i}") for i in range(3)]
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 3)})
    jobs = search_once("python", "India", 48, delay=0, client=routes.client())
    assert len(jobs) == 3 and jobs[0].source == "cutshort"
    routes = Routes(hubs={("python-jobs", 1): _hub(page1, 3)})
    jobs = search_once(["python", "python-jobs"], delay=0, client=routes.client())
    assert len(jobs) == 3


def test_public_payload_roundtrips_through_json():
    # the records are plain JSON (as the CDN serves them): a dumps/loads cycle must not change the parse
    rec = json.loads(json.dumps(_job("a", hours_ago=5)))
    assert parse_job(rec).posted_at == NOW - timedelta(hours=5)


# ----------------------------------------------------------------- cross-run cooldown


def test_public_cooldown_persists_and_doubles(session):
    assert cs.public_cooldown_until(session) is None and public_cooldown_active(session) is False
    now = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    until = set_public_cooldown(session, 2.0, now=now)
    session.commit()
    assert until == now + timedelta(hours=2)
    assert get_value(session, cs.PUBLIC_COOLDOWN_KEY) == until.isoformat()
    assert public_cooldown_active(session, now=now + timedelta(hours=1)) is True
    assert public_cooldown_active(session, now=now + timedelta(hours=3)) is False
    assert set_public_cooldown(session, 2.0, now=now + timedelta(hours=1)) == now + timedelta(hours=3)
    assert set_public_cooldown(session, 500, now=now) == now + timedelta(hours=48)


@pytest.mark.parametrize("value", ["remote-python-jobs", "python-jobs-in-pune", "ai-ml-jobs"])
def test_explicit_hub_slug_is_used_verbatim(value):
    assert hub_candidates(value) == [value]
