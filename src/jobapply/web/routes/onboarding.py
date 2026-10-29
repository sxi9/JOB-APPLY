"""Self-service setup wizard for a freshly signed-up user.

Every step saves through the SAME code paths the Profile page uses (imported, not copied): profile
fields and the master-resume paste go through `profile.profile_save`, the resume file upload through
`profile.resume_file_upload`. The role picker / locations / remote / salary step updates only the filter
fields it owns, merged over the user's existing `filters` (see `ob_roles`): it must NOT reuse
`settings.save_filters` wholesale, because that route rebuilds the entire `UserFilters` from its (full)
form and would wipe keywords / seniority / blocklist / age for anyone who re-posts this partial step. The
only thing unique to this router is the per-user "Connect <site>" cookie step (LinkedIn, Naukri, Instahyre,
Cutshort, Wellfound: the same `settings.connect_user_site` flow the Settings page uses) and the final
"onboarded" flag; both stay scoped to `ctx.view`, so one user can never touch another's data. Works with no
LLM key and headless only.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.pipeline.filters import UserFilters, assign_jobs
from jobapply.roles import all_fields
from jobapply.sites import connect
from jobapply.web.deps import Ctx, as_list, flash, get_ctx, read_form, split_lines
from jobapply.web.routes.profile import PROFILE_FIELDS, missing_fields, profile_save, resume_file_upload
from jobapply.web.routes.settings import (
    connect_user_site,
    mailbox_context,
    read_cookie_export,
    run_mailbox_test,
    save_mailbox_form,
)

router = APIRouter()

# Order the wizard walks through. Each POST saves its step and redirects to the next one. The "mailbox" step
# (Gmail app password for OTP / verification mail) reuses the Settings → Mailbox handlers. The last step is
# the generic "Connect <site>" page (its name is kept for existing links).
STEPS = ["basics", "eligibility", "roles", "resume", "answers", "mailbox", "linkedin"]
CONNECT_SITES = list(connect.SITES)
_FIELD_MAP = {key: (label, kind) for key, label, kind in PROFILE_FIELDS}


def _next_step(step: str) -> str | None:
    try:
        i = STEPS.index(step)
    except ValueError:
        return STEPS[0]
    return STEPS[i + 1] if i + 1 < len(STEPS) else None


def _to(step: str | None) -> RedirectResponse:
    return RedirectResponse(f"/onboarding?step={step}" if step else "/onboarding", status_code=303)


@router.get("/onboarding")
def onboarding_page(step: str = "basics", ctx: Ctx = Depends(get_ctx)):
    if step not in STEPS:
        step = STEPS[0]
    profile = ctx.view.profile or {}
    filters = ctx.view.filters or {}
    rows = {s.site: s for s in ctx.scope.sites()}
    connect_sites = [
        {"site": name, "label": connect.label(name), "domains": ", ".join(connect.COOKIE_DOMAINS[name]),
         "status": rows[name].login_status if name in rows else "unknown"}
        for name in CONNECT_SITES
    ]
    resume_file = str(profile.get("resume_file") or "")
    return ctx.render(
        "onboarding.html",
        step=step,
        steps=STEPS,
        field_map=_FIELD_MAP,
        profile=profile,
        filters=filters,
        role_fields=all_fields(),
        role_selected=set(filters.get("role_selection") or []),
        missing=missing_fields(profile),
        resume_file_name=Path(resume_file).name if resume_file else "",
        connect_sites=connect_sites,
        linkedin_status=connect_sites[0]["status"],
        **mailbox_context(ctx),
    )


@router.post("/onboarding/basics")
async def ob_basics(request: Request, ctx: Ctx = Depends(get_ctx)):
    await profile_save(request, ctx)
    return _to("eligibility")


@router.post("/onboarding/eligibility")
async def ob_eligibility(request: Request, ctx: Ctx = Depends(get_ctx)):
    await profile_save(request, ctx)
    return _to("roles")


@router.post("/onboarding/roles")
async def ob_roles(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Save the target-role picker (role_selection / locations / remote / salary) WITHOUT touching the
    filter fields this partial form does not carry. Start from the user's existing filters and override
    only the four fields this step owns, so re-posting the wizard never wipes keywords, seniority,
    company_blocklist or max_age the user set on the Settings page."""
    form = await read_form(request)
    f = UserFilters.model_validate(ctx.view.filters or {})
    f.role_selection = as_list(form.get("role_selection"))
    f.locations = split_lines(form.get("locations"))
    f.remote_only = bool(form.get("remote_only"))
    f.salary_floor = float(form["salary_floor"]) if form.get("salary_floor") else None
    ctx.view.filters = f.model_dump()
    ctx.session.add(ctx.view)
    added = assign_jobs(ctx.session, None, [ctx.view]).get(ctx.view.username, 0)
    ctx.session.commit()
    flash(request, f"Filters saved. {added} more recent jobs now match.")
    return _to("resume")


