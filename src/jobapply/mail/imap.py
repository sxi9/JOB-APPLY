"""IMAP reader over the standard library, with the connection injectable so tests use a fake server.

The client only needs the imaplib subset used here: login, select, search, fetch, store, logout.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message as EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from jobapply.collectors.base import html_to_text
from jobapply.logs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class MailboxConfig:
    host: str
    username: str
    password: str
    port: int = 993
    ssl: bool = True
    folder: str = "INBOX"


@dataclass
class Mail:
    uid: str
    message_id: str
    sender: str
    sender_name: str
    subject: str
    body: str
    html: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    to: str = ""
    in_reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # malformed header
        return value


def _body_parts(msg: EmailMessage) -> tuple[str, str]:
    text, html = "", ""
    parts: Iterable[EmailMessage] = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if part.get("Content-Disposition", "").startswith("attachment"):
            continue
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain" and not text.strip():
            text = decoded
        elif ctype == "text/html" and not html:
            html = decoded
    if not text.strip() and html:
        text = html_to_text(html)
    return text.strip(), html


def parse_message(uid: str, raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_decode(msg.get("From")))
    try:
        when = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else datetime.now(UTC)
        when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        when = datetime.now(UTC)
    text, html = _body_parts(msg)
    return Mail(
        uid=uid,
        message_id=(msg.get("Message-ID") or f"<uid-{uid}@local>").strip(),
        sender=addr.lower(),
        sender_name=name,
        subject=_decode(msg.get("Subject")),
        body=text,
        html=html,
        received_at=when,
        to=_decode(msg.get("To")),
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        headers={k: _decode(v) for k, v in msg.items() if k.lower() in ("list-unsubscribe", "reply-to", "x-mailer")},
    )


ClientFactory = Callable[[MailboxConfig], Any]


def _default_client(cfg: MailboxConfig) -> Any:
    client = imaplib.IMAP4_SSL(cfg.host, cfg.port) if cfg.ssl else imaplib.IMAP4(cfg.host, cfg.port)
    client.login(cfg.username, cfg.password)
    return client


class ImapReader:
    def __init__(self, cfg: MailboxConfig, client_factory: ClientFactory | None = None):
        self.cfg = cfg
        self._factory = client_factory or _default_client
        self._client: Any = None

    def _conn(self) -> Any:
        if self._client is None:
            self._client = self._factory(self.cfg)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def _search(self, criteria: list[str]) -> list[str]:
        conn = self._conn()
        conn.select(self.cfg.folder)
        status, data = conn.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return [u.decode() if isinstance(u, bytes) else str(u) for u in data[0].split()]

    def fetch(self, uids: list[str]) -> list[Mail]:
        conn = self._conn()
        out: list[Mail] = []
        for uid in uids:
            status, data = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            try:
                out.append(parse_message(uid, data[0][1]))
            except Exception as exc:  # one broken message must not stop the sync
                log.warning("cannot parse mail", uid=uid, error=str(exc))
        return out

    def recent(self, since: datetime | None = None, limit: int = 100, unseen_only: bool = False) -> list[Mail]:
        criteria = ["UNSEEN"] if unseen_only else ["ALL"]
        if since is not None:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        uids = self._search(criteria)[-limit:]
        mails = self.fetch(uids)
        if since is not None:
            mails = [m for m in mails if m.received_at >= since - timedelta(minutes=5)]
        return sorted(mails, key=lambda m: m.received_at)

    def mark_seen(self, uids: list[str]) -> None:
        conn = self._conn()
        for uid in uids:
            conn.store(uid, "+FLAGS", "\\Seen")

    def wait_for(
        self, predicate: Callable[[Mail], bool], timeout: float = 120, poll: float = 5, since: datetime | None = None
    ) -> Mail | None:
        """Poll until a mail matching `predicate` arrives (OTP codes, verification links)."""
        deadline = time.monotonic() + timeout
        since = since or datetime.now(UTC) - timedelta(minutes=10)
        while True:
            for m in reversed(self.recent(since=since, limit=30)):
                if predicate(m):
                    return m
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)


def sender_matches(mail: Mail, pattern: str) -> bool:
    """`pattern` is a domain, an address, or a regex (when it contains regex metacharacters)."""
    pattern = pattern.lower().strip()
    if not pattern:
        return True
    if re.search(r"[\\^$*+?()\[\]|]", pattern):
        return (
            re.search(pattern, mail.sender, re.I) is not None or re.search(pattern, mail.sender_name, re.I) is not None
        )
    return mail.sender == pattern or mail.sender.endswith("@" + pattern) or mail.sender.endswith("." + pattern)
