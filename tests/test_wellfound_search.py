"""Wellfound public collector: parsing of the role landing pages (`__NEXT_DATA__` Apollo cache, shape recorded
live 2026-10-29), the job page (JSON-LD), slug mapping, pagination, blocks and the app_settings cooldown.
Fixtures are inline, trimmed copies of the recorded structure. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from jobapply.sites.wellfound import search as ws
from jobapply.sites.wellfound.search import (
    GUEST_COOLDOWN_KEY,
    WellfoundCollector,
    apply_details,
    guest_cooldown_active,
    guest_cooldown_until,
    is_blocked_url,
    job_id_from,
    job_url,
    location_slug,
    looks_like_challenge,
    parse_job_html,
    parse_search_html,
    role_slug,
    search_once,
    search_url,
    set_guest_cooldown,
    within_hours,
)
from jobapply.switches import get_value
from jobapply.users import create_user

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
FRESH = int((NOW - timedelta(hours=3)).timestamp())
OLD = int((NOW - timedelta(days=17)).timestamp())


# ----------------------------------------------------------------- inline fixtures (recorded shape)


def viewer(logged_in: bool = False) -> dict:
    return {"__typename": "TalentViewer", "id": "u1" if logged_in else None, "canApplyToJobs": logged_in,
            "currentUser": {"__ref": "User:u1"} if logged_in else None,
            "currentCandidate": {"__ref": "Candidate:c1"} if logged_in else None}


DESCRIPTION = "**Job Description**\r\nExpert in Python. Comfortable with Flask or Django\r\n\r\nGood OO design."


def listing(job_id: str, title: str, *, live=FRESH, locations=("Bengaluru", "Bangalore Urban"), remote=False,
            remote_kind: str | None = None, accepted=(), compensation="", slug=None, years=2,
            description=DESCRIPTION) -> dict:
    remote_cfg = {"__typename": "JobListingRemoteConfig", "kind": remote_kind, "wfhFlexible": False}
    return {
        "__typename": "JobListingSearchResult", "autoPosted": False, "atsSource": None, "description": description,
        "jobType": "full-time", "liveStartAt": live, "locationNames": list(locations),
        "primaryRoleTitle": "Software Engineer",
        "remote": remote, "remoteConfig": remote_cfg if remote_kind else None,
        "acceptedRemoteLocationNames": list(accepted), "slug": slug or ws.slugify(title), "title": title,
        "compensation": compensation, "yearsExperienceMin": years, "yearsExperienceMax": None, "id": job_id,
        "isBookmarked": False,
    }


def startup(sid: str, name: str, slug: str, job_ids: list[str], size="SIZE_51_200") -> dict:
    return {"__typename": "StartupResult", "id": sid, "badges": [{"__ref": "Badge:ACTIVELY_HIRING"},
                                                                    {"__ref": f"Badge:B2B-{sid}"}],
            "companySize": size, "highConcept": "Pitch", "highlightedJobListings": [
                {"__ref": f"JobListingSearchResult:{j}"} for j in job_ids],
            "logoUrl": "https://photos.wellfound.com/x.jpg", "name": name, "slug": slug}


def role_page(startups: list[dict], listings: list[dict], *, page_count=1, total=None, logged_in=False,
              page_no=1, role="python-developer", location="india") -> str:
    key = f'seoLandingPageJobSearchResults({{"location":"{location}","page":{page_no},"role":"{role}"}})'
    results = {"__typename": "Results", "totalStartupCount": len(startups), "totalJobCount": total or len(listings),
               "perPage": 20, "pageCount": page_count,
               "startups": [{"__ref": f"StartupResult:{s['id']}"} for s in startups]}
    cache = {
        "ROOT_QUERY": {"__typename": "Query",
                       "talent": {"__typename": "Talent", "viewer": viewer(logged_in), key: results}},
        "Badge:ACTIVELY_HIRING": {"__typename": "Badge", "id": "ACTIVELY_HIRING", "name": "Actively Hiring"},
    }
    for s in startups:
        cache[f"StartupResult:{s['id']}"] = s
        cache[f"Badge:B2B-{s['id']}"] = {"__typename": "Badge", "id": f"B2B-{s['id']}", "name": "B2B"}
    for j in listings:
        cache[f"JobListingSearchResult:{j['id']}"] = j
    data = {"props": {"pageProps": {"location": location, "page": page_no, "role": role,
                                    "apolloState": {"data": cache}}},
            "page": "/seoLanding/roleLocationSearch", "query": {"role": role, "location": location}}
    return ('<!DOCTYPE html><html><head><title>Python Developer Jobs in India - 2027 | Wellfound</title></head><body>'
            '<div id="__next">cards</div><script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'
            + json.dumps(data) + '</script></body></html>')


_LD = json.dumps({
    "@context": "http://schema.org/", "@type": "JobPosting", "title": "Python Developer",
    "identifier": {"@type": "PropertyValue", "name": "Impact Analytics", "value": "1053169-python-developer"},
    "employmentType": "FULL_TIME", "hiringOrganization": {"@type": "Organization", "name": "Impact Analytics"},
    "directApply": True,
    "description": ("<p><strong>Job Description</strong><br>\nExpert in Python. Flask or Django<br>\n"
                    "Pandas or PySpark.</p>"),
    "datePosted": "2026-10-12T08:15:38Z",
    "jobLocation": [
        {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Bengaluru",
                                       "addressRegion": "Karnataka", "addressCountry": "India"}},
        {"@type": "Place", "address": {"@type": "PostalAddress", "addressRegion": "Karnataka",
                                       "addressCountry": "India"}},
    ],
})
JOB_PAGE = (
    '<!DOCTYPE html><html><head><title>Python Developer at Impact Analytics | Wellfound</title>\n'
    '<script type="application/ld+json">' + _LD + '</script></head>\n'
    '<body><div id="__next"><header><nav>'
    '<button class="x" onclick="window.location.href=\'/login?after_sign_in=%2Fjobs\'">Log In</button>\n'
    '<button onclick="window.location.href=\'/jobs/signup?source_content=global_header_join_text\'">Sign Up</button>'
    '</nav></header>\n'
    '<div data-test="JobListing"><div data-test="JobListingCompanyHeader">'
    '<a href="/company/impact-analytics-1"><span>Impact Analytics</span></a></div>\n'
    '<button class="save" onclick="window.location.href=\'/jobs/signup?after_action=save_job&job_listing=1053169\'">'
    'Save</button>\n'
    '<button class="apply" onclick="window.location.href=\'/jobs/1053169-python-developer?autoOpenApplication=true\'">'
    'Apply Now</button>\n'
    '<h1 class="inline text-xl">Python Developer</h1>\n'
    '<div><span class="label">Skills</span><div class="chips"><div class="chip">Python</div>'
    '<div class="chip">Django</div><div class="chip">Flask</div></div></div>\n'
    '<h2>About the job</h2><div><div id="job-description"><p><strong>Job Description</strong><br>\n'
    'Expert in Python. Flask or Django<br>\nPandas or PySpark.</p>\n</div></div></div></div></body></html>'
)

CHALLENGE_PAGE = ('<!DOCTYPE html><html><head><title>Security Check | Wellfound</title></head><body><script>'
                  "window._cf_chl_opt={cvId:'3'};</script><script src=\"/cdn-cgi/challenge-platform/h/g/orchestrate/"
                  'chl_page/v1?ray=1"></script></body></html>')

S1 = startup("1147664", "Impact Analytics", "impact-analytics-1", ["1053169", "1053170"], size="SIZE_1001_5000")
S2 = startup("9891813", "AssetIntel", "assetintel-1", ["4823723"])
J1 = listing("1053169", "Python Developer", slug="python-developer")
J2 = listing("1053170", "Senior Python Developer", live=OLD, compensation="₹15L – ₹25L • No equity",
             locations=("Pune",))
J3 = listing("4823723", "Python Developer (Remote)", remote=True, remote_kind="ONSITE_OR_REMOTE", accepted=("India",),
             locations=("Delhi",), compensation="$70k – $76k • No equity", description="Own the analytical engines.")
PAGE1 = role_page([S1, S2], [J1, J2, J3], page_count=2, total=3)


class Routes:
    """MockTransport keyed by URL substring; `status` forces an HTTP status (403 with the Cloudflare header
    when `cf` is set), `redirect` answers with a 302 to the login page."""

    def __init__(self, pages: dict[str, str] | None = None, status: dict[str, int] | None = None,
                 redirect: set[str] | None = None, cf: bool = True, retry_after: str | None = None):
        self.pages, self.status, self.redirect = pages or {}, status or {}, redirect or set()
        self.cf, self.retry_after = cf, retry_after
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith("https://wellfound.com/login"):
            return httpx.Response(200, text="<html><form id='new_user'></form></html>")
        for needle in self.redirect:
            if needle in url:
                return httpx.Response(302, headers={"location": "https://wellfound.com/login?after_sign_in=%2Fjobs"})
        for needle, code in self.status.items():
            if needle in url:
                headers = {}
                if code == 403 and self.cf:
                    headers["cf-mitigated"] = "challenge"
                if self.retry_after:
                    headers["retry-after"] = self.retry_after
                return httpx.Response(code, text=CHALLENGE_PAGE if code == 403 else "slow down", headers=headers)
        for needle, body in self.pages.items():
            if needle in url:
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="<html>not found</html>")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


def collector(routes: Routes, **kw) -> WellfoundCollector:
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("now", NOW)
    kw.setdefault("hours_old", 0)
    kw.setdefault("max_pages", 1)  # PAGE1 advertises pageCount=2; the pagination test opts in
    return WellfoundCollector(kw.pop("queries", ["python developer"]), kw.pop("location", "India"),
                              client=routes.client(), **kw)


# ----------------------------------------------------------------- parsing


def test_parse_role_page_normalises_jobs():
    jobs, info = parse_search_html(PAGE1, now=NOW)
    assert (info.page_count, info.total_jobs, info.total_startups, info.logged_in) == (2, 3, 2, False)
    assert [j.source_id for j in jobs] == ["1053169", "1053170", "4823723"]
    j = jobs[0]
    assert (j.title, j.company) == ("Python Developer", "Impact Analytics")
    assert j.location == "Bengaluru, Bangalore Urban" and j.remote is False
    assert j.url == j.apply_url == "https://wellfound.com/jobs/1053169-python-developer"
    assert j.source == "wellfound" and j.ats_type == "wellfound"
    assert j.posted_at == NOW - timedelta(hours=3)
    assert j.description.startswith("Job Description\nExpert in Python") and "**" not in j.description
    assert j.raw["company_slug"] == "impact-analytics-1" and j.raw["company_size"] == "SIZE_1001_5000"
    assert j.raw["badges"] == ["Actively Hiring", "B2B"] and j.raw["job_type"] == "full-time"
    assert j.raw["years_experience_min"] == 2 and j.raw["primary_role"] == "Software Engineer"
    # compensation strings -> salary columns (INR lakh strings keep the raw text, numbers are best effort)
    assert jobs[1].raw["compensation"].startswith("₹15L") and jobs[1].currency == "INR"
    r = jobs[2]
    assert r.remote is True and r.location == "Delhi (remote: India)" and r.company == "AssetIntel"
    assert (r.salary_min, r.salary_max, r.currency) == (70000.0, 76000.0, "USD")
    assert r.raw["remote_kind"] == "ONSITE_OR_REMOTE" and r.raw["accepted_remote_locations"] == ["India"]


def test_parse_handles_jobs_landing_shape_and_junk():
    # the /jobs landing page uses bare JobListing entries pointing at their Startup
    cache = {"ROOT_QUERY": {"talent": {"viewer": viewer()}},
             "Startup:1": {"__typename": "Startup", "id": "1", "name": "Slingshot", "slug": "slingshot"},
             "JobListing:9": {"__typename": "JobListing", "id": "9", "slug": "comms-lead", "title": "Comms Lead",
                              "compensation": "$140k – $180k", "locationNames": ["New York"], "liveStartAt": FRESH,
                              "remote": False, "primaryRole": {"__typename": "Role", "slug": "content-creator-1"},
                              "startup": {"__ref": "Startup:1"}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"props": {"pageProps": {"apolloState": {"data": cache}}}}) + "</script>"
    jobs, info = parse_search_html(html, now=NOW)
    assert len(jobs) == 1 and jobs[0].company == "Slingshot" and jobs[0].raw["primary_role"] == "content-creator-1"
    assert jobs[0].url == "https://wellfound.com/jobs/9-comms-lead" and info.page_count == 0
    # no title or no id: skipped; no NEXT_DATA at all: nothing
    bad = role_page([startup("5", "X", "x", ["7", "8"])], [listing("7", ""), {**listing("8", "T"), "id": ""}])
    assert parse_search_html(bad)[0] == []
    assert parse_search_html("<html><body>Please sign in</body></html>") == ([], ws.PageInfo())
    assert parse_search_html('<script id="__NEXT_DATA__">{broken</script>')[0] == []


def test_slugs_and_urls():
    assert role_slug("Python Developer") == "python-developer"
    assert role_slug("  SDE ") == "software-engineer" and role_slug("Backend Developer") == "backend-engineer"
    assert role_slug("Machine Learning Engineer") == "machine-learning-engineer"
    assert location_slug("India") == "india" and location_slug("Bengaluru") == "bangalore"
    assert location_slug("San Francisco") == "san-francisco" and location_slug("Remote") == ""
    assert search_url("python developer", "India") == "https://wellfound.com/role/l/python-developer/india"
    assert search_url("python developer", "India", page=2) == "https://wellfound.com/role/l/python-developer/india?page=2"
    assert search_url("python developer") == "https://wellfound.com/role/python-developer"
    assert search_url("python developer", "Remote") == "https://wellfound.com/role/r/python-developer"
    assert search_url("python developer", "India", remote_only=True) == "https://wellfound.com/role/r/python-developer"
    assert job_url("1053169", "python-developer") == "https://wellfound.com/jobs/1053169-python-developer"
    assert job_id_from("https://wellfound.com/jobs/1053169-python-developer?x=1") == "1053169"
    assert job_id_from("https://wellfound.com/jobs/1053169") == "1053169" and job_id_from("/company/x") is None
    assert is_blocked_url("https://wellfound.com/login?after_sign_in=%2Fjobs")
    assert is_blocked_url("https://wellfound.com/jobs/signup?x") and not is_blocked_url("https://wellfound.com/jobs/1")
    assert not is_blocked_url("https://example.com/login")
    assert looks_like_challenge(CHALLENGE_PAGE)
    assert not looks_like_challenge(PAGE1) and not looks_like_challenge(JOB_PAGE)


def test_parse_job_page_from_json_ld():
    d = parse_job_html(JOB_PAGE, "1053169")
    assert (d.title, d.company) == ("Python Developer", "Impact Analytics")
    assert d.location == "Bengaluru, Karnataka" and d.employment_type == "FULL_TIME" and d.direct_apply is True
    assert d.posted_at == datetime(2026, 10, 12, 8, 15, 38, tzinfo=UTC)
    assert d.description.startswith("Job Description") and "Expert in Python" in d.description
    assert "<" not in d.description
    assert d.apply_available is True and d.logged_out is True and d.empty is False
    assert d.skills == ["Python", "Django", "Flask"]
    # no JSON-LD: the h1 and the #job-description block still give a result; a challenge page is empty
    stripped = JOB_PAGE.split("<script type=\"application/ld+json\">")[0] + JOB_PAGE.split("</script>", 1)[1]
    d2 = parse_job_html(stripped, "1053169")
    assert d2.title == "Python Developer" and "Pandas or PySpark" in d2.description and d2.posted_at is None
    assert parse_job_html(CHALLENGE_PAGE, "1").empty is True


def test_apply_details_merges_without_mutating():
    base = parse_search_html(PAGE1, now=NOW)[0][0]
    merged = apply_details(base, parse_job_html(JOB_PAGE, "1053169"))
    assert merged.raw["details_fetched"] is True and merged.raw["employment_type"] == "FULL_TIME"
    assert merged.raw["direct_apply"] is True and merged.raw["apply_available"] is True
    assert merged.posted_at == base.posted_at  # the listing's liveStartAt stands
    assert "details_fetched" not in base.raw
    untouched = apply_details(base, ws.JobDetails(job_id="1053169"))
    assert "details_fetched" not in untouched.raw


def test_within_hours():
    j = parse_search_html(PAGE1, now=NOW)[0]
    assert within_hours(j[0], 24, NOW) is True and within_hours(j[1], 24, NOW) is False
    assert within_hours(j[1], 0, NOW) is True and within_hours(j[1].model_copy(update={"posted_at": None}), 1, NOW)


# ----------------------------------------------------------------- collector


def test_collect_filters_by_window_sorts_and_caps():
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1})
    c = collector(routes, hours_old=24, max_pages=1)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1053169", "4823723"]  # the 17-day-old listing dropped
    assert c.key == "wellfound:public" and c.source == "wellfound" and c.name == "wellfound"
    assert str(routes.requests[0].url) == "https://wellfound.com/role/l/python-developer/india"
    h = routes.requests[0].headers
    assert h["User-Agent"].startswith("Mozilla") and "sec-ch-ua" in h and h["Sec-Fetch-Mode"] == "navigate"
    c = collector(routes, hours_old=0, max_results=2, max_pages=1)
    assert len(c.collect()) == 2
    assert WellfoundCollector(["x"], user_id=7).key == "wellfound:public:u7"


def test_pagination_follows_page_count_and_stops_on_repeats():
    page2 = role_page([startup("77", "Co", "co", ["500"])], [listing("500", "Django Developer")], page_count=2,
                      page_no=2)
    routes = Routes(pages={"india?page=2": page2, "/role/l/python-developer/india": PAGE1})
    c = collector(routes, max_pages=5)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1053169", "4823723", "500", "1053170"]  # newest first
    assert [str(r.url) for r in routes.requests] == ["https://wellfound.com/role/l/python-developer/india",
                                                     "https://wellfound.com/role/l/python-developer/india?page=2"]
    # max_pages caps the walk even when pageCount is larger
    routes = Routes(pages={"india?page=2": page2, "/role/l/python-developer/india": PAGE1})
    collector(routes, max_pages=1).collect()
    assert len(routes.requests) == 1
    # a page repeating ids already seen ends the query
    routes = Routes(pages={"india?page=2": PAGE1, "/role/l/python-developer/india": PAGE1})
    assert len(collector(routes, max_pages=5).collect()) == 3 and len(routes.requests) == 2


def test_unknown_role_404_skips_query_without_block():
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1})
    cooldowns: list[float] = []
    c = collector(routes, queries=["underwater basket weaver", "python developer"], on_rate_limited=cooldowns.append)
    jobs = c.collect()
    assert len(jobs) == 3 and c.rate_limited is False and cooldowns == []
    assert len(routes.requests) == 2


def test_cloudflare_challenge_stops_run_and_records_cooldown():
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1}, status={"backend-engineer": 403})
    cooldowns: list[float] = []
    c = collector(routes, queries=["python developer", "backend engineer", "data scientist"],
                  on_rate_limited=cooldowns.append)
    jobs = c.collect()
    assert len(jobs) == 3 and c.rate_limited is True and c.last_block_reason == "cloudflare challenge"
    assert cooldowns == [2.0] and len(routes.requests) == 2  # third query never started
    # 429 with Retry-After: the longer window wins
    routes = Routes(status={"python-developer": 429}, retry_after="10800")
    cooldowns = []
    c = collector(routes, on_rate_limited=cooldowns.append)
    assert c.collect() == [] and cooldowns == [3.0] and c.last_block_reason == "http 429"
    # a 200 whose body is the challenge interstitial counts too
    routes = Routes(pages={"/role/l/python-developer/india": CHALLENGE_PAGE})
    c = collector(routes)
    assert c.collect() == [] and c.rate_limited and c.last_block_reason == "challenge page"
    # a redirect to the login page as well
    routes = Routes(redirect={"python-developer"})
    c = collector(routes)
    assert c.collect() == [] and c.rate_limited and c.last_block_reason == "login redirect"
    # a cooldown hook that raises never breaks the run
    routes = Routes(status={"python-developer": 403})

    def boom(_h):
        raise RuntimeError("db down")

    assert collector(routes, on_rate_limited=boom).collect() == []


def test_budget_skip_if_and_details():
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1})
    c = collector(routes, skip_if=lambda: True)
    assert c.collect() == [] and routes.requests == []
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1, "/jobs/1053169-python-developer": JOB_PAGE})
    c = collector(routes, with_details=True, max_details=5, max_requests=2)
    jobs = c.collect()
    assert c.budget_exhausted is True and len(routes.requests) == 2
    assert jobs[0].raw.get("details_fetched") is True and "details_fetched" not in jobs[1].raw
    # politeness delay: none before the first request, jittered afterwards
    slept: list[float] = []
    routes = Routes(pages={"/role/l/python-developer/india": PAGE1, "/jobs/1053169-python-developer": JOB_PAGE})
    c = WellfoundCollector(["python developer"], "India", 0, with_details=True, max_details=1, max_pages=1,
                           delay=2.0, sleep=slept.append, client=routes.client(), now=NOW)
    c.collect()
    assert len(slept) == 1 and 1.4 <= slept[0] <= 2.6


def test_search_once_helper():
    routes = Routes(pages={"/role/r/python-developer": role_page([S2], [J3], location="r")})
    jobs = search_once("python developer", "Remote", 0, delay=0, client=routes.client())
    assert len(jobs) == 1 and jobs[0].remote is True
    assert str(routes.requests[0].url) == "https://wellfound.com/role/r/python-developer"


# ----------------------------------------------------------------- cross-run cooldown


def test_guest_cooldown_persists_and_doubles(session):
    assert guest_cooldown_until(session) is None and guest_cooldown_active(session) is False
    now = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    until = set_guest_cooldown(session, 2.0, now=now)
    session.commit()
    assert until == now + timedelta(hours=2)
    assert get_value(session, GUEST_COOLDOWN_KEY) == until.isoformat()
    assert guest_cooldown_active(session, now=now + timedelta(hours=1)) is True
    assert guest_cooldown_active(session, now=now + timedelta(hours=3)) is False
    assert set_guest_cooldown(session, 2.0, now=now + timedelta(hours=1)) == now + timedelta(hours=3)
    assert set_guest_cooldown(session, 500, now=now) == now + timedelta(hours=48)
    # any user's challenge cooldown keeps the public scraper away from the shared IP
    from jobapply.sites.wellfound.session import COOLDOWN_KEY

    user = create_user(session, "alice", "pw12345678")
    far = now + timedelta(days=30)
    user.preferences = {COOLDOWN_KEY: (far + timedelta(hours=1)).isoformat()}
    session.add(user)
    session.commit()
    assert guest_cooldown_active(session, now=far) is True
