from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlmodel import select

from jobapply.models import Application, AppStatus, Job, JobSource, utcnow
from jobapply.pipeline.filters import UserFilters
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

ACTIONS = {
    "apply": AppStatus.approved,
    "queue": AppStatus.queued,
    "skip": AppStatus.skipped,
    "blocklist": AppStatus.skipped,
}


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
    if hours := p.get("hours"):
        stmt = stmt.where(
            or_(
                Job.posted_at >= utcnow() - timedelta(hours=int(hours)),
                (Job.posted_at.is_(None)) & (Job.first_seen_at >= utcnow() - timedelta(hours=int(hours))),
            )
        )
    if source := p.get("source"):
        stmt = stmt.where(Job.source == source)
    if (min_score := p.get("min_score")) and scope != "all":
        stmt = stmt.where(Application.score >= int(min_score))
    if location := p.get("location"):
        stmt = stmt.where(Job.location.ilike(f"%{location}%"))
    if p.get("remote"):
        stmt = stmt.where(Job.remote)
    if salary := p.get("salary"):
        stmt = stmt.where(Job.salary_max >= float(salary))
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
    page = max(int(p.get("page", 1)), 1)
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
