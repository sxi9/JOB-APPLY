"""`POST /api/whatsapp/inbound`: the bridge's webhook for inbound WhatsApp texts (subscriber bot).

No login, no CSRF: the request is authenticated by the `X-Webhook-Secret` header matching
`JOBAPPLY_WHATSAPP_WEBHOOK_SECRET` (401 otherwise, 503 while the secret is unset). The payload is the bridge's
record `{id, chat, from, phone?, pushName?, text, isGroup, ts}`; `bot.whatsapp.handle` answers it in a thread
(the replies go back through the bridge). Always `{"ok": true, "replies": n}` once authenticated - a bad
payload or a bot error is logged, never raised back to the bridge (which would only retry nothing).
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from jobapply.bot.whatsapp import handle
from jobapply.config import get_settings
from jobapply.logs import get_logger

router = APIRouter(prefix="/api/whatsapp")
log = get_logger(__name__)


def _handle_sync(engine, payload: dict) -> int:
    try:
        with Session(engine, expire_on_commit=False) as session:
            return len(handle(session, payload))
    except Exception as exc:  # noqa: BLE001 - the webhook must always answer; the bridge has no retry logic
        log.warning("whatsapp inbound failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return 0


@router.post("/inbound", include_in_schema=False)
async def inbound(request: Request):
    s = get_settings()
    secret = str(s.whatsapp_webhook_secret or "")
    if not secret:
        return JSONResponse({"ok": False, "error": "webhook secret not configured"}, status_code=503)
    given = str(request.headers.get("x-webhook-secret") or "")
    if not given or not secrets.compare_digest(given.encode(), secret.encode()):
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        log.warning("whatsapp inbound: payload is not a JSON object")
        return {"ok": True, "replies": 0}
    if not s.whatsapp_bot_enabled:
        return {"ok": True, "replies": 0, "disabled": True}
    replies = await run_in_threadpool(_handle_sync, request.app.state.engine, payload)
    return {"ok": True, "replies": replies}
