"""Notifier interface with a WhatsApp HTTP backend.

The WhatsApp backend posts to the existing bridge: `POST {JOBAPPLY_WHATSAPP_BASE_URL}/send` with
`{"to": "<number>", "text": "<text>"}` and `Authorization: Bearer <JOBAPPLY_WHATSAPP_TOKEN>`
(the bridge rejects any other field name with HTTP 400 "to and text are required").
Alerts are also logged; a user's `alert_types` preference decides which kinds reach them.

Targets. `send(user, kind, message)` goes to the user's `whatsapp_number` with a `[JOB-APPLY]` prefix (the
historical alerts). `send_text(to, text)` posts a ready-made text to one direct target: a number or a group
jid (`120363...@g.us`). `alert_targets(user)` resolves the user's preferences (`whatsapp_number`,
`whatsapp_group_jid`, `alerts_target` in number | group | both, default number) into that list of targets.
`list_groups()` (GET /groups, cached 10 minutes) and `bridge_health()` (GET /health) read the bridge for the
Settings page; both fail soft (empty list / ok=False) and never raise.
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx
from sqlmodel import Session

from jobapply.config import Settings, get_settings
from jobapply.logs import get_logger
from jobapply.models import User

log = get_logger(__name__)

ALERT_KINDS = ("interview", "assessment", "recruiter_question", "paused", "failed", "daily_summary",
               "new_jobs", "outcome")
ALERT_TARGETS = ("number", "group", "both")
GROUPS_CACHE_SECONDS = 600
EMPTY_CACHE_SECONDS = 60
READ_TIMEOUT = 5.0


class Notifier(Protocol):
    def send(self, user: User, kind: str, message: str) -> bool: ...


class LogNotifier:
    def send(self, user: User, kind: str, message: str) -> bool:
        log.info("notify", user=user.username, kind=kind, text=message)
        return True

    def send_text(self, to: str, text: str) -> bool:
        log.info("notify text", to=mask_target(to), text=text[:200])
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
        return self.send_text(number, f"[JOB-APPLY] {message}", who=user.username)

    def send_text(self, to: str, text: str, *, who: str = "") -> bool:
        """POST one text to a direct target (number or group jid). False on any failure, never raises."""
        to = str(to or "").strip()
        if not to or not text:
            return False
        try:
            resp = self.client.post(f"{self.base_url}/send", json={"to": to, "text": text}, headers=self._headers)
        except httpx.HTTPError as exc:
            log.warning("whatsapp send failed", user=who, to=mask_target(to), error=str(exc))
            return False
        if resp.status_code >= 300:
            log.warning("whatsapp bridge error", user=who, to=mask_target(to), status=resp.status_code,
                        body=resp.text[:200])
            return False
        return True

    def list_groups(self) -> list[dict]:
        """`[{"name", "jid"}, ...]` from GET /groups; [] (logged) on any error."""
        try:
            resp = self.client.get(f"{self.base_url}/groups", headers=self._headers, timeout=READ_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("whatsapp groups unavailable", error=str(exc)[:200])
            return []
        if isinstance(data, dict):
            data = data.get("groups") or data.get("data") or []
        out: list[dict] = []
        for g in data if isinstance(data, list) else []:
            if not isinstance(g, dict):
                continue
            jid = str(g.get("jid") or g.get("id") or "").strip()
            if not jid:
                continue
            out.append({"name": str(g.get("name") or g.get("subject") or jid), "jid": jid})
        out.sort(key=lambda g: g["name"].lower())
        return out

    def health(self) -> dict:
        """`{"ok", "status", "me_masked"}` from GET /health; ok only when the bridge says status "open"."""
        try:
            resp = self.client.get(f"{self.base_url}/health", headers=self._headers, timeout=READ_TIMEOUT)
            data = resp.json() if resp.status_code < 300 else {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("whatsapp bridge unreachable", error=str(exc)[:200])
            return {"ok": False, "status": "unreachable", "me_masked": "", "error": str(exc)[:200]}
        if not isinstance(data, dict):
            data = {}
        status = str(data.get("status") or ("error" if resp.status_code >= 300 else "unknown"))
        return {"ok": status == "open", "status": status, "me_masked": mask_target(str(data.get("me") or ""))}


class MultiNotifier:
    """Fan out to several backends; succeeds when any of them does."""

    def __init__(self, *backends: Notifier):
        self.backends = list(backends)

    def send(self, user: User, kind: str, message: str) -> bool:
        return any([b.send(user, kind, message) for b in self.backends])

    def send_text(self, to: str, text: str) -> bool:
        """Every backend gets the text; the verdict comes from the real transports (WhatsApp) when there are
        any, so a logged-but-not-delivered text does not count as sent."""
        verdicts: dict[int, bool] = {}
        for i, b in enumerate(self.backends):
            fn = getattr(b, "send_text", None)
            if callable(fn):
                verdicts[i] = bool(fn(to, text))
        real = [i for i, b in enumerate(self.backends) if isinstance(b, WhatsAppNotifier)]
        if real:
            return any(verdicts.get(i, False) for i in real)
        return any(verdicts.values())


def mask_target(value: str) -> str:
    """`919876543210@s.whatsapp.net` -> `••••3210`; a group jid keeps its `@g.us` tail; '' stays ''."""
    raw = str(value or "")
    if not raw:
        return ""
    local, _, domain = raw.partition("@")
    digits = "".join(ch for ch in local if ch.isdigit())
    tail = digits[-4:] if digits else local[-4:]
    masked = f"••••{tail}"
    return f"{masked}@{domain}" if domain == "g.us" else masked


def build_notifier(settings: Settings | None = None) -> Notifier:
    s = settings or get_settings()
    if s.whatsapp_base_url:
        token = s.whatsapp_token.get_secret_value() if s.whatsapp_token else None
        return MultiNotifier(LogNotifier(), WhatsAppNotifier(s.whatsapp_base_url, token))
    return LogNotifier()


_notifier: Notifier | None = None
_groups_cache: tuple[float, list[dict]] | None = None


def set_notifier(n: Notifier | None) -> None:
    global _notifier, _groups_cache
    _notifier = n
    _groups_cache = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = build_notifier()
    return _notifier


def whatsapp_backend() -> WhatsAppNotifier | None:
    """The configured WhatsApp transport, if any (inside a MultiNotifier or on its own)."""
    n = get_notifier()
    if isinstance(n, WhatsAppNotifier):
        return n
    for b in getattr(n, "backends", []) or []:
        if isinstance(b, WhatsAppNotifier):
            return b
    return None


def notify(session: Session, user: User, kind: str, message: str) -> bool:
    wanted = (user.preferences or {}).get("alert_types")
    if wanted is not None and kind not in wanted:
        return False
    return get_notifier().send(user, kind, message)


def alert_target_map(user: User) -> list[tuple[str, str]]:
    """`[(kind, address)]` for this user's alerts following `alerts_target`: `("number", "+91...")` and / or
    `("group", "...@g.us")`. Records (the `notified` events) carry the kind, never the address."""
    prefs = user.preferences or {}
    number = str(prefs.get("whatsapp_number") or "").strip()
    group = str(prefs.get("whatsapp_group_jid") or "").strip()
    mode = str(prefs.get("alerts_target") or "number")
    if mode not in ALERT_TARGETS:
        mode = "number"
    targets: list[tuple[str, str]] = []
    if mode in ("number", "both") and number:
        targets.append(("number", number))
    if mode in ("group", "both") and group:
        targets.append(("group", group))
    return targets


def alert_targets(user: User) -> list[str]:
    """Direct targets for this user's alerts from their preferences: the number, the group jid or both."""
    return [address for _kind, address in alert_target_map(user)]


