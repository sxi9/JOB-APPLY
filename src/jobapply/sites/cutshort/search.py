"""Cutshort public job search (no login).

Cutshort is a Next.js app whose listing pages (``/jobs/<hub-slug>``) hydrate from a public JSON endpoint,
the same one the browser calls:

- ``GET /backend-api/webpage/jobs/<hub-slug>?page=N`` returns ``{"success": true, "data": {"pageData":
  {"jobs": [...50 per page...], "liveJobCount": N}}}``, newest first. A hub slug is one of ~21k category
  pages (``python-jobs``, ``remote-python-jobs``, ``python-jobs-in-pune``); an unknown slug answers HTTP 400
  with ``errorCode = jobs_page_config_not_found``.
- ``GET /backend-api/webpage/job/<job-slug>`` returns one posting (same record shape plus ``jobValid``).
- ``GET /backend-api/webpage/jobs?page=N`` lists the hub categories (15 per page).

No cookies or tokens are needed; responses are CDN cached for 4 h (``cache-control: public, max-age=14400``),
so a run never sees postings younger than the cache. Each job record already carries the full description
(``sanitizedComment``), skills, experience range, salary band and the public job URL, so no per-job page is
fetched. Recorded 2026-10-29 with a plain httpx client; shapes are pinned by ``tests/test_cutshort_search.py``.

The site's own "Apply" is a logged-in one-click action, so ``apply_url`` is the public job page and
``ats_type`` is ``cutshort``: the apply path in ``sites.cutshort.apply`` runs inside the user's persistent
profile.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt
from jobapply.config import get_settings
from jobapply.sites.browser import DESKTOP_USER_AGENT

SITE = "cutshort"
BASE_URL = "https://cutshort.io"
HUB_URL = BASE_URL + "/backend-api/webpage/jobs/{slug}"
INDEX_URL = BASE_URL + "/backend-api/webpage/jobs"
JOB_API_URL = BASE_URL + "/backend-api/webpage/job/{slug}"
JOB_PAGE_URL = BASE_URL + "/job/{slug}"
HUB_PAGE_URL = BASE_URL + "/jobs/{slug}"
# The hub endpoint serves 50 records per page; `page` is 1-based.
PAGE_SIZE = 50
# Cloudflare / app answers that mean "go away": the run stops and keeps what it has.
RATE_LIMIT_STATUSES = (429, 403, 503)
UNKNOWN_SLUG_CODE = "jobs_page_config_not_found"
DEFAULT_MAX_REQUESTS = 40
DEFAULT_COOLDOWN_HOURS = 2.0
# How many hub slugs one query may try before giving up on it (each miss is one cheap 400).
MAX_SLUG_TRIES = 4

BROWSER_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/jobs",
}

# City hub suffixes seen in sitemap_listings.xml (2026-10-29): `<topic>-jobs-in-<city>`.
CITY_SLUGS: dict[str, str] = {
    "ahmedabad": "ahmedabad",
    "bangalore": "bangalore-bengaluru",
    "bengaluru": "bangalore-bengaluru",
    "bangalore-bengaluru": "bangalore-bengaluru",
    "bhubaneswar": "bhubaneswar",
    "chandigarh": "chandigarh",
    "chennai": "chennai",
    "coimbatore": "coimbatore",
    "delhi": "delhi-ncr-gurgaon-noida",
    "new-delhi": "delhi-ncr-gurgaon-noida",
    "delhi-ncr": "delhi-ncr-gurgaon-noida",
    "ncr": "delhi-ncr-gurgaon-noida",
    "gurgaon": "delhi-ncr-gurgaon-noida",
    "gurugram": "delhi-ncr-gurgaon-noida",
    "noida": "delhi-ncr-gurgaon-noida",
    "delhi-ncr-gurgaon-noida": "delhi-ncr-gurgaon-noida",
    "hyderabad": "hyderabad",
    "indore": "indore",
    "jaipur": "jaipur",
    "kochi": "kochi-cochin",
    "cochin": "kochi-cochin",
    "kochi-cochin": "kochi-cochin",
    "kolkata": "kolkata",
    "mumbai": "mumbai",
    "pune": "pune",
}
# Words that never form part of a hub topic; dropping them from the tail turns "python developer" into the
# `python-jobs` hub when `python-developer-jobs` does not exist (it does for android, not for python).
_GENERIC_TAIL = {"developer", "developers", "engineer", "engineers", "engineering", "programmer", "specialist",
                 "expert", "consultant", "analyst", "manager", "lead", "architect", "intern", "internship"}
_SENIORITY = {"senior", "junior", "sr", "jr", "lead", "principal", "staff", "mid", "entry", "level", "fresher"}
_NOISE = {"jobs", "job", "role", "roles", "opening", "openings", "position", "positions", "vacancy", "remote",
          "in", "at", "for", "and", "&"}
_SLUG_RE = re.compile(r"^(remote-)?[a-z0-9]+(-[a-z0-9]+)*-jobs(-in-[a-z0-9-]+)?$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _NON_SLUG.sub("-", (text or "").lower().replace("+", "plus").replace("#", "sharp")).strip("-")


def city_slug(location: str) -> str:
    """`"Bengaluru, India"` -> `bangalore-bengaluru`; '' for India-wide, remote or an unknown city."""
    first = (location or "").split(",")[0].strip()
    key = slugify(first)
    if not key or key in ("india", "remote", "anywhere", "wfh"):
        return ""
    return CITY_SLUGS.get(key, "")


def is_hub_slug(value: str) -> bool:
    return bool(_SLUG_RE.match((value or "").strip().lower()))


def hub_candidates(query: str, location: str = "", remote_only: bool = False) -> list[str]:
    """Hub slugs to try for a search query, most specific first.

    A value that already is a hub slug (``python-jobs-in-pune``) is used as is. Otherwise the query is
    slugified, noise words dropped, and the generic tail / leading seniority peeled off step by step; each
    topic is tried with the city suffix first, then India-wide. At most MAX_SLUG_TRIES candidates.
    """
    q = (query or "").strip().lower()
    if is_hub_slug(q):
        return [q]
    words = [w for w in slugify(q).split("-") if w and w not in _NOISE]
    remote = remote_only or "remote" in slugify(q).split("-")
    topics: list[str] = []
    while words:
        topic = "-".join(words)
        if topic not in topics:
            topics.append(topic)
        if len(words) > 1 and words[0] in _SENIORITY:
            words = words[1:]
        elif len(words) > 1 and words[-1] in _GENERIC_TAIL:
            words = words[:-1]
        else:
            break
    if not topics:
        return []
    city = city_slug(location)
    prefix = "remote-" if remote else ""
    out: list[str] = []
    for topic in topics:
        if city:
            out.append(f"{prefix}{topic}-jobs-in-{city}")
    for topic in topics:
        out.append(f"{prefix}{topic}-jobs")
    return list(dict.fromkeys(out))[:MAX_SLUG_TRIES]


def job_slug_from_url(url: str) -> str:
    """``https://cutshort.io/job/Senior-Dev-Acme-koBBDTQ1`` -> ``Senior-Dev-Acme-koBBDTQ1``."""
    path = urlsplit(url or "").path.rstrip("/")
    return path.rsplit("/", 1)[-1] if "/job/" in path else ""


