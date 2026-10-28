"""Inbox sync and follow-ups for one user: read new mail, classify, link, update the tracker, draft
replies, forward what matters, and send scheduled follow-ups."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from jobapply.ai.base import LLM
from jobapply.logs import get_logger
from jobapply.mail.classify import STATUS_FOR, classify_mail, link_application, should_update_status
from jobapply.mail.imap import ImapReader, Mail, MailboxConfig
from jobapply.mail.smtp import SmtpConfig, SmtpSender
from jobapply.models import Application, ApplicationEvent, AppStatus, Job, MailCategory, MailMessage, User, utcnow
from jobapply.notify import notify
from jobapply.repo import UserScope
from jobapply.switches import get_value, set_value
from jobapply.vault import Vault, VaultError

log = get_logger(__name__)

FORWARD_CATEGORIES = {MailCategory.interview, MailCategory.assessment}
FOLLOW_UP_AFTER_DAYS = 7
MAX_FOLLOW_UPS = 2


def mailbox_configs(session: Session, user: User) -> tuple[MailboxConfig | None, SmtpConfig | None]:
    """IMAP and SMTP settings from the user's `mailbox` credentials (kinds imap and smtp). Without an smtp
    entry the imap login is reused with the host's `imap` replaced by `smtp` on port 587."""
    try:
        vault = Vault()
    except VaultError:
        return None, None
    imap = vault.load(session, user.id, "mailbox", "imap")
    smtp = vault.load(session, user.id, "mailbox", "smtp")
    p = user.profile or {}
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    imap_cfg = None
    if imap and imap.get("host"):
        imap_cfg = MailboxConfig(
            host=imap["host"],
            username=imap["username"],
            password=imap.get("password", ""),
            port=int(imap.get("port") or 993),
        )
    if smtp and smtp.get("host"):
        port = int(smtp.get("port") or 587)
        smtp_cfg = SmtpConfig(
            host=smtp["host"],
            username=smtp["username"],
            password=smtp.get("password", ""),
            port=port,
            starttls=port != 465,
            from_name=name,
        )
    elif imap_cfg:
        smtp_cfg = SmtpConfig(
            host=imap_cfg.host.replace("imap", "smtp"),
            username=imap_cfg.username,
            password=imap_cfg.password,
            port=587,
            from_name=name,
        )
    else:
        smtp_cfg = None
    return imap_cfg, smtp_cfg


def _last_sync_key(user: User) -> str:
    return f"inbox_last_sync:{user.id}"


def sync_inbox(
    session: Session,
    user: User,
    reader: ImapReader,
    llm: LLM | None,
    sender: SmtpSender | None = None,
    since_days: int = 14,
    limit: int = 100,
) -> dict[str, int]:
    scope = UserScope(session, user)
    last = get_value(session, _last_sync_key(user))
    since = utcnow() - timedelta(days=since_days)
    if last:
        try:
            from datetime import datetime

            since = max(since, datetime.fromisoformat(last) - timedelta(hours=1))
        except ValueError:
            pass
    counts = {"seen": 0, "new": 0, "linked": 0, "status_changes": 0, "forwarded": 0}
    for mail in reader.recent(since=since, limit=limit):
        counts["seen"] += 1
        if session.exec(
            select(MailMessage).where(MailMessage.user_id == user.id, MailMessage.message_id == mail.message_id)
        ).first():
            continue
        row = ingest_mail(session, user, mail, llm, sender, scope)
        counts["new"] += 1
        counts["linked"] += int(row.application_id is not None)
        counts["status_changes"] += int(getattr(row, "_status_changed", False))
        counts["forwarded"] += int(row.forwarded)
        session.commit()
    set_value(session, _last_sync_key(user), utcnow().isoformat())
    session.commit()
    log.info("inbox synced", user=user.username, **counts)
    return counts


def ingest_mail(
    session: Session, user: User, mail: Mail, llm: LLM | None, sender: SmtpSender | None, scope: UserScope | None = None
) -> MailMessage:
    scope = scope or UserScope(session, user)
    result = classify_mail(session, llm, user, mail)
    category = MailCategory(result.category)
    app = None
    if category not in (MailCategory.spam, MailCategory.verification):
        app = link_application(session, user, mail, result.company, result.job_title)
    row = MailMessage(
        user_id=user.id,
        application_id=app.id if app else None,
        message_id=mail.message_id,
        sender=f"{mail.sender_name} <{mail.sender}>" if mail.sender_name else mail.sender,
        subject=mail.subject,
        body=mail.body[:20000],
        received_at=mail.received_at,
        category=category,
        draft_reply=result.draft_reply or "",
    )
    session.add(row)
    session.flush()
    row._status_changed = False  # type: ignore[attr-defined]
    if app is not None:
        new_status = STATUS_FOR.get(category)
        if new_status and should_update_status(app.status, new_status):
            scope.set_status(app, new_status, f"mail from {mail.sender}: {mail.subject[:80]}")
            row._status_changed = True  # type: ignore[attr-defined]
        else:
            session.add(
                ApplicationEvent(
                    application_id=app.id,
                    user_id=user.id,
                    kind="reply",
                    detail=f"{category.value}: {mail.subject[:80]}",
                )
            )
        if category in (MailCategory.interview, MailCategory.assessment, MailCategory.recruiter_question):
            app.follow_up_at = None  # they replied, no chaser needed
            session.add(app)
    if category in FORWARD_CATEGORIES or (category == MailCategory.recruiter_question and app is not None):
        job = session.get(Job, app.job_id) if app else None
        title = f"{job.title} at {job.company}" if job else mail.subject
        notify(session, user, category.value, f"{category.value.title()}: {title} — {result.summary or mail.subject}")
        row.forwarded = forward_mail(session, user, mail, sender)
    session.add(row)
    return row


