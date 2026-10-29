"""Run collectors, store their jobs and hand new jobs to each user's filters."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.engine import Engine
from sqlmodel import select

from jobapply.collectors.base import BaseCollector
from jobapply.collectors.registry import SearchConfig, build_collectors, load_searches
from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.models import CollectorRun, Job, User, utcnow
from jobapply.pipeline.dedupe import store_jobs
from jobapply.pipeline.filters import assign_jobs
from jobapply.roles import all_fields, match_title, search_queries
from jobapply.switches import get_value, set_value

log = get_logger(__name__)

# app_settings key that stores where the broad-query rotation left off, so successive collect runs cycle
# through the whole taxonomy a slice at a time.
BROAD_OFFSET_KEY = "broad_query_offset"


def run_collector(collector: BaseCollector, engine: Engine | None = None) -> list[int]:
    """Run one collector and record a CollectorRun. Failures are recorded, not raised."""
    run = CollectorRun(source=collector.key)
    new_ids: list[int] = []
    try:
        jobs = collector.collect()
        run.fetched = len(jobs)
        with session_scope(engine) as session:
            new_ids = store_jobs(session, jobs)
        run.new = len(new_ids)
        run.ok = True
        log.info("collector done", collector=collector.key, fetched=run.fetched, new=run.new)
    except Exception as exc:  # one broken source must not stop the others
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        log.error("collector failed", collector=collector.key, error=run.error)
    run.finished_at = utcnow()
    with session_scope(engine) as session:
        session.add(run)
    return new_ids


def user_queries(engine: Engine | None = None) -> list[str]:
    """Per-run search queries for the active users' own roles, so a user's sector is searched every run
    (not only when the broad rotation reaches it). Covers both the free-text `filters.roles` and the
    taxonomy picker `filters.role_selection`: each selected field is expanded to representative sub-role
    queries via roles.search_queries so a picker-only user is not left relying on the rotation."""
    with session_scope(engine) as session:
        users = session.exec(select(User).where(User.is_active)).all()
        queries: set[str] = set()
        field_keys: list[str] = []
        for u in users:
            f = u.filters or {}
            queries.update(r for r in (f.get("roles") or []) if r)
            for sel in f.get("role_selection") or []:
                fkey = str(sel).split(":", 1)[0].strip().lower()
                if fkey and fkey not in field_keys:
                    field_keys.append(fkey)
        if field_keys:
            queries.update(search_queries(fields=field_keys))
        return sorted(queries)


def broad_queries(searches: SearchConfig | None = None, engine: Engine | None = None,
                  per_run: int | None = None, *, advance: bool = True) -> list[str]:
    """A rotating slice of the whole-taxonomy breadth query set (roles.search_queries()).

    Each call returns the next `broad_queries_per_run` queries, advancing (and wrapping) a stored offset in
    app_settings, so over a handful of runs every sector gets searched without flooding LinkedIn in one go.

    `advance=False` returns the current slice without moving the offset, so the same sectors are retried on
    the next run. run_all uses this when the LinkedIn guest collector (the main breadth source) is on a
    cooldown and will not actually search this slice, to avoid burning rotation positions it never ran.
    """
    searches = searches if searches is not None else load_searches(
        get_settings().companies_file.with_name("searches.yaml"))
    all_q = search_queries()
    if not all_q:
        return []
    n = per_run if per_run is not None else searches.broad_queries_per_run
    n = max(1, min(int(n), len(all_q)))
    with session_scope(engine) as session:
        try:
            offset = int(get_value(session, BROAD_OFFSET_KEY) or 0)
        except ValueError:
            offset = 0
        offset %= len(all_q)
        picked = [all_q[(offset + i) % len(all_q)] for i in range(n)]
        if advance:
            set_value(session, BROAD_OFFSET_KEY, str((offset + n) % len(all_q)))
    log.info("broad query rotation", total=len(all_q), per_run=n, offset=offset, advanced=advance)
    return picked


def run_all(only: list[str] | None = None, collectors: list[BaseCollector] | None = None,
            engine: Engine | None = None) -> dict:
    extra: list[str] = []
    queries_ran = 0
    if collectors is None:
        searches = load_searches(get_settings().companies_file.with_name("searches.yaml"))
        # Breadth mode (default on): the shared pool is driven by the whole taxonomy, not only the active
        # users' roles. The broad slice leads so the pool covers every sector; each user's own roles are
        # still appended (deduped) so nothing a user cares about is dropped, and per-user assignment below
        # keeps each user to their own roles.
        if searches.broad:
            # Don't advance the rotation when the LinkedIn guest collector (the main breadth source) is on
            # a cooldown: it will skip this run, so advancing would burn this slice's sectors unsearched and
            # make them wait a whole cycle. Keep the slice and retry it next run instead.
            from jobapply.sites.linkedin.search import guest_cooldown_active

            with session_scope(engine) as session:
                cooldown = guest_cooldown_active(session)
            extra = broad_queries(searches, engine, advance=not cooldown)
        extra = list(dict.fromkeys([*extra, *user_queries(engine)]))
        queries_ran = len(dict.fromkeys([*searches.queries, *extra]))
        collectors = build_collectors(searches=searches, extra_queries=extra, only=only)
    new_ids: list[int] = []
    for c in collectors:
        new_ids += run_collector(c, engine)
    with session_scope(engine) as session:
        assigned = assign_jobs(session, new_ids)
    log.info("collect done", collectors=len(collectors), queries=queries_ran, new_jobs=len(new_ids))
    return {"collectors": len(collectors), "queries": queries_ran, "new_jobs": len(new_ids),
            "assigned": assigned}


def coverage_stats(session) -> dict:
    """How well the shared pool covers the taxonomy, for the dashboard and the `jobapply coverage` CLI.

    Returns `total`, `by_source` (count per collector source), `by_sector` (count per taxonomy field whose
    key a job title matches via roles.match_title, plus an "other" bucket for unmatched titles; a title can
    match several fields, so these can sum to more than `total`), and `last_24h` / `last_7d` (by posted_at,
    falling back to first_seen_at). Pure read; no side effects."""
    now = utcnow()
    field_keys = [fld.key for fld in all_fields()]
    jobs = session.exec(select(Job)).all()
    by_source: dict[str, int] = {}
    by_sector: dict[str, int] = {}
    last_24h = last_7d = 0
    for j in jobs:
        by_source[j.source or "(unknown)"] = by_source.get(j.source or "(unknown)", 0) + 1
        when = j.posted_at or j.first_seen_at
        if when:
            age = now - when
            if age <= timedelta(hours=24):
                last_24h += 1
            if age <= timedelta(days=7):
                last_7d += 1
        matched = False
        for key in field_keys:
            if match_title(j.title, [key]):
                by_sector[key] = by_sector.get(key, 0) + 1
                matched = True
        if not matched:
            by_sector["other"] = by_sector.get("other", 0) + 1
    return {"total": len(jobs), "by_source": by_source, "by_sector": by_sector,
            "last_24h": last_24h, "last_7d": last_7d}
