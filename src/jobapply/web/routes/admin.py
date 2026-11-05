from __future__ import annotations

import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlmodel import select

from jobapply.ai.providers import health_key, parse_order, provider_status, server_configs
from jobapply.config import get_settings
from jobapply.models import Application, CollectorRun, Job, LlmUsage, Role, User, utcnow
from jobapply.notify import bridge_health, list_groups
from jobapply.pipeline.feeds import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MAX_PER_RUN,
    add_feed,
    filters_summary,
    get_feed,
    load_feeds,
    normalize_feed,
    remove_feed,
    run_feed,
    run_feeds,
    set_enabled,
    update_feed,
)
from jobapply.roles import all_fields
from jobapply.switches import collect_interval, get_value, set_shared_group_jids, set_value, shared_group_jids
from jobapply.users import create_user, hash_password
from jobapply.web.deps import Ctx, admin_ctx, as_list, flash, read_form, split_lines

router = APIRouter(prefix="/admin")


def _admin_context(ctx: Ctx) -> dict:
    """Everything the admin page shows apart from the group feeds (`_feeds_context`)."""
    s = ctx.session
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month = utcnow() - timedelta(days=30)
    users = s.exec(select(User).order_by(User.username)).all()
    applied = dict(
        s.exec(
            select(Application.user_id, func.count())
            .where(Application.submitted_at >= today)
            .group_by(Application.user_id)
        ).all()
    )
    tokens = {
        uid: (i or 0) + (o or 0)
        for uid, i, o in s.exec(
            select(LlmUsage.user_id, func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.output_tokens))
            .where(LlmUsage.created_at >= month)
            .group_by(LlmUsage.user_id)
        ).all()
    }
    runs = s.exec(select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(50)).all()
    settings = get_settings()
    order = parse_order(settings.llm_providers)
    usage_by_model = s.exec(
        select(LlmUsage.model, func.count(), func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens))
        .where(LlmUsage.created_at >= month)
        .group_by(LlmUsage.model)
    ).all()
    # llm_usage.model is "<provider>#<n>:<model>", so 30-day usage folds onto the key label
    usage_by_label: dict[str, dict[str, int]] = {}
    for model, calls, toks in usage_by_model:
        u = usage_by_label.setdefault(str(model).split(":")[0], {"calls": 0, "tokens": 0})
        u["calls"] += calls or 0
        u["tokens"] += toks or 0
    health = {h["key"]: h for h in provider_status()}
    providers = []
    for n in order:
        for cfg in server_configs(n, settings):  # one row per server key, labelled "<provider> key #n (…last4)"
            providers.append(
                {
                    "spec": cfg.spec,
                    "label": cfg.label,
                    "title": f"{cfg.spec.name} key #{cfg.index} ({cfg.key_hint})" if cfg.api_key else cfg.spec.name,
                    "server_key": bool(cfg.api_key),
                    "model": cfg.model,
                    "health": health.get(health_key(cfg)),
                    "usage": usage_by_label.get(cfg.label, {"calls": 0, "tokens": 0}),
                }
            )
    return dict(
        users=users,
        applied=applied,
        tokens=tokens,
        runs=runs,
        providers=providers,
        llm_backend=settings.llm_backend,
        usage_by_model=usage_by_model,
        kill=get_value(s, "kill_switch") == "on",
        dry=get_value(s, "dry_run", "on") != "off",
        interval=collect_interval(s),
        roles=list(Role),
    )


