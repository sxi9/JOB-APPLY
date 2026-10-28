"""Score each user's new applications and decide: queue for applying, or leave for review."""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from jobapply.ai.base import LLM, LLMError
from jobapply.ai.service import get_llm, prepare_application, score_job
from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, User, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)


def score_user(session, user: User, llm: LLM, limit: int | None = None, prepare: bool = True) -> dict[str, int]:
    threshold = int((user.preferences or {}).get("score_threshold") or get_settings().score_threshold)
    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status == AppStatus.new, limit=limit or 500)
    counts = {"scored": 0, "queued": 0, "below": 0, "failed": 0}
    for app, job in rows:
        try:
            result = score_job(session, llm, user, job)
        except LLMError as exc:
            counts["failed"] += 1
            app.error = f"scoring failed: {exc}"[:500]
            app.updated_at = utcnow()
            session.add(app)
            log.warning("scoring failed", user=user.username, job=job.id, error=str(exc), retryable=exc.retryable)
            if not exc.retryable:
                continue
            break  # rate limited or network: stop this run, the rest stays `new` for next time
        app.score, app.score_reason, app.missing_skills = result.score, result.reason, result.missing_skills
        app.error = None
        counts["scored"] += 1
        if result.score >= threshold:
            if prepare:
                try:
                    prepare_application(session, llm, user, app, job)
                except LLMError as exc:
                    log.warning("tailoring failed, queued without it", user=user.username, job=job.id, error=str(exc))
            scope.set_status(app, AppStatus.queued, f"score {result.score} ≥ {threshold}")
            counts["queued"] += 1
        else:
            scope.set_status(app, AppStatus.scored, f"score {result.score} < {threshold}")
            counts["below"] += 1
        session.commit()
    return counts


def score_all(username: str | None = None, engine: Engine | None = None, limit: int | None = None) -> dict:
    out: dict[str, dict[str, int]] = {}
    with session_scope(engine) as session:
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        for user in session.exec(stmt).all():
            try:
                llm = get_llm(session, user)
            except LLMError as exc:
                log.error("no llm for user", user=user.username, error=str(exc))
                continue
            out[user.username] = score_user(session, user, llm, limit=limit)
            log.info("scored", user=user.username, **out[user.username])
    return out
