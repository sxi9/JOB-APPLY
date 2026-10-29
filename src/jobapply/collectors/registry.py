"""Build collector instances from config/companies.yaml, config/searches.yaml and settings."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from jobapply.sites.cutshort.search import PUBLIC_COOLDOWN_KEY as CUTSHORT_COOLDOWN_KEY
from jobapply.sites.cutshort.search import CutshortCollector
from jobapply.sites.instahyre.search import InstahyreCollector
from jobapply.sites.linkedin.search import DEFAULT_MAX_REQUESTS, LinkedInGuestCollector
from jobapply.sites.naukri.search import NaukriCollector
from jobapply.sites.wellfound.search import GUEST_COOLDOWN_KEY as WELLFOUND_COOLDOWN_KEY
from jobapply.sites.wellfound.search import WellfoundCollector

log = get_logger(__name__)

# Assisted sites with a logged-out (public) search collector. All share the uniform constructor
# `(queries, location, hours_old, max_results, max_requests, user_id, skip_if=, on_rate_limited=)`.
ASSISTED_COLLECTORS: dict[str, type[BaseCollector]] = {
    "naukri": NaukriCollector,
    "instahyre": InstahyreCollector,
    "cutshort": CutshortCollector,
    "wellfound": WellfoundCollector,
}
# Per-site keyword options the `assisted:` block may carry on top of the shared ones (passed through as-is; a
# value the collector rejects disables that site's search only, see assisted_collectors). Naukri's `user_id`
# is deliberately NOT accepted here: the shared pool must never run inside one user's logged-in profile (that
# user would absorb any CAPTCHA for everyone's search); the browser fallback stays a per-user CLI affair.
ASSISTED_EXTRAS: dict[str, tuple[str, ...]] = {
    "naukri": ("api", "browser_fallback"),
    "instahyre": ("job_type", "years", "with_details", "max_details"),
    "cutshort": ("remote_only", "hub_slugs"),
    "wellfound": ("remote_only", "with_details", "max_details", "max_pages"),
}
# app_settings key per site holding the public-search cooldown (`<site>_..._cooldown_until`, ISO datetime), the
# same mechanism as `linkedin_guest_cooldown_until`. Cutshort and Wellfound keep the key their module defines.
ASSISTED_COOLDOWN_KEYS: dict[str, str] = {
    "naukri": "naukri_public_cooldown_until",
    "instahyre": "instahyre_public_cooldown_until",
    "cutshort": CUTSHORT_COOLDOWN_KEY,
    "wellfound": WELLFOUND_COOLDOWN_KEY,
}
ASSISTED_DEFAULT_MAX_REQUESTS = 40  # per site per run (the collectors' own default)
ASSISTED_DEFAULT_COOLDOWN_HOURS = 2.0
ASSISTED_MAX_COOLDOWN_HOURS = 48.0

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
    hot: bool = False  # polled by the hot-polling tier (see hot_collectors)


@dataclass
class SearchConfig:
    queries: list[str] = field(default_factory=list)
    location: str = ""
    hours_old: int = 24
    feeds: list[str] = field(default_factory=lambda: list(FEED_COLLECTORS))
    jobspy_sites: list[str] = field(default_factory=list)
    # LinkedIn guest search: enabled, minutes_old, location, geo_id, remote_only,
    # easy_apply_only, max_results, max_details, max_requests (see config/searches.yaml).
    linkedin: dict[str, Any] = field(default_factory=dict)
    # Hot-polling tier (see hot_collectors): {companies: [...], linkedin: true, linkedin_minutes_old: 15}.
    hot: dict[str, Any] = field(default_factory=dict)
    # Assisted sites' public search: {naukri: {enabled, location, hours_old, max_results, max_requests, ...},
    # instahyre: {...}, cutshort: {...}, wellfound: {...}} (see ASSISTED_COLLECTORS / config/searches.yaml).
    assisted: dict[str, Any] = field(default_factory=dict)
    # Breadth mode: when true (default) the query-based collectors (LinkedIn guest, keyed aggregators) run
    # over the whole taxonomy (roles.search_queries()) instead of only the active users' roles, so the
    # shared pool covers every sector. A rotating slice of `broad_queries_per_run` queries runs each time
    # (cycling through the full set across successive runs) to stay within LinkedIn rate limits. Per-user
    # assignment still filters each user to their own roles, so a broad pool + per-user filters means each
    # user still sees only their roles.
    broad: bool = True
    broad_queries_per_run: int = 12


_LINKEDIN_INTS = {"minutes_old": 60, "max_results": 50, "max_details": 40, "max_requests": DEFAULT_MAX_REQUESTS}


def linkedin_options(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validated keyword arguments for LinkedInGuestCollector from the `linkedin:` block, or None when the
    block is off or malformed (logged; the other collectors still run)."""
    li = raw or {}
    if not li.get("enabled"):
        return None
    opts: dict[str, Any] = {"location": str(li.get("location") or ""), "remote_only": bool(li.get("remote_only")),
                            "easy_apply_only": bool(li.get("easy_apply_only")), "geo_id": li.get("geo_id")}
    for key, default in _LINKEDIN_INTS.items():
        value = li.get(key)
        try:
            opts[key] = int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            log.warning("bad linkedin setting in searches.yaml, linkedin guest search disabled", key=key,
                        value=value)
            return None
    return opts


