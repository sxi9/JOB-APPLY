"""Request context: database session, logged-in user, the user being viewed, CSRF, flash messages."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from jobapply.models import Role, User
from jobapply.repo import UserScope

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(Exception):
    pass


def db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        yield session


@dataclass
class Ctx:
    request: Request
    session: Session
    user: User  # who is logged in
    view: User  # whose data the page shows; differs only when an admin picks another user

    @property
    def is_admin(self) -> bool:
        return self.user.role == Role.admin

    @property
    def scope(self) -> UserScope:
        return UserScope(self.session, self.view)

    def render(self, template: str, status_code: int = 200, **context: Any):
        return render(self.request, template, status_code=status_code, ctx=self, **context)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(24)
    return token


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # Reassign the key: the session middleware only writes the cookie when the dict itself changes.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


def render(request: Request, template: str, status_code: int = 200, **context: Any):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, template, {"csrf": csrf_token(request), "flashes": flashes, **context},
                                      status_code=status_code)


def get_ctx(request: Request, session: Session = Depends(db)) -> Ctx:
    uid = request.session.get("uid")
    user = session.get(User, uid) if uid else None
    if user is None or not user.is_active:
        raise LoginRequired()
    view = user
    view_uid = request.session.get("view_uid")
    if view_uid and user.role == Role.admin and view_uid != user.id:
        view = session.get(User, view_uid) or user
    return Ctx(request, session, user, view)


def admin_ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(403, "admin only")
    return ctx


async def read_form(request: Request) -> dict[str, Any]:
    """Parse the posted form and check the CSRF token (form field or HTMX header)."""
    form = await request.form()
    token = form.get("csrf") or request.headers.get("x-csrf-token")
    if not token or not secrets.compare_digest(str(token), request.session.get("csrf", "")):
        raise HTTPException(403, "invalid CSRF token")
    return {k: (form.getlist(k) if len(form.getlist(k)) > 1 else form.get(k)) for k in form.keys()}


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def split_lines(text: str | None) -> list[str]:
    """Textarea or comma separated input to a clean list."""
    if not text:
        return []
    parts = [p.strip() for line in str(text).splitlines() for p in line.split(",")]
    return [p for p in parts if p]
