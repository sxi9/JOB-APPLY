"""LinkedIn guest job search (no login).

Uses the public endpoints the logged-out job search page itself calls:

- ``/jobs-guest/jobs/api/seeMoreJobPostings/search`` returns an HTML fragment of
  ``<li><div class="base-card" data-entity-urn="urn:li:jobPosting:<id>">`` cards,
  10 per page, paginated with ``start``.
- ``/jobs-guest/jobs/api/jobPosting/<id>`` returns the posting page (title, company,
  description, the Apply / Easy Apply CTA and, when the apply is offsite, sometimes
  the direct URL in ``<code id="applyUrl">`` wrapped in ``/jobs/view/externalApply/<id>?url=...``).

Both are rate limited for guests: HTTP 429, LinkedIn's own 999 or a redirect to the
authwall / login page means "go away", so a run stops there, returns what it already has
and (through the ``on_rate_limited`` hook, wired by the collector registry) records a
cooldown so the next scheduled run skips LinkedIn. Markup recorded 2026-10-29 lives in
``tests/fixtures/linkedin/``.
"""

from __future__ import annotations

import html as html_lib
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt
from jobapply.collectors.detector import detect_from_url
from jobapply.config import get_settings
from jobapply.sites.browser import DESKTOP_USER_AGENT

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}"
# The guest endpoint serves 10 cards per page (jobspy uses the same number); `start` advances by it.
PAGE_SIZE = 10
RATE_LIMIT_STATUSES = (429, 999)
# Final URLs (after redirects) that mean LinkedIn is pushing the guest away rather than serving jobs.
BLOCK_PATHS = ("/authwall", "/login", "/uas/", "/checkpoint", "/signup")
DEFAULT_MAX_REQUESTS = 60
DEFAULT_COOLDOWN_HOURS = 2.0

_CHROME_MAJOR = re.search(r"Chrome/(\d+)", DESKTOP_USER_AGENT)
_CHROME_V = _CHROME_MAJOR.group(1) if _CHROME_MAJOR else "131"
# Same UA as the persistent Playwright profile (sites.browser) so one IP shows one browser; the client
# hints and Sec-Fetch headers are what Chrome sends on a plain top-level navigation.
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

# ------------------------------------------------------------------ markup patterns

