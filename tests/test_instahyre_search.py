"""Instahyre public search collector: synthetic responses in the shape recorded live on 2026-10-29
(``/api/v1/job_search`` JSON, the server-rendered opportunity page with its JobPosting ld+json block)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from jobapply.sites.browser import DESKTOP_USER_AGENT
from jobapply.sites.instahyre.search import (
    JOB_TYPES,
    PAGE_SIZE,
    SEARCH_URL,
    InstahyreCollector,
    JobDetails,
    apply_details,
    is_blocked_url,
    is_fresh,
    job_id_from,
    matches_location,
    parse_card,
    parse_experience,
    parse_job_json,
    parse_job_page,
    parse_search_json,
    search_once,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
LOGIN = "https://www.instahyre.com/login/?next=/api/v1/job_search"
CF_HTML = ("<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><form id='challenge-form'>"
           "<div class='cf-turnstile'></div></form></body></html>")


def card(job_id: int, title: str = "Python Developer", company: str = "Acme Labs", locations: str = "Bangalore",
         keywords: list[str] | None = None) -> dict:
    """One ``objects[]`` row as the search API returns it."""
    return {
        "resource_uri": f"/api/v1/job_search/{job_id}", "interview_status": None, "reviewed_at": None,
        "public_url": f"https://www.instahyre.com/job-{job_id}-{title.lower().replace(' ', '-')}-at-acme/",
        "is_strong_match": None, "score": None, "candidate_title": title, "id": job_id, "title": title,
        "locations": locations, "keywords": keywords or ["Backend", "FastAPI", "Python"],
        "accept_outstation": True, "gender": 0, "is_sourced": False,
        "employer": {"resource_uri": "/api/v1/candidate_opportunity_employer/50", "id": 50, "company_name": company,
                     "company_tagline": "We build things", "company_founded": 2015, "employee_count": 3,
                     "instahyre_note": None, "profile_image_src": None, "show_diversity_employer_icon": False,
                     "show_featured_employer_icon": False},
    }


def search_response(ids: list[int], total: int | None = None, offset: int = 0, **kw) -> dict:
    total = len(ids) if total is None else total
    more = offset + len(ids) < total
    nxt = f"/api/v1/job_search?skills=python&limit={PAGE_SIZE}&offset={offset + PAGE_SIZE}" if more else None
    return {"objects": [card(i, **kw) for i in ids],
            "meta": {"offset": offset, "limit": PAGE_SIZE, "total_count": total, "next": nxt, "previous": None,
                     "job_type_counts": {"full_time": total, "internship": 0}}}


def job_page(job_id: int = 447614, posted: str = "2026-10-28", experience: str = "4-8 Years",
             description: str = "<p>We need a <strong>Python</strong> engineer.</p><ul><li>FastAPI</li></ul>") -> str:
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "employmentType": "FULL_TIME",
          "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress", "addressRegion": "Karnataka",
                                                         "addressLocality": "Bangalore", "addressCountry": "India"}}],
          "title": "Python Developer", "description": f"<html><body>{description}</body></html>",
          "hiringOrganization": {"@type": "Organization", "name": "Acme Labs"}, "datePosted": posted}
    return f"""<!DOCTYPE html><html><head><title>Python Developer job at Acme Labs - Instahyre</title></head>
