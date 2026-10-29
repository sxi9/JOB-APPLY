"""Score each user's new applications and decide: queue for applying, or leave for review.

Works with or without an LLM. When no provider has a key, the user set `ai_mode=off`, or the failover
chain is exhausted mid-run, the remaining jobs are scored by `jobapply.ai.heuristics` and the reason is
prefixed `[heuristic]` so the dashboard shows it. See docs/NO_AI_MODE.md.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from jobapply.ai.base import LLM, LLMError
from jobapply.ai.service import ai_mode, get_llm, prepare_application, score_job, score_job_fallback
from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, User, utcnow
from jobapply.repo import UserScope

log = get_logger(__name__)


def score_user(session, user: User, llm: LLM | None, limit: int | None = None, prepare: bool = True) -> dict[str, int]:
    """Score this user's `new` applications. `llm=None` means heuristics only."""
    threshold = int((user.preferences or {}).get("score_threshold") or get_settings().score_threshold)
    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status == AppStatus.new, limit=limit or 500)
    counts = {"scored": 0, "queued": 0, "below": 0, "failed": 0}
    heuristic = 0
    if llm is None and rows:
        log.info("no-AI mode: scoring with heuristics", user=user.username, reason=_no_ai_reason(user))
    for app, job in rows:
        result = None
        if llm is not None:
            try:
                result = score_job(session, llm, user, job)
            except LLMError as exc:
                log.warning("scoring failed", user=user.username, job=job.id, error=str(exc), retryable=exc.retryable)
                if not exc.retryable:
                    counts["failed"] += 1
                    app.error = f"scoring failed: {exc}"[:500]
                    app.updated_at = utcnow()
                    session.add(app)
                    session.commit()
                    continue
                # Rate limited, out of credits or network: the chain is exhausted for now. The rest of
                # this run is scored by heuristics so nothing waits on a provider.
                llm = None
                log.info("no-AI mode: LLM chain exhausted, scoring the rest with heuristics", user=user.username,
                         error=str(exc))
        if result is None:
            result = score_job_fallback(user, job)
            heuristic += 1
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
    if heuristic:
        log.info("scored without AI", user=user.username, heuristic=heuristic, total=counts["scored"])
    return counts


def _no_ai_reason(user: User) -> str:
    return "ai_mode=off" if ai_mode(user) == "off" else "no LLM provider configured"


def llm_or_none(session, user: User) -> LLM | None:
    """The user's LLM, or None when there is none to use (no key, `ai_mode=off`)."""
    try:
        return get_llm(session, user)
    except LLMError as exc:
        log.info("no llm for user, heuristics only", user=user.username, error=str(exc))
        return None


def score_all(username: str | None = None, engine: Engine | None = None, limit: int | None = None) -> dict:
    out: dict[str, dict[str, int]] = {}
    with session_scope(engine) as session:
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        for user in session.exec(stmt).all():
            llm = llm_or_none(session, user)
            out[user.username] = score_user(session, user, llm, limit=limit)
            log.info("scored", user=user.username, **out[user.username])
    return out