@router.post("/onboarding/resume")
async def ob_resume(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    upload = form.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        await resume_file_upload(request, ctx)  # stores the file + remembers its path, same as Profile
    elif pasted := str(form.get("resume_json") or "").strip():
        # Validate before delegating: profile_save flashes + redirects to /profile on bad JSON, which this
        # step would otherwise discard, silently advancing the user past unsaved input. Stay on this step.
        try:
            json.loads(pasted)
        except json.JSONDecodeError as exc:
            flash(request, f"Master resume is not valid JSON: {exc}", "error")
            return _to("resume")
        await profile_save(request, ctx)  # paste path: profile_save stores resume_json
    return _to("answers")


@router.post("/onboarding/answers")
async def ob_answers(request: Request, ctx: Ctx = Depends(get_ctx)):
    await profile_save(request, ctx)
    return _to("mailbox")


@router.post("/onboarding/mailbox")
async def ob_mailbox(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Save the mailbox (Gmail app password etc.) through the same `settings.save_mailbox_form` the Settings
    card uses; with `action=test` the connection test runs right after. Stays on the step so the result is
    visible; "Continue" / "Skip" move on."""
    form = await read_form(request)
    if save_mailbox_form(request, ctx, form) and str(form.get("action") or "") == "test":
        run_mailbox_test(request, ctx)
    return _to("mailbox")


@router.post("/onboarding/mailbox/test")
async def ob_mailbox_test(request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    run_mailbox_test(request, ctx)
    return _to("mailbox")


@router.post("/onboarding/mailbox/skip")
async def ob_mailbox_skip(request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)  # CSRF
    return _to("linkedin")


@router.post("/onboarding/connect/{site}")
async def ob_connect(site: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    """Import this user's own cookie export for `site` into their isolated browser profile and report status.

    The uploaded JSON is written under the git-ignored per-user data dir (`data/cookies/<user id>/<site>.json`),
    never into the repo, and imported headless via `sites.connect` (which keys the profile by user id). Sites
    with a session checker (Naukri, Instahyre, Cutshort, Wellfound) are verified right away.
    """
    form = await read_form(request)
    if not connect.is_site(site):
        raise HTTPException(404)
    raw = await read_cookie_export(request, form)
    if not raw:
        flash(request, f"Upload or paste your {connect.label(site)} cookie export first.", "error")
        return _to("linkedin")
    ok, message = connect_user_site(ctx, site, raw)
    flash(request, message, "ok" if ok else "error")
    return _to("linkedin")


@router.post("/onboarding/linkedin")
async def ob_linkedin(request: Request, ctx: Ctx = Depends(get_ctx)):
    """The original LinkedIn-only step, kept as an alias of `/onboarding/connect/linkedin`."""
    return await ob_connect("linkedin", request, ctx)


@router.post("/onboarding/done")
async def ob_done(request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)  # CSRF
    profile = dict(ctx.view.profile or {})
    profile["onboarded"] = True
    ctx.view.profile = profile
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, "Setup complete. You are ready to apply.")
    return RedirectResponse("/", status_code=303)