def forward_mail(session: Session, user: User, mail: Mail, sender: SmtpSender | None) -> bool:
    to = (user.preferences or {}).get("forward_email")
    if not to or sender is None:
        return False
    body = (
        f"Forwarded by JOB-APPLY\nFrom: {mail.sender_name} <{mail.sender}>\nDate: {mail.received_at:%Y-%m-%d %H:%M} UTC\n"  # noqa: E501
        f"Subject: {mail.subject}\n\n{mail.body}"
    )
    try:
        sender.send(to, f"Fwd: {mail.subject}", body)
        return True
    except Exception as exc:
        log.warning("forward failed", user=user.username, error=str(exc))
        return False


def send_reply(session: Session, user: User, row: MailMessage, body: str, sender: SmtpSender) -> str:
    to = row.sender
    if "<" in to:
        to = to.split("<", 1)[1].rstrip(">")
    subject = row.subject if row.subject.lower().startswith("re:") else f"Re: {row.subject}"
    message_id = sender.send(to, subject, body, in_reply_to=row.message_id)
    if row.application_id:
        app = session.get(Application, row.application_id)
        if app:
            app.answers = {**(app.answers or {}), "_last_message_id": message_id}
            session.add(
                ApplicationEvent(
                    application_id=app.id, user_id=user.id, kind="reply_sent", detail=f"to {to}: {subject[:80]}"
                )
            )
            session.add(app)
    row.draft_reply = body
    session.add(row)
    return message_id


FOLLOW_UP_TEMPLATE = """Hi,

I applied for the {title} role at {company} on {date} and wanted to check in on the status of my application.
I remain very interested in the role and would be glad to share anything else that would help.

Best regards,
{name}"""


def due_follow_ups(session: Session, user: User) -> list[tuple[Application, Job, MailMessage | None]]:
    now = utcnow()
    rows = session.exec(
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(
            Application.user_id == user.id,
            Application.status == AppStatus.applied,
            Application.follow_up_at.isnot(None),
            Application.follow_up_at <= now,
        )
    ).all()
    out = []
    for app, job in rows:
        mail = session.exec(
            select(MailMessage).where(MailMessage.application_id == app.id).order_by(MailMessage.received_at.desc())
        ).first()
        out.append((app, job, mail))
    return out


def send_follow_ups(session: Session, user: User, sender: SmtpSender | None) -> dict[str, int]:
    """Chase applications that went quiet. Only when there is someone to write to (a recruiter who mailed
    us); ATS-only applications get an event and the user a reminder instead."""
    counts = {"sent": 0, "reminded": 0, "skipped": 0}
    name = f"{(user.profile or {}).get('first_name', '')} {(user.profile or {}).get('last_name', '')}".strip()
    for app, job, mail in due_follow_ups(session, user):
        sent_count = int((app.answers or {}).get("_follow_ups", 0))
        if sent_count >= MAX_FOLLOW_UPS:
            app.follow_up_at = None
            session.add(app)
            counts["skipped"] += 1
            continue
        if mail is not None and sender is not None and "@" in mail.sender:
            body = FOLLOW_UP_TEMPLATE.format(
                title=job.title,
                company=job.company,
                name=name or user.username,
                date=(app.submitted_at or app.created_at).strftime("%d %b"),
            )
            try:
                send_reply(session, user, mail, body, sender)
            except Exception as exc:
                log.warning("follow-up failed", user=user.username, application=app.id, error=str(exc))
                counts["skipped"] += 1
                continue
            session.add(
                ApplicationEvent(
                    application_id=app.id, user_id=user.id, kind="follow_up", detail=f"follow-up {sent_count + 1} sent"
                )
            )
            counts["sent"] += 1
        else:
            session.add(
                ApplicationEvent(
                    application_id=app.id,
                    user_id=user.id,
                    kind="follow_up",
                    detail="follow-up due; no recruiter contact, check the ATS portal",
                )
            )
            notify(session, user, "daily_summary", f"Follow up on {job.title} at {job.company} ({job.apply_url})")
            counts["reminded"] += 1
        app.answers = {**(app.answers or {}), "_follow_ups": sent_count + 1}
        app.follow_up_at = utcnow() + timedelta(days=FOLLOW_UP_AFTER_DAYS)
        session.add(app)
    session.commit()
    return counts