_CARD = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_URN_ID = re.compile(r"urn:li:jobPosting:(\d+)")
_URL_ID = re.compile(r"/jobs/view/(?:[^/?\"']*?-)?(\d{6,})")
_TITLE = re.compile(r'<h3 class="base-search-card__title[^"]*">(.*?)</h3>', re.S)
_COMPANY = re.compile(r'<h4 class="base-search-card__subtitle[^"]*">(.*?)</h4>', re.S)
_LOCATION = re.compile(r'<span class="job-search-card__location[^"]*">(.*?)</span>', re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', re.S)
_LINK = re.compile(r'<a class="base-card__full-link[^"]*"[^>]*href="([^"]+)"', re.S)
_BENEFIT = re.compile(r'<span class="job-posting-benefits__text[^"]*">(.*?)</span>', re.S)
_SALARY = re.compile(r'<span class="job-search-card__salary-info[^"]*">(.*?)</span>', re.S)

_P_TITLE = re.compile(r'<h2 class="[^"]*topcard__title[^"]*">(.*?)</h2>', re.S)
_P_COMPANY = re.compile(r'<a class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', re.S)
_P_LOCATION = re.compile(r'<span class="topcard__flavor topcard__flavor--bullet">(.*?)</span>', re.S)
_P_POSTED = re.compile(r'<span class="posted-time-ago__text[^"]*">(.*?)</span>', re.S)
_P_APPLICANTS = re.compile(r'<figcaption class="num-applicants__caption">(.*?)</figcaption>', re.S)
_P_DESCRIPTION = re.compile(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>\s*<button', re.S)
_P_DESCRIPTION_FALLBACK = re.compile(r'<div class="description__text[^"]*">(.*?)</section>', re.S)
_P_CRITERIA = re.compile(r'<h3 class="description__job-criteria-subheader">(.*?)</h3>\s*'
                         r'<span class="description__job-criteria-text[^"]*">(.*?)</span>', re.S)
_P_APPLY_URL_CODE = re.compile(r'<code id="applyUrl"[^>]*>\s*<!--\s*"?([^"\s]+?)"?\s*-->\s*</code>', re.S)
# The primary CTA of the top card: on the 2026 guest page a <button class="sign-up-modal__outlet
# top-card-layout__cta ... top-card-layout__cta--primary">; older markup used class="apply-button".
_P_PRIMARY_CTA = re.compile(
    r'<(?:button|a)[^>]*class="[^"]*(?:top-card-layout__cta--primary|apply-button)[^"]*"[^>]*>(.*?)</(?:button|a)>',
    re.S)
_P_APPLY_HREF = re.compile(
    r'<a[^>]*class="[^"]*(?:apply-button|top-card-layout__cta--primary)[^"]*"[^>]*href="([^"]+)"', re.S)
# The guest page encodes the apply kind in impression ids / tracking names: apply-link-onsite is Easy Apply,
# apply-link-offsite goes to the company site.
_P_ONSITE = re.compile(r'data-(?:impression-id|tracking-control-name)="[^"]*(?:apply-link-onsite|easy-apply)[^"]*"',
                       re.I)
_P_OFFSITE = re.compile(r'data-(?:impression-id|tracking-control-name)="[^"]*apply-link-offsite[^"]*"', re.I)
_P_OFFSITE_HREF = re.compile(r'<a[^>]*data-tracking-control-name="[^"]*apply-link-offsite[^"]*"[^>]*href="([^"]+)"',
                             re.S)
_P_JOB_MARKUP = re.compile(r"base-card|topcard__title|top-card-layout", re.I)

_RELATIVE = re.compile(r"(\d+)\s*(minute|hour|day|week|month)s?\s+ago", re.I)
_TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", fragment))).strip()


def minutes_to_tpr(minutes: int) -> str:
    """LinkedIn's posting-age filter takes seconds: 60 minutes -> ``r3600``."""
    return f"r{max(1, int(minutes)) * 60}"


def relative_to_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """'12 hours ago' -> aware UTC datetime. None when the text is not relative."""
    m = _RELATIVE.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n),
             "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
    return (now or datetime.now(UTC)) - delta


def job_id_from(value: str) -> str | None:
    m = _URN_ID.search(value or "") or _URL_ID.search(value or "")
    return m.group(1) if m else None


def unwrap_external_apply(url: str) -> str:
    """``https://www.linkedin.com/jobs/view/externalApply/<id>?url=<encoded>&urlHash=..`` -> the target URL.

    Loops while the result is still a LinkedIn wrapper (double encoding). Other URLs come back unchanged.
    """
    for _ in range(3):
        parts = urlsplit(url)
        if "linkedin.com" not in parts.netloc.lower():
            return url
        target = parse_qs(parts.query).get("url")
        if not target or not target[0]:
            return url
        url = unquote(target[0]).strip()
    return url


def _normalize_apply_url(url: str) -> str:
    """Unescape, unwrap the externalApply redirect and drop LinkedIn's own sign-in / signup / authwall links."""
    url = html_lib.unescape(url or "").strip()
    if not url or url.startswith("#"):
        return ""
    url = unwrap_external_apply(url)
    parts = urlsplit(url)
    if "linkedin.com" in parts.netloc.lower():
        path = parts.path.lower()
        if not path.startswith("/jobs/view/") or "session_redirect=" in parts.query.lower():
            return ""
    return url


def is_blocked_url(url: str) -> bool:
    """True when a guest request ended on the authwall / login / checkpoint / signup page."""
    parts = urlsplit(url or "")
    if "linkedin.com" not in parts.netloc.lower():
        return False
    path = parts.path.lower()
    return any(path.startswith(p) for p in BLOCK_PATHS)


# ------------------------------------------------------------------ parsing (pure)


@dataclass
class PostingDetails:
    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    apply_url: str = ""
    easy_apply: bool = False
    offsite: bool = False
    apply_redirect: str = ""
    posted: str = ""
    applicants: str = ""
    criteria: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """No title, description or apply link: an interstitial or authwall, not a posting."""
        return not (self.title or self.description or self.apply_url)


def parse_search_html(page: str, now: datetime | None = None, easy_apply: bool | None = None) -> list[NormalizedJob]:
    """Parse one search page into jobs. Cards without a job id or title are skipped.

    `easy_apply=True` marks every card as Easy Apply: the search cards carry no marker of their own, so the
    `f_AL=true` server-side filter is the source of truth for a filtered search.
    """
    jobs: list[NormalizedJob] = []
    for card in _CARD.findall(page):
        job = parse_card(card, now, easy_apply=easy_apply)
        if job:
            jobs.append(job)
    return jobs


def parse_card(card: str, now: datetime | None = None, easy_apply: bool | None = None) -> NormalizedJob | None:
    if "base-card" not in card:
        return None
    link = _LINK.search(card)
    job_id = job_id_from(card) or (job_id_from(link.group(1)) if link else None)
    title = _text(m.group(1)) if (m := _TITLE.search(card)) else ""
    if not job_id or not title:
        return None
    company = _text(m.group(1)) if (m := _COMPANY.search(card)) else ""
    location = _text(m.group(1)) if (m := _LOCATION.search(card)) else ""
    listed, posted_at = "", None
    if (m := _TIME.search(card)):
        listed = _text(m.group(2))
        posted_at = relative_to_datetime(listed, now) or parse_dt(m.group(1))
    benefits = [_text(b) for b in _BENEFIT.findall(card)]
    salary = _text(m.group(1)) if (m := _SALARY.search(card)) else ""
    url = JOB_URL.format(job_id=job_id)
    card_easy = any("easy apply" in b.lower() for b in benefits) or bool(_P_ONSITE.search(card))
    raw: dict[str, Any] = {
        "easy_apply": bool(easy_apply) if easy_apply is not None else card_easy,
        "listed": listed,
        "listing_url": html_lib.unescape(link.group(1)) if link else url,
        "benefits": benefits,
    }
    if easy_apply:
        raw["easy_apply_filter"] = True
    if salary:
        raw["salary"] = salary
    return NormalizedJob(
        title=title, company=company, location=location,
        remote=looks_remote(location, title),
        posted_at=posted_at, url=url, apply_url=url,
        source="linkedin", source_id=job_id, raw=raw,
    )


def parse_posting_html(page: str, job_id: str) -> PostingDetails:
    d = PostingDetails(job_id=job_id)
    d.title = _text(m.group(1)) if (m := _P_TITLE.search(page)) else ""
    d.company = _text(m.group(1)) if (m := _P_COMPANY.search(page)) else ""
    d.location = _text(m.group(1)) if (m := _P_LOCATION.search(page)) else ""
    d.posted = _text(m.group(1)) if (m := _P_POSTED.search(page)) else ""
    d.applicants = _text(m.group(1)) if (m := _P_APPLICANTS.search(page)) else ""
    desc = _P_DESCRIPTION.search(page) or _P_DESCRIPTION_FALLBACK.search(page)
    d.description = html_to_text(desc.group(1)) if desc else ""
    d.criteria = {_text(k): _text(v) for k, v in _P_CRITERIA.findall(page)}

    cta = _P_PRIMARY_CTA.search(page)
    cta_text = _text(cta.group(1)).lower() if cta else ""
    d.offsite = bool(_P_OFFSITE.search(page))
    d.easy_apply = ("easy apply" in cta_text or bool(_P_ONSITE.search(page))) and not d.offsite
    if (m := _P_APPLY_URL_CODE.search(page)):
        raw_url = html_lib.unescape(m.group(1)).strip()
        d.apply_url = _normalize_apply_url(raw_url)
        if d.apply_url and d.apply_url != raw_url:
            d.apply_redirect = raw_url
    if not d.apply_url and (m := _P_OFFSITE_HREF.search(page) or _P_APPLY_HREF.search(page)):
        d.apply_url = _normalize_apply_url(m.group(1))
    return d


def apply_details(job: NormalizedJob, d: PostingDetails) -> NormalizedJob:
    """Merge a posting page into a listing: description, direct apply link, ATS, Easy Apply flag.

    An empty page (authwall, interstitial) leaves the listing untouched and unmarked so a later run retries.
    """
    if d.empty:
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
    raw = dict(job.raw)
    # The posting page is authoritative when it says anything about the apply kind; otherwise the
    # search-filter flag (f_AL=true) stands.
    page_knows = d.easy_apply or d.offsite or bool(d.apply_url)
    raw["easy_apply"] = bool(d.easy_apply if page_knows else raw.get("easy_apply"))
    raw["details_fetched"] = True
    if d.criteria:
        raw["criteria"] = d.criteria
    if d.applicants:
        raw["applicants"] = d.applicants
    if d.apply_redirect:
        raw["apply_redirect"] = d.apply_redirect
    if d.apply_url and not raw["easy_apply"]:
        update["apply_url"] = d.apply_url
        hit = detect_from_url(d.apply_url)
        update["ats_type"] = hit.ats if hit else None
    else:
        update["apply_url"] = job.url
        update["ats_type"] = None
    if d.description or d.location:
        update["remote"] = job.remote or looks_remote(job.location, d.location, job.title)
    return job.model_copy(update={**update, "raw": raw})


# ------------------------------------------------------------------ collector


class RateLimited(RuntimeError):
    """LinkedIn answered 429/999 or redirected to the authwall; the run stops and keeps what it collected."""


class LinkedInGuestCollector(BaseCollector):
    source = "linkedin"

    def __init__(
        self,
        queries: list[str],
        location: str = "",
        minutes_old: int = 60,
        *,
        remote_only: bool = False,
        easy_apply_only: bool = False,
        max_results: int = 100,
        with_details: bool = True,
        max_details: int = 40,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        geo_id: str | None = None,
        delay: float = 1.5,
        shuffle_queries: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_if: Callable[[], bool] | None = None,
        on_rate_limited: Callable[[float], None] | None = None,
    ):
        """`skip_if()` true skips the whole run (a cooldown from an earlier block is active);
        `on_rate_limited(hours)` is called once when LinkedIn blocks the run, to persist a cooldown."""
        super().__init__(client)
        self.queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        if shuffle_queries:
            random.shuffle(self.queries)  # every user's roles get their turn first across scheduled runs
        self.location = location
        self.minutes_old = max(1, int(minutes_old))
        self.remote_only, self.easy_apply_only = remote_only, easy_apply_only
        self.max_results, self.with_details, self.max_details = max_results, with_details, max_details
        self.max_requests = max(1, int(max_requests))
        self.geo_id = str(geo_id) if geo_id else None
        self.delay, self._sleep = delay, sleep
        self.skip_if, self.on_rate_limited = skip_if, on_rate_limited
        self.rate_limited = False
        self.budget_exhausted = False
        # Set once the search phase has used its reserved slice of the budget (see _search_budget): the
        # search phase stops there, but enrich() may still spend the reserved remainder on posting pages.
        self.search_budget_reached = False
        self._in_search = True
        self.requests_made = 0

    @property
    def _search_budget(self) -> int:
        """Request budget for the search phase alone, so a wide query set cannot spend the whole budget on
        search pages and leave every job un-enriched (a bare /jobs/view link, no ATS type, Easy Apply
        unresolved). Reserve room for detail fetches only when the budget comfortably exceeds the detail
        cap; small or explicit budgets keep the full budget for search so their behaviour is unchanged."""
        if self.with_details and self.max_details > 0 and self.max_requests > self.max_details:
            reserve = min(self.max_details, self.max_requests // 3)
            return max(1, self.max_requests - reserve)
        return self.max_requests

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().http_timeout, headers=BROWSER_HEADERS,
                                        follow_redirects=True)
        return self._client

    @property
    def key(self) -> str:
        return "linkedin:guest"

    # -- requests

    def search_params(self, query: str, start: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"keywords": query, "location": self.location,
                                  "f_TPR": minutes_to_tpr(self.minutes_old), "start": start}
        if self.remote_only:
            params["f_WT"] = "2"
        if self.easy_apply_only:
            params["f_AL"] = "true"
        if self.geo_id:
            params["geoId"] = self.geo_id
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
        self.log.warning("blocked by linkedin, stopping run", reason=reason, url=url, requests=self.requests_made,
                         cooldown_hours=round(hours, 2))
        if self.on_rate_limited is not None:
            try:
                self.on_rate_limited(hours)
            except Exception as exc:  # noqa: BLE001 - persisting the cooldown must not break the run
                self.log.warning("could not record linkedin cooldown", error=str(exc)[:200])

    def _get(self, url: str, **kwargs: Any) -> str | None:
        """GET with politeness delay and budget. Returns the body, or None when blocked, exhausted or missing."""
        if self.rate_limited:
            return None
        if self._in_search and self._search_budget < self.max_requests and self.requests_made >= self._search_budget:
            if not self.search_budget_reached:
                self.search_budget_reached = True
                self.log.info("linkedin search budget reached, reserving the rest for detail fetches",
                              search_budget=self._search_budget, max_requests=self.max_requests)
            return None
        if self.requests_made >= self.max_requests:
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.log.info("linkedin request budget reached", max_requests=self.max_requests)
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
        if resp.status_code >= 400:
            self.log.warning("http error", status=resp.status_code, url=url)
            return None
        final = str(resp.url)
        if is_blocked_url(final) or any(is_blocked_url(str(h.url)) for h in resp.history):
            self._block("authwall redirect", final)
            return None
        body = resp.text
        if body.strip() and not _P_JOB_MARKUP.search(body):
            # 200 with a page that is not a job list / posting: an interstitial LinkedIn serves blocked guests.
            self._block("no job markup in response", final)
            return None
        return body

    def fetch_page(self, query: str, start: int = 0) -> list[NormalizedJob]:
        body = self._get(SEARCH_URL, params=self.search_params(query, start))
        return parse_search_html(body, easy_apply=True if self.easy_apply_only else None) if body else []

    def fetch_details(self, job_id: str) -> PostingDetails | None:
        body = self._get(POSTING_URL.format(job_id=job_id))
        return parse_posting_html(body, job_id) if body else None

    # -- pipeline

    def search(self, query: str) -> list[NormalizedJob]:
        found: dict[str, NormalizedJob] = {}
        start = 0
        while (len(found) < self.max_results and not self.rate_limited and not self.budget_exhausted
               and not self.search_budget_reached):
            page = self.fetch_page(query, start)
            if not page:
                break
            new = [j for j in page if j.source_id not in found]
            for job in new:
                found[job.source_id] = job
            if not new or len(page) < PAGE_SIZE:
                break
            start += len(page)
        self.log.info("searched", query=query, location=self.location, minutes_old=self.minutes_old,
                      found=len(found), rate_limited=self.rate_limited)
        return list(found.values())[: self.max_results]

    def enrich(self, jobs: list[NormalizedJob]) -> list[NormalizedJob]:
        self._in_search = False  # the reserved remainder of the budget is now available for posting pages
        out, fetched = [], 0
        for job in jobs:
            if fetched >= self.max_details or self.rate_limited or self.budget_exhausted:
                out.append(job)
                continue
            fetched += 1
            details = self.fetch_details(job.source_id)
            out.append(apply_details(job, details) if details else job)
        if fetched:
            self.log.info("fetched details", count=fetched, skipped=len(jobs) - fetched)
        return out

    def collect(self) -> list[NormalizedJob]:
        if self.skip_if is not None and self.skip_if():
            self.log.info("linkedin guest search skipped: cooldown active")
            return []
        seen: dict[str, NormalizedJob] = {}
        for query in self.queries:
            if self.rate_limited or self.budget_exhausted or self.search_budget_reached:
                break
            for job in self.search(query):
                seen.setdefault(job.source_id, job)
        jobs = list(seen.values())
        if self.with_details and jobs:
            jobs = self.enrich(jobs)
        if self.easy_apply_only:
            # The f_AL=true filter already applied server-side; only a fetched posting page can overrule it.
            jobs = [j for j in jobs if j.raw.get("easy_apply") or not j.raw.get("details_fetched")]
        return jobs


def search_once(
    queries: list[str] | str,
    location: str = "",
    minutes_old: int = 60,
    *,
    remote_only: bool = False,
    easy_apply_only: bool = False,
    max_results: int = 100,
    with_details: bool = True,
    max_details: int = 40,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    geo_id: str | None = None,
    delay: float = 1.5,
    client: httpx.Client | None = None,
) -> list[NormalizedJob]:
    """One-shot guest search for the CLI (`jobapply linkedin search`). No database side effects."""
    collector = LinkedInGuestCollector(
        [queries] if isinstance(queries, str) else list(queries), location, minutes_old,
        remote_only=remote_only, easy_apply_only=easy_apply_only, max_results=max_results,
        with_details=with_details, max_details=max_details, max_requests=max_requests, geo_id=geo_id,
        delay=delay, client=client,
    )
    return collector.collect()


# ------------------------------------------------------------------ cross-run cooldown (app_settings)

GUEST_COOLDOWN_KEY = "linkedin_guest_cooldown_until"


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
    """True while the guest scraper should stay away: its own cooldown, or any user's challenge cooldown."""
    from sqlmodel import select

    from jobapply.models import User
    from jobapply.sites.linkedin.session import cooldown_until

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
    """Record a cooldown, doubling the window when the previous one is still active (repeat blocks)."""
    from jobapply.switches import set_value

    now = now or datetime.now(UTC)
    previous = guest_cooldown_until(session)
    if previous and previous > now:
        remaining = (previous - now).total_seconds() / 3600
        hours = max(hours, remaining * 2)
    until = now + timedelta(hours=min(hours, 48))
    set_value(session, GUEST_COOLDOWN_KEY, until.isoformat())
    return until
