from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.mail.imap import Mail
from jobapply.mail.inbox import forward_mail, mailbox_configs, send_reply
from jobapply.mail.smtp import SmtpSender
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
    _imap, smtp = mailbox_configs(ctx.session, ctx.view)
    return ctx.render(
        "mail_detail.html",
        mail=mail,
        app=app,
        job=job,
        categories=list(MailCategory),
        can_send=smtp is not None,
        forward_to=(ctx.view.preferences or {}).get("forward_email", ""),
    )


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


def _sender(ctx: Ctx) -> SmtpSender | None:
    _imap, smtp = mailbox_configs(ctx.session, ctx.view)
    return SmtpSender(smtp) if smtp else None


@router.post("/inbox/{mail_id}/send")
async def mail_send(mail_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    mail = ctx.scope.get(MailMessage, mail_id)
    if mail is None:
        raise HTTPException(404)
    body = str(form.get("draft_reply") or "").strip()
    sender = _sender(ctx)
    if not body or sender is None:
        flash(request, "Write a reply and save SMTP credentials for site `mailbox` first", "error")
        return RedirectResponse(f"/inbox/{mail_id}", status_code=303)
    try:
        send_reply(ctx.session, ctx.view, mail, body, sender)
    except Exception as exc:
        flash(request, f"Sending failed: {exc}", "error")
        return RedirectResponse(f"/inbox/{mail_id}", status_code=303)
    ctx.session.commit()
    flash(request, "Reply sent")
    return RedirectResponse(f"/inbox/{mail_id}", status_code=303)


@router.post("/inbox/{mail_id}/forward")
async def mail_forward(mail_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    row = ctx.scope.get(MailMessage, mail_id)
    if row is None:
        raise HTTPException(404)
    sender_addr = row.sender.split("<", 1)[1].rstrip(">") if "<" in row.sender else row.sender
    mail = Mail(
        uid="",
        message_id=row.message_id,
        sender=sender_addr,
        sender_name=row.sender.split("<")[0].strip(),
        subject=row.subject,
        body=row.body,
        received_at=row.received_at,
    )
    ok = forward_mail(ctx.session, ctx.view, mail, _sender(ctx))
    if ok:
        row.forwarded = True
        ctx.session.add(row)
        ctx.session.commit()
        flash(request, "Forwarded")
    else:
        flash(
            request, "Set a forward address under Settings → Notifications and SMTP credentials for `mailbox`", "error"
        )
    return RedirectResponse(f"/inbox/{mail_id}", status_code=303)
