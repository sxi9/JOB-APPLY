from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.ai.providers import CATALOG, parse_keys, parse_order, server_configs
from jobapply.ai.service import user_keys
from jobapply.config import get_settings
from jobapply.models import Credential, UserSite, utcnow
from jobapply.pipeline.filters import SENIORITY_ORDER, UserFilters
from jobapply.roles import all_fields
from jobapply.sites import connect
from jobapply.users import SITES as SITE_DEFAULTS
from jobapply.vault import Vault, VaultError, mask
from jobapply.web.deps import Ctx, as_list, flash, get_ctx, read_form, split_lines
from jobapply.web.routes.jobs import AGE_PRESETS

router = APIRouter()

ALERT_TYPES = ["interview", "assessment", "paused", "failed", "daily_summary"]
TONES = ["professional", "warm", "concise", "enthusiastic"]
CREDENTIAL_KINDS = ["login", "imap", "smtp", "api_key"]


def site_kind(site: str) -> str:
    return str(SITE_DEFAULTS.get(site, {}).get("kind", "ats"))


def connectable(site: str) -> bool:
    """Sites whose login is a cookie import (the assisted ones): they get Connect / Check controls."""
    return connect.is_site(site)


async def read_cookie_export(request: Request, form: dict) -> str:
    """The pasted or uploaded cookie-export text from a connect form, or '' when neither was given.

    The Sites form carries one `file` input per connectable row, and a browser posts every untouched one as an
    empty part (`filename=""`), so `file` may arrive as a list: the chosen export is the first part that has a
    filename, wherever it sits in the form."""
    uploads = form.get("file")
    for upload in (uploads if isinstance(uploads, list) else [uploads]):
        if upload is not None and getattr(upload, "filename", ""):
            return (await upload.read()).decode("utf-8-sig", errors="replace")
    return str(form.get("cookies_json") or "").strip()


def connect_user_site(ctx: Ctx, site: str, raw: str) -> tuple[bool, str]:
    """Save + import a cookie export for the viewed user and record the outcome on their site row.
    Returns (ok, message). Never raises on a bad export; the row is marked `expired` then."""
    row: UserSite | None = ctx.scope.site(site)
    name = connect.label(site)
    try:
        result = connect.connect_site(ctx.view.id, site, raw, engine=ctx.request.app.state.engine)
    except Exception as exc:  # noqa: BLE001 - bad export / expired cookies / playwright error
        if row is not None:
            row.login_status = "expired"
            ctx.session.add(row)
            ctx.session.commit()
        return False, f"Could not connect {name}: {str(exc).splitlines()[0]}"
    if row is not None:
        row.login_status = result.login_status
        if result.status == "ok":
            row.last_login_at = utcnow()
        ctx.session.add(row)
        ctx.session.commit()
    if result.status == "ok":
        return True, f"{name} connected. Imported {result.count} cookies into your private profile."
    return False, (f"{name}: imported {result.count} cookies, but the session check says {result.status}. "
                   "Export the cookies again from a logged-in browser window and retry.")


