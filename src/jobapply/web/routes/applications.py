from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from jobapply.config import get_settings
from jobapply.models import Application, AppStatus, Job, MailMessage
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

TRACKED = (AppStatus.dry_run, AppStatus.applied, AppStatus.failed, AppStatus.paused, AppStatus.replied,
           AppStatus.assessment, AppStatus.interview, AppStatus.offer, AppStatus.rejected, AppStatus.approved)


@router.get("/applications")
def applications_page(request: Request, ctx: Ctx = Depends(get_ctx)):
    status = request.query_params.get("status")
    where = [Application.status == status] if status else [Application.status.in_(TRACKED)]
    return ctx.render("applications.html", rows=ctx.scope.applications_with_jobs(*where), status=status,
                      statuses=TRACKED)


@router.get("/applications/{app_id}")
def application_detail(app_id: int, ctx: Ctx = Depends(get_ctx)):
    app = ctx.scope.get(Application, app_id)
    if app is None:
        raise HTTPException(404)
    job = ctx.session.get(Job, app.job_id)
    mails = ctx.scope.mail(MailMessage.application_id == app.id)
    return ctx.render("application_detail.html", app=app, job=job, events=ctx.scope.events(app), mails=mails,
                      statuses=list(AppStatus))


@router.post("/applications/{app_id}")
async def application_update(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    app = ctx.scope.get(Application, app_id)
    if app is None:
        raise HTTPException(404)
    if follow_up := form.get("follow_up_at"):
        app.follow_up_at = datetime.fromisoformat(str(follow_up))
        ctx.session.add(app)
    if (status := form.get("status")) and status != app.status:
        ctx.scope.set_status(app, AppStatus(status), f"set by {ctx.user.username}")
    if note := str(form.get("note") or "").strip():
        from jobapply.models import ApplicationEvent

        ctx.session.add(ApplicationEvent(application_id=app.id, user_id=app.user_id, kind="note", detail=note))
    ctx.session.commit()
    flash(request, "Updated")
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@router.get("/files/screenshot/{app_id}")
def screenshot(app_id: int, ctx: Ctx = Depends(get_ctx)):
    app = ctx.scope.get(Application, app_id)
    if app is None or not app.screenshot_path:
        raise HTTPException(404)
    root = get_settings().screenshots_dir.resolve()
    path = Path(app.screenshot_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
