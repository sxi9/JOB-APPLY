"""Mail runs for every user with a configured mailbox: inbox sync and follow-ups."""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import select

from jobapply.ai.base import LLMError
from jobapply.ai.service import get_llm
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.mail.imap import ImapReader
from jobapply.mail.inbox import mailbox_configs, send_follow_ups, sync_inbox
from jobapply.mail.smtp import SmtpSender
from jobapply.models import User

log = get_logger(__name__)


def _users(session, username: str | None):
    stmt = select(User).where(User.is_active)
    if username:
        stmt = stmt.where(User.username == username)
    return session.exec(stmt).all()


def sync_all(username: str | None = None, engine: Engine | None = None, limit: int = 100) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with session_scope(engine) as session:
        for user in _users(session, username):
            imap_cfg, smtp_cfg = mailbox_configs(session, user)
            if imap_cfg is None:
                log.debug("no mailbox configured", user=user.username)
                continue
            try:
                llm = get_llm(session, user)
            except LLMError as exc:
                log.warning("no llm for inbox, rules only", user=user.username, error=str(exc))
                llm = None
            reader = ImapReader(imap_cfg)
            sender = SmtpSender(smtp_cfg) if smtp_cfg else None
            try:
                out[user.username] = sync_inbox(session, user, reader, llm, sender, limit=limit)
            except Exception as exc:  # a broken mailbox must not stop the other users
                log.error("inbox sync failed", user=user.username, error=f"{type(exc).__name__}: {exc}")
                out[user.username] = {"error": str(exc)[:200]}
            finally:
                reader.close()
    return out


def follow_ups_all(username: str | None = None, engine: Engine | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with session_scope(engine) as session:
        for user in _users(session, username):
            _imap_cfg, smtp_cfg = mailbox_configs(session, user)
            sender = SmtpSender(smtp_cfg) if smtp_cfg else None
            out[user.username] = send_follow_ups(session, user, sender)
    return out
