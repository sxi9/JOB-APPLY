from __future__ import annotations

import threading
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlmodel import select

from jobapply.models import Application, CollectorRun, LlmUsage, Role, User, utcnow
from jobapply.switches import collect_interval, get_value, set_value
from jobapply.users import create_user, hash_password
from jobapply.web.deps import Ctx, admin_ctx, flash, read_form

router = APIRouter(prefix="/admin")


@router.get("")
def admin_page(ctx: Ctx = Depends(admin_ctx)):
    s = ctx.session
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month = utcnow() - timedelta(days=30)
    users = s.exec(select(User).order_by(User.username)).all()
    applied = dict(s.exec(select(Application.user_id, func.count()).where(Application.submitted_at >= today)
                          .group_by(Application.user_id)).all())
    tokens = {uid: (i or 0) + (o or 0) for uid, i, o in s.exec(
        select(LlmUsage.user_id, func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.output_tokens))
        .where(LlmUsage.created_at >= month).group_by(LlmUsage.user_id)).all()}
    runs = s.exec(select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(50)).all()
    return ctx.render("admin.html", users=users, applied=applied, tokens=tokens, runs=runs,
                      kill=get_value(s, "kill_switch") == "on", dry=get_value(s, "dry_run", "on") != "off",
                      interval=collect_interval(s), roles=list(Role))


@router.post("/users")
async def add_user(request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    username, password = str(form.get("username") or "").strip(), str(form.get("password") or "")
    if not username or len(password) < 8:
        flash(request, "Username and a password of at least 8 characters are required", "error")
        return RedirectResponse("/admin", status_code=303)
    try:
        create_user(ctx.session, username, password, email=str(form.get("email") or ""),
                    role=Role(form.get("role") or "user"))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin", status_code=303)
    ctx.session.commit()
    flash(request, f"User {username} created")
    return RedirectResponse("/admin", status_code=303)


@router.post("/users/{uid}")
async def update_user(uid: int, request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    user = ctx.session.get(User, uid)
    if user is None:
        raise HTTPException(404)
    if uid != ctx.user.id:
        user.is_active = bool(form.get("is_active"))
        user.role = Role(form.get("role") or user.role)
    if cap := form.get("daily_cap"):
        user.preferences = {**(user.preferences or {}), "daily_cap": max(0, int(cap))}
    if password := str(form.get("password") or ""):
        user.password_hash = hash_password(password)
    ctx.session.add(user)
    ctx.session.commit()
    flash(request, f"Updated {user.username}")
    return RedirectResponse("/admin", status_code=303)


@router.post("/view")
async def view_as(request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    uid = int(form.get("uid") or ctx.user.id)
    if ctx.session.get(User, uid) is None:
        raise HTTPException(404)
    request.session["view_uid"] = uid
    return RedirectResponse(str(form.get("back") or "/"), status_code=303)


@router.post("/switches")
async def switches(request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    if "kill_switch" in form:
        set_value(ctx.session, "kill_switch", "on" if form["kill_switch"] == "on" else "off")
    if "dry_run" in form:
        set_value(ctx.session, "dry_run", "on" if form["dry_run"] == "on" else "off")
    if interval := form.get("collect_interval_minutes"):
        set_value(ctx.session, "collect_interval_minutes", str(max(10, int(interval))))
    ctx.session.commit()
    flash(request, "Saved. Interval changes apply when the scheduler restarts.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/collect")
async def collect_now(request: Request, ctx: Ctx = Depends(admin_ctx)):
    await read_form(request)
    engine = request.app.state.engine

    def run() -> None:
        from jobapply.pipeline.collect import run_all

        run_all(engine=engine)

    threading.Thread(target=run, name="collect-now", daemon=True).start()
    flash(request, "Collectors started in the background. Refresh in a minute.")
    return RedirectResponse("/admin", status_code=303)
