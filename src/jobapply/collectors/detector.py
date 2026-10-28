"""Detect which ATS a company careers page uses, so it can be polled through the ATS API."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

# (ats, regex with a `slug` group). Order matters: embed URLs before generic hosts.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?"
                              r"(?P<slug>[\w-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/(?P<slug>[\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/(?P<slug>[\w.-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/(?P<slug>[\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?(?P<slug>[\w-]+)", re.I)),
    ("workable", re.compile(r"(?P<slug>[\w-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[\w-]+)", re.I)),
    ("workday", re.compile(r"(?P<slug>[\w-]+)\.wd\d+\.myworkdayjobs\.com(?:/[\w-]+)?", re.I)),
]
_IGNORED_SLUGS = {"embed", "j", "api", "jobs", "v1", "www"}


@dataclass(frozen=True)
class AtsHit:
    ats: str
    slug: str


def detect_from_url(url: str) -> AtsHit | None:
    for ats, pattern in _PATTERNS:
        m = pattern.search(url or "")
        if m and m.group("slug").lower() not in _IGNORED_SLUGS:
            return AtsHit(ats, m.group("slug"))
    return None


def detect(careers_url: str, client: httpx.Client | None = None) -> AtsHit | None:
    """Check the URL, then follow redirects and look for ATS links or embeds in the HTML."""
    hit = detect_from_url(careers_url)
    if hit:
        return hit
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=20,
                                    headers={"User-Agent": "Mozilla/5.0 (jobapply ATS detector)"})
    try:
        resp = client.get(careers_url)
    except httpx.HTTPError:
        return None
    finally:
        if own:
            client.close()
    return detect_from_url(str(resp.url)) or detect_from_url(resp.text)