<body><div id="employer-profile" ng-controller="anonEmployerProfileCtrl">
<div class="job-locations"><span class="location-span">Bangalore</span>
<span class="experience"><img src="x.svg" alt="">{experience}</span></div>
<div class="apply-button" ng-controller="candidateOpportunityCtrl">
<button id="job_details_top" type="button" ng-click="setPostAuthRedirectionUrl()">Apply now</button></div>
<div class="job-posted-by-info"><span class="rec-name">Recruiter Person</span></div>
<div class="profile-content job-description">
<div class="quill-formatted"><html><body>{description}</body></html></div></div>
</div>
<script type="application/ld+json">{json.dumps(ld)}</script></body></html>"""


class Routes:
    """MockTransport: search pages keyed by offset, opportunity pages by job id. `status` keys ("offset=20" or a
    job id) answer with that HTTP status (403 with a Cloudflare body); `redirect` keys 302 to the login page."""

    def __init__(self, pages: dict[int, dict | str] | None = None, postings: dict[str, str] | None = None,
                 status: dict[str, int] | None = None, redirect: set[str] | None = None):
        self.pages, self.postings, self.status = pages or {}, postings or {}, status or {}
        self.redirect = redirect or set()
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith(LOGIN):
            return httpx.Response(200, text="<html><body><form ng-submit='loginUser()'></form></body></html>")
        if url.startswith(SEARCH_URL):
            params = dict(parse_qsl(urlsplit(url).query))
            offset = int(params.get("offset", 0))
            key = f"offset={offset}"
            if key in self.redirect:
                return httpx.Response(302, headers={"location": LOGIN})
            if key in self.status:
                code = self.status[key]
                return httpx.Response(code, text=CF_HTML if code == 403 else "slow down",
                                      headers={"retry-after": "10800"} if code == 429 else {})
            body = self.pages.get(offset, {"objects": [], "meta": {"total_count": 0}})
            return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)
        job_id = job_id_from(url) or ""
        if job_id in self.redirect:
            return httpx.Response(302, headers={"location": LOGIN})
        if job_id in self.status:
            code = self.status[job_id]
            return httpx.Response(code, text=CF_HTML if code == 403 else "")
        if job_id in self.postings:
            return httpx.Response(200, text=self.postings[job_id])
        return httpx.Response(404, text="")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


def collector(routes: Routes, **kw) -> InstahyreCollector:
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("with_details", False)
    kw.setdefault("now", NOW)
    return InstahyreCollector(kw.pop("queries", ["python"]), kw.pop("location", ""), kw.pop("hours_old", 24),
                              client=routes.client(), **kw)


def params_of(req: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(req.url)).query)


# ----------------------------------------------------------------- parsing


def test_parse_search_json_cards():
    jobs = parse_search_json(search_response([447614, 448474], locations="Mumbai,Navi Mumbai"))
    assert [j.source_id for j in jobs] == ["447614", "448474"]
    j = jobs[0]
    assert (j.title, j.company) == ("Python Developer", "Acme Labs")
    assert j.location == "Mumbai, Navi Mumbai"
    assert j.url == j.apply_url == "https://www.instahyre.com/job-447614-python-developer-at-acme/"
    assert j.source == "instahyre" and j.ats_type == "instahyre" and j.remote is False
    assert j.posted_at is None  # the search API carries no posting date
    assert j.raw["keywords"] == ["Backend", "FastAPI", "Python"] and j.raw["one_click_apply"] is True
    assert j.raw["employer_id"] == 50 and j.raw["employee_count"] == 3
    remote = parse_card(card(1, locations="Work From Home"))
    assert remote.remote is True and remote.location == "Work From Home"
    assert parse_card({"id": 5, "title": ""}) is None and parse_card("junk") is None
    assert parse_search_json({"meta": {}}) == [] and parse_search_json("nope") == []
    no_url = parse_card({"id": 9, "candidate_title": "Dev", "employer": {"company_name": "Co"}})
    assert no_url.url == "https://www.instahyre.com/job-9/" and no_url.company == "Co"
    assert job_id_from("https://www.instahyre.com/job-447614-python-developer-at-acme/") == "447614"
    assert job_id_from(447614) == "447614" and job_id_from("x") is None and job_id_from(None) is None


def test_parse_job_page_ld_json_and_chips():
    d = parse_job_page(job_page(), "447614")
    assert d.title == "Python Developer" and d.company == "Acme Labs" and d.location == "Bangalore"
    assert d.posted_at == datetime(2026, 10, 28, tzinfo=UTC) and d.employment_type == "FULL_TIME"
    assert d.description.startswith("We need a Python engineer.") and "- FastAPI" in d.description
    assert "<" not in d.description
    assert d.experience == "4-8 Years" and (d.workex_min, d.workex_max) == (4, 8)
    assert d.posted_by == "Recruiter Person" and d.empty is False and d.blocked is False
    assert parse_experience("10+ Years") == (10, None) and parse_experience("Fresher") == (None, None)
    # a Cloudflare interstitial or a login page is empty and (for Cloudflare) blocked
    cf = parse_job_page(CF_HTML, "1")
    assert cf.blocked is True and cf.empty is True
    login = parse_job_page("<html><body><form ng-submit='loginUser()'></form></body></html>", "1")
    assert login.empty is True and login.blocked is False


def test_parse_job_json_record():
    data = {"candidate_title": "Lead Python Engineer", "hiring_company_name": "Acme Labs",
            "locations": ["Bangalore", "Work From Home"], "description": "<p>Remote ok</p>", "is_internship": False,
            "workex_min": 6, "workex_max": 10, "recruiter_name": "Recruiter Person", "is_active": True}
    d = parse_job_json(data, "1")
    assert d.title == "Lead Python Engineer" and d.location == "Bangalore, Work From Home"
    assert d.description == "Remote ok" and d.experience == "6-10 Years" and d.employment_type == "FULL_TIME"
    assert d.posted_at is None
    assert parse_job_json("junk", "1").empty is True


def test_apply_details_merges_and_leaves_empty_pages_alone():
    base = parse_card(card(447614))
    merged = apply_details(base, parse_job_page(job_page(), "447614"))
    assert merged.posted_at == datetime(2026, 10, 28, tzinfo=UTC)
    assert merged.description.startswith("We need") and merged.raw["details_fetched"] is True
    assert merged.raw["experience"] == "4-8 Years" and merged.raw["workex_max"] == 8
    assert merged.raw["employment_type"] == "FULL_TIME" and merged.raw["posted_by"] == "Recruiter Person"
    assert merged.apply_url == base.apply_url and merged.ats_type == "instahyre"
    assert "details_fetched" not in base.raw  # input not mutated
    same = apply_details(base, JobDetails(job_id="447614"))
    assert same.raw.get("details_fetched") is None and same.posted_at is None
    blocked = apply_details(base, parse_job_page(CF_HTML, "447614"))
    assert blocked.raw.get("details_fetched") is None
    # a remote location on the page flips the remote flag
    remote_page = job_page().replace('"addressLocality": "Bangalore"', '"addressLocality": "Work From Home"')
    assert apply_details(parse_card(card(1, locations="")), parse_job_page(remote_page, "1")).remote is True


def test_freshness_and_location_filters():
    base = parse_card(card(1))
    assert is_fresh(base, 24, NOW) is True  # unknown date is not old
    assert is_fresh(base.model_copy(update={"posted_at": datetime(2026, 10, 28, tzinfo=UTC)}), 24, NOW) is True
    assert is_fresh(base.model_copy(update={"posted_at": datetime(2026, 10, 27, tzinfo=UTC)}), 24, NOW) is False
    assert is_fresh(base.model_copy(update={"posted_at": datetime(2026, 10, 25, tzinfo=UTC)}), 24 * 7, NOW) is True
    assert matches_location(base, "") and matches_location(base, "bangalore")
    assert not matches_location(base, "Pune")
    assert matches_location(parse_card(card(2, locations="Work From Home")), "Pune")  # remote matches anywhere
    assert is_blocked_url(LOGIN) and is_blocked_url("https://www.instahyre.com/x?__cf_chl_rt_tk=1")
    assert not is_blocked_url("https://www.instahyre.com/job-1-x/")


# ----------------------------------------------------------------- collector


def test_search_params_headers_and_job_types():
    routes = Routes(pages={0: search_response([1, 2, 3])})
    c = collector(routes, years=5, job_type="full_time")
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1", "2", "3"]
    p = params_of(routes.requests[0])
    assert ("skills", "python") in p and ("job_type", "1") in p and ("years", "5") in p
    assert ("limit", str(PAGE_SIZE)) in p and ("offset", "0") in p and ("isLandingPage", "true") in p
    assert ("search", "true") in p and not any(k == "location" for k, _ in p)  # the API ignores it
    h = routes.requests[0].headers
    assert h["user-agent"] == DESKTOP_USER_AGENT and h["accept"].startswith("application/json")
    assert h["x-requested-with"] == "XMLHttpRequest" and h["referer"].startswith("https://www.instahyre.com/")
    assert c.key == "instahyre:public" and c.source == "instahyre" and c.ats_type == "instahyre"
    assert c.user_id is None and c.hours_old == 24
    # a multi-word query is one skills value (never comma-joined: that is a 400 live)
    plain = InstahyreCollector(["python developer"], job_type=0, user_id=7)
    assert plain.search_params("python developer")[0] == ("skills", "python developer")
    assert plain.job_type == 0 and plain.user_id == 7
    assert InstahyreCollector(["x"], job_type="internship").job_type == JOB_TYPES["internship"]
    assert InstahyreCollector(["x"], job_type=2).job_type == 2
    with pytest.raises(ValueError):
        InstahyreCollector(["x"], job_type="contract")


def test_location_is_filtered_client_side():
    cards = [card(1, locations="Bangalore"), card(2, locations="Udaipur"), card(3, locations="Work From Home"),
             card(4, locations="Mumbai,Navi Mumbai")]
    routes = Routes(pages={0: {"objects": cards, "meta": {"total_count": 4}}})
    jobs = collector(routes, location="Bangalore").collect()
    assert [j.source_id for j in jobs] == ["1", "3"]
    jobs = collector(Routes(pages={0: search_response([1, 2])}), location="").collect()
    assert len(jobs) == 2


def test_pagination_uses_offset_and_total_count():
    assert PAGE_SIZE == 20
    first = list(range(100, 100 + PAGE_SIZE))
    second = list(range(200, 200 + PAGE_SIZE))
    routes = Routes(pages={0: search_response(first, total=45), 20: search_response(second, total=45, offset=20),
                           40: search_response([300, 301, 302, 303, 304], total=45, offset=40)})
    jobs = collector(routes, max_results=100).collect()
    assert len(jobs) == 45
    assert [int(dict(params_of(r))["offset"]) for r in routes.requests] == [0, 20, 40]
    # max_results stops early
    routes = Routes(pages={0: search_response(first, total=45), 20: search_response(second, total=45, offset=20)})
    assert len(collector(routes, max_results=25).collect()) == 25
    assert [int(dict(params_of(r))["offset"]) for r in routes.requests] == [0, 20]
    # a page repeating ids already seen ends the query
    routes = Routes(pages={0: search_response(first, total=60), 20: search_response(first, total=60, offset=20)})
    assert len(collector(routes, max_results=100).collect()) == PAGE_SIZE
    assert len(routes.requests) == 2
    routes = Routes(pages={0: {"objects": [], "meta": {"total_count": 0}}})
    assert collector(routes).collect() == [] and len(routes.requests) == 1
    # a non-JSON body is logged and ends the query without a block
    routes = Routes(pages={0: "<html>oops</html>"})
    c = collector(routes)
    assert c.collect() == [] and c.rate_limited is False


def test_request_budget_caps_a_run():
    first = list(range(100, 100 + PAGE_SIZE))
    second = list(range(200, 200 + PAGE_SIZE))
    postings = {str(i): job_page(i) for i in first + second}
    routes = Routes(pages={0: search_response(first, total=40), 20: search_response(second, total=40, offset=20)},
                    postings=postings)
    c = collector(routes, queries=["a"], max_results=100, max_requests=3, with_details=True)
    jobs = c.collect()
    assert len(routes.requests) == 3 and c.budget_exhausted is True and c.rate_limited is False
    assert len(jobs) == 40 and sum(1 for j in jobs if j.raw.get("details_fetched")) == 1
    # a wide query set keeps a slice of the budget for opportunity pages
    routes = Routes(pages={0: search_response(first, total=40), 20: search_response(second, total=40, offset=20)},
                    postings=postings)
    # 12-request budget, 6-detail cap: 4 are reserved, so the search phase stops after 8 pages (5 queries would
    # need 10) and enrichment still gets its slice.
    c = collector(routes, queries=["a", "b", "c", "d", "e"], max_results=100, max_requests=12, max_details=6,
                  with_details=True)
    jobs = c.collect()
    assert c.search_budget_reached is True and c.rate_limited is False
    searches = [r for r in routes.requests if str(r.url).startswith(SEARCH_URL)]
    assert len(searches) == 8 and len(routes.requests) == 12
    assert sum(1 for j in jobs if j.raw.get("details_fetched")) == 4


def test_cloudflare_and_rate_limit_stop_the_run():
    first = list(range(100, 100 + PAGE_SIZE))
    for status, hours in ((403, 2.0), (429, 3.0), (503, 2.0)):
        routes = Routes(pages={0: search_response(first, total=60)}, status={"offset=20": status})
        cooldowns: list[float] = []
        c = collector(routes, queries=["python", "django"], max_results=100, on_rate_limited=cooldowns.append)
        jobs = c.collect()
        assert len(jobs) == PAGE_SIZE and c.rate_limited is True
        assert len(routes.requests) == 2 and cooldowns == [hours]  # Retry-After 10800 s wins over the default
    # a login redirect counts as a block too
    routes = Routes(pages={0: search_response(first, total=60)}, redirect={"offset=20"})
    cooldowns = []
    c = collector(routes, queries=["python", "django"], with_details=True, on_rate_limited=cooldowns.append)
    jobs = c.collect()
    assert c.rate_limited is True and cooldowns == [2.0] and len(jobs) == PAGE_SIZE
    assert not any(j.raw.get("details_fetched") for j in jobs)
    # a 200 search response that is the Cloudflare page is a block as well
    c = collector(Routes(pages={0: CF_HTML}))
    assert c.collect() == [] and c.rate_limited is True


def test_rate_limit_during_details_keeps_listings():
    routes = Routes(pages={0: search_response([1, 2, 3])}, postings={"1": job_page(1)}, status={"2": 403})
    c = collector(routes, with_details=True)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1", "2", "3"] and c.rate_limited is True
    assert jobs[0].raw["details_fetched"] is True and jobs[0].description
    assert jobs[1].raw.get("details_fetched") is None and jobs[2].raw.get("details_fetched") is None
    assert len(routes.requests) == 3  # search, page 1, page 2 (403) and then nothing
    # an opportunity page redirecting to the login page stops enrich() the same way
    routes = Routes(pages={0: search_response([1, 2, 3])}, postings={"1": job_page(1)}, redirect={"2"})
    c = collector(routes, with_details=True)
    jobs = c.collect()
    assert c.rate_limited is True and jobs[0].raw["details_fetched"] is True and "details_fetched" not in jobs[2].raw
    # a 404 opportunity page is skipped without a block
    routes = Routes(pages={0: search_response([1, 2])}, postings={"1": job_page(1)})
    c = collector(routes, with_details=True)
    jobs = c.collect()
    assert c.rate_limited is False and "details_fetched" not in jobs[1].raw and len(routes.requests) == 3


def test_details_enrichment_filters_stale_postings_and_sends_page_headers():
    postings = {"1": job_page(1, posted="2026-10-29"), "2": job_page(2, posted="2026-10-20"), "3": job_page(3)}
    routes = Routes(pages={0: search_response([1, 2, 3, 4])}, postings=postings)
    c = collector(routes, queries=["python", "django"], with_details=True, max_details=3, hours_old=24)
    jobs = c.collect()
    # 2 is nine days old -> dropped; 4 is over max_details, no date -> kept
    assert [j.source_id for j in jobs] == ["1", "3", "4"]
    assert jobs[0].posted_at == datetime(2026, 10, 29, tzinfo=UTC) and jobs[2].posted_at is None
    page_reqs = [r for r in routes.requests if not str(r.url).startswith(SEARCH_URL)]
    assert [job_id_from(str(r.url)) for r in page_reqs] == ["1", "2", "3"]
    assert page_reqs[0].headers["accept"].startswith("text/html") and "x-requested-with" not in page_reqs[0].headers
    assert page_reqs[0].headers["sec-fetch-mode"] == "navigate"
    # with a week's window the nine-day-old one is still out, a week-old one is in
    postings = {"1": job_page(1, posted="2026-10-23"), "2": job_page(2, posted="2026-10-20")}
    routes = Routes(pages={0: search_response([1, 2])}, postings=postings)
    jobs = collector(routes, with_details=True, hours_old=24 * 7).collect()
    assert [j.source_id for j in jobs] == ["1"]


def test_skip_if_cooldown_makes_no_requests():
    routes = Routes(pages={0: search_response([1])})
    c = collector(routes, skip_if=lambda: True)
    assert c.collect() == [] and routes.requests == []


def test_politeness_delay_and_search_once():
    routes = Routes(pages={0: search_response([1, 2])}, postings={"1": job_page(1), "2": job_page(2)})
    slept: list[float] = []
    jobs = search_once("python", "", 48, delay=2.0, with_details=True, client=routes.client())
    assert len(jobs) == 2 and all(j.raw["details_fetched"] for j in jobs)
    c = InstahyreCollector(["x"], client=routes.client(), delay=2.0, sleep=slept.append, with_details=False)
    c.collect()
    c.collect()
    assert slept and all(1.4 <= s <= 2.6 for s in slept)
    jobs = search_once(["python", "backend"], max_results=1, with_details=False, delay=0, client=routes.client())
    assert len(jobs) == 1
    with pytest.raises(ValueError):
        search_once("x", job_type="part_time", client=routes.client())
