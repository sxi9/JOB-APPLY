"""SMTP sender over the standard library, connection injectable for tests."""

from __future__ import annotations

import mimetypes
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from jobapply.logs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    username: str
    password: str
    port: int = 587
    starttls: bool = True
    from_name: str = ""


SmtpFactory = Callable[[SmtpConfig], Any]


def _default_client(cfg: SmtpConfig) -> Any:
    if cfg.port == 465 and not cfg.starttls:
        client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30)
    else:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
        client.ehlo()
        if cfg.starttls:
            client.starttls()
            client.ehlo()
    if cfg.username:
        client.login(cfg.username, cfg.password)
    return client


class SmtpSender:
    def __init__(self, cfg: SmtpConfig, client_factory: SmtpFactory | None = None):
        self.cfg = cfg
        self._factory = client_factory or _default_client

    def build(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        attachments: list[Path] | None = None,
        cc: str = "",
    ) -> EmailMessage:
        msg = EmailMessage()
        sender = f"{self.cfg.from_name} <{self.cfg.username}>" if self.cfg.from_name else self.cfg.username
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject
        msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
        if cc:
            msg["Cc"] = cc
        if in_reply_to:
            msg["In-Reply-To"] = msg["References"] = in_reply_to
        msg.set_content(body)
        for path in attachments or []:
            ctype, _ = mimetypes.guess_type(str(path))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype, filename=Path(path).name)
        return msg

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        attachments: list[Path] | None = None,
        cc: str = "",
    ) -> str:
        """Send and return the Message-ID."""
        msg = self.build(to, subject, body, in_reply_to, attachments, cc)
        client = self._factory(self.cfg)
        try:
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        log.info("mail sent", to=to, subject=subject[:60])
        return msg["Message-ID"]
