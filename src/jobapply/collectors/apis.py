"""Collectors for job APIs: public feeds (no key) and keyed search APIs (Adzuna, JSearch)."""

from __future__ import annotations

from typing import Any

import httpx

from jobapply.collectors.base import (
    BaseCollector,
    CollectorError,
    NormalizedJob,
    first,
    html_to_text,
    looks_remote,
    parse_dt,
    parse_salary,
)
from jobapply.collectors.detector import detect_from_url


def _ats(url: str | None) -> str | None:
    hit = detect_from_url(url or "")
    return hit.ats if hit else None


class RemotiveCollector(BaseCollector):
    source = "remotive"

    def __init__(self, client: httpx.Client | None = None, category: str | None = None, limit: int = 200):
        super().__init__(client)
        self.category, self.limit = category, limit

    def collect(self) -> list[NormalizedJob]:
        params: dict[str, Any] = {"limit": self.limit}
        if self.category:
            params["category"] = self.category
        data = self.get_json("https://remotive.com/api/remote-jobs", params=params)
        return [self.parse(j) for j in data.get("jobs", [])]

    def parse(self, j: dict) -> NormalizedJob:
        smin, smax, cur = parse_salary(j.get("salary"))
        return NormalizedJob(
            title=j["title"].strip(),
            company=j.get("company_name", ""),
            location=j.get("candidate_required_location") or "Remote",
            remote=True,
            posted_at=parse_dt(j.get("publication_date")),
            url=j.get("url", ""),
            apply_url=j.get("url", ""),
            description=html_to_text(j.get("description")),
            salary_min=smin, salary_max=smax, currency=cur,
            source=self.source,
            source_id=str(j["id"]),
            raw=j,
        )


class RemoteOKCollector(BaseCollector):
    source = "remoteok"

    def collect(self) -> list[NormalizedJob]:
        data = self.get_json("https://remoteok.com/api")
        # First element is a legal notice, not a job.
        return [self.parse(j) for j in data if isinstance(j, dict) and j.get("id") and j.get("position")]

    def parse(self, j: dict) -> NormalizedJob:
        return NormalizedJob(
            title=j["position"].strip(),
            company=j.get("company", ""),
            location=j.get("location") or "Remote",
            remote=True,
            posted_at=parse_dt(first(j.get("date"), j.get("epoch"))),
            url=j.get("url", ""),
            apply_url=j.get("apply_url") or j.get("url", ""),
            description=html_to_text(j.get("description")),
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            currency="USD" if j.get("salary_min") else None,
            source=self.source,
            source_id=str(j["id"]),
            raw=j,
        )


class JobicyCollector(BaseCollector):
    source = "jobicy"

    def __init__(self, client: httpx.Client | None = None, count: int = 50, tag: str | None = None):
        super().__init__(client)
        self.count, self.tag = count, tag

    def collect(self) -> list[NormalizedJob]:
        params: dict[str, Any] = {"count": self.count}
        if self.tag:
            params["tag"] = self.tag
        data = self.get_json("https://jobicy.com/api/v2/remote-jobs", params=params)
        return [self.parse(j) for j in data.get("jobs", [])]

    def parse(self, j: dict) -> NormalizedJob:
        return NormalizedJob(
            title=html_to_text(j["jobTitle"]),
            company=j.get("companyName", ""),
            location=j.get("jobGeo") or "Remote",
            remote=True,
            posted_at=parse_dt(j.get("pubDate")),
            url=j.get("url", ""),
            apply_url=j.get("url", ""),
            description=html_to_text(first(j.get("jobDescription"), j.get("jobExcerpt"))),
            salary_min=j.get("annualSalaryMin") or None,
            salary_max=j.get("annualSalaryMax") or None,
            currency=j.get("salaryCurrency"),
            source=self.source,
            source_id=str(j["id"]),
            raw=j,
        )


class ArbeitnowCollector(BaseCollector):
    source = "arbeitnow"

    def __init__(self, client: httpx.Client | None = None, pages: int = 1):
        super().__init__(client)
        self.pages = pages

    def collect(self) -> list[NormalizedJob]:
        jobs = []
        for page in range(1, self.pages + 1):
            data = self.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
            jobs += [self.parse(j) for j in data.get("data", [])]
        return jobs

    def parse(self, j: dict) -> NormalizedJob:
        return NormalizedJob(
            title=j["title"].strip(),
            company=j.get("company_name", ""),
            location=j.get("location", ""),
            remote=bool(j.get("remote")) or looks_remote(j.get("location")),
            posted_at=parse_dt(j.get("created_at")),
            url=j.get("url", ""),
            apply_url=j.get("url", ""),
            description=html_to_text(j.get("description")),
            source=self.source,
            source_id=j["slug"],
            raw=j,
        )


