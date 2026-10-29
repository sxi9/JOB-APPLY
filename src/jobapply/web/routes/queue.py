from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.models import Application, AppStatus, utcnow
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

REVIEW_STATUSES = (AppStatus.queued, AppStatus.dry_run, AppStatus.paused)


@router.get("/queue")
def queue_page(ctx: Ctx = Depends(get_ctx)):
    rows = ctx.scope.applications_with_jobs(Application.status.in_(REVIEW_STATUSES))
    rows.sort(key=lambda r: (r[0].status != AppStatus.paused, -(r[0].score or 0)))
    return ctx.render("queue.html", rows=rows)


@router.post("/queue/{app_id}")
async def queue_decide(app_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    app = ctx.scope.get(Application, app_id)
    if app is None:
        raise HTTPException(404)
    answers = {k.removeprefix("answer__"): v for k, v in form.items() if k.startswith("answer__")}
    if answers:
        app.answers = {**(app.answers or {}), **answers}
    if "cover_letter" in form:
        app.cover_letter = str(form["cover_letter"])
    if "message" in form:
        app.message = str(form["message"])
    app.updated_at = utcnow()
    decision = form.get("decision")
    if decision == "approve":
        ctx.scope.set_status(app, AppStatus.approved, f"approved by {ctx.user.username}")
        message = "Approved. It will be submitted on the next apply run."
    elif decision == "reject":
        ctx.scope.set_status(app, AppStatus.skipped, f"rejected by {ctx.user.username}")
        message = "Rejected."
    else:
        ctx.session.add(app)
        message = "Saved."
    ctx.session.commit()
    if request.headers.get("hx-request"):
        return ctx.render("_queue_done.html", app=app, message=message)
    flash(request, message)
    return RedirectResponse("/queue", status_code=303)
