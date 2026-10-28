"""Build collector instances from config/companies.yaml, config/searches.yaml and settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from jobapply.collectors.apis import (
    AdzunaCollector,
    ArbeitnowCollector,
    JobicyCollector,
    JSearchCollector,
    RemoteOKCollector,
    RemotiveCollector,
    TheMuseCollector,
)
from jobapply.collectors.ats import ATS_COLLECTORS
from jobapply.collectors.base import BaseCollector
from jobapply.collectors.detector import detect
from jobapply.collectors.hn import HNWhoIsHiringCollector
from jobapply.collectors.jobspy_adapter import JOBSPY_SITES, JobSpyCollector
from jobapply.config import Settings, get_settings
from jobapply.logs import get_logger

log = get_logger(__name__)

FEED_COLLECTORS: dict[str, type[BaseCollector]] = {
    "remotive": RemotiveCollector,
    "remoteok": RemoteOKCollector,
    "jobicy": JobicyCollector,
    "arbeitnow": ArbeitnowCollector,
    "themuse": TheMuseCollector,
    "hn": HNWhoIsHiringCollector,
}


@dataclass
class Company:
    name: str
    ats: str | None = None
    slug: str | None = None
    careers_url: str | None = None


@dataclass
class SearchConfig:
    queries: list[str] = field(default_factory=list)
    location: str = ""
    hours_old: int = 24
    feeds: list[str] = field(default_factory=lambda: list(FEED_COLLECTORS))
    jobspy_sites: list[str] = field(default_factory=list)


def load_companies(path: Path) -> list[Company]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [Company(**c) for c in data.get("companies", [])]


def save_companies(path: Path, companies: list[Company]) -> None:
    rows = [{k: v for k, v in c.__dict__.items() if v} for c in companies]
    header = "# Target companies. `jobapply detect-companies` fills in ats and slug from careers_url.\n"
    path.write_text(header + yaml.safe_dump({"companies": rows}, sort_keys=False, allow_unicode=True))


def load_searches(path: Path) -> SearchConfig:
    if not path.exists():
        return SearchConfig()
    data = yaml.safe_load(path.read_text()) or {}
    return SearchConfig(**data)


def detect_companies(companies: list[Company], client: httpx.Client | None = None) -> int:
    """Fill ats/slug for companies that only have a careers URL. Returns how many were updated."""
    updated = 0
    for c in companies:
        if c.ats and c.slug or not c.careers_url:
            continue
        hit = detect(c.careers_url, client)
        if hit:
            c.ats, c.slug = hit.ats, hit.slug
            updated += 1
            log.info("detected ats", company=c.name, ats=hit.ats, slug=hit.slug)
        else:
            log.warning("ats not detected", company=c.name, url=c.careers_url)
    return updated


def build_collectors(
    settings: Settings | None = None,
    companies: list[Company] | None = None,
    searches: SearchConfig | None = None,
    extra_queries: list[str] | None = None,
    only: list[str] | None = None,
) -> list[BaseCollector]:
    s = settings or get_settings()
    companies = load_companies(s.companies_file) if companies is None else companies
    searches = searches or load_searches(s.companies_file.with_name("searches.yaml"))
    queries = list(dict.fromkeys([*searches.queries, *(extra_queries or [])]))

    out: list[BaseCollector] = []
    for c in companies:
        cls = ATS_COLLECTORS.get(c.ats or "")
        if cls and c.slug:
            out.append(cls(c.slug, c.name))
        elif c.ats and c.ats not in ATS_COLLECTORS:
            log.debug("no public collector for ats", company=c.name, ats=c.ats)
    for name in searches.feeds:
        if name in FEED_COLLECTORS:
            out.append(FEED_COLLECTORS[name]())
    if queries:
        for site in searches.jobspy_sites:
            if site in JOBSPY_SITES:
                out.append(JobSpyCollector(site, queries, searches.location, searches.hours_old))
        if s.adzuna_app_id and s.adzuna_app_key:
            out.append(AdzunaCollector(s.adzuna_app_id, s.adzuna_app_key.get_secret_value(), queries,
                                       s.adzuna_country, max_days_old=max(1, searches.hours_old // 24)))
        if s.rapidapi_key:
            out.append(JSearchCollector(s.rapidapi_key.get_secret_value(), queries))
    if only:
        out = [c for c in out if c.source in only or c.key in only]
    return out


def describe(collectors: list[BaseCollector]) -> list[dict[str, Any]]:
    return [{"key": c.key, "source": c.source} for c in collectors]
