"""Instahyre public job search (no login).

Uses the JSON endpoint the logged-out listing pages (``/search-jobs``, ``/python-jobs``, ...) call themselves
(AngularJS app ``auctioned``, controller ``opportunitiesListCtrl``), verified live on 2026-10-29:

- ``GET /api/v1/job_search?skills=<q>&job_type=1&limit=20&offset=0&search=true&isLandingPage=true`` returns
  ``{"objects": [...], "meta": {"total_count", "next", "offset", "limit", ...}}``; 20 cards per page. Each
  card has ``id``, ``title`` / ``candidate_title``, ``locations`` (comma-joined string), ``keywords``,
  ``public_url`` (the opportunity page) and an ``employer`` block (``company_name``, ``employee_count``, ...).
  ``skills`` takes free text ("python developer") and may be repeated; a comma-joined value is a 400.
  ``years`` (max experience) and ``job_type`` (0 both, 1 full-time, 2 internship) are honoured;
  ``location`` is silently ignored by the server, so the location filter is applied client-side here.
- ``GET <public_url>`` (``/job-<id>-<slug>/``) is server-rendered: a ``<script type="application/ld+json">``
  JobPosting block (title, HTML description, ``datePosted``, ``employmentType``, ``jobLocation``) plus an
  ``<span class="experience">4-8 Years</span>`` chip. One request per job enriches everything.
- ``GET /api/v1/employer_public_jobs/<id>`` is the JSON alternative (description, ``workex_min/max``,
  ``is_active``) but carries no posting date, so the page is preferred.

Plain httpx with Chrome-like headers was served everything; headless Chromium got a Cloudflare interstitial
(403 "Just a moment..."). HTTP 403/429/503, a Cloudflare body or a redirect to the login page means "go away":
the run stops there, returns what it already has and calls ``on_rate_limited`` so the next run can skip.
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt
from jobapply.config import get_settings
from jobapply.sites.browser import DESKTOP_USER_AGENT
from jobapply.sites.instahyre.session import BASE_URL, is_challenge_url, is_login_url

SEARCH_URL = f"{BASE_URL}/api/v1/job_search"
PUBLIC_JOB_API_URL = f"{BASE_URL}/api/v1/employer_public_jobs/{{job_id}}"
JOB_URL = f"{BASE_URL}/job-{{job_id}}/"
PAGE_SIZE = 20
JOB_TYPES = {"both": 0, "full_time": 1, "internship": 2}
RATE_LIMIT_STATUSES = (403, 429, 503)
DEFAULT_MAX_REQUESTS = 40
DEFAULT_COOLDOWN_HOURS = 2.0

_CHROME_MAJOR = re.search(r"Chrome/(\d+)", DESKTOP_USER_AGENT)
_CHROME_V = _CHROME_MAJOR.group(1) if _CHROME_MAJOR else "131"
_COMMON_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": f'"Chromium";v="{_CHROME_V}", "Google Chrome";v="{_CHROME_V}", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
# What the Angular $resource sends from the listing page.
API_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BASE_URL}/search-jobs",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
# A plain top-level navigation, for the opportunity page.
PAGE_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ------------------------------------------------------------------ markup patterns (opportunity page)

_LD_JSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_EXPERIENCE = re.compile(r'<span class="experience">(.*?)</span>', re.S)
_YEARS_RANGE = re.compile(r"(\d+)\s*(?:-\s*(\d+))?\s*\+?\s*years?", re.I)
_QUILL_DESCRIPTION = re.compile(r'<div class="quill-formatted">(.*?)</div>\s*</div>', re.S)
_POSTED_BY = re.compile(r'<span class="rec-name">\s*(?:Posted by\s*)?(.*?)</span>', re.S)
_TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_CF_BODY = re.compile(r"just a moment|cf-challenge|challenge-platform|cf-turnstile|attention required", re.I)
_JOB_MARKUP = re.compile(r"application/ld\+json|quill-formatted|job_details_top|employer-profile", re.I)
_TAGS = re.compile(r"<[^>]+>")
_JOB_ID_IN_URL = re.compile(r"/job-(\d+)")


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", fragment))).strip()


def job_id_from(value: str | int | None) -> str | None:
    """``447614`` / ``https://www.instahyre.com/job-447614-python-developer-at-topdoc-bangalore/`` -> ``447614``."""
    if value is None:
        return None
    s = str(value).strip()
    if s.isdigit():
        return s
    m = _JOB_ID_IN_URL.search(s)
    return m.group(1) if m else None


