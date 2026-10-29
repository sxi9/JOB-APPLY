"""Naukri search collector on inline fixtures: the JSON API shape, the rendered SRP cards, blocks and the
headless-browser fallback (fake page). No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobapply.collectors.base import BaseCollector, NormalizedJob
from jobapply.sites.naukri import search as nsrch
from jobapply.sites.naukri.search import (
    API_HEADERS,
    API_URL,
    PAGE_SIZE,
    NaukriCollector,
    hours_to_job_age,
    job_id_from_url,
    job_url_from,
    parse_inr_salary,
    parse_relative_label,
    parse_search_html,
    parse_search_json,
    scrape_search_page,
    search_page_url,
    seo_key,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
CREATED_MS = int((NOW - timedelta(hours=5)).timestamp() * 1000)


def api_job(job_id: str, title: str = "Python Developer", **over) -> dict:
    d = {
        "title": title, "logoPath": "", "logoPathV3": "https://img.naukimg.com/logo_images/groups/v1/1.gif",
        "jobId": job_id, "currency": "INR", "footerPlaceholderLabel": "3 Days Ago",
        "footerPlaceholderColor": "#474d6a", "companyName": "Acme Technologies", "isSaved": False,
        "tagsAndSkills": "Python,Django,REST API,AWS",
        "placeholders": [{"type": "experience", "label": "3-6 Yrs"}, {"type": "salary", "label": "5-10 Lacs PA"},
                         {"type": "location", "label": "Bengaluru, Hyderabad"}],
        "companyId": 4567,
        "jdURL": f"job-listings-python-developer-acme-technologies-bengaluru-3-to-6-years-{job_id}",
        "staticUrl": "acme-technologies-jobs-careers-4567",
        "ambitionBoxData": {"AggregateRating": "4.1", "ReviewsCount": 120},
        "jobDescription": "<p>Build <b>APIs</b> in Python.</p><ul><li>Django</li><li>AWS</li></ul>",
        "showMultipleApply": False, "groupId": 0, "isTopGroup": 0, "createdDate": CREATED_MS, "mode": "listing",
        "board": "1", "vacancy": 2, "jobType": "Permanent",
    }
    d.update(over)
    return d


def api_page(ids: list[str], total: int | None = None) -> dict:
    return {"noOfJobs": total if total is not None else len(ids), "jobDetails": [api_job(i) for i in ids],
            "sortBy": "r", "sid": "abc"}


SRP_HTML = """
<div class="styles_jlc__main__VdwtF">
<div class="srp-jobtuple-wrapper" data-job-id="291025501111">
 <div class="cust-job-tuple layout-wrapper lay-2 sjw__tuple">
  <div class="row1"><a class="title" href="https://www.naukri.com/job-listings-python-developer-acme-291025501111"
     title="Python Developer" target="_blank">Python Developer</a></div>
  <div class="row2"><span class="comp-dtls-wrap"><a class="comp-name mw-25" href="https://www.naukri.com/acme-jobs"
     title="Acme">Acme</a></span></div>
  <div class="row3"><div class="job-details"><span class="exp-wrap"><span class="exp"><span class="expwdth"
     title="3-6 Yrs">3-6 Yrs</span></span></span><span class="sal-wrap"><span class="sal"><i></i><span
     title="5-10 Lacs PA">5-10 Lacs PA</span></span></span><span class="loc-wrap"><span class="loc"><span
     class="locWdth" title="Bengaluru">Bengaluru</span></span></span></div></div>
  <div class="row4"><span class="job-desc ni-job-tuple-icon">Build APIs in Python and Django</span></div>
  <div class="row5"><ul class="tags-gt"><li class="dot-gt tag-li">Python</li>
     <li class="dot-gt tag-li">Django</li></ul></div>
  <div class="row6"><span class="job-post-day">2 Days Ago</span></div>
 </div>
