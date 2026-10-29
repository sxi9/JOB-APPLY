"""Naukri job search (no login).

Two ways in, same normalized output:

1. **JSON search API** the logged-out search page itself calls:
   ``GET https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword&searchType=adv
   &keyword=...&location=...&pageNo=N&jobAge=<days>&seoKey=<slug>-jobs&src=jobsearchDesk`` with the
   required ``appid: 109`` / ``systemid: Naukri`` headers and a desktop browser UA. The answer is
   ``{"noOfJobs": N, "jobDetails": [ {...}, ... ]}``; one entry per job with ``title``, ``companyName``,
   ``jdURL``, ``jobId``, ``createdDate`` (epoch ms), ``footerPlaceholderLabel`` ("Just Now", "3 Days Ago"),
   ``tagsAndSkills`` (comma list), ``jobDescription`` (HTML) and ``placeholders`` (experience / salary /
   location labels). Live 2026-10-29 from this IP the very first plain-HTTP call answered **HTTP 406
   ``{"message": "recaptcha required"}``** (no Retry-After), so the API is treated as *sometimes*
   available: 406 / 403 / 429 or a non-JSON body stop the API phase at once (no retries).
2. **Headless browser fallback** through the user's persistent profile (``sites.browser.launch_persistent``,
   always ``headless=True`` on this machine): opens the public search page
   ``https://www.naukri.com/<slug>-jobs?k=...&l=...&jobAge=<days>``, captures the ``/jobapi/v3/search``
   response the page makes with its own session (same JSON parser), or, failing that, parses the rendered
   ``div.srp-jobtuple-wrapper`` cards. Search needs no login; a CAPTCHA page stops the run.

Both paths are budgeted (``max_requests``) and politely delayed; a block records a cooldown through the
``on_rate_limited`` hook (wired by the collector registry, like LinkedIn's guest collector).
"""

from __future__ import annotations

import html as html_lib
import json
import math
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt
from jobapply.config import get_settings
from jobapply.logs import get_logger
from jobapply.sites.browser import DESKTOP_USER_AGENT

log = get_logger(__name__)

SITE = "naukri"
BASE_URL = "https://www.naukri.com"
API_URL = f"{BASE_URL}/jobapi/v3/search"
PAGE_SIZE = 20
DEFAULT_MAX_REQUESTS = 40
DEFAULT_COOLDOWN_HOURS = 2.0
# 406 = {"message": "recaptcha required"}; 403 / 429 = plain blocks. None of these is retried.
BLOCK_STATUSES = (403, 406, 429)
RECAPTCHA_TEXT = "recaptcha required"

# The headers the public search page sends on its own XHR. appid / systemid are REQUIRED: without them the
# API answers an error page. Client hints match the persistent Playwright profile's browser family.
_CHROME_MAJOR = re.search(r"Chrome/(\d+)", DESKTOP_USER_AGENT)
_CHROME_V = _CHROME_MAJOR.group(1) if _CHROME_MAJOR else "131"
API_HEADERS: dict[str, str] = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "appid": "109",
    "systemid": "Naukri",
    "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
    "Referer": f"{BASE_URL}/jobs-in-india",
    "sec-ch-ua": f'"Chromium";v="{_CHROME_V}", "Google Chrome";v="{_CHROME_V}", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# ------------------------------------------------------------------ small parsers (pure)

