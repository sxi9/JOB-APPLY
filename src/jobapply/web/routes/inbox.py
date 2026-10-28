from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.models import Application, Job, MailCategory, MailMessage
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()


@router.get("/inbox")
def inbox_page(request: Request, ctx: Ctx = Depends(get_ctx)):
    category = request.query_params.get("category")
    where = [MailMessage.category == category] if category else [MailMessage.category != MailCategory.spam]
    return ctx.render("inbox.html", mails=ctx.scope.mail(*where), category=category, categories=list(MailCategory))


@router.get("/inbox/{mail_id}")
def mail_detail(mail_id: int, ctx: Ctx = Depends(get_ctx)):
    mail = ctx.scope.get(MailMessage, mail_id)
    if mail is None:
        raise HTTPException(404)
    app = ctx.scope.get(Application, mail.application_id) if mail.application_id else None
    job = ctx.session.get(Job, app.job_id) if app else None
    return ctx.render("mail_detail.html", mail=mail, app=app, job=job, categories=list(MailCategory))


@router.post("/inbox/{mail_id}")
async def mail_update(mail_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    mail = ctx.scope.get(MailMessage, mail_id)
    if mail is None:
        raise HTTPException(404)
    if category := form.get("category"):
        mail.category = MailCategory(category)
    if "draft_reply" in form:
        mail.draft_reply = str(form["draft_reply"])
    ctx.session.add(mail)
    ctx.session.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/inbox/{mail_id}", status_code=303)
