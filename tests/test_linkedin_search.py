"""LinkedIn guest search collector: fixtures recorded 2026-10-29 (search page, one posting)
plus two synthetic postings (Easy Apply CTA in the guest markup, offsite applyUrl in the externalApply wrapper)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jobapply.collectors.registry import SearchConfig, build_collectors, describe, linkedin_options
from jobapply.config import Settings
from jobapply.sites.linkedin import session as ls
from jobapply.sites.linkedin.search import (
    GUEST_COOLDOWN_KEY,
    PAGE_SIZE,
    POSTING_URL,
    SEARCH_URL,
    LinkedInGuestCollector,
    PostingDetails,
    _normalize_apply_url,
    apply_details,
    guest_cooldown_active,
    guest_cooldown_until,
    is_blocked_url,
    job_id_from,
    minutes_to_tpr,
    parse_posting_html,
    parse_search_html,
    relative_to_datetime,
    search_once,
    set_guest_cooldown,
    unwrap_external_apply,
)
from jobapply.switches import get_value
from jobapply.users import create_user

FIX = Path(__file__).parent / "fixtures" / "linkedin"
SEARCH = (FIX / "search_python_india.html").read_text(encoding="utf-8")
POSTING = (FIX / "posting_4475182617.html").read_text(encoding="utf-8")
EASY = (FIX / "posting_easy_apply.html").read_text(encoding="utf-8")
OFFSITE = (FIX / "posting_offsite_greenhouse.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)


def _card(job_id: str, title: str = "Job") -> str:
    return (f'<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
            f'<a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/x-{job_id}?p=1"></a>'
            f'<h3 class="base-search-card__title">{title}</h3><h4 class="base-search-card__subtitle">'
            f'<a>Co</a></h4><span class="job-search-card__location">Pune, India</span>'
            f'<time datetime="2026-10-29">5 minutes ago</time></div></li>')


def _page(ids: list[str]) -> str:
    return "\n".join(_card(i) for i in ids)


AUTHWALL = "https://www.linkedin.com/authwall?trk=qf&original_referer=https://www.linkedin.com/jobs"
AUTHWALL_HTML = "<html><body><h1>Join LinkedIn</h1><form class='join-form'></form></body></html>"


class Routes:
    """MockTransport whose search pages are keyed by `start`, posting pages by id. `redirect` keys
    ("start=10" or a job id) answer with a 302 to the authwall, `status` keys with that HTTP status."""

    def __init__(self, pages: dict[int, str] | None = None, postings: dict[str, str] | None = None,
                 status: dict[str, int] | None = None, redirect: set[str] | None = None):
        self.pages, self.postings, self.status = pages or {}, postings or {}, status or {}
        self.redirect = redirect or set()
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith(AUTHWALL):
            return httpx.Response(200, text=AUTHWALL_HTML)
        if url.startswith(SEARCH_URL):
            start = int(req.url.params.get("start", 0))
            if f"start={start}" in self.redirect:
                return httpx.Response(302, headers={"location": AUTHWALL})
            if f"start={start}" in self.status:
                return httpx.Response(self.status[f"start={start}"], text="slow down",
                                      headers={"retry-after": "10800"})
            return httpx.Response(200, text=self.pages.get(start, ""))
        job_id = url.rsplit("/", 1)[-1]
        if job_id in self.redirect:
            return httpx.Response(302, headers={"location": AUTHWALL})
        if job_id in self.status:
            return httpx.Response(self.status[job_id], text="")
        if job_id in self.postings:
            return httpx.Response(200, text=self.postings[job_id])
        return httpx.Response(404, text="")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)


def collector(routes: Routes, **kw) -> LinkedInGuestCollector:
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("with_details", False)
    return LinkedInGuestCollector(kw.pop("queries", ["python developer"]), kw.pop("location", "India"),
                                  kw.pop("minutes_old", 60), client=routes.client(), **kw)


# ----------------------------------------------------------------- parsing


def test_parse_recorded_search_page():
    jobs = parse_search_html(SEARCH, now=NOW)
    assert [j.source_id for j in jobs] == ["4475182617", "4435810459", "4484770473"]
    j = jobs[0]
    assert (j.title, j.company) == ("Python Senior Developer", "Infosys")
    assert j.location == "Bengaluru East, Karnataka, India"
    assert j.url == j.apply_url == "https://www.linkedin.com/jobs/view/4475182617"
    assert j.source == "linkedin" and j.ats_type is None and j.remote is False
    assert j.raw["easy_apply"] is False and j.raw["listed"] == "12 hours ago"
    assert j.raw["benefits"] == ["Actively Hiring"]
    assert j.raw["listing_url"].startswith("https://in.linkedin.com/jobs/view/python-senior-developer-at-infosys-4475182617")
    # relative text wins over the day-granular <time datetime>
    assert j.posted_at == datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    assert jobs[2].posted_at == datetime(2026, 10, 29, 1, 0, tzinfo=UTC)


def test_parse_card_falls_back_to_datetime_and_skips_junk():
    card = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:1"><h3 class="base-search-card__title">'
            'Dev</h3><time datetime="2026-10-20">Yesterday</time></div></li>')
    jobs = parse_search_html(card)
    assert jobs[0].posted_at == datetime(2026, 10, 20, tzinfo=UTC)
    no_id = '<li><div class="base-card"><h3 class="base-search-card__title">no id</h3></div></li>'
    assert parse_search_html(no_id) == []
    assert parse_search_html("<li>no card</li>") == []
    assert job_id_from("https://in.linkedin.com/jobs/view/senior-dev-at-acme-4435810459?x=1") == "4435810459"
    assert job_id_from("https://www.linkedin.com/jobs/view/4435810459") == "4435810459"
    assert job_id_from("nothing") is None


def test_minutes_to_tpr_and_relative_times():
    assert minutes_to_tpr(60) == "r3600"
    assert minutes_to_tpr(1) == "r60"
    assert minutes_to_tpr(0) == "r60"  # floor at 1 minute
    assert minutes_to_tpr(1440) == "r86400"
    assert relative_to_datetime("2 hours ago", NOW) == datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    assert relative_to_datetime("1 minute ago", NOW) == datetime(2026, 10, 29, 2, 59, tzinfo=UTC)
    assert relative_to_datetime("3 days ago", NOW) == datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    assert relative_to_datetime("Yesterday", NOW) is None


def test_parse_recorded_posting():
    d = parse_posting_html(POSTING, "4475182617")
    assert (d.title, d.company) == ("Python Senior Developer", "Infosys")
    assert d.location == "Bengaluru East, Karnataka, India"
    assert d.posted == "12 hours ago" and d.applicants == "Over 200 applicants"
    assert d.criteria["Employment type"] == "Full-time"
    assert "Django" in d.description and "<" not in d.description and len(d.description) > 500
    # guest page hides the offsite link behind a sign-in modal: no apply URL, not Easy Apply, but the
    # modal's impression id says apply-link-offsite, and the signup link in it is never taken as apply_url
    assert d.apply_url == "" and d.easy_apply is False and d.offsite is True and d.empty is False


def test_parse_easy_apply_and_offsite_postings():
    # 2026 guest markup: the CTA is a sign-up-modal__outlet button reading "Apply" for both kinds; the
    # kind is in the contextual sign-in modal's impression id (apply-link-onsite = Easy Apply)
    e = parse_posting_html(EASY, "4480000001")
    assert e.easy_apply is True and e.offsite is False and e.apply_url == ""
    assert e.description.startswith("We build control software") and "- 3+ years" in e.description
    assert e.criteria == {"Seniority level": "Mid-Senior level", "Employment type": "Full-time"}
    o = parse_posting_html(OFFSITE, "4480000002")
    assert o.easy_apply is False and o.offsite is True
    # the applyUrl is LinkedIn's externalApply redirect wrapper: unwrapped to the real target
    assert o.apply_url == "https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin"
    assert o.apply_redirect.startswith("https://www.linkedin.com/jobs/view/externalApply/4480000002?url=")
    # older markup (class="apply-button" reading "Easy Apply") still parses
    legacy = EASY.replace("apply-link-onsite", "x").replace(
        'class="sign-up-modal__outlet top-card-layout__cta mt-2 ml-1.5 h-auto babybear:flex-auto '
        'top-card-layout__cta--primary btn-md btn-primary"',
        'class="apply-button apply-button--default top-card-layout__cta--primary"').replace(
        "      Apply\n    </button>", "      Easy Apply\n    </button>")
    assert "Easy Apply" in legacy and parse_posting_html(legacy, "1").easy_apply is True
    # an authwall / interstitial has none of the posting markup: empty, never recorded as fetched
    assert parse_posting_html(AUTHWALL_HTML, "1").empty is True


def test_apply_url_normalisation():
    wrapped = ("https://www.linkedin.com/jobs/view/externalApply/4480000002?url=https%3A%2F%2Fboards.greenhouse.io"
               "%2Facme%2Fjobs%2F1%3Fsrc%3Dli&urlHash=x")
    assert unwrap_external_apply(wrapped) == "https://boards.greenhouse.io/acme/jobs/1?src=li"
    inner = "https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F1%3Fsrc%3Dli"
    double = "https://www.linkedin.com/jobs/view/externalApply/1?url=" + inner.replace("%", "%25")
    assert unwrap_external_apply(double) == "https://boards.greenhouse.io/acme/jobs/1?src=li"
    assert unwrap_external_apply("https://jobs.lever.co/acme/123") == "https://jobs.lever.co/acme/123"
    # a wrapper hiding a workday host must not leak an encoded-garbage slug to detect_from_url
    wd = "https://www.linkedin.com/jobs/view/externalApply/5?url=https%3A%2F%2Facme.wd5.myworkdayjobs.com%2Fen%2Fjob%2F9"
    assert _normalize_apply_url(wd) == "https://acme.wd5.myworkdayjobs.com/en/job/9"
    # LinkedIn's own sign-in / signup / authwall links are never an apply URL
    for bad in ("https://www.linkedin.com/signup/cold-join?source=jobs_registration&session_redirect=x",
                "https://www.linkedin.com/login?x=1", "https://www.linkedin.com/uas/login", "#",
                "https://www.linkedin.com/authwall?trk=1", "https://www.linkedin.com/checkpoint/lg/login"):
        assert _normalize_apply_url(bad) == ""
    assert _normalize_apply_url("https://www.linkedin.com/jobs/view/123") == "https://www.linkedin.com/jobs/view/123"
    assert is_blocked_url(AUTHWALL) and is_blocked_url("https://www.linkedin.com/login?x")
    assert is_blocked_url("https://www.linkedin.com/checkpoint/challenge/1")
    assert not is_blocked_url("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/1")
    assert not is_blocked_url("https://boards.greenhouse.io/login")


def test_signup_href_fallback_is_not_an_apply_url():
    # the only href-bearing CTA on a guest page may be the sign-up link: falls back to the job URL
    page = OFFSITE.replace('<code id="applyUrl" style="display: none">', '<code id="applyUrl-x">').replace(
        '<button class="apply-button', '<a class="apply-button" href="https://www.linkedin.com/signup/cold-join?'
        'source=jobs_registration&amp;session_redirect=https%3A%2F%2Fin.linkedin.com%2Fjobs%2Fview%2F1"')
    d = parse_posting_html(page, "4480000002")
    assert d.apply_url == "" and d.offsite is True
    base = parse_search_html(_card("4480000002", "Platform Engineer"))[0]
    merged = apply_details(base, d)
    assert merged.apply_url == base.url and merged.ats_type is None and merged.raw["easy_apply"] is False


def test_apply_details_sets_ats_and_easy_apply_flag():
    base = parse_search_html(_card("4480000002", "Platform Engineer"))[0]
    offsite = apply_details(base, parse_posting_html(OFFSITE, "4480000002"))
    assert offsite.ats_type == "greenhouse"
    assert offsite.apply_url.startswith("https://boards.greenhouse.io/stripe/jobs/6543210")
    assert offsite.url == "https://www.linkedin.com/jobs/view/4480000002"
    assert offsite.raw["easy_apply"] is False and offsite.raw["details_fetched"] is True
    assert offsite.raw["apply_redirect"].startswith("https://www.linkedin.com/jobs/view/externalApply/")
    assert "deploy pipeline" in offsite.description
    # the posting page overrules the search filter's Easy Apply guess when it knows the apply kind
    filtered = parse_search_html(_card("4480000002", "Platform Engineer"), easy_apply=True)[0]
    assert filtered.raw["easy_apply"] is True and filtered.raw["easy_apply_filter"] is True
    assert apply_details(filtered, parse_posting_html(OFFSITE, "4480000002")).raw["easy_apply"] is False
    assert apply_details(filtered, parse_posting_html(POSTING, "4480000002")).raw["easy_apply"] is False
    # ... and an empty page (authwall) changes nothing and is not recorded as fetched
    same = apply_details(filtered, PostingDetails(job_id="4480000002"))
    assert same.raw.get("details_fetched") is None and same.raw["easy_apply"] is True

    easy = apply_details(base, parse_posting_html(EASY, "4480000001"))
    assert easy.raw["easy_apply"] is True and easy.ats_type is None
    assert easy.apply_url == easy.url  # Easy Apply: the LinkedIn job page is the apply URL
    assert easy.remote is True  # "Remote, India" from the posting page
    assert easy.raw["criteria"]["Seniority level"] == "Mid-Senior level"
    assert base.raw.get("details_fetched") is None  # input not mutated


# ----------------------------------------------------------------- collector


def test_search_params_and_headers():
    routes = Routes(pages={0: SEARCH})
    c = collector(routes, minutes_old=5, remote_only=True, easy_apply_only=True, geo_id=102713980)
    jobs = c.collect()
    # f_AL=true filtered server-side: the cards carry no marker, so the filter is trusted (3 jobs kept)
    assert len(jobs) == 3 and all(j.raw["easy_apply"] is True for j in jobs)
    req = routes.requests[0]
    assert req.url.params["keywords"] == "python developer"
    assert req.url.params["location"] == "India"
    assert req.url.params["f_TPR"] == "r300"
    assert req.url.params["f_WT"] == "2"
    assert req.url.params["f_AL"] == "true"
    assert req.url.params["geoId"] == "102713980"
    assert req.url.params["start"] == "0"
    assert c.key == "linkedin:guest" and c.source == "linkedin"
    plain = LinkedInGuestCollector(["x"], minutes_old=1)
    assert plain.search_params("x") == {"keywords": "x", "location": "", "f_TPR": "r60", "start": 0}
    h = plain.client.headers
    assert "Mozilla" in h["User-Agent"] and h["Accept-Language"].startswith("en")
    # same browser family as the persistent profile, with the client hints a real Chrome sends
    from jobapply.sites.browser import DESKTOP_USER_AGENT

    assert h["User-Agent"] == DESKTOP_USER_AGENT and "Cache-Control" not in h
    assert h["sec-ch-ua-platform"] == '"Windows"' and h["Sec-Fetch-Mode"] == "navigate"
    assert DESKTOP_USER_AGENT.split("Chrome/")[1].split(".")[0] in h["sec-ch-ua"]


def test_easy_apply_only_with_details_drops_only_confirmed_offsite():
    routes = Routes(pages={0: _page(["1", "2", "3", "4"])}, postings={"1": EASY, "2": OFFSITE, "3": POSTING})
    c = collector(routes, easy_apply_only=True, with_details=True, max_details=3)
    jobs = c.collect()
    # 1: Easy Apply confirmed; 2 and 3: posting page says offsite -> dropped; 4: no details, filter trusted
    assert [j.source_id for j in jobs] == ["1", "4"]
    assert jobs[1].raw["easy_apply"] is True and "details_fetched" not in jobs[1].raw


def test_pagination_stops_on_short_page_and_max_results():
    assert PAGE_SIZE == 10  # the guest endpoint serves 10 cards per page
    full = [str(5000 + i) for i in range(PAGE_SIZE)]
    routes = Routes(pages={0: _page(full), 10: _page(["9001", "9002"]), 20: _page(["9999"])})
    jobs = collector(routes, max_results=100).collect()
    assert len(jobs) == PAGE_SIZE + 2  # page 2 was short, page 3 never requested
    assert [int(r.url.params["start"]) for r in routes.requests] == [0, 10]

    full2 = [str(6000 + i) for i in range(PAGE_SIZE)]
    full3 = [str(7000 + i) for i in range(PAGE_SIZE)]
    routes = Routes(pages={0: _page(full), 10: _page(full2), 20: _page(full3)})
    jobs = collector(routes, max_results=15).collect()
    assert len(jobs) == 15
    assert [int(r.url.params["start"]) for r in routes.requests] == [0, 10]
    jobs = collector(Routes(pages={0: _page(full), 10: _page(full2), 20: _page(full3)}), max_results=50).collect()
    assert len(jobs) == 30  # three full pages, then an empty fourth page ends the query

    # a page that repeats ids already seen ends the query (LinkedIn pads the tail with repeats)
    routes = Routes(pages={0: _page(full), 10: _page(full), 20: _page(full2)})
    jobs = collector(routes, max_results=100).collect()
    assert len(jobs) == PAGE_SIZE and [int(r.url.params["start"]) for r in routes.requests] == [0, 10]

    routes = Routes(pages={0: ""})
    assert collector(routes).collect() == []
    assert len(routes.requests) == 1


def test_request_budget_caps_a_run():
    full = [str(5000 + i) for i in range(PAGE_SIZE)]
    full2 = [str(6000 + i) for i in range(PAGE_SIZE)]
    routes = Routes(pages={0: _page(full), 10: _page(full2)}, postings={"5000": EASY, "5001": EASY})
    # query a: two full pages + the empty third, then one posting page; the other 19 are never requested
    c = collector(routes, queries=["a"], max_results=100, max_requests=4, with_details=True)
    jobs = c.collect()
    assert len(routes.requests) == 4 and c.budget_exhausted is True and c.rate_limited is False
    assert len(jobs) == 20 and sum(1 for j in jobs if j.raw.get("details_fetched")) == 1
    # a budget spent on searching leaves later queries and every posting page untouched
    routes = Routes(pages={0: _page(full), 10: _page(full2)}, postings={"5000": EASY})
    c = collector(routes, queries=["a", "b"], max_results=100, max_requests=3, with_details=True)
    jobs = c.collect()
    assert [r.url.params.get("keywords") for r in routes.requests] == ["a", "a", "a"]
    assert len(jobs) == 20 and not any(j.raw.get("details_fetched") for j in jobs)


def test_rate_limit_stops_run_and_keeps_results():
    full = [str(5000 + i) for i in range(PAGE_SIZE)]
    for status in (429, 999):
        routes = Routes(pages={0: _page(full)}, status={"start=10": status})
        cooldowns: list[float] = []
        c = collector(routes, queries=["python", "backend"], max_results=100, on_rate_limited=cooldowns.append)
        jobs = c.collect()
        assert len(jobs) == PAGE_SIZE and c.rate_limited is True
        # second query never started, and no more requests after the limit
        assert len(routes.requests) == 2
        assert all(r.url.params["keywords"] == "python" for r in routes.requests)
        assert cooldowns == [3.0]  # Retry-After 10800 s wins over the 2 h default


def test_authwall_redirect_counts_as_a_block():
    full = [str(5000 + i) for i in range(PAGE_SIZE)]
    # search page 2 redirects to the authwall (HTTP 200 after the redirect)
    routes = Routes(pages={0: _page(full)}, redirect={"start=10"})
    cooldowns: list[float] = []
    c = collector(routes, queries=["python", "backend"], max_results=100, with_details=True,
                  on_rate_limited=cooldowns.append)
    jobs = c.collect()
    assert c.rate_limited is True and cooldowns == [2.0]
    assert len(jobs) == PAGE_SIZE and not any(j.raw.get("details_fetched") for j in jobs)
    # no posting pages were requested after the block
    assert all(str(r.url).startswith((SEARCH_URL, AUTHWALL)) for r in routes.requests)

    # a posting page redirecting to the authwall stops enrich(), the earlier listing keeps its details
    routes = Routes(pages={0: _page(["1", "2", "3"])}, postings={"1": EASY}, redirect={"2"})
    c = collector(routes, with_details=True)
    jobs = c.collect()
    assert c.rate_limited is True
    assert jobs[0].raw["details_fetched"] is True and "details_fetched" not in jobs[1].raw
    assert "details_fetched" not in jobs[2].raw

    # a 200 whose body has no job markup (interstitial) is treated the same way
    routes = Routes(pages={0: "<html><body>Please sign in to continue</body></html>"})
    c = collector(routes)
    assert c.collect() == [] and c.rate_limited is True


def test_skip_if_cooldown_makes_no_requests():
    routes = Routes(pages={0: SEARCH})
    c = collector(routes, skip_if=lambda: True)
    assert c.collect() == [] and routes.requests == []


def test_rate_limit_during_details_keeps_listings():
    routes = Routes(pages={0: _page(["1", "2", "3"])}, postings={"1": EASY}, status={"2": 429})
    c = collector(routes, with_details=True)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1", "2", "3"]
    assert jobs[0].raw["easy_apply"] is True and jobs[0].description
    assert jobs[1].raw.get("details_fetched") is None and jobs[2].raw.get("details_fetched") is None
    assert len(routes.requests) == 3  # search, posting 1, posting 2 (429) and then nothing


def test_details_enrichment_capped_and_deduped_across_queries():
    routes = Routes(pages={0: _page(["1", "2", "3"])}, postings={"1": EASY, "2": OFFSITE, "3": POSTING})
    c = collector(routes, queries=["python", "django"], with_details=True, max_details=2)
    jobs = c.collect()
    assert [j.source_id for j in jobs] == ["1", "2", "3"]  # same ids from both queries merged once
    assert jobs[0].raw["easy_apply"] is True and jobs[0].ats_type is None
    assert jobs[1].ats_type == "greenhouse" and jobs[1].apply_url.startswith("https://boards.greenhouse.io/")
    assert jobs[2].description == "" and "details_fetched" not in jobs[2].raw  # over max_details
    posting_requests = [r for r in routes.requests if str(r.url).startswith(POSTING_URL.format(job_id=""))]
    assert [str(r.url).rsplit("/", 1)[-1] for r in posting_requests] == ["1", "2"]


def test_fetch_details_direct_and_politeness_delay():
    routes = Routes(postings={"4475182617": POSTING})
    slept: list[float] = []
    c = LinkedInGuestCollector(["x"], client=routes.client(), delay=2.0, sleep=slept.append)
    d = c.fetch_details("4475182617")
    assert d and d.company == "Infosys"
    assert c.fetch_details("missing") is None
    assert slept == [] or len(slept) == 1  # no sleep before the first request, jittered one after
    c.fetch_details("4475182617")
    assert len(slept) == 2 and all(1.4 <= s <= 2.6 for s in slept)


def test_search_once_helper():
    routes = Routes(pages={0: SEARCH})
    jobs = search_once("python developer", "India", 30, with_details=False, delay=0, client=routes.client())
    assert len(jobs) == 3 and routes.requests[0].url.params["f_TPR"] == "r1800"
    jobs = search_once(["python developer", "backend"], minutes_old=1, with_details=False, delay=0,
                       client=routes.client())
    assert len(jobs) == 3
    # --easy-apply --no-details keeps the server-filtered cards instead of printing "0 job(s)"
    jobs = search_once("python", easy_apply_only=True, with_details=False, delay=0,
                       client=Routes(pages={0: SEARCH}).client())
    assert len(jobs) == 3 and all(j.raw["easy_apply"] for j in jobs)


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
    # a repeat block while the first cooldown is still active doubles the remaining window
    again = set_guest_cooldown(session, 2.0, now=now + timedelta(hours=1))
    assert again == now + timedelta(hours=3)
    # capped at 48 h
    assert set_guest_cooldown(session, 500, now=now) == now + timedelta(hours=48)
    # any user's challenge cooldown also keeps the guest scraper away from the shared IP
    user = create_user(session, "alice", "pw12345678")
    far = now + timedelta(days=30)
    user.preferences = {ls.COOLDOWN_KEY: (far + timedelta(hours=1)).isoformat()}
    session.add(user)
    session.commit()
    assert guest_cooldown_active(session, now=far) is True
    user.preferences = {}
    session.add(user)
    session.commit()
    assert guest_cooldown_active(session, now=far) is False


# ----------------------------------------------------------------- registry


def test_registry_builds_linkedin_only_when_enabled():
    s = Settings(_env_file=None)
    off = build_collectors(s, [], SearchConfig(queries=["python"], feeds=[]))
    assert [c.key for c in off] == []
    off = build_collectors(s, [], SearchConfig(queries=["python"], feeds=[], linkedin={"enabled": False}))
    assert [c.key for c in off] == []
    on = build_collectors(s, [], SearchConfig(queries=["python"], feeds=[], linkedin={
        "enabled": True, "minutes_old": 15, "location": "India", "remote_only": True, "easy_apply_only": True,
        "max_results": 10, "max_details": 3, "geo_id": 102713980}), extra_queries=["backend engineer"])
    assert [c.key for c in on] == ["linkedin:guest"]
    li = on[0]
    assert isinstance(li, LinkedInGuestCollector)
    assert set(li.queries) == {"python", "backend engineer"}  # shuffled per run so every role gets a turn
    assert (li.minutes_old, li.location, li.remote_only, li.easy_apply_only) == (15, "India", True, True)
    assert (li.max_results, li.max_details, li.geo_id, li.max_requests) == (10, 3, "102713980", 60)
    assert li.skip_if is not None and li.on_rate_limited is not None  # app_settings cooldown wired
    assert describe(on) == [{"key": "linkedin:guest", "source": "linkedin"}]
    cfg = SearchConfig(queries=["python"], feeds=[], linkedin={"enabled": True})
    assert [c.key for c in build_collectors(s, [], cfg, only=["linkedin"])] == ["linkedin:guest"]


def test_registry_survives_bad_linkedin_config_and_warns_on_double_linkedin(caplog):
    assert linkedin_options(None) is None and linkedin_options({"enabled": False}) is None
    assert linkedin_options({"enabled": True, "max_requests": 12})["max_requests"] == 12
    with caplog.at_level(logging.WARNING, logger="jobapply.collectors.registry"):
        assert linkedin_options({"enabled": True, "minutes_old": "30m"}) is None
        assert linkedin_options({"enabled": True, "max_results": "fifty"}) is None
    assert "bad linkedin setting" in caplog.text
    s = Settings(_env_file=None)
    # a malformed block disables only LinkedIn; the feeds still run
    cfg = SearchConfig(queries=["python"], feeds=["remotive"], linkedin={"enabled": True, "max_details": "x"})
    assert [c.source for c in build_collectors(s, [], cfg)] == ["remotive"]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jobapply.collectors.registry"):
        cfg = SearchConfig(queries=["python"], feeds=[], jobspy_sites=["linkedin"], linkedin={"enabled": True})
        keys = [c.key for c in build_collectors(s, [], cfg)]
    assert "linkedin:guest" in keys and "two source rows" in caplog.text
