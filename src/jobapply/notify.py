"""Notifier interface with a WhatsApp HTTP backend.

The WhatsApp backend posts to the existing bridge: `POST {JOBAPPLY_WHATSAPP_BASE_URL}/send` with
`{"to": "<number>", "message": "<text>"}` and `Authorization: Bearer <JOBAPPLY_WHATSAPP_TOKEN>`.
Alerts are also logged; a user's `alert_types` preference decides which kinds reach them.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from sqlmodel import Session

from jobapply.config import Settings, get_settings
from jobapply.logs import get_logger
from jobapply.models import User

log = get_logger(__name__)

ALERT_KINDS = ("interview", "assessment", "recruiter_question", "paused", "failed", "daily_summary")


class Notifier(Protocol):
    def send(self, user: User, kind: str, message: str) -> bool: ...


class LogNotifier:
    def send(self, user: User, kind: str, message: str) -> bool:
        log.info("notify", user=user.username, kind=kind, message=message)
        return True


class WhatsAppNotifier:
    def __init__(self, base_url: str, token: str | None = None, client: httpx.Client | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(timeout=timeout, headers=headers)
        self._headers = headers

    def send(self, user: User, kind: str, message: str) -> bool:
        number = str((user.preferences or {}).get("whatsapp_number") or "").strip()
        if not number:
            log.debug("no whatsapp number", user=user.username)
            return False
        try:
            resp = self.client.post(f"{self.base_url}/send", json={"to": number, "message": f"[JOB-APPLY] {message}"},
                                    headers=self._headers)
        except httpx.HTTPError as exc:
            log.warning("whatsapp send failed", user=user.username, error=str(exc))
            return False
        if resp.status_code >= 300:
            log.warning("whatsapp bridge error", user=user.username, status=resp.status_code, body=resp.text[:200])
            return False
        return True


class MultiNotifier:
    """Fan out to several backends; succeeds when any of them does."""

    def __init__(self, *backends: Notifier):
        self.backends = list(backends)

    def send(self, user: User, kind: str, message: str) -> bool:
        return any([b.send(user, kind, message) for b in self.backends])


def build_notifier(settings: Settings | None = None) -> Notifier:
    s = settings or get_settings()
    if s.whatsapp_base_url:
        token = s.whatsapp_token.get_secret_value() if s.whatsapp_token else None
        return MultiNotifier(LogNotifier(), WhatsAppNotifier(s.whatsapp_base_url, token))
    return LogNotifier()


_notifier: Notifier | None = None


def set_notifier(n: Notifier | None) -> None:
    global _notifier
    _notifier = n


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = build_notifier()
    return _notifier


def notify(session: Session, user: User, kind: str, message: str) -> bool:
    wanted = (user.preferences or {}).get("alert_types")
    if wanted is not None and kind not in wanted:
        return False
    return get_notifier().send(user, kind, message)