# ------------------------------------------------------------------ parsing (pure)


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or value.get("city") or "").strip()
    if isinstance(value, list):
        return ", ".join(t for t in (_text_of(v) for v in value) if t)
    return ""


def _salary(item: dict[str, Any]) -> tuple[float | None, float | None, str | None, str]:
    rng = item.get("salaryRange")
    text = _text_of(item.get("salaryRangeText")) or (rng if isinstance(rng, str) else "")
    if not isinstance(rng, dict) or rng.get("hideSalary"):
        return None, None, None, text
    lo = rng.get("userMin") or rng.get("min")
    hi = rng.get("userMax") or rng.get("max")
    try:
        lo_f = float(lo) if lo not in (None, "", 0) else None
        hi_f = float(hi) if hi not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None, None, None, text
    if lo_f is None and hi_f is None:
        return None, None, None, text
    return lo_f, hi_f, str(rng.get("currency") or "INR"), text


def parse_job(item: dict[str, Any], hub_slug: str = "") -> NormalizedJob | None:
    """One record of ``pageData.jobs`` (or ``pageData`` of the job endpoint) -> NormalizedJob.

    Records without an id or headline are skipped; ``jobValid: false`` (closed posting) too.
    """
    if not isinstance(item, dict):
        return None
    job_id = str(item.get("_id") or "").strip()
    title = _text_of(item.get("headline"))
    if not job_id or not title or item.get("jobValid") is False:
        return None
    company_details = item.get("companyDetails") if isinstance(item.get("companyDetails"), dict) else {}
    company_ref = item.get("companyId") if isinstance(item.get("companyId"), dict) else {}
    company = _text_of(company_details.get("name")) or _text_of(company_ref.get("name"))
    facts = item.get("jobFactSummary") if isinstance(item.get("jobFactSummary"), dict) else {}
    location = (_text_of(item.get("locations")) or _text_of(item.get("locationsText"))
                or _text_of(facts.get("locations")))
    remote_type = str(item.get("remoteType") or "").lower()
    remote = remote_type.startswith("remote") or looks_remote(location, title, _text_of(facts.get("remotePolicy")))
    url = _text_of(item.get("publicUrl"))
    if not url:
        url = JOB_PAGE_URL.format(slug=job_id)
    posted_at = parse_dt(facts.get("postedDate") or item.get("hiringIntentShownOn") or item.get("createdAt"))
    skills = [s for s in (_text_of(v) for v in (item.get("allSkills") or [])) if s]
    description = html_to_text(item.get("sanitizedComment") or item.get("comment") or "")
    if skills:
        description = (description + "\n\nSkills: " + ", ".join(skills)).strip()
    exp = item.get("expRange") if isinstance(item.get("expRange"), dict) else {}
    salary_min, salary_max, currency, salary_text = _salary(item)
    raw: dict[str, Any] = {
        "job_id": job_id,
        "slug": job_slug_from_url(url),
        "hub_slug": hub_slug,
        "auth_apply_url": _text_of(item.get("authApplyUrl")) or f"{BASE_URL}/profile/all-jobs?jobid={job_id}",
        "skills": skills,
        "exp_min": exp.get("min"),
        "exp_max": exp.get("max"),
        "remote_type": remote_type,
        "role_types": [str(r) for r in (item.get("roleTypes") or [])],
        "company_type": _text_of(company_details.get("type")),
        "company_size": _text_of(company_details.get("size")),
        "company_stage": _text_of(company_details.get("stage")),
        "posted_label": _text_of(facts.get("postedDateLabel")),
        "one_click_apply": True,
    }
    if salary_text:
        raw["salary_text"] = salary_text
    return NormalizedJob(
        title=title, company=company, location=location, remote=bool(remote), posted_at=posted_at,
        url=url, apply_url=url, description=description, ats_type=SITE,
        salary_min=salary_min, salary_max=salary_max, currency=currency,
        source=SITE, source_id=job_id, raw=raw,
    )