_RELATIVE = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month)s?\s+ago", re.I)
_PLUS_DAYS = re.compile(r"(\d+)\s*\+\s*days?\s+ago", re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return _WS.sub(" ", html_lib.unescape(_TAGS.sub(" ", fragment))).strip()


def parse_relative_label(label: str | None, now: datetime | None = None) -> datetime | None:
    """Naukri's posting-age labels -> aware UTC datetime, or None when the label is not relative.

    ``Just now`` / ``Today`` -> now; ``Few hours ago`` -> now - 3 h; ``3 days ago`` / ``30+ days ago`` /
    ``2 weeks ago`` -> the obvious delta (``N+`` is treated as N: a floor, never younger than the truth).
    """
    text = _WS.sub(" ", (label or "")).strip().lower()
    if not text:
        return None
    now = now or datetime.now(UTC)
    if text in ("just now", "today", "posted today", "few seconds ago"):
        return now
    if "few hour" in text or "few hrs" in text:
        return now - timedelta(hours=3)
    if text in ("yesterday", "1 day ago"):
        return now - timedelta(days=1)
    m = _PLUS_DAYS.search(text) or _RELATIVE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower() if m.lastindex and m.lastindex >= 2 else "day"
    delta = {
        "minute": timedelta(minutes=n), "min": timedelta(minutes=n),
        "hour": timedelta(hours=n), "hr": timedelta(hours=n),
        "day": timedelta(days=n), "week": timedelta(weeks=n), "month": timedelta(days=30 * n),
    }[unit]
    return now - delta


_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*(lacs?|lakhs?|lpa|l\b)", re.I)
_RUPEES = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:-|to)\s*(\d[\d,]*(?:\.\d+)?)", re.I)
_UNDISCLOSED = re.compile(r"not disclosed|undisclosed|confidential|^\s*$", re.I)


def parse_inr_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """``5-10 Lacs PA`` -> (500000, 1000000, 'INR'); ``Not disclosed`` -> Nones; ``3,00,000-4,50,000 PA``
    -> (300000, 450000, 'INR')."""
    if not text or _UNDISCLOSED.search(text):
        return None, None, None
    m = _LAKH.search(text)
    if m:
        lo = float(m.group(1)) * 100_000
        hi = float(m.group(2)) * 100_000 if m.group(2) else lo
        return min(lo, hi), max(lo, hi), "INR"
    m = _RUPEES.search(text)
    if m:
        lo, hi = (float(v.replace(",", "")) for v in m.groups())
        return min(lo, hi), max(lo, hi), "INR"
    return None, None, None


def _placeholder(d: dict[str, Any], kind: str) -> str:
    for p in d.get("placeholders") or []:
        if isinstance(p, dict) and str(p.get("type", "")).lower() == kind:
            return _text(str(p.get("label") or ""))
    return ""


def job_url_from(jd_url: str | None, job_id: str | None = None) -> str:
    """``jdURL`` as served (relative ``job-listings-...`` or absolute) -> absolute ``https://www.naukri.com/...``."""
    raw = html_lib.unescape(str(jd_url or "")).strip()
    if raw.startswith("http"):
        return raw
    if raw:
        return f"{BASE_URL}/{raw.lstrip('/')}"
    return f"{BASE_URL}/job-listings-{job_id}" if job_id else ""


_JOB_ID_IN_URL = re.compile(r"-(\d{9,})(?:[?#].*)?$")


