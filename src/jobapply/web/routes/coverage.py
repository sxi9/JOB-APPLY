"""Coverage view: how big the shared job pool is, and how much of it matches this user.

Answers the recurring "why so few jobs?" confusion on-screen: the pool is large; a small matched
number means the user's filters are narrow, not that collection is failing. Everything here is a
server-side count. Read-only (GET), so no CSRF. Per-user scoping via ctx.view, so an admin "View as"
sees that user's matched count against the shared pool.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlmodel import select

from jobapply.models import Application, ApplicationEvent, Job, utcnow
from jobapply.web.deps import Ctx, get_ctx

router = APIRouter()


def _recent(cut):
    """Same recency predicate the /jobs page uses (jobs._recent): posted_at when known, falling back to
    first_seen_at only when posted_at is NULL. Kept identical so the coverage tiles agree with the page
    they deep-link to."""
    return or_(Job.posted_at >= cut, (Job.posted_at.is_(None)) & (Job.first_seen_at >= cut))


def new_counts(session, now=None) -> tuple[int, int]:
    """(new_24h, new_7d) counted with the /jobs recency semantics, so each 'New' tile count matches the
    number of rows the /jobs?hours=N link it points at actually shows. Counting first_seen_at here instead
    would overcount jobs posted days ago but scraped recently, which the destination page then excludes."""
    now = now or utcnow()
    n24 = session.exec(select(func.count()).select_from(Job).where(_recent(now - timedelta(hours=24)))).one()
    n7 = session.exec(select(func.count()).select_from(Job).where(_recent(now - timedelta(days=7)))).one()
    return n24, n7


def matched_count(session, user_id: int) -> int:
    """How many pool jobs actually passed this user's filters: Applications carrying a 'passed filters'
    (kind='new') event, which assign_jobs writes on every filter match. Manual skip/blocklist/queue actions
    from the all-jobs view also create Application rows for jobs that never matched; those have no 'new'
    event, so counting them (every Application for the user) would overstate the filter-match number."""
    return session.exec(
        select(func.count(func.distinct(Application.id)))
        .join(ApplicationEvent, ApplicationEvent.application_id == Application.id)
        .where(Application.user_id == user_id, ApplicationEvent.kind == "new")
    ).one()


def _pool_stats(session) -> dict:
    """Total pool size and a breakdown by source.

    Prefer the collector side's shared `coverage_stats(session)` helper when it is importable, so the
    two views agree; fall back to a minimal inline computation otherwise, so this page (and its tests)
    never depend on that helper having landed yet. Only the keys we understand are trusted; anything
    else falls back.
    """
    external = _external_stats(session)
    if external is not None:
        total = external.get("total")
        by_source = external.get("by_source")
        if isinstance(total, int) and by_source is not None:
            return {"total": total, "by_source": _as_pairs(by_source)}

    total = session.exec(select(func.count()).select_from(Job)).one()
    by_source = session.exec(
        select(Job.source, func.count()).group_by(Job.source).order_by(func.count().desc())
    ).all()
    return {"total": total, "by_source": [(src or "(unknown)", n) for src, n in by_source]}


def _external_stats(session) -> dict | None:
    """Best-effort call to the shared coverage_stats(session). Imported lazily and guarded so a missing
    helper, an import error or an unexpected return never breaks the page."""
    coverage_stats = None
    for module in ("jobapply.pipeline.collect", "jobapply.pipeline.coverage"):
        try:
            mod = __import__(module, fromlist=["coverage_stats"])
            coverage_stats = getattr(mod, "coverage_stats", None)
            if coverage_stats is not None:
                break
        except Exception:
            continue
    if coverage_stats is None:
        return None
    try:
        result = coverage_stats(session)
        return dict(result) if result else None
    except Exception:
        return None


def _as_pairs(by_source) -> list[tuple[str, int]]:
    """Normalise whatever the helper returned for the source breakdown into sorted (source, count) pairs."""
    items = by_source.items() if isinstance(by_source, dict) else by_source
    pairs = [(str(src or "(unknown)"), int(n)) for src, n in items]
    return sorted(pairs, key=lambda p: p[1], reverse=True)


def sector_counts(session) -> list[tuple[str, int]]:
    """Count pool jobs whose title matches each taxonomy field, so the user sees coverage by sector
    (Software Engineering, Cybersecurity, Data & AI, ...) across all fields. Sorted by count, biggest
    first; fields with no matches are kept so the full taxonomy is visible."""
    from jobapply.roles.taxonomy import all_fields, match_title

    titles = session.exec(select(Job.title)).all()
    out: list[tuple[str, int]] = []
    for fld in all_fields():
        selection = [fld.key]
        out.append((fld.label, sum(1 for t in titles if match_title(t, selection))))
    return sorted(out, key=lambda p: p[1], reverse=True)


@router.get("/coverage")
def coverage(ctx: Ctx = Depends(get_ctx)):
    s, uid = ctx.session, ctx.view.id
    now = utcnow()
    pool = _pool_stats(s)
    total = pool["total"]
    by_source = pool["by_source"]
    new_24h, new_7d = new_counts(s, now)
    matched = matched_count(s, uid)
    sectors = sector_counts(s)
    tiles = [
        ("Jobs in pool", total, "/jobs?scope=all"),
        ("New (24h)", new_24h, "/jobs?scope=all&hours=24"),
        ("New (7d)", new_7d, "/jobs?scope=all&hours=168"),
        ("Match your filters", matched, "/jobs"),
    ]
    return ctx.render(
        "coverage.html",
        tiles=tiles,
        total=total,
        matched=matched,
        sectors=sectors,
        sector_max=max((n for _l, n in sectors), default=0),
        by_source=by_source,
        source_max=max((n for _s, n in by_source), default=0),
    )