def parse_hub_payload(data: Any, hub_slug: str = "") -> tuple[list[NormalizedJob], int | None]:
    """``(jobs, live_job_count)`` from a hub endpoint body. A malformed body gives ``([], None)``."""
    if not isinstance(data, dict) or not data.get("success"):
        return [], None
    page_data = (data.get("data") or {}).get("pageData") if isinstance(data.get("data"), dict) else None
    if not isinstance(page_data, dict):
        return [], None
    jobs: list[NormalizedJob] = []
    for item in page_data.get("jobs") or []:
        job = parse_job(item, hub_slug)
        if job:
            jobs.append(job)
    count = page_data.get("liveJobCount")
    return jobs, (int(count) if isinstance(count, int | float) else None)


def parse_job_payload(data: Any) -> NormalizedJob | None:
    """The job endpoint's body -> NormalizedJob (None for an invalid / closed posting)."""
    if not isinstance(data, dict) or not data.get("success"):
        return None
    page_data = (data.get("data") or {}).get("pageData") if isinstance(data.get("data"), dict) else None
    return parse_job(page_data) if isinstance(page_data, dict) else None


def error_code(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return str(data["error"].get("errorCode") or "")
    return ""


# ------------------------------------------------------------------ collector


class CutshortCollector(BaseCollector):
    """Logged-out collector over the public hub endpoint. ``name``/``source`` are both ``cutshort``.

    `skip_if()` true skips the whole run (a cooldown from an earlier block is active); `on_rate_limited(hours)`
    is called once when Cutshort blocks the run. `user_id` is accepted for interface parity with the assisted
    collectors but unused: nothing here needs a login.
    """

    name: ClassVar[str] = SITE
    source: ClassVar[str] = SITE
    ats_type: ClassVar[str | None] = SITE

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
        hub_slugs: list[str] | None = None,
        delay: float = 1.5,
        shuffle_queries: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_if: Callable[[], bool] | None = None,
        on_rate_limited: Callable[[float], None] | None = None,
        now: datetime | None = None,
        **kw: Any,
    ):
        super().__init__(client)
        self.queries = [q for q in dict.fromkeys(q.strip() for q in queries or []) if q]
        self.queries.extend(s for s in (hub_slugs or []) if is_hub_slug(s) and s not in self.queries)
        if shuffle_queries:
            random.shuffle(self.queries)
        self.location = location or ""
        self.hours_old = max(1, int(hours_old))
        self.max_results = max(1, int(max_results))
        self.max_requests = max(1, int(max_requests))
        self.user_id = user_id
        self.remote_only = bool(remote_only)
        self.delay, self._sleep = delay, sleep
        self.skip_if, self.on_rate_limited = skip_if, on_rate_limited
        self._now = now
        self.rate_limited = False
        self.budget_exhausted = False
        self.requests_made = 0
        self.resolved_hubs: dict[str, str] = {}  # query -> hub slug that answered
        if kw:
            self.log.debug("ignored collector options", keys=sorted(kw))

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().http_timeout, headers=BROWSER_HEADERS,
                                        follow_redirects=True)
        return self._client

    @property
    def key(self) -> str:
        return f"{SITE}:public"

    @property
    def cutoff(self) -> datetime:
        return (self._now or datetime.now(UTC)) - timedelta(hours=self.hours_old)

    # -- requests

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
        self.log.warning("blocked by cutshort, stopping run", reason=reason, url=url, requests=self.requests_made,
                         cooldown_hours=round(hours, 2))
        if self.on_rate_limited is not None:
            try:
                self.on_rate_limited(hours)
            except Exception as exc:  # noqa: BLE001 - persisting the cooldown must not break the run
                self.log.warning("could not record cutshort cooldown", error=str(exc)[:200])

    def _get_json(self, url: str, **kwargs: Any) -> tuple[int, Any]:
        """``(status, body)`` with politeness delay and budget. ``(0, None)`` when blocked, exhausted or failed;
        a 4xx stops the run except the cheap "unknown slug" 400, which is handed back to the caller."""
        if self.rate_limited:
            return 0, None
        if self.requests_made >= self.max_requests:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.log.info("cutshort request budget reached", max_requests=self.max_requests)
            return 0, None
        self._pause()
        self.requests_made += 1
        try:
            resp = self.client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            self.log.warning("request failed", url=url, error=str(exc))
            return 0, None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code in RATE_LIMIT_STATUSES:
            self._block(f"http {resp.status_code}", url, resp.headers.get("retry-after"))
            return resp.status_code, None
        if resp.status_code == 400 and error_code(body) == UNKNOWN_SLUG_CODE:
            return 400, body
        if resp.status_code >= 400:
            self.log.warning("http error, stopping run", status=resp.status_code, url=url)
            self.rate_limited = True  # a 401 / 404 / 5xx on a public endpoint: do not keep hammering it
            return resp.status_code, None
        if body is None:
            self._block("non-json response", url)
            return resp.status_code, None
        return resp.status_code, body

    def fetch_hub_page(self, slug: str, page: int = 1) -> tuple[list[NormalizedJob], int | None, bool]:
        """``(jobs, live_job_count, slug_exists)`` for one hub page."""
        params = {"page": page} if page > 1 else None
        status, body = self._get_json(HUB_URL.format(slug=slug), params=params)
        if status == 400:
            return [], None, False
        if not body:
            return [], None, True
        jobs, count = parse_hub_payload(body, slug)
        return jobs, count, True

    def fetch_job(self, slug: str) -> NormalizedJob | None:
        status, body = self._get_json(JOB_API_URL.format(slug=slug))
        return parse_job_payload(body) if body else None

    # -- pipeline

    def resolve_hub(self, query: str) -> tuple[str, list[NormalizedJob], int | None]:
        """First hub slug for `query` that exists, with its first page. ``("", [], None)`` when none does."""
        for slug in hub_candidates(query, self.location, self.remote_only):
            if self.rate_limited or self.budget_exhausted:
                break
            jobs, count, exists = self.fetch_hub_page(slug, 1)
            if exists:
                self.resolved_hubs[query] = slug
                self.log.info("cutshort hub resolved", query=query, hub=slug, live_jobs=count)
                return slug, jobs, count
            self.log.debug("cutshort hub missing", query=query, hub=slug)
        self.log.info("no cutshort hub for query", query=query, location=self.location)
        return "", [], None

    def _fresh(self, jobs: list[NormalizedJob]) -> list[NormalizedJob]:
        cutoff = self.cutoff
        return [j for j in jobs if j.posted_at is None or j.posted_at >= cutoff]

    def search(self, query: str) -> list[NormalizedJob]:
        slug, page_jobs, _count = self.resolve_hub(query)
        if not slug:
            return []
        found: dict[str, NormalizedJob] = {}
        page = 1
        while True:
            fresh = self._fresh(page_jobs)
            for job in fresh:
                found.setdefault(job.source_id, job)
            # Newest first: once a page holds anything older than the window, the rest is older still.
            older = len(fresh) < len(page_jobs)
            if (older or not page_jobs or len(page_jobs) < PAGE_SIZE or len(found) >= self.max_results
                    or self.rate_limited or self.budget_exhausted):
                break
            page += 1
            page_jobs, _count, _exists = self.fetch_hub_page(slug, page)
        self.log.info("searched", query=query, hub=slug, hours_old=self.hours_old, pages=page, found=len(found),
                      rate_limited=self.rate_limited)
        return list(found.values())[: self.max_results]

    def collect(self) -> list[NormalizedJob]:
        if self.skip_if is not None and self.skip_if():
            self.log.info("cutshort search skipped: cooldown active")
            return []
        seen: dict[str, NormalizedJob] = {}
        for query in self.queries:
            if self.rate_limited or self.budget_exhausted:
                break
            for job in self.search(query):
                seen.setdefault(job.source_id, job)
        return list(seen.values())