def job_id_from_url(url: str) -> str | None:
    m = _JOB_ID_IN_URL.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def normalize_api_job(d: dict[str, Any], now: datetime | None = None) -> NormalizedJob | None:
    """One ``jobDetails`` entry -> NormalizedJob (source / ats_type ``naukri``, apply_url = the job page)."""
    if not isinstance(d, dict):
        return None
    job_id = str(d.get("jobId") or "").strip()
    title = _text(str(d.get("title") or ""))
    url = job_url_from(d.get("jdURL"), job_id)
    if not job_id:
        job_id = job_id_from_url(url) or ""
    if not job_id or not title:
        return None
    company = _text(str(d.get("companyName") or ""))
    location = _placeholder(d, "location")
    experience = _placeholder(d, "experience") or _text(str(d.get("experienceText") or ""))
    salary = _placeholder(d, "salary") or _text(str(d.get("salaryText") or ""))
    skills_raw = d.get("tagsAndSkills") or ""
    skills = [s.strip() for s in str(skills_raw).split(",") if s.strip()] if isinstance(skills_raw, str) else [
        str(s).strip() for s in skills_raw if str(s).strip()]
    label = _text(str(d.get("footerPlaceholderLabel") or ""))
    posted_at = parse_dt(d.get("createdDate")) or parse_relative_label(label, now)
    description = html_to_text(str(d.get("jobDescription") or ""))
    if skills:
        description = (description + "\n\nSkills: " + ", ".join(skills)).strip()
    sal_min, sal_max, currency = parse_inr_salary(salary)
    if (sal_min or sal_max) and not currency:
        currency = str(d.get("currency") or "INR")
    raw: dict[str, Any] = {
        "skills": skills, "experience": experience, "salary": salary, "posted_label": label,
        "company_id": d.get("companyId"), "job_type": d.get("jobType"), "vacancy": d.get("vacancy"),
        "show_multiple_apply": bool(d.get("showMultipleApply")), "static_url": d.get("staticUrl"),
        "logo": d.get("logoPathV3") or d.get("logoPath"),
    }
    rating = (d.get("ambitionBoxData") or {}).get("AggregateRating") if isinstance(d.get("ambitionBoxData"),
                                                                                    dict) else None
    if rating:
        raw["rating"] = rating
    return NormalizedJob(
        title=title, company=company, location=location,
        remote=looks_remote(location, title, str(d.get("jobType") or "")),
        posted_at=posted_at, url=url, apply_url=url, description=description,
        ats_type=SITE, salary_min=sal_min, salary_max=sal_max, currency=currency,
        source=SITE, source_id=job_id, raw=raw,
    )


def parse_search_json(data: Any, now: datetime | None = None) -> list[NormalizedJob]:
    """The API body (dict or JSON text) -> jobs. A body without ``jobDetails`` yields []."""
    if isinstance(data, str | bytes):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    if not isinstance(data, dict):
        return []
    out: list[NormalizedJob] = []
    for entry in data.get("jobDetails") or []:
        job = normalize_api_job(entry, now)
        if job:
            out.append(job)
    return out


def is_recaptcha_body(body: str | bytes | None) -> bool:
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else (body or "")
    return RECAPTCHA_TEXT in text.lower()[:400]


# ------------------------------------------------------------------ rendered search page (fallback markup)

_CARD = re.compile(r'<div class="srp-jobtuple-wrapper[^"]*"[^>]*data-job-id="(\d+)"[^>]*>(.*?)'
                   r'(?=<div class="srp-jobtuple-wrapper|$)', re.S)
