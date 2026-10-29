"""FastAPI dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine
from starlette.middleware.sessions import SessionMiddleware

from jobapply.config import get_settings
from jobapply.db import get_engine
from jobapply.web import filters as jinja_filters
from jobapply.web.deps import LoginRequired, templates
from jobapply.web.routes import (
    admin,
    applications,
    auth,
    home,
    inbox,
    jobs,
    onboarding,
    profile,
    queue,
    referrals,
    settings,
)


def create_app(engine: Engine | None = None, secret_key: str | None = None) -> FastAPI:
    s = get_settings()
    app = FastAPI(title="JOB-APPLY", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine or get_engine()
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or s.secret_key.get_secret_value(),
        session_cookie="jobapply_session",
        same_site="lax",
        https_only=False,  # served behind Tailscale; set a TLS proxy in front for https_only
        max_age=14 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    jinja_filters.register(templates.env)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    for module in (auth, home, jobs, queue, applications, inbox, referrals, profile, settings, onboarding, admin):
        app.include_router(module.router)
    return app