def parse_experience(text: str) -> tuple[int | None, int | None]:
    """``4-8 Years`` -> (4, 8); ``10+ Years`` -> (10, None); anything else -> (None, None)."""
    m = _YEARS_RANGE.search(text or "")
    if not m:
        return None, None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else None
    return lo, hi


def _locations_text(value: Any) -> str:
    """The API gives a comma-joined string on search cards and a list on job records."""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return re.sub(r"\s*,\s*", ", ", str(value or "")).strip()


def is_blocked_url(url: str) -> bool:
    """True when a request ended on the login page or a Cloudflare challenge URL."""
    return is_login_url(url) or is_challenge_url(url)


# ------------------------------------------------------------------ parsing (pure)


def parse_search_json(data: Any) -> list[NormalizedJob]:
    """Parse one ``/api/v1/job_search`` response into jobs. Cards without an id or title are skipped."""
    rows = data.get("objects") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    jobs: list[NormalizedJob] = []
    for row in rows:
        job = parse_card(row)
        if job:
            jobs.append(job)
    return jobs


def parse_card(row: Any) -> NormalizedJob | None:
    if not isinstance(row, dict):
        return None
    job_id = job_id_from(row.get("id"))
    title = str(row.get("candidate_title") or row.get("title") or "").strip()
    if not job_id or not title:
        return None
    employer = row.get("employer") if isinstance(row.get("employer"), dict) else {}
    company = str(employer.get("company_name") or row.get("hiring_company_name") or "").strip()
    location = _locations_text(row.get("locations"))
    url = str(row.get("public_url") or "").strip() or JOB_URL.format(job_id=job_id)
    keywords = [str(k) for k in (row.get("keywords") or []) if k]
    raw: dict[str, Any] = {
        "keywords": keywords,
        "employer_id": employer.get("id"),
        "company_tagline": employer.get("company_tagline") or "",
        "employee_count": employer.get("employee_count"),
        "company_founded": employer.get("company_founded"),
        "accept_outstation": bool(row.get("accept_outstation", False)),
        "is_sourced": bool(row.get("is_sourced", False)),
        "one_click_apply": True,
    }
    return NormalizedJob(
        title=title, company=company, location=location,
        remote=looks_remote(location, title),
        url=url, apply_url=url,
        source="instahyre", source_id=job_id, ats_type="instahyre", raw=raw,
    )


@dataclass
class JobDetails:
    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    posted_at: datetime | None = None
    employment_type: str = ""
    experience: str = ""
    workex_min: int | None = None
    workex_max: int | None = None
    posted_by: str = ""
    blocked: bool = False

    @property
    def empty(self) -> bool:
        """No title, description or date: a challenge / login page, not an opportunity page."""
        return not (self.title or self.description or self.posted_at)