def _guest_cooldown_active() -> bool:
    from jobapply.db import session_scope
    from jobapply.sites.linkedin.search import guest_cooldown_active

    with session_scope() as session:
        return guest_cooldown_active(session)


def _record_guest_cooldown(hours: float) -> None:
    from jobapply.db import session_scope
    from jobapply.sites.linkedin.search import set_guest_cooldown

    with session_scope() as session:
        until = set_guest_cooldown(session, hours)
    log.warning("linkedin guest search paused", until=until.isoformat())


# --- Assisted sites' public search ----------------------------------------------------------------------


def assisted_options(site: str, raw: dict[str, Any] | None, default_hours_old: int = 24) -> dict[str, Any] | None:
    """Validated keyword arguments for one assisted site's collector from its `assisted.<site>:` block, or None
    when the block is off, unknown or malformed (logged; every other collector still runs)."""
    if site not in ASSISTED_COLLECTORS:
        log.warning("unknown assisted site in searches.yaml, ignored", site=site)
        return None
    block = raw or {}
    if not isinstance(block, dict) or not block.get("enabled"):
        return None
    opts: dict[str, Any] = {"location": str(block.get("location") or "")}
    ints = {"hours_old": default_hours_old, "max_results": 50, "max_requests": ASSISTED_DEFAULT_MAX_REQUESTS}
    for key, default in ints.items():
        value = block.get(key)
        try:
            opts[key] = int(value) if value not in (None, "") else int(default)
        except (TypeError, ValueError):
            log.warning("bad assisted setting in searches.yaml, site search disabled", site=site, key=key,
                        value=value)
            return None
    for key in ASSISTED_EXTRAS.get(site, ()):
        if key in block and block[key] is not None:
            opts[key] = block[key]
    return opts


def assisted_cooldown_until(session: Any, site: str) -> datetime | None:
    from jobapply.switches import get_value

    key = ASSISTED_COOLDOWN_KEYS.get(site)
    raw = get_value(session, key) if key else None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def assisted_cooldown_active(session: Any, site: str, now: datetime | None = None) -> bool:
    """True while `site`'s public scraper should stay away: its own cooldown (a block seen on an earlier run) or
    any active user's challenge cooldown on that site (the apply path hit a captcha from this IP)."""
    from sqlmodel import select

    from jobapply.models import User

    now = now or datetime.now(UTC)
    until = assisted_cooldown_until(session, site)
    if until and until > now:
        return True
    try:
        session_mod = importlib.import_module(f"jobapply.sites.{site}.session")
        cooldown_until = session_mod.cooldown_until
    except (ImportError, AttributeError):
        return False
    for user in session.exec(select(User).where(User.is_active)).all():
        user_until = cooldown_until(user)
        if user_until and user_until > now:
            return True
    return False


def set_assisted_cooldown(session: Any, site: str, hours: float = ASSISTED_DEFAULT_COOLDOWN_HOURS,
                          now: datetime | None = None) -> datetime:
    """Record a public-search cooldown for `site`, doubling while the previous one is still active, 48 h cap."""
    from jobapply.switches import set_value

    now = now or datetime.now(UTC)
    previous = assisted_cooldown_until(session, site)
    if previous and previous > now:
        remaining = (previous - now).total_seconds() / 3600
        hours = max(hours, remaining * 2)
    until = now + timedelta(hours=min(float(hours), ASSISTED_MAX_COOLDOWN_HOURS))
    set_value(session, ASSISTED_COOLDOWN_KEYS[site], until.isoformat())
    return until


def _assisted_skip(site: str) -> Callable[[], bool]:
    def skip() -> bool:
        from jobapply.db import session_scope

        with session_scope() as session:
            return assisted_cooldown_active(session, site)

    return skip


def _assisted_block(site: str) -> Callable[[float], None]:
    def record(hours: float) -> None:
        from jobapply.db import session_scope

        with session_scope() as session:
            until = set_assisted_cooldown(session, site, hours)
        log.warning("public search paused", site=site, until=until.isoformat())

    return record