class TheMuseCollector(BaseCollector):
    source = "themuse"

    def __init__(self, client: httpx.Client | None = None, pages: int = 1, category: str | None = None):
        super().__init__(client)
        self.pages, self.category = pages, category

    def collect(self) -> list[NormalizedJob]:
        jobs = []
        for page in range(1, self.pages + 1):
            params: dict[str, Any] = {"page": page, "descending": "true"}
            if self.category:
                params["category"] = self.category
            data = self.get_json("https://www.themuse.com/api/public/jobs", params=params)
            jobs += [self.parse(j) for j in data.get("results", [])]
        return jobs

    def parse(self, j: dict) -> NormalizedJob:
        location = "; ".join(loc["name"] for loc in j.get("locations", []))
        url = (j.get("refs") or {}).get("landing_page", "")
        return NormalizedJob(
            title=j["name"].strip(),
            company=(j.get("company") or {}).get("name", ""),
            location=location,
            remote=looks_remote(location),
            posted_at=parse_dt(j.get("publication_date")),
            url=url,
            apply_url=url,
            description=html_to_text(j.get("contents")),
            source=self.source,
            source_id=str(j["id"]),
            raw=j,
        )


class AdzunaCollector(BaseCollector):
    """https://developer.adzuna.com — needs app_id and app_key."""

    source = "adzuna"

    def __init__(self, app_id: str, app_key: str, queries: list[str], country: str = "in",
                 where: str | None = None, max_days_old: int = 2, client: httpx.Client | None = None):
        super().__init__(client)
        if not (app_id and app_key):
            raise CollectorError("adzuna: app_id and app_key are required")
        self.app_id, self.app_key = app_id, app_key
        self.queries, self.country, self.where, self.max_days_old = queries, country, where, max_days_old

    def collect(self) -> list[NormalizedJob]:
        jobs = []
        for q in self.queries:
            params: dict[str, Any] = {
                "app_id": self.app_id, "app_key": self.app_key, "what": q,
                "max_days_old": self.max_days_old, "results_per_page": 50, "sort_by": "date",
            }
            if self.where:
                params["where"] = self.where
            data = self.get_json(f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1", params=params)
            jobs += [self.parse(j) for j in data.get("results", [])]
        return jobs

    def parse(self, j: dict) -> NormalizedJob:
        location = (j.get("location") or {}).get("display_name", "")
        return NormalizedJob(
            title=html_to_text(j["title"]),
            company=(j.get("company") or {}).get("display_name", ""),
            location=location,
            remote=looks_remote(location, j.get("title"), j.get("description")),
            posted_at=parse_dt(j.get("created")),
            url=j.get("redirect_url", ""),
            apply_url=j.get("redirect_url", ""),
            description=html_to_text(j.get("description")),
            salary_min=j.get("salary_min"),
            salary_max=j.get("salary_max"),
            currency={"in": "INR", "gb": "GBP", "us": "USD"}.get(self.country),
            source=self.source,
            source_id=str(j["id"]),
            raw=j,
        )


class JSearchCollector(BaseCollector):
    """JSearch on RapidAPI (Google for Jobs). Needs a RapidAPI key."""

    source = "jsearch"
    host = "jsearch.p.rapidapi.com"

    def __init__(self, api_key: str, queries: list[str], date_posted: str = "today",
                 client: httpx.Client | None = None):
        super().__init__(client)
        if not api_key:
            raise CollectorError("jsearch: RapidAPI key is required")
        self.api_key, self.queries, self.date_posted = api_key, queries, date_posted

    def collect(self) -> list[NormalizedJob]:
        headers = {"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": self.host}
        jobs = []
        for q in self.queries:
            data = self.get_json(f"https://{self.host}/search", headers=headers,
                                 params={"query": q, "page": 1, "num_pages": 1,
                                         "date_posted": self.date_posted})
            jobs += [self.parse(j) for j in data.get("data", [])]
        return jobs

    def parse(self, j: dict) -> NormalizedJob:
        location = ", ".join(p for p in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if p)
        apply = j.get("job_apply_link", "")
        return NormalizedJob(
            title=j["job_title"].strip(),
            company=j.get("employer_name", ""),
            location=location,
            remote=bool(j.get("job_is_remote")),
            posted_at=parse_dt(first(j.get("job_posted_at_datetime_utc"), j.get("job_posted_at_timestamp"))),
            url=apply,
            apply_url=apply,
            description=j.get("job_description", ""),
            ats_type=_ats(apply),
            salary_min=j.get("job_min_salary"),
            salary_max=j.get("job_max_salary"),
            currency=j.get("job_salary_currency"),
            source=self.source,
            source_id=j["job_id"],
            raw=j,
        )