def _job_posting_block(page: str) -> dict[str, Any]:
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(html_lib.unescape(block.strip()) if "&quot;" in block else block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and str(item.get("@type", "")).lower() == "jobposting":
                return item
    return {}


def _ld_locations(block: dict[str, Any]) -> str:
    places = block.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    names: list[str] = []
    for place in places:
        addr = place.get("address") if isinstance(place, dict) else None
        if not isinstance(addr, dict):
            continue
        name = addr.get("addressLocality") or addr.get("streetAddress") or addr.get("addressRegion") or ""
        if name and name not in names:
            names.append(str(name))
    return ", ".join(names)


def parse_job_page(page: str, job_id: str) -> JobDetails:
    """Parse a saved opportunity page. A Cloudflare / login page comes back ``blocked`` and ``empty``."""
    d = JobDetails(job_id=job_id)
    if _CF_BODY.search(_TITLE_TAG.search(page or "").group(1) if _TITLE_TAG.search(page or "") else "") or (
            not _JOB_MARKUP.search(page or "") and _CF_BODY.search(page or "")):
        d.blocked = True
        return d
    block = _job_posting_block(page)
    d.title = _text(str(block.get("title") or ""))
    org = block.get("hiringOrganization") or {}
    d.company = _text(str(org.get("name") or "")) if isinstance(org, dict) else ""
    d.location = _ld_locations(block)
    desc = block.get("description") or ""
    if not desc and (m := _QUILL_DESCRIPTION.search(page)):
        desc = m.group(1)
    d.description = html_to_text(str(desc))
    d.posted_at = parse_dt(block.get("datePosted"))
    d.employment_type = str(block.get("employmentType") or "")
    if (m := _EXPERIENCE.search(page)):
        d.experience = _text(m.group(1))
        d.workex_min, d.workex_max = parse_experience(d.experience)
    if (m := _POSTED_BY.search(page)):
        d.posted_by = _text(m.group(1))
    return d


def parse_job_json(data: Any, job_id: str) -> JobDetails:
    """Parse an ``/api/v1/employer_public_jobs/<id>`` record (no posting date in it)."""
    d = JobDetails(job_id=job_id)
    if not isinstance(data, dict):
        return d
    d.title = str(data.get("candidate_title") or data.get("title") or "").strip()
    d.company = str(data.get("hiring_company_name") or "").strip()
    d.location = _locations_text(data.get("locations"))
    d.description = html_to_text(str(data.get("description") or ""))
    d.employment_type = "INTERN" if data.get("is_internship") else "FULL_TIME"
    d.workex_min = data.get("workex_min") if isinstance(data.get("workex_min"), int) else None
    d.workex_max = data.get("workex_max") if isinstance(data.get("workex_max"), int) else None
    if d.workex_min is not None:
        d.experience = f"{d.workex_min}-{d.workex_max} Years" if d.workex_max is not None else f"{d.workex_min}+ Years"
    d.posted_by = str(data.get("recruiter_name") or "").strip()
    return d


def apply_details(job: NormalizedJob, d: JobDetails) -> NormalizedJob:
    """Merge an opportunity page into a listing. An empty / blocked page leaves the listing untouched and
    unmarked so a later run retries."""
    if d.empty or d.blocked:
        return job
    update: dict[str, Any] = {}
    if d.description:
        update["description"] = d.description
    if d.title and not job.title:
        update["title"] = d.title
    if d.company and not job.company:
        update["company"] = d.company
    if d.location and not job.location:
        update["location"] = d.location
    if d.posted_at:
        update["posted_at"] = d.posted_at
    raw = dict(job.raw)
    raw["details_fetched"] = True
    if d.employment_type:
        raw["employment_type"] = d.employment_type
    if d.experience:
        raw["experience"] = d.experience
        raw["workex_min"], raw["workex_max"] = d.workex_min, d.workex_max
    if d.posted_by:
        raw["posted_by"] = d.posted_by
    if d.description or d.location:
        update["remote"] = job.remote or looks_remote(job.location, d.location, job.title)
    return job.model_copy(update={**update, "raw": raw})


def is_fresh(job: NormalizedJob, hours_old: int, now: datetime | None = None) -> bool:
    """Posting dates are day-granular, so the cutoff is a date: a job dated on or after ``(now - hours_old)``'s
    day is fresh. A job without a known date is kept (unknown is not old)."""
    if job.posted_at is None:
        return True
    cutoff = ((now or datetime.now(UTC)) - timedelta(hours=max(1, int(hours_old)))).date()
    return job.posted_at.date() >= cutoff


def matches_location(job: NormalizedJob, location: str) -> bool:
    """Client-side location filter (the API ignores ``location``). Empty filter matches everything; a remote
    posting ("Work From Home") matches any location."""
    want = (location or "").strip().lower()
    if not want:
        return True
    have = (job.location or "").lower()
    return want in have or job.remote


# ------------------------------------------------------------------ collector


class InstahyreCollector(BaseCollector):
    source = "instahyre"
    ats_type = "instahyre"

    def __init__(
        self,
        queries: list[str],
        location: str = "",
        hours_old: int = 24,
        max_results: int = 50,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_id: int | None = None,
        *,
        job_type: str | int = "full_time",
        years: int | None = None,
        with_details: bool = True,
        max_details: int = 20,
        delay: float = 1.5,
        shuffle_queries: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_if: Callable[[], bool] | None = None,
        on_rate_limited: Callable[[float], None] | None = None,
        now: datetime | None = None,
    ):
        """Public search; `user_id` is accepted for interface uniformity and unused (no login is needed).

        `years` is the API's maximum-experience filter; `job_type` is both / full_time / internship (or 0/1/2).
        `skip_if()` true skips the whole run; `on_rate_limited(hours)` is called once when Instahyre blocks it.
        """
        super().__init__(client)
        self.queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        if shuffle_queries:
            random.shuffle(self.queries)
        self.location = location
        self.hours_old = max(1, int(hours_old))
        self.max_results = max(1, int(max_results))
        self.max_requests = max(1, int(max_requests))
        self.user_id = user_id
        self.job_type = JOB_TYPES.get(str(job_type), job_type) if not isinstance(job_type, int) else job_type
        if self.job_type not in JOB_TYPES.values():
            raise ValueError(f"job_type must be one of {sorted(JOB_TYPES)} or 0/1/2, got {job_type!r}")
        self.years = int(years) if years is not None else None
        self.with_details, self.max_details = with_details, max(0, int(max_details))
        self.delay, self._sleep = delay, sleep
        self.skip_if, self.on_rate_limited = skip_if, on_rate_limited
        self._now = now
        self.rate_limited = False
        self.budget_exhausted = False
        self.search_budget_reached = False
        self._in_search = True
        self.requests_made = 0

    @property
    def _search_budget(self) -> int:
        """Keep a slice of the budget for opportunity pages so a wide query set cannot leave every job
        without a description or a posting date."""
        if self.with_details and self.max_details > 0 and self.max_requests > self.max_details:
            reserve = min(self.max_details, self.max_requests // 3)
            return max(1, self.max_requests - reserve)
        return self.max_requests

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().http_timeout, headers=API_HEADERS,
                                        follow_redirects=True)
        return self._client

    @property
    def key(self) -> str:
        return "instahyre:public"

    # -- requests

    def search_params(self, query: str, offset: int = 0) -> list[tuple[str, Any]]:
        params: list[tuple[str, Any]] = [("skills", query), ("company_size", 0), ("job_type", self.job_type),
                                         ("isLandingPage", "true"), ("search", "true"),
                                         ("limit", PAGE_SIZE), ("offset", offset)]
        if self.years is not None:
            params.append(("years", self.years))
        return params

    def _pause(self) -> None:
        if self.requests_made and self.delay > 0:
            self._sleep(self.delay * random.uniform(0.7, 1.3))

    def _block(self, reason: str, url: str, retry_after: str | None = None) -> None:
        self.rate_limited = True
        hours = DEFAULT_COOLDOWN_HOURS
        try:
            if retry_after:
                hours = max(hours, float(retry_after) / 3600)
        except ValueError:
            pass
        self.log.warning("blocked by instahyre, stopping run", reason=reason, url=url, requests=self.requests_made,
                         cooldown_hours=round(hours, 2))
        if self.on_rate_limited is not None:
            try:
                self.on_rate_limited(hours)
            except Exception as exc:  # noqa: BLE001 - persisting the cooldown must not break the run
                self.log.warning("could not record instahyre cooldown", error=str(exc)[:200])

    def _get(self, url: str, **kwargs: Any) -> httpx.Response | None:
        """GET with politeness delay and budget. None when blocked, exhausted, failed or a 4xx/5xx."""
        if self.rate_limited:
            return None
        if self._in_search and self._search_budget < self.max_requests and self.requests_made >= self._search_budget:
            if not self.search_budget_reached:
                self.search_budget_reached = True
                self.log.info("instahyre search budget reached, reserving the rest for opportunity pages",
                              search_budget=self._search_budget, max_requests=self.max_requests)
            return None
        if self.requests_made >= self.max_requests:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.log.info("instahyre request budget reached", max_requests=self.max_requests)
            return None
        self._pause()
        self.requests_made += 1
        try:
            resp = self.client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            self.log.warning("request failed", url=url, error=str(exc))
            return None
        if resp.status_code in RATE_LIMIT_STATUSES:
            self._block(f"http {resp.status_code}", url, resp.headers.get("retry-after"))
            return None
        final = str(resp.url)
        if is_blocked_url(final) or any(is_blocked_url(str(h.url)) for h in resp.history):
            self._block("login / challenge redirect", final)
            return None
        if resp.status_code >= 400:
            self.log.warning("http error", status=resp.status_code, url=url, body=resp.text[:160])
            return None
        return resp

    def fetch_page(self, query: str, offset: int = 0) -> tuple[list[NormalizedJob], int | None]:
        """One search page: (jobs, total_count)."""
        resp = self._get(SEARCH_URL, params=self.search_params(query, offset), headers=API_HEADERS)
        if resp is None:
            return [], None
        try:
            data = resp.json()
        except ValueError:
            body = resp.text
            if _CF_BODY.search(body):
                self._block("challenge page instead of json", str(resp.url))
            else:
                self.log.warning("search response is not json", url=str(resp.url))
            return [], None
        meta = data.get("meta") if isinstance(data, dict) else None
        total = meta.get("total_count") if isinstance(meta, dict) else None
        return parse_search_json(data), (int(total) if isinstance(total, int) else None)

    def fetch_details(self, job: NormalizedJob) -> JobDetails | None:
        resp = self._get(job.url or JOB_URL.format(job_id=job.source_id), headers=PAGE_HEADERS)
        if resp is None:
            return None
        d = parse_job_page(resp.text, job.source_id)
        if d.blocked:
            self._block("challenge page instead of the opportunity page", str(resp.url))
            return None
        return d

    # -- pipeline

    def search(self, query: str) -> list[NormalizedJob]:
        found: dict[str, NormalizedJob] = {}
        offset = 0
        while (len(found) < self.max_results and not self.rate_limited and not self.budget_exhausted
               and not self.search_budget_reached):
            page, total = self.fetch_page(query, offset)
            if not page:
                break
            new = [j for j in page if j.source_id not in found]
            for job in new:
                found[job.source_id] = job
            offset += len(page)
            if not new or len(page) < PAGE_SIZE or (total is not None and offset >= total):
                break
        self.log.info("searched", query=query, location=self.location, hours_old=self.hours_old,
                      found=len(found), rate_limited=self.rate_limited)
        return list(found.values())[: self.max_results]

    def enrich(self, jobs: list[NormalizedJob]) -> list[NormalizedJob]:
        self._in_search = False  # the reserved remainder of the budget is now available for opportunity pages
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
        """Normalized jobs (``source`` / ``ats_type`` = instahyre, ``apply_url`` = the opportunity page).
        ``[j.model_dump() for j in jobs]`` gives plain dicts."""
        if self.skip_if is not None and self.skip_if():
            self.log.info("instahyre search skipped: cooldown active")
            return []
        seen: dict[str, NormalizedJob] = {}
        for query in self.queries:
            if self.rate_limited or self.budget_exhausted or self.search_budget_reached:
                break
            for job in self.search(query):
                seen.setdefault(job.source_id, job)
        jobs = [j for j in seen.values() if matches_location(j, self.location)]
        if len(jobs) < len(seen):
            self.log.info("location filter applied client-side", location=self.location,
                          kept=len(jobs), dropped=len(seen) - len(jobs))
        if self.with_details and jobs:
            jobs = self.enrich(jobs)
        fresh = [j for j in jobs if is_fresh(j, self.hours_old, self._now)]
        if len(fresh) < len(jobs):
            self.log.info("dropped stale postings", hours_old=self.hours_old, dropped=len(jobs) - len(fresh))
        return fresh


def search_once(
    queries: list[str] | str,
    location: str = "",
    hours_old: int = 24,
    *,
    max_results: int = 50,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    job_type: str | int = "full_time",
    years: int | None = None,
    with_details: bool = True,
    max_details: int = 20,
    delay: float = 1.5,
    client: httpx.Client | None = None,
) -> list[NormalizedJob]:
    """One-shot public search for a CLI. No database side effects."""
    collector = InstahyreCollector(
        [queries] if isinstance(queries, str) else list(queries), location, hours_old,
        max_results=max_results, max_requests=max_requests, job_type=job_type, years=years,
        with_details=with_details, max_details=max_details, delay=delay, client=client,
    )
    return collector.collect()


__all__ = ["InstahyreCollector", "JobDetails", "search_once", "parse_search_json", "parse_card", "parse_job_page",
           "parse_job_json", "apply_details", "is_fresh", "matches_location", "parse_experience", "job_id_from",
           "is_blocked_url", "SEARCH_URL", "PUBLIC_JOB_API_URL", "JOB_URL", "PAGE_SIZE", "JOB_TYPES",
           "RATE_LIMIT_STATUSES", "API_HEADERS", "PAGE_HEADERS"]