</div>
<div class="srp-jobtuple-wrapper" data-job-id="291025502222">
 <div class="cust-job-tuple">
  <div class="row1"><a class="title" href="job-listings-remote-sre-beta-291025502222" title="SRE">SRE (Remote)</a></div>
  <div class="row2"><a class="comp-name" href="#">Beta Labs</a></div>
  <div class="row3"><span class="locWdth">Remote</span><span class="sal"><span>Not disclosed</span></span></div>
  <div class="row6"><span class="job-post-day">Just now</span></div>
 </div>
</div>
<div class="srp-jobtuple-wrapper" data-job-id="291025503333">
 <div class="cust-job-tuple"><span>no title</span></div></div>
</div>
"""


# ------------------------------------------------------------------ small parsers


@pytest.mark.parametrize("label,delta", [
    ("Just now", timedelta(0)), ("Today", timedelta(0)), ("Few Hours Ago", timedelta(hours=3)),
    ("1 Day Ago", timedelta(days=1)), ("3 Days Ago", timedelta(days=3)), ("30+ Days Ago", timedelta(days=30)),
    ("2 weeks ago", timedelta(weeks=2)), ("1 month ago", timedelta(days=30)), ("45 minutes ago", timedelta(minutes=45)),
])
def test_parse_relative_label(label, delta):
    assert parse_relative_label(label, NOW) == NOW - delta
    assert parse_relative_label(label, NOW).tzinfo is not None


def test_parse_relative_label_rejects_non_relative():
    assert parse_relative_label("", NOW) is None
    assert parse_relative_label("Posted on 29 Oct", NOW) is None


@pytest.mark.parametrize("text,expected", [
    ("5-10 Lacs PA", (500_000.0, 1_000_000.0, "INR")),
    ("12 Lacs PA", (1_200_000.0, 1_200_000.0, "INR")),
    ("8.5-9.5 LPA", (850_000.0, 950_000.0, "INR")),
    ("3,00,000 - 4,50,000 PA", (300_000.0, 450_000.0, "INR")),
    ("Not disclosed", (None, None, None)),
    ("", (None, None, None)),
])
def test_parse_inr_salary(text, expected):
    assert parse_inr_salary(text) == expected


def test_urls_and_slugs():
    assert seo_key("Python Developer") == "python-developer-jobs"
    assert seo_key("python developer", "Bengaluru") == "python-developer-jobs-in-bengaluru"
    assert seo_key("") == "jobs"
    assert hours_to_job_age(1) == 1 and hours_to_job_age(24) == 1 and hours_to_job_age(25) == 2
    assert search_page_url("python developer", "Pune", 48) == (
        "https://www.naukri.com/python-developer-jobs-in-pune?k=python%20developer&jobAge=2&l=Pune")
    assert job_url_from("job-listings-x-291025501234") == "https://www.naukri.com/job-listings-x-291025501234"
    assert job_url_from("/job-listings-x-1") == "https://www.naukri.com/job-listings-x-1"
    assert job_url_from("https://www.naukri.com/job-listings-y-2") == "https://www.naukri.com/job-listings-y-2"
    assert job_url_from("", "9") == "https://www.naukri.com/job-listings-9"
    assert job_id_from_url("https://www.naukri.com/job-listings-python-dev-acme-291025501234?src=x") == "291025501234"
    assert job_id_from_url("https://www.naukri.com/python-developer-jobs") is None


# ------------------------------------------------------------------ API json


def test_parse_search_json_normalizes_jobs():
    jobs = parse_search_json(api_page(["291025501234"]), NOW)
    assert len(jobs) == 1
    j = jobs[0]
    assert isinstance(j, NormalizedJob)
    assert j.source == "naukri" and j.ats_type == "naukri" and j.source_id == "291025501234"
    assert j.title == "Python Developer" and j.company == "Acme Technologies"
    assert j.location == "Bengaluru, Hyderabad" and j.remote is False
    assert j.url == j.apply_url == (
        "https://www.naukri.com/job-listings-python-developer-acme-technologies-bengaluru-3-to-6-years-291025501234")
    assert j.posted_at == NOW - timedelta(hours=5)  # createdDate (epoch ms) wins over the "3 Days Ago" label
    assert j.posted_at.tzinfo is not None
    assert "Build APIs in Python." in j.description and "<b>" not in j.description
    assert "Skills: Python, Django, REST API, AWS" in j.description
    assert (j.salary_min, j.salary_max, j.currency) == (500_000.0, 1_000_000.0, "INR")
    assert j.raw["experience"] == "3-6 Yrs" and j.raw["skills"] == ["Python", "Django", "REST API", "AWS"]
    assert j.raw["posted_label"] == "3 Days Ago" and j.raw["rating"] == "4.1"


def test_parse_search_json_falls_back_to_the_label_and_skips_junk():
    data = api_page(["1"])
    data["jobDetails"][0].update(createdDate=None, footerPlaceholderLabel="Just Now", jdURL="",
                                 placeholders=[{"type": "salary", "label": "Not disclosed"}], tagsAndSkills="")
    data["jobDetails"].append({"title": "", "jobId": "2"})
    data["jobDetails"].append("garbage")
    data["jobDetails"].append(api_job("", jdURL="https://www.naukri.com/job-listings-abc-291025509999"))
    jobs = parse_search_json(json.dumps(data), NOW)
    assert [j.source_id for j in jobs] == ["1", "291025509999"]
    assert jobs[0].posted_at == NOW and jobs[0].url == "https://www.naukri.com/job-listings-1"
    assert jobs[0].salary_min is None and jobs[0].currency is None and "Skills:" not in jobs[0].description
    assert parse_search_json("not json") == [] and parse_search_json({"jobDetails": None}) == []
    assert parse_search_json([1, 2]) == []


def test_remote_flag_from_location_or_title():
    j = parse_search_json(api_page(["1"]), NOW)[0]
    assert j.remote is False
    data = api_page(["2"])
    data["jobDetails"][0]["placeholders"] = [{"type": "location", "label": "Remote"}]
    assert parse_search_json(data, NOW)[0].remote is True


# ------------------------------------------------------------------ rendered cards


def test_parse_search_html_cards():
    jobs = parse_search_html(SRP_HTML, NOW)
    assert [j.source_id for j in jobs] == ["291025501111", "291025502222"]  # the id-only card is skipped
    a, b = jobs
    assert a.title == "Python Developer" and a.company == "Acme" and a.location == "Bengaluru"
    assert a.url == "https://www.naukri.com/job-listings-python-developer-acme-291025501111" and a.apply_url == a.url
    assert a.posted_at == NOW - timedelta(days=2)
    assert a.raw["skills"] == ["Python", "Django"] and a.raw["experience"] == "3-6 Yrs" and a.raw["via"] == "browser"
    assert (a.salary_min, a.salary_max, a.currency) == (500_000.0, 1_000_000.0, "INR")
    assert "Skills: Python, Django" in a.description and "Build APIs" in a.description
    assert b.url == "https://www.naukri.com/job-listings-remote-sre-beta-291025502222"
    assert b.remote is True and b.posted_at == NOW and b.salary_min is None
    assert parse_search_html("") == []


# ------------------------------------------------------------------ collector over httpx MockTransport


class Routes:
    """Answers the API by (keyword, pageNo); `status` forces an HTTP status, `body` a raw body."""

    def __init__(self, pages: dict[tuple[str, int], dict] | None = None, status: int | None = None,
                 body: str | None = None, fail_after: int | None = None):
        self.pages, self.status, self.body, self.fail_after = pages or {}, status, body, fail_after
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return httpx.Response(406, json={"message": "recaptcha required", "statusCode": 406, "data": None})
        if self.status is not None:
            return httpx.Response(self.status, text=self.body or "", headers={"content-type": "application/json"})
        if self.body is not None:
            return httpx.Response(200, text=self.body)
        key = (req.url.params.get("keyword", ""), int(req.url.params.get("pageNo", 1)))
        page = self.pages.get(key)
        if page is None:
            return httpx.Response(200, json={"noOfJobs": 0, "jobDetails": []})
        return httpx.Response(200, json=page)


def make(routes: Routes, queries=("python developer",), **kw) -> NaukriCollector:
    kw.setdefault("delay", 0)
    kw.setdefault("now", NOW)
    return NaukriCollector(list(queries), client=httpx.Client(transport=httpx.MockTransport(routes)), **kw)


def test_collector_is_a_base_collector_with_the_uniform_signature():
    c = NaukriCollector(["x"], location="Pune", hours_old=24, max_results=50, max_requests=40, user_id=None)
    assert isinstance(c, BaseCollector)
    assert c.name == "naukri" and c.source == "naukri" and c.ats_type == "naukri" and c.key == "naukri:search"
    assert c.hours_old == 24 and c.max_results == 50 and c.max_requests == 40 and c.user_id is None
    assert NaukriCollector(["x"], user_id="3").user_id == 3
    assert NaukriCollector([" a ", "a", "", "b"]).queries == ["a", "b"]


def test_collect_sends_required_headers_and_params():
    routes = Routes({("python developer", 1): api_page(["1", "2"])})
    jobs = make(routes, location="Bengaluru", hours_old=30).collect()
    assert [j.source_id for j in jobs] == ["1", "2"]
    req = routes.requests[0]
    assert str(req.url).startswith(API_URL)
    assert req.headers["appid"] == "109" and req.headers["systemid"] == "Naukri"
    assert "Chrome/" in req.headers["user-agent"] and req.headers["accept"].startswith("application/json")
    p = req.url.params
    assert p["keyword"] == "python developer" and p["location"] == "Bengaluru"
    assert p["noOfResults"] == str(PAGE_SIZE) and p["urlType"] == "search_by_key_loc" and p["searchType"] == "adv"
    assert p["jobAge"] == "2" and p["pageNo"] == "1" and p["seoKey"] == "python-developer-jobs-in-bengaluru"
    assert API_HEADERS["appid"] == "109"


def test_collect_paginates_dedupes_and_caps():
    ids1 = [str(i) for i in range(1, PAGE_SIZE + 1)]
    ids2 = [str(i) for i in range(PAGE_SIZE, PAGE_SIZE + 6)]  # overlaps on one id
    routes = Routes({("python developer", 1): api_page(ids1, total=25),
                     ("python developer", 2): api_page(ids2, total=25),
                     ("django", 1): api_page(["3", "99"])})
    c = make(routes, queries=("python developer", "django"), max_results=100)
    jobs = c.collect()
    assert len(jobs) == PAGE_SIZE + 5 + 1
    assert [int(r.url.params["pageNo"]) for r in routes.requests] == [1, 2, 1]
    assert c.requests_made == 3 and c.rate_limited is False
    # max_results stops paging early
    routes2 = Routes({("python developer", 1): api_page(ids1, total=25)})
    assert len(make(routes2, max_results=5).collect()) == 5 and len(routes2.requests) == 1


def test_collect_filters_jobs_outside_the_window():
    data = api_page(["new", "old"])
    data["jobDetails"][1]["createdDate"] = int((NOW - timedelta(days=4)).timestamp() * 1000)
    data["jobDetails"].append(api_job("undated", createdDate=None, footerPlaceholderLabel=""))
    jobs = make(Routes({("python developer", 1): data}), hours_old=24).collect()
    assert [j.source_id for j in jobs] == ["new", "undated"]


def test_recaptcha_406_stops_at_once_and_records_a_cooldown():
    cooldowns: list[float] = []
    body = '{"message":"recaptcha required","statusCode":406,"validationErrors":[],"data":null}'
    routes = Routes(status=406, body=body)
    c = make(routes, queries=("a", "b"), on_rate_limited=cooldowns.append)
    assert c.collect() == []
    assert len(routes.requests) == 1  # never retried, second query never tried
    assert c.rate_limited is True and c.block_reason == "recaptcha required"
    assert cooldowns == [nsrch.DEFAULT_COOLDOWN_HOURS]


@pytest.mark.parametrize("status", [403, 429])
def test_plain_blocks_stop_too(status):
    routes = Routes(status=status, body="")
    c = make(routes)
    assert c.collect() == [] and c.rate_limited and c.block_reason == f"http {status}"


def test_non_json_body_is_a_block_and_other_errors_are_not():
    c = make(Routes(body="<html>Access Denied</html>"))
    assert c.collect() == [] and c.rate_limited and "non-json" in c.block_reason
    c = make(Routes(status=500, body=""))
    assert c.collect() == [] and c.rate_limited is False


def test_block_mid_run_keeps_what_was_collected():
    routes = Routes({("a", 1): api_page(["1"]), ("b", 1): api_page(["2"])}, fail_after=1)
    c = make(routes, queries=("a", "b"))
    assert [j.source_id for j in c.collect()] == ["1"]
    assert c.rate_limited is True and len(routes.requests) == 2


def test_request_budget_and_skip_if():
    ids = [str(i) for i in range(PAGE_SIZE)]
    routes = Routes({("a", 1): api_page(ids, total=60), ("a", 2): api_page(["x"], total=60)})
    c = make(routes, queries=("a",), max_requests=1, max_results=100)
    assert len(c.collect()) == PAGE_SIZE and c.budget_exhausted and len(routes.requests) == 1
    routes = Routes({("a", 1): api_page(["1"])})
    assert make(routes, skip_if=lambda: True).collect() == [] and routes.requests == []


def test_politeness_delay_between_requests():
    slept: list[float] = []
    ids = [str(i) for i in range(PAGE_SIZE)]
    routes = Routes({("a", 1): api_page(ids, total=40), ("a", 2): api_page(["x"], total=40)})
    make(routes, queries=("a",), delay=1.0, sleep=slept.append, max_results=100).collect()
    assert len(slept) == 1 and 0.7 <= slept[0] <= 1.3


# ------------------------------------------------------------------ browser fallback (fake page)


class FakeResp:
    def __init__(self, url: str, status: int = 200, payload=None):
        self.url, self.status, self._payload = url, status, payload

    def json(self):
        return self._payload


class FakePage:
    def __init__(self, html: str = "", xhr: dict | None = None, captcha: bool = False, status: int = 200):
        self.html, self.xhr, self.captcha, self.status = html, xhr, captcha, status
        self.handlers: list = []
        self.visited: list[str] = []
        self.url = "about:blank"

    def on(self, event, handler):
        self.handlers.append(handler)

    def goto(self, url, **kw):
        self.visited.append(url)
        self.url = url
        if self.xhr is not None:
            for h in self.handlers:
                h(FakeResp("https://www.naukri.com/jobapi/v3/search?x=1", 200, self.xhr))
        return FakeResp(url, self.status)

    def locator(self, selector):
        n = 1 if (self.captcha and "captcha" in selector) else 0

        class L:
            def count(self_inner):
                return n

        return L()

    def content(self):
        return self.html

    def evaluate(self, js):
        return "" if "innerText" in js else ""

    def wait_for_selector(self, *a, **kw):
        return None


def test_scrape_prefers_the_captured_api_json():
    page = FakePage(html=SRP_HTML, xhr=api_page(["42"]))
    jobs = scrape_search_page(page, "https://www.naukri.com/python-developer-jobs?k=python", now=NOW)
    assert [j.source_id for j in jobs] == ["42"]
    assert page.visited == ["https://www.naukri.com/python-developer-jobs?k=python"]


def test_scrape_falls_back_to_the_rendered_cards_and_stops_on_captcha():
    jobs = scrape_search_page(FakePage(html=SRP_HTML), "https://www.naukri.com/x", now=NOW)
    assert [j.source_id for j in jobs] == ["291025501111", "291025502222"]
    hits: list[tuple[str, str]] = []
    assert scrape_search_page(FakePage(html=SRP_HTML, captcha=True), "https://www.naukri.com/x", now=NOW,
                              on_challenge=lambda r, u: hits.append((r, u))) == []
    assert hits == [("captcha on the search page", "https://www.naukri.com/x")]
    hits.clear()
    assert scrape_search_page(FakePage(html=SRP_HTML, status=406), "https://www.naukri.com/x", now=NOW,
                              on_challenge=lambda r, u: hits.append((r, u))) == []
    assert hits[0][0] == "http 406"
    assert scrape_search_page(FakePage(html=SRP_HTML), "https://www.naukri.com/x", now=NOW,
                              spend=lambda: False) == []


def test_api_block_falls_back_to_the_browser_profile():
    closed: list[bool] = []

    class Ctx:
        def close(self):
            closed.append(True)

    page = FakePage(html=SRP_HTML)
    routes = Routes(status=406, body='{"message":"recaptcha required"}')
    cooldowns: list[float] = []
    c = make(routes, queries=("python developer",), user_id=5, page_factory=lambda: (Ctx(), page), hours_old=72,
             on_rate_limited=cooldowns.append)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["291025501111", "291025502222"]
    assert all(j.raw.get("via") == "browser" for j in jobs)
    assert page.visited == [search_page_url("python developer", "", 72)]
    assert closed == [True] and c.requests_made == 2  # one API call, one page load
    assert c.api_blocked and c.block_reason == "recaptcha required"
    assert c.rate_limited is False and cooldowns == []  # the fallback got through: no cooldown recorded


def test_browser_fallback_captcha_records_the_cooldown():
    cooldowns: list[float] = []
    page = FakePage(html=SRP_HTML, captcha=True)
    routes = Routes(status=406, body='{"message":"recaptcha required"}')
    c = make(routes, queries=("a",), user_id=5, page_factory=lambda: (None, page), on_rate_limited=cooldowns.append)
    assert c.collect() == []
    assert c.rate_limited and c.block_reason == "captcha on the search page"
    assert cooldowns == [nsrch.DEFAULT_COOLDOWN_HOURS]


def test_api_block_without_a_user_profile_returns_what_it_has(caplog):
    routes = Routes({("a", 1): api_page(["1"]), ("b", 1): api_page(["2"])}, fail_after=1)
    cooldowns: list[float] = []
    c = make(routes, queries=("a", "b"), user_id=None, on_rate_limited=cooldowns.append)
    with caplog.at_level("WARNING"):
        jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1"]
    assert c.rate_limited and cooldowns == [nsrch.DEFAULT_COOLDOWN_HOURS]
    assert any("no user profile for the browser fallback" in r.getMessage() for r in caplog.records)


def test_browser_only_mode_skips_the_api():
    page = FakePage(html=SRP_HTML)
    routes = Routes({("a", 1): api_page(["1"])})
    c = make(routes, queries=("a",), api=False, user_id=1, page_factory=lambda: (None, page), hours_old=72)
    assert [j.source_id for j in c.collect()] == ["291025501111", "291025502222"]
    assert routes.requests == []
    # with the default 24 h window the 2-day-old card is dropped
    c = make(routes, queries=("a",), api=False, user_id=1, page_factory=lambda: (None, page))
    assert [j.source_id for j in c.collect()] == ["291025502222"]


def test_browser_fallback_failure_is_logged_not_raised():
    def boom():
        raise RuntimeError("Executable doesn't exist")

    routes = Routes(status=406, body='{"message":"recaptcha required"}')
    c = make(routes, queries=("a",), user_id=1, page_factory=boom)
    assert c.collect() == []


def test_search_once_helper():
    routes = Routes({("a", 1): api_page(["1"])})
    jobs = nsrch.search_once("a", client=httpx.Client(transport=httpx.MockTransport(routes)), delay=0)
    assert [j.source_id for j in jobs] == ["1"]
