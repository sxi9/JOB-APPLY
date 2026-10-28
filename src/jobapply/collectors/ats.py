"""Collectors for company ATS boards with public JSON APIs. One instance per company."""

from __future__ import annotations

from typing import Any

import httpx

from jobapply.collectors.base import (
    BaseCollector,
    NormalizedJob,
    first,
    html_to_text,
    looks_remote,
    parse_dt,
    parse_salary,
)


class CompanyCollector(BaseCollector):
    def __init__(self, slug: str, company: str | None = None, client: httpx.Client | None = None):
        super().__init__(client)
        self.slug = slug
        self.company = company or slug

    @property
    def key(self) -> str:
        return f"{self.source}:{self.slug}"

    def job(self, **fields: Any) -> NormalizedJob:
        fields.setdefault("company", self.company)
        fields.setdefault("ats_type", self.ats_type)
        fields.setdefault("source", self.source)
        fields["source_id"] = f"{self.slug}:{fields['source_id']}"
        return NormalizedJob(**fields)


class GreenhouseCollector(CompanyCollector):
    source = ats_type = "greenhouse"

    def collect(self) -> list[NormalizedJob]:
        data = self.get_json(f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs",
                             params={"content": "true"})
        return [self.parse(j) for j in data.get("jobs", [])]

    def parse(self, j: dict) -> NormalizedJob:
        location = (j.get("location") or {}).get("name", "")
        description = html_to_text(j.get("content"))
        return self.job(
            title=j["title"].strip(),
            company=j.get("company_name") or self.company,
            location=location,
            remote=looks_remote(location),
            posted_at=parse_dt(first(j.get("first_published"), j.get("updated_at"))),
            url=j.get("absolute_url", ""),
            apply_url=f"https://job-boards.greenhouse.io/{self.slug}/jobs/{j['id']}",
            description=description,
            source_id=str(j["id"]),
            raw=j,
        )


class LeverCollector(CompanyCollector):
    source = ats_type = "lever"

    def collect(self) -> list[NormalizedJob]:
        data = self.get_json(f"https://api.lever.co/v0/postings/{self.slug}", params={"mode": "json"})
        return [self.parse(j) for j in data]

    def parse(self, j: dict) -> NormalizedJob:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        salary = j.get("salaryRange") or {}
        return self.job(
            title=j["text"].strip(),
            location=location,
            remote=j.get("workplaceType") == "remote" or looks_remote(location),
            posted_at=parse_dt(j.get("createdAt")),
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
            description=first(j.get("descriptionPlain"), html_to_text(j.get("description"))) or "",
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            currency=salary.get("currency"),
            source_id=j["id"],
            raw=j,
        )


class AshbyCollector(CompanyCollector):
    source = ats_type = "ashby"

    def collect(self) -> list[NormalizedJob]:
        data = self.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}",
                             params={"includeCompensation": "true"})
        return [self.parse(j) for j in data.get("jobs", []) if j.get("isListed", True)]

    def parse(self, j: dict) -> NormalizedJob:
        comp = j.get("compensation") or {}
        smin, smax, cur = parse_salary(comp.get("scrapeableCompensationSalarySummary"))
        location = j.get("location", "")
        return self.job(
            title=j["title"].strip(),
            location=location,
            remote=bool(j.get("isRemote")) or looks_remote(location),
            posted_at=parse_dt(j.get("publishedAt")),
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=first(j.get("descriptionPlain"), html_to_text(j.get("descriptionHtml"))) or "",
            salary_min=smin,
            salary_max=smax,
            currency=cur,
            source_id=j["id"],
            raw=j,
        )


class WorkableCollector(CompanyCollector):
    source = ats_type = "workable"

    def collect(self) -> list[NormalizedJob]:
        data = self.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{self.slug}",
                             params={"details": "true"})
        company = data.get("name") or self.company
        return [self.parse(j, company) for j in data.get("jobs", [])]

    def parse(self, j: dict, company: str) -> NormalizedJob:
        location = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
        return self.job(
            title=j["title"].strip(),
            company=company,
            location=location,
            remote=bool(j.get("telecommuting")) or looks_remote(location),
            posted_at=parse_dt(first(j.get("published_on"), j.get("created_at"))),
            url=j.get("url") or j.get("shortlink", ""),
            apply_url=j.get("application_url") or j.get("url", ""),
            description=html_to_text(j.get("description")),
            source_id=j["shortcode"],
            raw=j,
        )


class SmartRecruitersCollector(CompanyCollector):
    """Listing endpoint has no description; `with_details=True` fetches each posting."""

    source = ats_type = "smartrecruiters"

    def __init__(self, slug: str, company: str | None = None, client: httpx.Client | None = None,
                 with_details: bool = False, max_pages: int = 5):
        super().__init__(slug, company, client)
        self.with_details = with_details
        self.max_pages = max_pages

    def collect(self) -> list[NormalizedJob]:
        base = f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings"
        jobs: list[NormalizedJob] = []
        offset = 0
        for _ in range(self.max_pages):
            data = self.get_json(base, params={"limit": 100, "offset": offset})
            content = data.get("content", [])
            for j in content:
                if self.with_details:
                    j = {**j, **self.get_json(f"{base}/{j['id']}")}
                jobs.append(self.parse(j))
            offset += len(content)
            if not content or offset >= data.get("totalFound", 0):
                break
        return jobs

    def parse(self, j: dict) -> NormalizedJob:
        loc = j.get("location") or {}
        sections = ((j.get("jobAd") or {}).get("sections")) or {}
        description = "\n\n".join(html_to_text(s.get("text")) for s in sections.values() if s.get("text"))
        company_id = (j.get("company") or {}).get("identifier", self.slug)
        url = j.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company_id}/{j['id']}"
        return self.job(
            title=j["name"].strip(),
            company=(j.get("company") or {}).get("name") or self.company,
            location=loc.get("fullLocation") or ", ".join(p for p in (loc.get("city"), loc.get("country")) if p),
            remote=bool(loc.get("remote")),
            posted_at=parse_dt(j.get("releasedDate")),
            url=url,
            apply_url=j.get("applyUrl") or url,
            description=description,
            source_id=str(j["id"]),
            raw=j,
        )


ATS_COLLECTORS: dict[str, type[CompanyCollector]] = {
    c.source: c
    for c in (GreenhouseCollector, LeverCollector, AshbyCollector, WorkableCollector, SmartRecruitersCollector)
}