@router.get("/settings")
def settings_page(ctx: Ctx = Depends(get_ctx)):
    creds = ctx.scope.all(Credential, order_by=Credential.site)
    prefs = ctx.view.preferences or {}
    order = parse_order(prefs.get("llm_providers")) or parse_order(get_settings().llm_providers)
    providers = []
    try:
        vault: Vault | None = Vault()  # once per request, not once per provider
    except VaultError:
        vault = None
    for name, spec in CATALOG.items():
        servers = server_configs(name)
        mine = user_keys(ctx.session, ctx.view, name, vault=vault) if vault is not None else []
        providers.append(
            {
                "spec": spec,
                "enabled": name in order,
                "position": order.index(name) + 1 if name in order else 99,
                "server_key": bool(servers[0].api_key),
                "server_keys": len([c for c in servers if c.api_key]),
                "user_key": bool(mine),
                "user_keys": len(mine),
                "model": (prefs.get("llm_models") or {}).get(name, ""),
                "default_model": servers[0].model,
            }
        )
    providers.sort(key=lambda p: (p["position"], p["spec"].name))
    filters = UserFilters.model_validate(ctx.view.filters or {})
    return ctx.render(
        "settings.html",
        filters=filters,
        role_fields=all_fields(),
        role_selected=set(filters.role_selection),
        prefs=prefs,
        sites=ctx.scope.sites(),
        site_kind=site_kind,
        connectable=connectable,
        site_label=connect.label,
        creds=[c for c in creds if c.kind != "api_key"],
        mask=mask,
        seniority_levels=SENIORITY_ORDER,
        age_presets=AGE_PRESETS,
        alert_types=ALERT_TYPES,
        tones=TONES,
        credential_kinds=CREDENTIAL_KINDS,
        providers=providers,
        live=get_settings().llm_backend != "fake",
    )


