from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from jobapply.users import authenticate
from jobapply.web.deps import db, flash, read_form, render

router = APIRouter()


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next))


@router.post("/login")
async def login(request: Request, session: Session = Depends(db)):
    form = await read_form(request)
    user = authenticate(session, str(form.get("username", "")).strip(), str(form.get("password", "")))
    if not user:
        flash(request, "Wrong username or password", "error")
        return render(request, "login.html", status_code=401, next=_safe_next(form.get("next")))
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update(uid=user.id, csrf=csrf)
    return RedirectResponse(_safe_next(form.get("next")), status_code=303)


@router.post("/logout")
async def logout(request: Request):
    await read_form(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
