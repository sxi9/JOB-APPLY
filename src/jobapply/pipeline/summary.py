"""Daily summary per user: what happened today, sent through the notifier."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, MailMessage, User, utcnow
from jobapply.notify import notify

log = get_logger(__name__)


def summary_for(session: Session, user: User) -> str:
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    uid = user.id

    def count(*where) -> int:
        return session.exec(
            select(func.count()).select_from(Application).where(Application.user_id == uid, *where)
        ).one()

    new = count(Application.created_at >= today)
    applied = count(Application.submitted_at >= today)
    queued = count(Application.status.in_([AppStatus.queued, AppStatus.dry_run]))
    paused = count(Application.status == AppStatus.paused)
    replies = session.exec(
        select(func.count())
        .select_from(MailMessage)
        .where(
            MailMessage.user_id == uid,
            MailMessage.received_at >= today - timedelta(days=1),
            MailMessage.category.in_(["interview", "assessment", "recruiter_question", "rejection"]),
        )
    ).one()
    interviews = count(Application.status == AppStatus.interview)
    return (
        f"Today: {new} new matches, {applied} applied, {queued} waiting in the queue, {paused} paused for you, "
        f"{replies} replies in the last day, {interviews} interviews in progress."
    )


def send_summaries(username: str | None = None, engine: Engine | None = None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    with session_scope(engine) as session:
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        for user in session.exec(stmt).all():
            out[user.username] = notify(session, user, "daily_summary", summary_for(session, user))
    return out
