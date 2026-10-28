from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.models import Credential, UserSite, utcnow
from jobapply.pipeline.filters import SENIORITY_ORDER, UserFilters
from jobapply.vault import Vault, VaultError, mask
from jobapply.web.deps import Ctx, as_list, flash, get_ctx, read_form, split_lines

router = APIRouter()

ALERT_TYPES = ["interview", "assessment", "paused", "failed", "daily_summary"]
TONES = ["professional", "warm", "concise", "enthusiastic"]
CREDENTIAL_KINDS = ["login", "imap", "smtp", "api_key"]


@router.get("/settings")
def settings_page(ctx: Ctx = Depends(get_ctx)):
    creds = ctx.scope.all(Credential, order_by=Credential.site)
    return ctx.render("settings.html", filters=UserFilters.model_validate(ctx.view.filters or {}),
                      prefs=ctx.view.preferences or {}, sites=ctx.scope.sites(), creds=creds, mask=mask,
                      seniority_levels=SENIORITY_ORDER, alert_types=ALERT_TYPES, tones=TONES,
                      credential_kinds=CREDENTIAL_KINDS)


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
    if api_key := str(form.get("anthropic_api_key") or "").strip():
        try:
            Vault().store(ctx.session, ctx.view.id, "anthropic", "api_key", "", {"password": api_key})
        except VaultError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/settings#ai", status_code=303)
    prefs["updated_at"] = utcnow().isoformat()
    ctx.view.preferences = prefs
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, "Saved")
    return RedirectResponse(f"/settings#{form.get('section', 'ai')}", status_code=303)
