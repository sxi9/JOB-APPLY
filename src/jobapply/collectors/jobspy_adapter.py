"""Aggregator listings through python-jobspy (optional extra: `uv sync --extra jobspy`).

Indeed, LinkedIn guest search, Glassdoor and ZipRecruiter. Listing only, no login.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from jobapply.collectors.base import BaseCollector, NormalizedJob, looks_remote, parse_dt
from jobapply.collectors.detector import detect_from_url

JOBSPY_SITES = ("indeed", "linkedin", "glassdoor", "zip_recruiter")

ScrapeFn = Callable[..., list[dict[str, Any]]]


def _default_scrape(**kwargs: Any) -> list[dict[str, Any]]:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("python-jobspy is not installed; run `uv sync --extra jobspy`") from exc
    return scrape_jobs(**kwargs).to_dict("records")


def _clean(v: Any) -> Any:
    return None if isinstance(v, float) and math.isnan(v) else v


class JobSpyCollector(BaseCollector):
    def __init__(self, site: str, queries: list[str], location: str = "", hours_old: int = 24,
                 results_wanted: int = 50, country_indeed: str = "India", scrape: ScrapeFn | None = None):
        if site not in JOBSPY_SITES:
            raise ValueError(f"unsupported jobspy site {site!r}")
        self.source = site  # type: ignore[misc]
        super().__init__(client=None)
        self.site, self.queries, self.location = site, queries, location
        self.hours_old, self.results_wanted, self.country_indeed = hours_old, results_wanted, country_indeed
        self.scrape = scrape or _default_scrape

    def collect(self) -> list[NormalizedJob]:
        jobs = []
        for q in self.queries:
            rows = self.scrape(
                site_name=[self.site], search_term=q, location=self.location, hours_old=self.hours_old,
                results_wanted=self.results_wanted, country_indeed=self.country_indeed,
                linkedin_fetch_description=False,
            )
            jobs += [j for j in (self.parse(r) for r in rows) if j]
        return jobs

    def parse(self, r: dict[str, Any]) -> NormalizedJob | None:
        r = {k: _clean(v) for k, v in r.items()}
        if not r.get("title") or not r.get("job_url"):
            return None
        direct = r.get("job_url_direct") or ""
        hit = detect_from_url(direct)
        location = r.get("location") or ""
        return NormalizedJob(
            title=str(r["title"]).strip(),
            company=str(r.get("company") or ""),
            location=str(location),
            remote=bool(r.get("is_remote")) or looks_remote(str(location)),
            posted_at=parse_dt(r.get("date_posted")),
            url=r["job_url"],
            apply_url=direct or r["job_url"],
            description=str(r.get("description") or ""),
            ats_type=hit.ats if hit else None,
            salary_min=r.get("min_amount"),
            salary_max=r.get("max_amount"),
            currency=r.get("currency"),
            source=self.site,
            source_id=str(r.get("id") or r["job_url"]),
            raw={k: (str(v) if v is not None else None) for k, v in r.items() if k != "description"},
        )
