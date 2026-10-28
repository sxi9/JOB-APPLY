from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import select

from jobapply.models import Application, ApplicationEvent, AppStatus, CollectorRun, Job, MailMessage, utcnow
from jobapply.web.deps import Ctx, get_ctx

router = APIRouter()

FUNNEL = [
    ("found", None),
    ("matched", {AppStatus.queued, AppStatus.approved, AppStatus.dry_run, AppStatus.applied, AppStatus.replied,
                 AppStatus.assessment, AppStatus.interview, AppStatus.offer, AppStatus.rejected, AppStatus.paused}),
    ("applied", {AppStatus.applied, AppStatus.replied, AppStatus.assessment, AppStatus.interview, AppStatus.offer,
                 AppStatus.rejected}),
    ("replied", {AppStatus.replied, AppStatus.assessment, AppStatus.interview, AppStatus.offer, AppStatus.rejected}),
    ("interview", {AppStatus.interview, AppStatus.offer}),
    ("offer", {AppStatus.offer}),
]
STALE_AFTER = timedelta(hours=3)


def collector_health(session) -> list[dict]:
    latest = (select(CollectorRun.source, func.max(CollectorRun.id).label("id"))
              .group_by(CollectorRun.source).subquery())
    runs = session.exec(select(CollectorRun).join(latest, CollectorRun.id == latest.c.id)
                        .order_by(CollectorRun.source)).all()
    now = utcnow()
    return [{"run": r, "healthy": r.ok and r.started_at > now - STALE_AFTER} for r in runs]


@router.get("/")
def home(ctx: Ctx = Depends(get_ctx)):
    s, uid = ctx.session, ctx.view.id
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(s.exec(select(Application.status, func.count()).where(Application.user_id == uid)
                         .group_by(Application.status)).all())
    total = sum(counts.values())
    applied_today = s.exec(select(func.count()).select_from(Application).where(
        Application.user_id == uid, Application.submitted_at >= today)).one()
    new_today = s.exec(select(func.count()).select_from(Application).where(
        Application.user_id == uid, Application.created_at >= today)).one()
    replies = s.exec(select(func.count()).select_from(MailMessage).where(
        MailMessage.user_id == uid, MailMessage.category != "spam", MailMessage.received_at >= today - timedelta(days=7)
    )).one()
    funnel = [(name, total if statuses is None else sum(counts.get(st, 0) for st in statuses))
              for name, statuses in FUNNEL]
    events = s.exec(
        select(ApplicationEvent, Application, Job)
        .join(Application, Application.id == ApplicationEvent.application_id)
        .join(Job, Job.id == Application.job_id)
        .where(ApplicationEvent.user_id == uid)
        .order_by(ApplicationEvent.created_at.desc()).limit(15)
    ).all()
    cap = int((ctx.view.preferences or {}).get("daily_cap", 40))
    tiles = [
        ("New today", new_today, "/jobs?hours=24"),
        ("Queued", counts.get(AppStatus.queued, 0) + counts.get(AppStatus.paused, 0), "/queue"),
        ("Applied today", f"{applied_today} / {cap}", "/applications"),
        ("Replies (7d)", replies, "/inbox"),
        ("Interviews", counts.get(AppStatus.interview, 0), "/applications?status=interview"),
    ]
    return ctx.render("home.html", tiles=tiles, funnel=funnel, funnel_max=max(total, 1), events=events,
                      health=collector_health(s))
