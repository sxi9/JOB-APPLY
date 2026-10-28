"""Notifier interface. Step 9 adds the WhatsApp HTTP backend; until then alerts are logged so nothing
is lost."""

from __future__ import annotations

from typing import Protocol

from sqlmodel import Session

from jobapply.logs import get_logger
from jobapply.models import User

log = get_logger(__name__)


class Notifier(Protocol):
    def send(self, user: User, kind: str, message: str) -> bool: ...


class LogNotifier:
    def send(self, user: User, kind: str, message: str) -> bool:
        log.info("notify", user=user.username, kind=kind, message=message)
        return True


_notifier: Notifier = LogNotifier()


def set_notifier(n: Notifier) -> None:
    global _notifier
    _notifier = n


def notify(session: Session, user: User, kind: str, message: str) -> bool:
    wanted = (user.preferences or {}).get("alert_types")
    if wanted is not None and kind not in wanted:
        return False
    return _notifier.send(user, kind, message)