@router.post("/users")
async def add_user(request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    username, password = str(form.get("username") or "").strip(), str(form.get("password") or "")
    if not username or len(password) < 8:
        flash(request, "Username and a password of at least 8 characters are required", "error")
        return RedirectResponse("/admin", status_code=303)
    try:
        create_user(
            ctx.session, username, password, email=str(form.get("email") or ""), role=Role(form.get("role") or "user")
        )
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


@router.post("/shared-groups")
async def save_shared_groups(request: Request, ctx: Ctx = Depends(admin_ctx)):
    """The groups every user may route their alerts into (`app_settings["shared_group_jids"]`): the multi-select
    of the bridge's groups plus pasted jids (comma or newline separated, for when the bridge list is empty)."""
    form = await read_form(request)
    jids = [str(j) for j in as_list(form.get("shared_group_jids"))] + split_lines(form.get("shared_group_jids_text"))
    saved = set_shared_group_jids(ctx.session, jids)
    ctx.session.commit()
    flash(request, f"{len(saved)} WhatsApp group(s) shared with every user" if saved
          else "No WhatsApp group is shared with users now")
    return RedirectResponse("/admin#shared-groups", status_code=303)


# ----------------------------------------------------------------- WhatsApp group feeds (pipeline.feeds)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _feeds_context(s, edit_id: str | None = None) -> dict:
    """The Group feeds card: every feed (with `last_run` as a datetime for the `dt` filter), the feed being
    edited (prefills the form), the bridge's groups and health, the career-field picker and the sources
    seen in the pool."""
    feeds = load_feeds(s)
    for f in feeds:
        f["last_run"] = _parse_iso(f.get("last_run_at"))
    editing = next((f for f in feeds if f["id"] == edit_id), None) if edit_id else None
    groups = list_groups()
    sources = sorted(src for src in s.exec(select(Job.source).distinct()).all() if src)
    form = editing or normalize_feed({"name": "", "group_jid": "", "enabled": True})
    return {
        "feeds": feeds,
        "feed_edit": editing,
        "feed_form": form,
        "groups": groups,
        "group_names": {g["jid"]: g["name"] for g in groups},
        "shared_groups": shared_group_jids(s),
        "bridge": bridge_health(),
        "role_fields": all_fields(),
        "feed_role_selected": set(form["filters"]["role_selection"]),
        "feed_sources": sources,
        "feed_summary": filters_summary,
    }


@router.get("")
def admin_page(ctx: Ctx = Depends(admin_ctx), feed: str | None = None):
    return ctx.render("admin.html", **_admin_context(ctx), **_feeds_context(ctx.session, feed))


def _feed_from_form(form: dict) -> dict:
    """Feed fields from the admin form; numbers are normalised (and clamped) by `pipeline.feeds`."""
    group = str(form.get("group_jid_text") or "").strip() or str(form.get("group_jid") or "").strip()
    filters = {
        "role_selection": [str(v) for v in as_list(form.get("role_selection"))],
        "roles": split_lines(form.get("roles")),
        "keywords_include": split_lines(form.get("keywords_include")),
        "keywords_exclude": split_lines(form.get("keywords_exclude")),
        "locations": split_lines(form.get("locations")),
        "sources": split_lines(form.get("sources")),
        "remote_only": bool(form.get("remote_only")),
        "max_age_hours": form.get("max_age_hours") or DEFAULT_MAX_AGE_HOURS,
        "min_salary": form.get("min_salary") or None,
    }
    return {"name": str(form.get("name") or "").strip(), "group_jid": group, "filters": filters,
            "max_per_run": form.get("max_per_run") or DEFAULT_MAX_PER_RUN, "enabled": bool(form.get("enabled"))}


def _feed_or_404(ctx: Ctx, fid: str) -> dict:
    feed = get_feed(ctx.session, fid)
    if feed is None:
        raise HTTPException(404, "no such feed")
    return feed


@router.post("/feeds")
async def save_feed(request: Request, ctx: Ctx = Depends(admin_ctx)):
    form = await read_form(request)
    data = _feed_from_form(form)
    fid = str(form.get("id") or "").strip()
    try:
        if fid:
            feed = update_feed(ctx.session, fid, **data)
            message = f"Feed {feed['name']} saved"
        else:
            feed = add_feed(ctx.session, **data)
            message = f"Feed {feed['name']} added"
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin#feed-form", status_code=303)
    ctx.session.commit()
    flash(request, message)
    return RedirectResponse("/admin#feeds", status_code=303)


@router.post("/feeds/{fid}/enable")
async def enable_feed(fid: str, request: Request, ctx: Ctx = Depends(admin_ctx)):
    await read_form(request)
    _feed_or_404(ctx, fid)
    feed = set_enabled(ctx.session, fid, True)
    ctx.session.commit()
    flash(request, f"Feed {feed['name']} enabled")
    return RedirectResponse("/admin#feeds", status_code=303)


@router.post("/feeds/{fid}/disable")
async def disable_feed(fid: str, request: Request, ctx: Ctx = Depends(admin_ctx)):
    await read_form(request)
    _feed_or_404(ctx, fid)
    feed = set_enabled(ctx.session, fid, False)
    ctx.session.commit()
    flash(request, f"Feed {feed['name']} disabled; nothing is posted until it is enabled again")
    return RedirectResponse("/admin#feeds", status_code=303)


@router.post("/feeds/{fid}/remove")
async def remove_feed_route(fid: str, request: Request, ctx: Ctx = Depends(admin_ctx)):
    await read_form(request)
    feed = _feed_or_404(ctx, fid)
    remove_feed(ctx.session, fid)
    ctx.session.commit()
    flash(request, f"Feed {feed['name']} removed")
    return RedirectResponse("/admin#feeds", status_code=303)


@router.post("/feeds/{fid}/preview")
async def preview_feed(fid: str, request: Request, ctx: Ctx = Depends(admin_ctx)):
    """Dry run: render the admin page with the messages this feed would post now; nothing sent or written."""
    await read_form(request)
    feed = _feed_or_404(ctx, fid)
    result = run_feed(ctx.session, feed, None, dry_run=True, ignore_disabled=True)
    return ctx.render("admin.html", **_admin_context(ctx), **_feeds_context(ctx.session), feed_preview=result)


@router.post("/feeds/{fid}/run")
async def run_feed_now(fid: str, request: Request, ctx: Ctx = Depends(admin_ctx)):
    await read_form(request)
    feed = _feed_or_404(ctx, fid)
    out = run_feeds(ctx.session, None, feed_ids=[fid])
    r = out["feeds"][0] if out["feeds"] else None
    if r is None or r["skipped"]:
        flash(request, f"Feed {feed['name']}: nothing posted ({(r or {}).get('skipped') or 'not run'})", "error")
    elif r["error"]:
        flash(request, f"Feed {feed['name']}: {r['sent']} message(s) sent, {r['posted']} job(s) posted, "
                       f"then {r['error']}; the rest is retried next run", "error")
    else:
        flash(request, f"Feed {feed['name']}: {r['sent']} message(s) sent, {r['posted']} job(s) posted "
                       f"({r['candidates']} matched)")
    return RedirectResponse("/admin#feeds", status_code=303)