_CARD_TITLE = re.compile(r'<a[^>]*class="title[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_CARD_TITLE_ALT = re.compile(r'<a[^>]*href="([^"]+)"[^>]*class="title[^"]*"[^>]*>(.*?)</a>', re.S)
_CARD_COMPANY = re.compile(r'<a[^>]*class="comp-name[^"]*"[^>]*>(.*?)</a>', re.S)
_CARD_EXP = re.compile(r'<span[^>]*class="expwdth[^"]*"[^>]*>(.*?)</span>', re.S)
_CARD_SAL = re.compile(r'<span[^>]*class="sal[^"]*"[^>]*>\s*(?:<i[^>]*></i>)?\s*<span[^>]*>(.*?)</span>', re.S)
_CARD_LOC = re.compile(r'<span[^>]*class="locWdth[^"]*"[^>]*>(.*?)</span>', re.S)
_CARD_DESC = re.compile(r'<span[^>]*class="job-desc[^"]*"[^>]*>(.*?)</span>', re.S)
_CARD_TAG = re.compile(r'<li[^>]*class="[^"]*tag-li[^"]*"[^>]*>(.*?)</li>', re.S)
_CARD_POSTED = re.compile(r'<span[^>]*class="job-post-day[^"]*"[^>]*>(.*?)</span>', re.S)


def parse_search_html(page: str, now: datetime | None = None) -> list[NormalizedJob]:
    """Rendered search page (``div.srp-jobtuple-wrapper`` cards) -> jobs. Cards without id/title are skipped."""
    out: list[NormalizedJob] = []
    for job_id, card in _CARD.findall(page or ""):
        m = _CARD_TITLE.search(card) or _CARD_TITLE_ALT.search(card)
        if not m:
            continue
        href, title = html_lib.unescape(m.group(1)).strip(), _text(m.group(2))
        if not title:
            continue
        url = job_url_from(href, job_id)
        skills = [_text(t) for t in _CARD_TAG.findall(card) if _text(t)]
        posted = _text(m2.group(1)) if (m2 := _CARD_POSTED.search(card)) else ""
        salary = _text(m2.group(1)) if (m2 := _CARD_SAL.search(card)) else ""
        description = _text(m2.group(1)) if (m2 := _CARD_DESC.search(card)) else ""
        if skills:
            description = (description + "\n\nSkills: " + ", ".join(skills)).strip()
        sal_min, sal_max, currency = parse_inr_salary(salary)
        location = _text(m2.group(1)) if (m2 := _CARD_LOC.search(card)) else ""
        out.append(NormalizedJob(
            title=title, company=_text(m2.group(1)) if (m2 := _CARD_COMPANY.search(card)) else "",
            location=location, remote=looks_remote(location, title),
            posted_at=parse_relative_label(posted, now), url=url, apply_url=url, description=description,
            ats_type=SITE, salary_min=sal_min, salary_max=sal_max, currency=currency,
            source=SITE, source_id=job_id,
            raw={"skills": skills, "posted_label": posted, "salary": salary,
                 "experience": _text(m2.group(1)) if (m2 := _CARD_EXP.search(card)) else "",
                 "via": "browser"},
        ))
    return out


# ------------------------------------------------------------------ URLs


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())).strip("-")


def seo_key(query: str, location: str = "") -> str:
    """``python developer`` / ``Bengaluru`` -> ``python-developer-jobs-in-bengaluru`` (the SRP slug)."""
    key = f"{_slug(query) or 'jobs'}-jobs" if _slug(query) else "jobs"
    if _slug(location):
        key += f"-in-{_slug(location)}"
    return key


def hours_to_job_age(hours_old: int) -> int:
    """``jobAge`` is whole days: 1 h -> 1, 24 h -> 1, 25 h -> 2."""
    return max(1, math.ceil(max(1, int(hours_old)) / 24))


def search_page_url(query: str, location: str = "", hours_old: int = 24) -> str:
    url = f"{BASE_URL}/{seo_key(query, location)}?k={quote(query)}&jobAge={hours_to_job_age(hours_old)}"
    if location:
        url += f"&l={quote(location)}"
    return url


# ------------------------------------------------------------------ collector


class RateLimited(RuntimeError):
    """Naukri answered 406 (recaptcha required) / 403 / 429; the run stops and keeps what it collected."""


PageFactory = Callable[[], tuple[Any, Any]]  # () -> (context, page); context.close() is called at the end


