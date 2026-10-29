from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlmodel import select

from jobapply.models import Application, AppStatus, Job, JobSource, utcnow
from jobapply.pipeline.filters import EXACT_TIMESTAMP_SOURCES, UserFilters
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

ACTIONS = {
    "apply": AppStatus.approved,
    "queue": AppStatus.queued,
    "skip": AppStatus.skipped,
    "blocklist": AppStatus.skipped,
}

# "Posted within" presets, in minutes. The first three are sub-hour.
AGE_PRESETS = [(10, "10 min"), (30, "30 min"), (60, "1 hour"), (360, "6 hours"),
               (1440, "24 hours"), (4320, "3 days"), (10080, "7 days")]
FRESH_MINUTES = 60


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def rel_age(value: datetime | None) -> str:
    """Relative posting age for the jobs table: '7 min ago', '2 h ago', '3 d ago'."""
    value = _aware(value)
    if value is None:
        return "—"
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 0:
        return "soon"
    minutes = seconds // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 1440:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def is_fresh(value: datetime | None) -> bool:
    value = _aware(value)
    return value is not None and datetime.now(UTC) - value < timedelta(minutes=FRESH_MINUTES)


def _int(value: str | None) -> int | None:
    """Parse a query-string number, returning None for missing/blank/non-numeric so a crafted value such as
    ?within=abc skips the filter instead of raising a 500."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _query(ctx: Ctx, request: Request):
    p = request.query_params
    scope = p.get("scope", "mine")
    if scope == "all":
        stmt = select(Job, Application).outerjoin(
            Application, (Application.job_id == Job.id) & (Application.user_id == ctx.view.id)
        )
    else:
        stmt = (
            select(Job, Application)
            .join(Application, Application.job_id == Job.id)
            .where(Application.user_id == ctx.view.id)
        )
    def _recent(cut):
        return or_(Job.posted_at >= cut, (Job.posted_at.is_(None)) & (Job.first_seen_at >= cut))

    if (minutes := _int(p.get("within"))) is not None and minutes > 0:
        # Minute-level recency with posted-time trust: the window is applied to the minute only for
        # exact-timestamp sources (ATS + LinkedIn); coarse-timestamp sources fall back to whole-hour
        # granularity (ceil, min 1 h) so a minutes filter never hides them wrongly. Applied for every
        # window, not just sub-hour ones, so this live path matches pipeline.filters.age_cutoff for
        # non-preset values (e.g. ?within=75) too.
        now = utcnow()
        exact_cut = now - timedelta(minutes=minutes)
        coarse_cut = now - timedelta(hours=max(1, ceil(minutes / 60)))
        exact_src = Job.source.in_(EXACT_TIMESTAMP_SOURCES)
        stmt = stmt.where(or_(exact_src & _recent(exact_cut), ~exact_src & _recent(coarse_cut)))
    elif (hours := _int(p.get("hours"))) is not None and hours > 0:
        stmt = stmt.where(_recent(utcnow() - timedelta(hours=hours)))
    if source := p.get("source"):
        stmt = stmt.where(Job.source == source)
    if (min_score := _int(p.get("min_score"))) is not None and scope != "all":
        stmt = stmt.where(Application.score >= min_score)
    if location := p.get("location"):
        stmt = stmt.where(Job.location.ilike(f"%{location}%"))
    if p.get("remote"):
        stmt = stmt.where(Job.remote)
    if (salary := _float(p.get("salary"))) is not None:
        stmt = stmt.where(Job.salary_max >= salary)
    if company := p.get("company"):
        stmt = stmt.where(Job.company.ilike(f"%{company}%"))
    if q := p.get("q"):
        stmt = stmt.where(or_(Job.title.ilike(f"%{q}%"), Job.description.ilike(f"%{q}%")))
    if (status := p.get("status")) and scope != "all":
        stmt = stmt.where(Application.status == status)
    order = p.get("sort", "posted")
    if order == "score" and scope != "all":
        stmt = stmt.order_by(Application.score.desc().nulls_last(), Job.posted_at.desc())
    else:
        stmt = stmt.order_by(Job.posted_at.desc().nulls_last(), Job.first_seen_at.desc())
    page = max(_int(p.get("page")) or 1, 1)
    return stmt.offset((page - 1) * 50).limit(51), page


@router.get("/jobs")
def jobs_page(request: Request, ctx: Ctx = Depends(get_ctx)):
    stmt, page = _query(ctx, request)
    rows = ctx.session.exec(stmt).all()
    sources = ctx.session.exec(select(Job.source).distinct().order_by(Job.source)).all()
    template = (
        "_job_rows.html"
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "job-rows"
        else "jobs.html"
    )
    return ctx.render(
        template,
        rows=rows[:50],
        has_more=len(rows) > 50,
        page=page,
        sources=sources,
        params=dict(request.query_params),
        statuses=list(AppStatus),
        age_presets=AGE_PRESETS,
        rel_age=rel_age,
        is_fresh=is_fresh,
    )


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, ctx: Ctx = Depends(get_ctx)):
    job = ctx.session.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    app = ctx.scope.application_for(job_id)
    sources = ctx.session.exec(select(JobSource).where(JobSource.job_id == job_id)).all()
    return ctx.render("job_detail.html", job=job, app=app, sources=sources, events=ctx.scope.events(app) if app else [])


@router.post("/jobs/{job_id}/action")
async def job_action(job_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    action = form.get("action")
    job = ctx.session.get(Job, job_id)
    if not job or action not in ACTIONS:
        raise HTTPException(400)
    app = ctx.scope.application_for(job_id)
    if app is None:
        app = Application(user_id=ctx.view.id, job_id=job_id)
        ctx.session.add(app)
        ctx.session.flush()
    if action == "blocklist":
        filters = UserFilters.model_validate(ctx.view.filters or {})
        if job.company not in filters.company_blocklist:
            filters.company_blocklist.append(job.company)
        ctx.view.filters = filters.model_dump()
        ctx.session.add(ctx.view)
    ctx.scope.set_status(app, ACTIONS[action], f"{action} from dashboard by {ctx.user.username}")
    ctx.session.commit()
    message = {
        "apply": "Approved, applies on the next run",
        "queue": "Added to the review queue",
        "skip": "Skipped",
        "blocklist": f"Skipped and blocklisted {job.company}",
    }[action]
    if request.headers.get("hx-request"):
        return ctx.render("_status_chip.html", app=app, message=message)
    flash(request, message)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