def assisted_collectors(searches: SearchConfig, queries: list[str],
                        skip_if: Callable[[str], Callable[[], bool]] | None = None,
                        on_rate_limited: Callable[[str], Callable[[float], None]] | None = None,
                        ) -> list[BaseCollector]:
    """One collector per enabled site in the `assisted:` block, fed the same query list as the LinkedIn guest
    collector. Each gets the site's own cooldown hooks (`skip_if` / `on_rate_limited`)."""
    out: list[BaseCollector] = []
    if not queries:
        return out
    for site, cls in ASSISTED_COLLECTORS.items():
        opts = assisted_options(site, (searches.assisted or {}).get(site), searches.hours_old)
        if not opts:
            continue
        try:
            collector = cls(list(queries), skip_if=(skip_if or _assisted_skip)(site),
                            on_rate_limited=(on_rate_limited or _assisted_block)(site), **opts)
        except (TypeError, ValueError) as exc:
            # a per-site extra the collector rejects (unknown job_type, non-int years / max_pages ...) must
            # disable this site only, never abort the whole collect run
            log.warning("bad assisted settings in searches.yaml, site search disabled", site=site,
                        keys=sorted(k for k in opts if k in ASSISTED_EXTRAS.get(site, ())), error=str(exc)[:200])
            continue
        out.append(collector)
    return out


def load_companies(path: Path) -> list[Company]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Company(**c) for c in data.get("companies", [])]


def save_companies(path: Path, companies: list[Company]) -> None:
    rows = [{k: v for k, v in c.__dict__.items() if v} for c in companies]
    header = "# Target companies. `jobapply detect-companies` fills in ats and slug from careers_url.\n"
    path.write_text(header + yaml.safe_dump({"companies": rows}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_searches(path: Path) -> SearchConfig:
    if not path.exists():
        return SearchConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        li = linkedin_options(searches.linkedin)
        if li:
            if "linkedin" in searches.jobspy_sites:
                # jobspy stores source_id "li-<id>", the guest collector "<id>": the same posting gets two
                # JobSource rows. Run one of them.
                log.warning("both jobspy linkedin and the linkedin guest collector are enabled; "
                            "each posting will get two source rows, enable only one")
            minutes_old = li.pop("minutes_old")
            out.append(LinkedInGuestCollector(
                queries, minutes_old=minutes_old, shuffle_queries=True,
                skip_if=_guest_cooldown_active, on_rate_limited=_record_guest_cooldown, **li,
            ))
        out.extend(assisted_collectors(searches, queries))
    if only:
        out = [c for c in out if c.source in only or c.key in only]
    return out


def describe(collectors: list[BaseCollector]) -> list[dict[str, Any]]:
    return [{"key": c.key, "source": c.source} for c in collectors]


# --- Hot-polling tier (additive) -------------------------------------------------------------------
# A small, fast subset of collectors for minute-level recency: the ATS company APIs for the hot company
# list plus the LinkedIn guest search. Driven by the `hot:` block in config/searches.yaml. Idempotent:
# run through the same run_all / store_jobs / dedupe path as the normal collect task.


def hot_collectors(
    settings: Settings | None = None,
    companies: list[Company] | None = None,
    searches: SearchConfig | None = None,
    extra_queries: list[str] | None = None,
) -> list[BaseCollector]:
    """Fast exact-timestamp collectors for the hot tier. Empty when nothing is marked hot."""
    s = settings or get_settings()
    companies = load_companies(s.companies_file) if companies is None else companies
    searches = searches or load_searches(s.companies_file.with_name("searches.yaml"))
    hot = searches.hot or {}
    hot_names = {str(n).strip().lower() for n in (hot.get("companies") or [])}

    out: list[BaseCollector] = []
    for c in companies:
        is_hot = c.name.strip().lower() in hot_names or bool(getattr(c, "hot", False))
        cls = ATS_COLLECTORS.get(c.ats or "")
        if is_hot and cls and c.slug:
            out.append(cls(c.slug, c.name))

    if hot.get("linkedin"):
        # The hot tier shares the single guest cooldown (skip_if / on_rate_limited) with the normal tier on
        # purpose: both poll the same guest endpoint from the same IP, so a 429/999 the hot tier triggers
        # must also pause the normal tier (never pile onto a rate-limited IP). The trade-off is that an
        # aggressive hot cadence (JOBAPPLY_HOT_INTERVAL_MINUTES, default 5) can get the whole LinkedIn guest
        # search cooled down for 2h+. Keep the hot LinkedIn cadence conservative; do NOT give the hot tier
        # its own cooldown key, which would let the normal tier hammer a just-challenged IP.
        queries = list(dict.fromkeys([*searches.queries, *(extra_queries or [])]))
        li = linkedin_options(searches.linkedin)
        if queries and li:
            minutes_old = li.pop("minutes_old")
            override = hot.get("linkedin_minutes_old")
            if override not in (None, ""):
                try:
                    minutes_old = int(override)
                except (TypeError, ValueError):
                    log.warning("bad hot.linkedin_minutes_old in searches.yaml, using linkedin.minutes_old",
                                value=override)
            out.append(LinkedInGuestCollector(
                queries, minutes_old=minutes_old, shuffle_queries=True,
                skip_if=_guest_cooldown_active, on_rate_limited=_record_guest_cooldown, **li,
            ))
    return out
