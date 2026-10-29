from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from jobapply.config import get_settings
from jobapply.models import Role
from jobapply.users import authenticate, create_user
from jobapply.web.deps import db, flash, read_form, render

router = APIRouter()

# 3-32 chars: letters, numbers, dot, dash or underscore. Also caps absurd input cheaply (no external deps).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_MIN_PASSWORD = 8
_MAX_PASSWORD = 200


def signup_allowed() -> bool:
    return bool(get_settings().allow_signup)


def _safe_next(target: str | None) -> str:
    # Only same-origin, path-absolute targets. Reject protocol-relative "//host", the backslash variants
    # ("/\\host", "/\\/host") browsers normalise to "//host", and control chars like NUL/newline — any of
    # which could turn the post-login redirect into an open redirect to an attacker host.
    if (target and target.startswith("/") and not target.startswith("//")
            and "\\" not in target and not any(c < " " for c in target)):
        return target
    return "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), allow_signup=signup_allowed())


@router.post("/login")
async def login(request: Request, session: Session = Depends(db)):
    form = await read_form(request)
    user = authenticate(session, str(form.get("username", "")).strip(), str(form.get("password", "")))
    if not user:
        flash(request, "Wrong username or password", "error")
        return render(request, "login.html", status_code=401, next=_safe_next(form.get("next")),
                      allow_signup=signup_allowed())
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update(uid=user.id, csrf=csrf)
    return RedirectResponse(_safe_next(form.get("next")), status_code=303)


@router.get("/signup")
def signup_page(request: Request):
    if not signup_allowed():
        raise HTTPException(404)
    return render(request, "signup.html", username="", email="")


@router.post("/signup")
async def signup(request: Request, session: Session = Depends(db)):
    if not signup_allowed():
        raise HTTPException(404)
    form = await read_form(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm", ""))
    email = str(form.get("email", "")).strip()
    errors: list[str] = []
    if not _USERNAME_RE.match(username):
        errors.append("Username must be 3-32 characters: letters, numbers, dot, dash or underscore.")
    if len(password) < _MIN_PASSWORD:
        errors.append(f"Password must be at least {_MIN_PASSWORD} characters.")
    elif len(password) > _MAX_PASSWORD:
        errors.append("Password is too long.")
    if password != confirm:
        errors.append("The two passwords do not match.")
    if email and ("@" not in email or len(email) > _MAX_PASSWORD):
        errors.append("Enter a valid email address, or leave it blank.")
    user = None
    if not errors:
        try:
            # Public sign-up always creates a plain user; admin is never selectable here.
            user = create_user(session, username, password, email=email, role=Role.user)
            session.commit()
        except ValueError:
            errors.append("That username is already taken.")
    if errors or user is None:
        for message in errors:
            flash(request, message, "error")
        return render(request, "signup.html", status_code=400, username=username, email=email)
    # Log the new user in with the same session mechanism as /login, then send them to the wizard.
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update(uid=user.id, csrf=csrf)
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    await read_form(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
