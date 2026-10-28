"""Run collectors, store their jobs and hand new jobs to each user's filters."""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from jobapply.collectors.base import BaseCollector
from jobapply.collectors.registry import build_collectors
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.models import CollectorRun, User, utcnow
from jobapply.pipeline.dedupe import store_jobs
from jobapply.pipeline.filters import assign_jobs

log = get_logger(__name__)


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
    with session_scope(engine) as session:
        users = session.exec(select(User).where(User.is_active)).all()
        return sorted({r for u in users for r in (u.filters or {}).get("roles", [])})


def run_all(only: list[str] | None = None, collectors: list[BaseCollector] | None = None,
            engine: Engine | None = None) -> dict:
    collectors = collectors if collectors is not None else build_collectors(
        extra_queries=user_queries(engine), only=only)
    new_ids: list[int] = []
    for c in collectors:
        new_ids += run_collector(c, engine)
    with session_scope(engine) as session:
        assigned = assign_jobs(session, new_ids)
    return {"collectors": len(collectors), "new_jobs": len(new_ids), "assigned": assigned}
