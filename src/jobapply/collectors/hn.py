"""HN "Ask HN: Who is hiring?" monthly thread, via the Algolia HN API (one call per thread)."""

from __future__ import annotations

import html
import re

import httpx

from jobapply.collectors.base import BaseCollector, NormalizedJob, html_to_text, looks_remote, parse_dt
from jobapply.collectors.detector import detect_from_url

_ROLE = re.compile(
    r"engineer|developer|scientist|designer|manager|architect|analyst|lead|devops|sre|"
    r"researcher|founding|intern|head of|director|cto|programmer|administrator", re.I)
_NOT_LOCATION = re.compile(r"full[- ]?time|part[- ]?time|contract|https?://|\$|€|£|visa|salary|equity", re.I)
_HREF = re.compile(r'href="([^"]+)"')


class HNWhoIsHiringCollector(BaseCollector):
    source = "hn"

    def __init__(self, client: httpx.Client | None = None, story_id: int | None = None):
        super().__init__(client)
        self.story_id = story_id

    def latest_story_id(self) -> int:
        data = self.get_json("https://hn.algolia.com/api/v1/search_by_date",
                             params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
        for hit in data.get("hits", []):
            if hit.get("title", "").lower().startswith("ask hn: who is hiring"):
                return int(hit["objectID"])
        raise LookupError("no 'Who is hiring' thread found")

    def collect(self) -> list[NormalizedJob]:
        story_id = self.story_id or self.latest_story_id()
        story = self.get_json(f"https://hn.algolia.com/api/v1/items/{story_id}")
        jobs = []
        for c in story.get("children", []):
            job = self.parse(c)
            if job:
                jobs.append(job)
        return jobs

    def parse(self, c: dict) -> NormalizedJob | None:
        raw_html = c.get("text") or ""
        if not raw_html or c.get("type") != "comment":
            return None
        header_html = raw_html.split("<p>", 1)[0]
        header = html_to_text(header_html)
        parts = [p.strip() for p in re.split(r"\s+[|—–]\s+|\s*\|\s*", header) if p.strip()]
        if len(parts) < 2:
            return None
        company = parts[0]
        rest = parts[1:]
        title = next((p for p in rest if _ROLE.search(p)), "Multiple roles (see post)")
        candidates = [p for p in rest if p is not title and not _NOT_LOCATION.search(p) and not _ROLE.search(p)]
        location = next((p for p in candidates if "," in p and "/" not in p), None) or next(
            (p for p in candidates if looks_remote(p)), "")
        links = [html.unescape(h) for h in _HREF.findall(raw_html)]
        apply = next((u for u in links if detect_from_url(u)), links[0] if links else "")
        hn_url = f"https://news.ycombinator.com/item?id={c['id']}"
        hit = detect_from_url(apply)
        return NormalizedJob(
            title=title[:200],
            company=company[:120],
            location=location[:200],
            remote=looks_remote(header),
            posted_at=parse_dt(c.get("created_at_i") or c.get("created_at")),
            url=hn_url,
            apply_url=apply or hn_url,
            description=html_to_text(raw_html),
            ats_type=hit.ats if hit else None,
            source=self.source,
            source_id=str(c["id"]),
            raw={k: c.get(k) for k in ("id", "author", "created_at", "parent_id")},
        )