class NaukriCollector(BaseCollector):
    """``NaukriCollector(queries, location, hours_old, max_results, max_requests, user_id).collect()``.

    Logged-out search through the JSON API first; when it is blocked (or ``api=False``) and a ``user_id`` is
    given, the public search page is scraped headless inside that user's persistent profile. With no user and
    a blocked API the run returns what it has and logs why.
    """

    source = SITE
    name = SITE
    ats_type = SITE

    def __init__(
        self,
        queries: list[str],
        location: str = "",
        hours_old: int = 24,
        max_results: int = 50,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_id: int | None = None,
        *,
        api: bool = True,
        browser_fallback: bool = True,
        delay: float = 1.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        page_factory: PageFactory | None = None,
        skip_if: Callable[[], bool] | None = None,
        on_rate_limited: Callable[[float], None] | None = None,
        now: datetime | None = None,
        **kw: Any,
    ):
        super().__init__(client)
        self.queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        self.location = (location or "").strip()
        self.hours_old = max(1, int(hours_old))
        self.max_results = max(1, int(max_results))
        self.max_requests = max(1, int(max_requests))
        self.user_id = int(user_id) if user_id is not None else None
        self.api, self.browser_fallback = api, browser_fallback
        self.delay, self._sleep = delay, sleep
        self.page_factory = page_factory
        self.skip_if, self.on_rate_limited = skip_if, on_rate_limited
        self._now = now
        self.rate_limited = False
        self.api_blocked = False
        self.budget_exhausted = False
        self.requests_made = 0
        self.block_reason = ""
        self._cooldown_hours = DEFAULT_COOLDOWN_HOURS
        self.extra = kw

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().http_timeout, headers=API_HEADERS,
                                        follow_redirects=True)
        return self._client

    @property
    def key(self) -> str:
        return "naukri:search"

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    # -- requests

    def api_params(self, query: str, page_no: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {
            "noOfResults": PAGE_SIZE,
            "urlType": "search_by_key_loc" if self.location else "search_by_keyword",
            "searchType": "adv",
            "keyword": query,
            "pageNo": page_no,
            "jobAge": hours_to_job_age(self.hours_old),
            "seoKey": seo_key(query, self.location),
            "src": "jobsearchDesk",
            "latLong": "",
        }
        if self.location:
            params["location"] = self.location
        return params

    def _pause(self) -> None:
        if self.requests_made and self.delay > 0:
            self._sleep(self.delay * random.uniform(0.7, 1.3))

    def _block(self, reason: str, url: str, retry_after: str | None = None) -> None:
        """Stop the current phase. The cooldown hook fires once, at the end of ``collect``, and only when the run
        really ended blocked (a browser fallback that got through the API's recaptcha wall needs no cooldown)."""
        self.rate_limited = True
        self.block_reason = reason
        try:
            if retry_after:
                self._cooldown_hours = max(self._cooldown_hours, float(retry_after) / 3600)
        except ValueError:
            pass
        self.log.warning("blocked by naukri, stopping", reason=reason, url=url, requests=self.requests_made,
                         cooldown_hours=round(self._cooldown_hours, 2))

    def _record_cooldown(self) -> None:
        if self.on_rate_limited is None:
            return
        try:
            self.on_rate_limited(self._cooldown_hours)
        except Exception as exc:  # noqa: BLE001 - persisting the cooldown must not break the run
            self.log.warning("could not record naukri cooldown", error=str(exc)[:200])

    def _spend(self) -> bool:
        """One unit of the request budget. False when the budget is gone (logged once)."""
        if self.requests_made >= self.max_requests:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.log.info("naukri request budget reached", max_requests=self.max_requests)
            return False
        self._pause()
        self.requests_made += 1
        return True

    def _get_json(self, query: str, page_no: int) -> dict[str, Any] | None:
        """GET one API page. None when blocked, budget gone, HTTP error or not JSON (all logged, never retried)."""
        if self.rate_limited or not self._spend():
            return None
        try:
            # Headers on every request (not only on the default client): appid / systemid are mandatory.
            resp = self.client.get(API_URL, params=self.api_params(query, page_no), headers=API_HEADERS)
        except httpx.HTTPError as exc:
            self.log.warning("naukri request failed", error=str(exc)[:200])
            return None
        body = resp.text
        if resp.status_code in BLOCK_STATUSES or is_recaptcha_body(body):
            what = "recaptcha required" if is_recaptcha_body(body) else f"http {resp.status_code}"
            self._block(what, str(resp.url), resp.headers.get("retry-after"))
            return None
        if resp.status_code >= 400:
            self.log.warning("naukri http error", status=resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            self._block("non-json body from the search api", str(resp.url))
            return None
        if not isinstance(data, dict):
            return None
        return data

    # -- API phase

    def search_api(self, query: str) -> list[NormalizedJob]:
        found: dict[str, NormalizedJob] = {}
        page_no, total = 1, None
        while len(found) < self.max_results and not self.rate_limited and not self.budget_exhausted:
            data = self._get_json(query, page_no)
            if data is None:
                break
            total = data.get("noOfJobs") if isinstance(data.get("noOfJobs"), int) else total
            jobs = parse_search_json(data, self.now)
            new = [j for j in jobs if j.source_id not in found]
            for job in new:
                found[job.source_id] = job
            if not new or len(jobs) < PAGE_SIZE or (total is not None and page_no * PAGE_SIZE >= total):
                break
            page_no += 1
        self.log.info("naukri api searched", query=query, location=self.location, hours_old=self.hours_old,
                      found=len(found), total=total, rate_limited=self.rate_limited)
        return list(found.values())[: self.max_results]

    # -- browser fallback

    def _open_profile(self) -> tuple[Any, Any, Any]:
        """(playwright, context, page) for the user's persistent profile, HEADLESS."""
        from playwright.sync_api import sync_playwright

        from jobapply.sites.browser import launch_persistent

        pw = sync_playwright().start()
        try:
            context = launch_persistent(pw, self.user_id, SITE, headless=True)
        except Exception:
            pw.stop()
            raise
        return pw, context, context.new_page()

    def search_browser(self, queries: list[str]) -> list[NormalizedJob]:
        """Scrape the public search page for each query inside the user's persistent profile."""
        if self.user_id is None and self.page_factory is None:
            self.log.warning("naukri api blocked and no user profile for the browser fallback; returning what "
                             "was collected", reason=self.block_reason or "api disabled")
            return []
        pw = context = page = None
        try:
            if self.page_factory is not None:
                context, page = self.page_factory()
            else:
                pw, context, page = self._open_profile()
        except Exception as exc:  # noqa: BLE001 - browser missing, profile locked
            self.log.warning("naukri browser fallback could not start", error=str(exc).splitlines()[0][:200])
            return []
        found: dict[str, NormalizedJob] = {}
        try:
            for query in queries:
                if self.rate_limited or self.budget_exhausted or len(found) >= self.max_results:
                    break
                for job in scrape_search_page(page, search_page_url(query, self.location, self.hours_old),
                                              now=self.now, spend=self._spend, on_challenge=self._browser_blocked):
                    found.setdefault(job.source_id, job)
                self.log.info("naukri browser searched", query=query, found=len(found),
                              rate_limited=self.rate_limited)
        finally:
            for closer in (getattr(context, "close", None), getattr(pw, "stop", None)):
                try:
                    if callable(closer):
                        closer()
                except Exception:  # noqa: BLE001
                    pass
        return list(found.values())[: self.max_results]

    def _browser_blocked(self, reason: str, url: str) -> None:
        self._block(reason, url)

    # -- pipeline

    def _within_window(self, job: NormalizedJob) -> bool:
        """Drop jobs clearly older than the window (jobAge is whole days; keep unknown dates)."""
        if job.posted_at is None:
            return True
        return job.posted_at >= self.now - timedelta(hours=self.hours_old + 12)

    def collect(self) -> list[NormalizedJob]:
        if self.skip_if is not None and self.skip_if():
            self.log.info("naukri search skipped: cooldown active")
            return []
        seen: dict[str, NormalizedJob] = {}
        pending = list(self.queries)
        if self.api:
            for query in list(pending):
                if self.rate_limited or self.budget_exhausted:
                    break
                for job in self.search_api(query):
                    seen.setdefault(job.source_id, job)
                if not self.rate_limited:
                    pending.remove(query)
        can_browse = self.browser_fallback and (self.user_id is not None or self.page_factory is not None)
        if pending and self.browser_fallback and not self.budget_exhausted:
            if self.rate_limited and can_browse:
                # The API block is per client; the browser path has its own session. Let it try once.
                self.api_blocked, self.rate_limited = True, False
                self.log.info("naukri api blocked, trying the headless browser fallback", reason=self.block_reason)
            for job in self.search_browser(pending):
                seen.setdefault(job.source_id, job)
        if self.rate_limited:
            self._record_cooldown()
        jobs = [j for j in seen.values() if self._within_window(j)]
        self.log.info("naukri collected", jobs=len(jobs), requests=self.requests_made, blocked=self.rate_limited,
                      api_blocked=self.api_blocked or self.rate_limited, reason=self.block_reason or None)
        return jobs[: self.max_results]


def scrape_search_page(page: Any, url: str, *, now: datetime | None = None,
                       spend: Callable[[], bool] | None = None,
                       on_challenge: Callable[[str, str], None] | None = None,
                       wait_ms: int = 15_000) -> list[NormalizedJob]:
    """Load one public search page in a Playwright page and return its jobs.

    Prefers the JSON the page itself fetches from ``/jobapi/v3/search`` (captured through ``page.on("response")``),
    else parses the rendered cards. A CAPTCHA / block page calls ``on_challenge`` and returns [].
    """
    from jobapply.sites.naukri.session import classify_page

    if spend is not None and not spend():
        return []
    captured: list[Any] = []

    def _on_response(resp: Any) -> None:
        try:
            if "/jobapi/v3/search" in str(resp.url) and resp.status == 200:
                captured.append(resp.json())
        except Exception:  # noqa: BLE001 - body gone, not json
            pass

    on = getattr(page, "on", None)
    if callable(on):
        page.on("response", _on_response)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        log.warning("naukri search page did not load", url=url, error=str(exc).splitlines()[0][:200])
        return []
    status = getattr(resp, "status", None)
    if status in BLOCK_STATUSES:
        if on_challenge:
            on_challenge(f"http {status}", url)
        return []
    waiter = getattr(page, "wait_for_selector", None)
    if callable(waiter):
        try:
            waiter("div.srp-jobtuple-wrapper, iframe[src*='captcha'], .g-recaptcha", timeout=wait_ms,
                   state="attached")
        except Exception:  # noqa: BLE001 - keep whatever rendered
            pass
    if classify_page(page) == "challenge":
        if on_challenge:
            on_challenge("captcha on the search page", url)
        return []
    for data in captured:
        jobs = parse_search_json(data, now)
        if jobs:
            return jobs
    try:
        html = page.content()
    except Exception:  # noqa: BLE001
        return []
    return parse_search_html(html, now)


def search_once(queries: list[str] | str, location: str = "", hours_old: int = 24, *, max_results: int = 50,
                max_requests: int = DEFAULT_MAX_REQUESTS, user_id: int | None = None, delay: float = 1.5,
                client: httpx.Client | None = None, **kw: Any) -> list[NormalizedJob]:
    """One-shot search for a CLI. No database side effects."""
    collector = NaukriCollector([queries] if isinstance(queries, str) else list(queries), location, hours_old,
                                max_results=max_results, max_requests=max_requests, user_id=user_id, delay=delay,
                                client=client, **kw)
    return collector.collect()


__all__ = [
    "NaukriCollector", "RateLimited", "search_once", "scrape_search_page", "parse_search_json", "parse_search_html",
    "normalize_api_job", "parse_relative_label", "parse_inr_salary", "job_url_from", "job_id_from_url", "seo_key",
    "search_page_url", "hours_to_job_age", "is_recaptcha_body", "API_URL", "API_HEADERS", "PAGE_SIZE",
    "BLOCK_STATUSES", "DEFAULT_MAX_REQUESTS", "DEFAULT_COOLDOWN_HOURS", "SITE",
]
