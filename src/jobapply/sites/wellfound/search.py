"""Wellfound public job search (no login).

Uses the SEO landing pages a logged-out visitor can open (verified live 2026-10-29 with plain HTTP and
browser headers, HTTP 200 behind Cloudflare):

- ``/role/<role-slug>``                  e.g. ``/role/python-developer``
- ``/role/l/<role-slug>/<location-slug>`` e.g. ``/role/l/python-developer/india``
- ``/role/r/<role-slug>``                 remote-only variant
- ``?page=N`` paginates (20 startups per page, each with its highlighted job listings; ``pageCount`` is in
  the payload).

Each page is a Next.js page whose ``<script id="__NEXT_DATA__">`` embeds the Apollo cache:
``StartupResult:<id>`` entries (name, slug, size, badges) referencing ``JobListingSearchResult:<id>`` entries
(title, slug, description, locationNames, remote / remoteConfig, compensation, liveStartAt, jobType,
yearsExperienceMin). The job page is ``/jobs/<id>-<slug>`` (server-rendered, JSON-LD ``JobPosting``), which
``fetch_details`` can parse when ``with_details`` is on.

``/company/<slug>/jobs`` answered a Cloudflare challenge (403, ``cf-mitigated: challenge``) and is not used.
A 403/429, a challenge page or a redirect to login stops the run, keeps what was collected and records a
cooldown through ``on_rate_limited`` (the registry wires it to ``set_guest_cooldown``).
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt, parse_salary
from jobapply.config import get_settings
from jobapply.sites.browser import DESKTOP_USER_AGENT

SITE = "wellfound"
BASE_URL = "https://wellfound.com"
ROLE_URL = BASE_URL + "/role/{role}"
ROLE_LOCATION_URL = BASE_URL + "/role/l/{role}/{location}"
ROLE_REMOTE_URL = BASE_URL + "/role/r/{role}"
JOB_URL = BASE_URL + "/jobs/{job_id}-{slug}"
PAGE_SIZE = 20  # startups per landing page
RATE_LIMIT_STATUSES = (403, 429)
BLOCK_PATHS = ("/login", "/join", "/jobs/signup", "/users/sign_in")
DEFAULT_MAX_REQUESTS = 40
DEFAULT_MAX_PAGES = 3
DEFAULT_COOLDOWN_HOURS = 2.0

_CHROME_MAJOR = re.search(r"Chrome/(\d+)", DESKTOP_USER_AGENT)
_CHROME_V = _CHROME_MAJOR.group(1) if _CHROME_MAJOR else "131"
BROWSER_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": f'"Chromium";v="{_CHROME_V}", "Google Chrome";v="{_CHROME_V}", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_JOB_DESCRIPTION = re.compile(r'<div id="job-description">(.*?)</div>\s*</div>', re.S)
_TITLE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_CHALLENGE_MARKUP = re.compile(r"challenge-platform|_cf_chl_opt|cf-chl|<title>Security Check", re.I)
_LOGIN_BUTTON = re.compile(r"window\.location\.href='/login", re.I)
_APPLY_BUTTON = re.compile(r"autoOpenApplication=true", re.I)
# The "Skills" block of the job page: a label span followed by one chip div per skill.
_SKILLS_BLOCK = re.compile(r"<span[^>]*>\s*Skills\s*</span>\s*<div[^>]*>((?:\s*<div[^>]*>[^<]{1,60}</div>)+)", re.S)
_CHIP = re.compile(r"<div[^>]*>([^<]{1,60})</div>", re.S)
_MD_MARKS = re.compile(r"(\*\*|__|^#{1,6}\s*)", re.M)
_TAGS = re.compile(r"<[^>]+>")

# Query wording -> Wellfound role slug. Everything else is slugified as-is; an unknown slug 404s and the query
# is skipped with a log line (a 404 is not a block).
ROLE_ALIASES: dict[str, str] = {
    "sde": "software-engineer",
    "software developer": "software-engineer",
    "software development engineer": "software-engineer",
    "backend developer": "backend-engineer",
    "back end engineer": "backend-engineer",
    "frontend developer": "frontend-engineer",
    "front end engineer": "frontend-engineer",
    "full stack developer": "full-stack-engineer",
    "fullstack developer": "full-stack-engineer",
    "fullstack engineer": "full-stack-engineer",
    "ml engineer": "machine-learning-engineer",
    "machine learning": "machine-learning-engineer",
    "devops": "devops-engineer",
    "sre": "site-reliability-engineer",
    "data science": "data-scientist",
    "python": "python-developer",
    "django developer": "python-developer",
}
LOCATION_ALIASES: dict[str, str] = {
    "bengaluru": "bangalore",
    "bangalore urban": "bangalore",
    "bombay": "mumbai",
    "gurugram": "gurgaon",
    "new york city": "new-york",
    "nyc": "new-york",
    "sf": "san-francisco",
    "sf bay area": "san-francisco-bay-area",
    "bay area": "san-francisco-bay-area",
    "uk": "united-kingdom",
    "usa": "united-states",
    "us": "united-states",
}
REMOTE_WORDS = {"remote", "anywhere", "work from home", "wfh", "worldwide"}


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())).strip("-")


def role_slug(query: str) -> str:
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    return ROLE_ALIASES.get(q) or slugify(q)


def location_slug(location: str) -> str:
    """'Bengaluru' -> 'bangalore', 'San Francisco' -> 'san-francisco'. Empty for remote wording."""
    loc = re.sub(r"\s+", " ", (location or "").strip().lower()).rstrip(".")
    if not loc or loc in REMOTE_WORDS:
        return ""
    return LOCATION_ALIASES.get(loc) or slugify(loc)


def is_remote_location(location: str) -> bool:
    return (location or "").strip().lower() in REMOTE_WORDS


def search_url(query: str, location: str = "", remote_only: bool = False, page: int = 1) -> str:
    role = role_slug(query)
    if remote_only or is_remote_location(location):
        url = ROLE_REMOTE_URL.format(role=role)
    elif location_slug(location):
        url = ROLE_LOCATION_URL.format(role=role, location=location_slug(location))
    else:
        url = ROLE_URL.format(role=role)
    return f"{url}?page={page}" if page > 1 else url


def job_url(job_id: str, slug: str = "") -> str:
    return JOB_URL.format(job_id=job_id, slug=slug or "job").rstrip("-")


def job_id_from(value: str) -> str | None:
    m = re.search(r"/jobs/(\d{4,})(?:-|$|\?)", value or "")
    return m.group(1) if m else None


def is_blocked_url(url: str) -> bool:
    parts = urlsplit(url or "")
    if "wellfound.com" not in parts.netloc.lower():
        return False
    path = parts.path.lower()
    return any(path.startswith(p) for p in BLOCK_PATHS)


def looks_like_challenge(body: str) -> bool:
    return bool(_CHALLENGE_MARKUP.search(body or ""))


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", fragment))).strip()


def clean_description(text: str | None) -> str:
    """Listing descriptions are markdown-ish (`**Job Description**`, `### ...`): drop the marks, keep lines."""
    text = (text or "").replace("\r\n", "\n")
    text = _MD_MARKS.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ------------------------------------------------------------------ parsing (pure)


@dataclass
class PageInfo:
    page_count: int = 0
    total_jobs: int = 0
    total_startups: int = 0
    logged_in: bool | None = None


@dataclass
class JobDetails:
    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    posted_at: datetime | None = None
    employment_type: str = ""
    direct_apply: bool | None = None
    apply_available: bool = False
    logged_out: bool = False
    skills: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.title or self.description)


def extract_next_data(page: str) -> dict[str, Any] | None:
    m = _NEXT_DATA.search(page or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def apollo_cache(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    props = data.get("props") or {}
    state = (props.get("pageProps") or {}).get("apolloState") or props.get("apolloState") or {}
    if isinstance(state, dict) and isinstance(state.get("data"), dict):
        return state["data"]
    return state if isinstance(state, dict) else {}


def _ref(cache: dict[str, Any], value: Any) -> dict[str, Any] | None:
    """Resolve an Apollo `{"__ref": "Type:id"}` (or an inline object) to its cache entry."""
    if isinstance(value, dict):
        if "__ref" in value:
            hit = cache.get(str(value["__ref"]))
            return hit if isinstance(hit, dict) else None
        return value
    return None


def _results_entry(cache: dict[str, Any]) -> dict[str, Any] | None:
    talent = (cache.get("ROOT_QUERY") or {}).get("talent") or {}
    for key, value in talent.items():
        if key.startswith("seoLandingPageJobSearchResults") and isinstance(value, dict):
            return value
    return None


def page_info(cache: dict[str, Any]) -> PageInfo:
    info = PageInfo()
    results = _results_entry(cache)
    if results:
        info.page_count = int(results.get("pageCount") or 0)
        info.total_jobs = int(results.get("totalJobCount") or 0)
        info.total_startups = int(results.get("totalStartupCount") or 0)
    viewer = ((cache.get("ROOT_QUERY") or {}).get("talent") or {}).get("viewer") or {}
    if isinstance(viewer, dict) and "currentUser" in viewer:
        info.logged_in = bool(viewer.get("currentUser"))
    return info


def _badge_names(cache: dict[str, Any], startup: dict[str, Any]) -> list[str]:
    names = []
    for b in startup.get("badges") or []:
        entry = _ref(cache, b) or {}
        ref = b.get("__ref") if isinstance(b, dict) else ""
        name = str(entry.get("name") or entry.get("label") or entry.get("id") or ref or "")
        if name.startswith("Badge:"):
            name = name.split(":", 1)[1].split("-", 1)[0]
        if name:
            names.append(name)
    return names


def _job_from_entry(cache: dict[str, Any], entry: dict[str, Any], startup: dict[str, Any] | None,
                    now: datetime | None = None) -> NormalizedJob | None:
    job_id = str(entry.get("id") or "")
    title = _text(str(entry.get("title") or ""))
    if not job_id or not title:
        return None
    startup = startup or _ref(cache, entry.get("startup")) or {}
    company = _text(str(startup.get("name") or ""))
    locations = [str(x) for x in (entry.get("locationNames") or []) if x]
    remote_cfg = _ref(cache, entry.get("remoteConfig")) or {}
    remote_kind = str(remote_cfg.get("kind") or "")
    accepted = [str(x) for x in (entry.get("acceptedRemoteLocationNames") or []) if x]
    remote = bool(entry.get("remote")) or remote_kind.upper() in ("REMOTE", "ONSITE_OR_REMOTE", "REMOTE_ONLY")
    location = ", ".join(locations)
    if remote and accepted:
        location = f"{location} (remote: {', '.join(accepted)})" if location else f"Remote: {', '.join(accepted)}"
    elif remote and not location:
        location = "Remote"
    compensation = str(entry.get("compensation") or "").strip()
    salary_min, salary_max, currency = parse_salary(compensation)
    slug = str(entry.get("slug") or slugify(title))
    url = job_url(job_id, slug)
    posted_at = parse_dt(entry.get("liveStartAt"))
    primary_role = entry.get("primaryRoleTitle") or (_ref(cache, entry.get("primaryRole")) or {}).get("slug") or ""
    raw: dict[str, Any] = {
        "slug": slug,
        "job_type": entry.get("jobType") or "",
        "compensation": compensation,
        "years_experience_min": entry.get("yearsExperienceMin"),
        "years_experience_max": entry.get("yearsExperienceMax"),
        "remote_kind": remote_kind,
        "accepted_remote_locations": accepted,
        "primary_role": primary_role,
        "ats_source": entry.get("atsSource"),
        "auto_posted": bool(entry.get("autoPosted", False)),
        "company_id": str(startup.get("id") or ""),
        "company_slug": startup.get("slug") or "",
        "company_size": startup.get("companySize") or "",
        "company_pitch": startup.get("highConcept") or "",
        "badges": _badge_names(cache, startup),
        "listing_url": url,
    }
    return NormalizedJob(
        title=title, company=company or "Unknown startup", location=location,
        remote=remote or looks_remote(location, title),
        posted_at=posted_at, url=url, apply_url=url,
        description=clean_description(entry.get("description")),
        ats_type=SITE, salary_min=salary_min, salary_max=salary_max, currency=currency,
        source=SITE, source_id=job_id, raw=raw,
    )


def parse_cache(cache: dict[str, Any], now: datetime | None = None) -> list[NormalizedJob]:
    """Jobs from an Apollo cache: startups' highlighted listings (role pages) and bare `JobListing` entries
    that point at their startup (the `/jobs` landing page). Ordered as the page lists them, deduplicated."""
    found: dict[str, NormalizedJob] = {}
    for key, startup in cache.items():
        if not key.startswith(("StartupResult:", "Startup:")) or not isinstance(startup, dict):
            continue
        for ref in startup.get("highlightedJobListings") or startup.get("jobListings") or []:
            entry = _ref(cache, ref)
            if entry and (job := _job_from_entry(cache, entry, startup, now)) and job.source_id not in found:
                found[job.source_id] = job
    for key, entry in cache.items():
        if not key.startswith(("JobListingSearchResult:", "JobListing:")) or not isinstance(entry, dict):
            continue
        job = _job_from_entry(cache, entry, None, now)
        if job and job.source_id not in found:
            found[job.source_id] = job
    return list(found.values())


def parse_search_html(page: str, now: datetime | None = None) -> tuple[list[NormalizedJob], PageInfo]:
    cache = apollo_cache(extract_next_data(page))
    if not cache:
        return [], PageInfo()
    return parse_cache(cache, now), page_info(cache)


def parse_job_html(page: str, job_id: str) -> JobDetails:
    """The server-rendered job page: JSON-LD `JobPosting` plus the apply / login buttons."""
    d = JobDetails(job_id=job_id)
    for m in _LD_JSON.finditer(page or ""):
        try:
            ld = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(ld, dict) or ld.get("@type") != "JobPosting":
            continue
        d.title = _text(str(ld.get("title") or ""))
        d.company = _text(str((ld.get("hiringOrganization") or {}).get("name") or ""))
        d.description = html_to_text(ld.get("description") or "")
        d.posted_at = parse_dt(ld.get("datePosted"))
        d.employment_type = str(ld.get("employmentType") or "")
        if "directApply" in ld:
            d.direct_apply = bool(ld.get("directApply"))
        places = ld.get("jobLocation") or []
        if isinstance(places, dict):
            places = [places]
        names = []
        for p in places:
            addr = (p or {}).get("address") or {}
            name = addr.get("addressLocality") or addr.get("addressRegion") or addr.get("addressCountry")
            if name and name not in names:
                names.append(str(name))
        d.location = ", ".join(names)
        break
    if not d.description and (m := _JOB_DESCRIPTION.search(page or "")):
        d.description = html_to_text(m.group(1))
    if not d.title and (m := _TITLE_H1.search(page or "")):
        d.title = _text(m.group(1))
    d.apply_available = bool(_APPLY_BUTTON.search(page or ""))
    d.logged_out = bool(_LOGIN_BUTTON.search(page or ""))
    if (m := _SKILLS_BLOCK.search(page or "")):
        d.skills = [t for t in (_text(c) for c in _CHIP.findall(m.group(1))) if t][:20]
    return d


def apply_details(job: NormalizedJob, d: JobDetails) -> NormalizedJob:
    """Merge a job page into a listing. An empty page (challenge, interstitial) leaves it untouched."""
    if d.empty:
        return job
    update: dict[str, Any] = {}
    if d.description and len(d.description) > len(job.description):
        update["description"] = d.description
    if d.title and not job.title:
        update["title"] = d.title
    if d.company and (not job.company or job.company == "Unknown startup"):
        update["company"] = d.company
    if d.location and not job.location:
        update["location"] = d.location
    if d.posted_at and not job.posted_at:
        update["posted_at"] = d.posted_at
    raw = dict(job.raw)
    raw["details_fetched"] = True
    if d.employment_type:
        raw["employment_type"] = d.employment_type
    if d.direct_apply is not None:
        raw["direct_apply"] = d.direct_apply
    raw["apply_available"] = d.apply_available
    return job.model_copy(update={**update, "raw": raw})


def within_hours(job: NormalizedJob, hours_old: int, now: datetime | None = None) -> bool:
    """True when the listing went live within `hours_old` (0 or less: no filter; unknown date: kept)."""
    if hours_old <= 0 or job.posted_at is None:
        return True
    return job.posted_at >= (now or datetime.now(UTC)) - timedelta(hours=hours_old)


# ------------------------------------------------------------------ collector


class WellfoundCollector(BaseCollector):
    """Logged-out search over the public role pages. `user_id` is accepted for interface uniformity: the public
    surface needs no login, so the persistent profile is never opened here (it only tags the log lines)."""

    source = SITE
    ats_type = SITE
    name = SITE

    def __init__(
        self,
        queries: list[str],
        location: str = "",
        hours_old: int = 24,
        max_results: int = 50,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_id: int | None = None,
        *,
        remote_only: bool = False,
        with_details: bool = False,
        max_details: int = 20,
        max_pages: int = DEFAULT_MAX_PAGES,
        delay: float = 2.0,
        shuffle_queries: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_if: Callable[[], bool] | None = None,
        on_rate_limited: Callable[[float], None] | None = None,
        now: datetime | None = None,
        **kw: Any,
    ):
        super().__init__(client)
        self.queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        if shuffle_queries:
            random.shuffle(self.queries)
        self.location = location or ""
        self.hours_old = int(hours_old)
        self.max_results = max(1, int(max_results))
        self.max_requests = max(1, int(max_requests))
        self.user_id = user_id
        self.remote_only = bool(remote_only)
        self.with_details, self.max_details = with_details, max(0, int(max_details))
        self.max_pages = max(1, int(max_pages))
        self.delay, self._sleep = delay, sleep
        self.skip_if, self.on_rate_limited = skip_if, on_rate_limited
        self._now = now
        self.rate_limited = False
        self.budget_exhausted = False
        self.requests_made = 0
        self.last_block_reason = ""
        if kw:
            self.log.debug("ignored collector options", options=sorted(kw))

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().http_timeout, headers=BROWSER_HEADERS,
                                        follow_redirects=True)
        return self._client

    @property
    def key(self) -> str:
        return f"{SITE}:public" if self.user_id is None else f"{SITE}:public:u{self.user_id}"

    # -- requests

    def _pause(self) -> None:
        if self.requests_made and self.delay > 0:
            self._sleep(self.delay * random.uniform(0.7, 1.3))

    def _block(self, reason: str, url: str, retry_after: str | None = None) -> None:
        self.rate_limited = True
        self.last_block_reason = reason
        hours = DEFAULT_COOLDOWN_HOURS
        try:
            if retry_after:
                hours = max(hours, float(retry_after) / 3600)
        except ValueError:
            pass
        self.log.warning("blocked by wellfound, stopping run", reason=reason, url=url, requests=self.requests_made,
                         cooldown_hours=round(hours, 2))
        if self.on_rate_limited is not None:
            try:
                self.on_rate_limited(hours)
            except Exception as exc:  # noqa: BLE001 - persisting the cooldown must not break the run
                self.log.warning("could not record wellfound cooldown", error=str(exc)[:200])

    def _get(self, url: str) -> str | None:
        """GET with politeness delay and budget. None when blocked, exhausted, missing or not a job page."""
        if self.rate_limited:
            return None
        if self.requests_made >= self.max_requests:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.log.info("wellfound request budget reached", max_requests=self.max_requests)
            return None
        self._pause()
        self.requests_made += 1
        try:
            resp = self.client.get(url, headers=BROWSER_HEADERS)  # also on an injected client
        except httpx.HTTPError as exc:
            self.log.warning("request failed", url=url, error=str(exc))
            return None
        if resp.status_code in RATE_LIMIT_STATUSES:
            challenged = resp.headers.get("cf-mitigated") == "challenge"
            self._block("cloudflare challenge" if challenged else f"http {resp.status_code}", url,
                        resp.headers.get("retry-after"))
            return None
        if resp.status_code == 404:
            self.log.info("wellfound page not found (unknown role or location slug?)", url=url)
            return None
        if resp.status_code >= 400:
            self.log.warning("http error", status=resp.status_code, url=url)
            return None
        final = str(resp.url)
        if is_blocked_url(final) or any(is_blocked_url(str(h.url)) for h in resp.history):
            self._block("login redirect", final)
            return None
        body = resp.text
        if looks_like_challenge(body):
            self._block("challenge page", final)
            return None
        return body

    def fetch_page(self, query: str, page: int = 1) -> tuple[list[NormalizedJob], PageInfo]:
        body = self._get(search_url(query, self.location, self.remote_only, page))
        return parse_search_html(body, self._now) if body else ([], PageInfo())

    def fetch_details(self, job: NormalizedJob) -> JobDetails | None:
        body = self._get(job.url)
        return parse_job_html(body, job.source_id) if body else None

    # -- pipeline

    def search(self, query: str) -> list[NormalizedJob]:
        found: dict[str, NormalizedJob] = {}
        page = 1
        page_count = 1
        while (page <= min(page_count, self.max_pages) and len(found) < self.max_results
               and not self.rate_limited and not self.budget_exhausted):
            jobs, info = self.fetch_page(query, page)
            if info.page_count:
                page_count = info.page_count
            new = [j for j in jobs if j.source_id not in found]
            for job in new:
                found[job.source_id] = job
            if not jobs or not new:
                break
            page += 1
        self.log.info("searched", query=query, role=role_slug(query), location=self.location,
                      remote_only=self.remote_only, found=len(found), pages=page - 1, rate_limited=self.rate_limited)
        return list(found.values())

    def enrich(self, jobs: list[NormalizedJob]) -> list[NormalizedJob]:
        out, fetched = [], 0
        for job in jobs:
            if fetched >= self.max_details or self.rate_limited or self.budget_exhausted:
                out.append(job)
                continue
            fetched += 1
            details = self.fetch_details(job)
            out.append(apply_details(job, details) if details else job)
        if fetched:
            self.log.info("fetched details", count=fetched, skipped=len(jobs) - fetched)
        return out

    def collect(self) -> list[NormalizedJob]:
        if self.skip_if is not None and self.skip_if():
            self.log.info("wellfound public search skipped: cooldown active")
            return []
        seen: dict[str, NormalizedJob] = {}
        for query in self.queries:
            if self.rate_limited or self.budget_exhausted:
                break
            for job in self.search(query):
                seen.setdefault(job.source_id, job)
        jobs = list(seen.values())
        kept = [j for j in jobs if within_hours(j, self.hours_old, self._now)]
        if len(kept) < len(jobs):
            self.log.info("dropped listings older than the window", hours_old=self.hours_old,
                          dropped=len(jobs) - len(kept), kept=len(kept))
        kept.sort(key=lambda j: j.posted_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        kept = kept[: self.max_results]
        if self.with_details and kept:
            kept = self.enrich(kept)
        return kept


def search_once(
    queries: list[str] | str,
    location: str = "",
    hours_old: int = 24,
    *,
    remote_only: bool = False,
    max_results: int = 50,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    with_details: bool = False,
    delay: float = 2.0,
    client: httpx.Client | None = None,
) -> list[NormalizedJob]:
    """One-shot public search for a CLI. No database side effects."""
    collector = WellfoundCollector(
        [queries] if isinstance(queries, str) else list(queries), location, hours_old,
        max_results=max_results, max_requests=max_requests, remote_only=remote_only,
        with_details=with_details, delay=delay, client=client,
    )
    return collector.collect()


# ------------------------------------------------------------------ cross-run cooldown (app_settings)

GUEST_COOLDOWN_KEY = "wellfound_guest_cooldown_until"


def guest_cooldown_until(session: Any) -> datetime | None:
    from jobapply.switches import get_value

    raw = get_value(session, GUEST_COOLDOWN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def guest_cooldown_active(session: Any, now: datetime | None = None) -> bool:
    """True while the public scraper should stay away: its own cooldown, or any user's challenge cooldown."""
    from sqlmodel import select

    from jobapply.models import User
    from jobapply.sites.wellfound.session import cooldown_until

    now = now or datetime.now(UTC)
    until = guest_cooldown_until(session)
    if until and until > now:
        return True
    for user in session.exec(select(User).where(User.is_active)).all():
        user_until = cooldown_until(user)
        if user_until and user_until > now:
            return True
    return False


def set_guest_cooldown(session: Any, hours: float = DEFAULT_COOLDOWN_HOURS, now: datetime | None = None) -> datetime:
    """Record a cooldown, doubling the window when the previous one is still active (repeat blocks), 48 h cap."""
    from jobapply.switches import set_value

    now = now or datetime.now(UTC)
    previous = guest_cooldown_until(session)
    if previous and previous > now:
        remaining = (previous - now).total_seconds() / 3600
        hours = max(hours, remaining * 2)
    until = now + timedelta(hours=min(hours, 48))
    set_value(session, GUEST_COOLDOWN_KEY, until.isoformat())
    return until


__all__ = ["SITE", "WellfoundCollector", "search_once", "parse_search_html", "parse_cache", "parse_job_html",
           "apply_details", "search_url", "role_slug", "location_slug", "job_url", "job_id_from", "is_blocked_url",
           "looks_like_challenge", "within_hours", "PageInfo", "JobDetails", "GUEST_COOLDOWN_KEY",
           "guest_cooldown_until", "guest_cooldown_active", "set_guest_cooldown", "BROWSER_HEADERS", "PAGE_SIZE",
           "RATE_LIMIT_STATUSES", "DEFAULT_COOLDOWN_HOURS"]