def send_to(target: str, text: str) -> bool:
    """Send a ready-made text to one direct target through the configured backend (LogNotifier logs)."""
    n = get_notifier()
    fn = getattr(n, "send_text", None)
    if not callable(fn):
        log.info("notify text", to=mask_target(target), text=text[:200])
        return True
    try:
        return bool(fn(target, text))
    except Exception as exc:  # noqa: BLE001 - a transport error is a failed send, never a crash
        log.warning("send_to failed", to=mask_target(target), error=str(exc)[:200])
        return False


def list_groups(*, refresh: bool = False) -> list[dict]:
    """Groups the bridge account is in (`[{"name", "jid"}]`), cached for 10 minutes; [] without a bridge."""
    global _groups_cache
    now = time.monotonic()
    if not refresh and _groups_cache is not None and _groups_cache[0] > now:
        return list(_groups_cache[1])
    backend = whatsapp_backend()
    if backend is None:
        log.debug("no whatsapp bridge configured, no groups")
        return []
    groups = backend.list_groups()
    # an empty answer is usually a bridge hiccup: keep it only briefly so the next page load retries
    _groups_cache = (now + (GROUPS_CACHE_SECONDS if groups else EMPTY_CACHE_SECONDS), groups)
    return list(groups)


def bridge_health() -> dict:
    """`{"ok", "status", "me_masked"}`; `ok` False (status "not configured") without a bridge."""
    backend = whatsapp_backend()
    if backend is None:
        return {"ok": False, "status": "not configured", "me_masked": ""}
    return backend.health()