@router.post("/settings/filters")
async def save_filters(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    f = UserFilters(
        roles=split_lines(form.get("roles")),
        keywords_include=split_lines(form.get("keywords_include")),
        keywords_exclude=split_lines(form.get("keywords_exclude")),
        locations=split_lines(form.get("locations")),
        remote_only=bool(form.get("remote_only")),
        salary_floor=float(form["salary_floor"]) if form.get("salary_floor") else None,
        seniority=as_list(form.get("seniority")),
        company_blocklist=split_lines(form.get("company_blocklist")),
        max_age_hours=int(form.get("max_age_hours") or 72),
        max_age_minutes=int(form["max_age_minutes"]) if form.get("max_age_minutes") else None,
        role_selection=as_list(form.get("role_selection")),
    )
    ctx.view.filters = f.model_dump()
    ctx.session.add(ctx.view)
    from jobapply.pipeline.filters import assign_jobs

    added = assign_jobs(ctx.session, None, [ctx.view]).get(ctx.view.username, 0)
    ctx.session.commit()
    flash(request, f"Filters saved. {added} more recent jobs now match.")
    return RedirectResponse("/settings#filters", status_code=303)


@router.post("/settings/sites")
async def save_sites(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    for site in ctx.scope.sites():
        site.enabled = bool(form.get(f"enabled__{site.site}"))
        site.auto_submit = bool(form.get(f"auto__{site.site}"))
        if cap := form.get(f"cap__{site.site}"):
            site.daily_cap = max(0, int(cap))
        ctx.session.add(site)
    ctx.session.commit()
    flash(request, "Sites saved")
    return RedirectResponse("/settings#sites", status_code=303)


@router.post("/settings/sites/{site}/relogin")
async def relogin(site: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    row: UserSite | None = ctx.scope.site(site)
    if row is None:
        raise HTTPException(404)
    row.login_status = "relogin_requested"
    ctx.session.add(row)
    ctx.session.commit()
    flash(request, f"Re-login for {site} requested. The OTP flow runs on the apply worker.")
    return RedirectResponse("/settings#sites", status_code=303)


@router.post("/settings/sites/{site}/cookies")
async def connect_cookies(site: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Connect an assisted site from a cookie export (file upload or pasted JSON). The export is written
    under the per-user data dir and imported into the user's own headless browser profile; the sites with a
    session checker are verified right away and the Login column shows the result."""
    form = await read_form(request)
    if not connectable(site) or ctx.scope.site(site) is None:
        raise HTTPException(404)
    raw = await read_cookie_export(request, form)
    if not raw:
        flash(request, f"Upload or paste your {connect.label(site)} cookie export first.", "error")
        return RedirectResponse("/settings#sites", status_code=303)
    ok, message = connect_user_site(ctx, site, raw)
    flash(request, message, "ok" if ok else "error")
    return RedirectResponse("/settings#sites", status_code=303)


@router.post("/settings/sites/{site}/check")
async def check_site(site: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Open the site headless with the saved profile and refresh the Login column."""
    await read_form(request)
    row: UserSite | None = ctx.scope.site(site)
    if not connectable(site) or row is None:
        raise HTTPException(404)
    try:
        status = connect.check_session_for(site, ctx.view.id, engine=ctx.request.app.state.engine)
    except Exception as exc:  # noqa: BLE001 - playwright / profile trouble
        flash(request, f"Could not check {connect.label(site)}: {str(exc).splitlines()[0]}", "error")
        return RedirectResponse("/settings#sites", status_code=303)
    row.login_status = "expired" if status == "login" else status
    if status == "ok":
        row.last_login_at = utcnow()
    ctx.session.add(row)
    ctx.session.commit()
    flash(request, f"{connect.label(site)} session: {status}", "ok" if status == "ok" else "error")
    return RedirectResponse("/settings#sites", status_code=303)


@router.post("/settings/credentials")
async def save_credential(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    site, kind = str(form.get("site") or "").strip().lower(), str(form.get("kind") or "login")
    if not site or kind not in CREDENTIAL_KINDS:
        flash(request, "Site and kind are required", "error")
        return RedirectResponse("/settings#credentials", status_code=303)
    secret = {"password": str(form.get("secret") or "")}
    for extra in ("host", "port"):
        if form.get(extra):
            secret[extra] = str(form[extra])
    try:
        Vault().store(ctx.session, ctx.view.id, site, kind, str(form.get("username") or ""), secret)
    except VaultError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings#credentials", status_code=303)
    ctx.session.commit()
    flash(request, f"Saved {kind} credential for {site} (encrypted)")
    return RedirectResponse("/settings#credentials", status_code=303)


@router.post("/settings/credentials/{cred_id}/delete")
async def delete_credential(cred_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    cred = ctx.scope.get(Credential, cred_id)
    if cred is None:
        raise HTTPException(404)
    ctx.session.delete(cred)
    ctx.session.commit()
    flash(request, "Credential deleted")
    return RedirectResponse("/settings#credentials", status_code=303)


@router.post("/settings/preferences")
async def save_preferences(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    prefs = dict(ctx.view.preferences or {})
    if "score_threshold" in form:
        prefs["score_threshold"] = max(0, min(100, int(form.get("score_threshold") or 70)))
    if "cover_letter_tone" in form:
        prefs["cover_letter_tone"] = str(form["cover_letter_tone"])
    if "whatsapp_number" in form:
        prefs["whatsapp_number"] = str(form["whatsapp_number"]).strip()
        prefs["forward_email"] = str(form.get("forward_email") or "").strip()
        prefs["alert_types"] = as_list(form.get("alert_types"))
    if "provider_order" in form:
        enabled = [n for n in CATALOG if form.get(f"use__{n}")]
        order = [n for n in parse_order(form.get("provider_order")) if n in enabled]
        order += [n for n in enabled if n not in order]
        prefs["llm_providers"] = ",".join(order)
        prefs["llm_models"] = {
            n: str(form.get(f"model__{n}") or "").strip() for n in CATALOG if str(form.get(f"model__{n}") or "").strip()
        }
        for name in CATALOG:
            # textarea "API keys (one per line)"; the older single `key__` field is still accepted
            keys = parse_keys(str(form.get(f"keys__{name}") or ""), str(form.get(f"key__{name}") or ""))
            if keys:
                secret = {"password": keys[0], "keys": keys}  # `password` keeps older readers working
                try:
                    Vault().store(ctx.session, ctx.view.id, name, "api_key", "", secret)
                except VaultError as exc:
                    flash(request, str(exc), "error")
                    return RedirectResponse("/settings#ai", status_code=303)
            elif form.get(f"clear__{name}"):
                cred = ctx.scope.credential(name, "api_key")
                if cred:
                    ctx.session.delete(cred)
    prefs["updated_at"] = utcnow().isoformat()
    ctx.view.preferences = prefs
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/settings#{form.get('section', 'ai')}", status_code=303)