def search_once(
    queries: list[str] | str,
    location: str = "",
    hours_old: int = 24,
    *,
    remote_only: bool = False,
    max_results: int = 50,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    delay: float = 1.5,
    client: httpx.Client | None = None,
) -> list[NormalizedJob]:
    """One-shot public search for a CLI. No database side effects."""
    collector = CutshortCollector(
        [queries] if isinstance(queries, str) else list(queries), location, hours_old,
        max_results=max_results, max_requests=max_requests, remote_only=remote_only, delay=delay, client=client,
    )
    return collector.collect()


# ------------------------------------------------------------------ cross-run cooldown (app_settings)

PUBLIC_COOLDOWN_KEY = "cutshort_public_cooldown_until"


def public_cooldown_until(session: Any) -> datetime | None:
    from jobapply.switches import get_value

    raw = get_value(session, PUBLIC_COOLDOWN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def public_cooldown_active(session: Any, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    until = public_cooldown_until(session)
    return bool(until and until > now)


def set_public_cooldown(session: Any, hours: float = DEFAULT_COOLDOWN_HOURS, now: datetime | None = None) -> datetime:
    """Record a cooldown, doubling the window when the previous one is still active (repeat blocks)."""
    from jobapply.switches import set_value

    now = now or datetime.now(UTC)
    previous = public_cooldown_until(session)
    if previous and previous > now:
        remaining = (previous - now).total_seconds() / 3600
        hours = max(hours, remaining * 2)
    until = now + timedelta(hours=min(hours, 48))
    set_value(session, PUBLIC_COOLDOWN_KEY, until.isoformat())
    return until


__all__ = [
    "BASE_URL", "CITY_SLUGS", "CutshortCollector", "HUB_URL", "INDEX_URL", "JOB_API_URL", "JOB_PAGE_URL",
    "PAGE_SIZE", "PUBLIC_COOLDOWN_KEY", "RATE_LIMIT_STATUSES", "SITE", "UNKNOWN_SLUG_CODE", "city_slug",
    "hub_candidates", "is_hub_slug", "job_slug_from_url", "parse_hub_payload", "parse_job", "parse_job_payload",
    "public_cooldown_active", "public_cooldown_until", "search_once", "set_public_cooldown", "slugify",
]
