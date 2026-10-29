"""Self-service setup wizard for a freshly signed-up user.

Every step saves through the SAME code paths the Profile page uses (imported, not copied): profile
fields and the master-resume paste go through `profile.profile_save`, the resume file upload through
`profile.resume_file_upload`. The role picker / locations / remote / salary step updates only the filter
fields it owns, merged over the user's existing `filters` (see `ob_roles`): it must NOT reuse
`settings.save_filters` wholesale, because that route rebuilds the entire `UserFilters` from its (full)
form and would wipe keywords / seniority / blocklist / age for anyone who re-posts this partial step. The
only thing unique to this router is the per-user LinkedIn cookie connect step and the final "onboarded"
flag; both stay scoped to `ctx.view`, so one user can never touch another's data. Works with no LLM key
and headless only.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from jobapply.config import get_settings
from jobapply.models import utcnow
from jobapply.pipeline.filters import UserFilters, assign_jobs
from jobapply.roles import all_fields
from jobapply.sites import browser
from jobapply.web.deps import Ctx, as_list, flash, get_ctx, read_form, split_lines
from jobapply.web.routes.profile import PROFILE_FIELDS, missing_fields, profile_save, resume_file_upload

router = APIRouter()

# Order the wizard walks through. Each POST saves its step and redirects to the next one.
STEPS = ["basics", "eligibility", "roles", "resume", "answers", "linkedin"]
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
    site = ctx.scope.site("linkedin")
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
        linkedin_status=(site.login_status if site else "unknown"),
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
    return _to("linkedin")


@router.post("/onboarding/linkedin")
async def ob_linkedin(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Import this user's own LinkedIn cookie export into their isolated browser profile and report status.

    The uploaded JSON is written under the git-ignored per-user data dir, never into the repo, and the
    import runs headless via `sites.browser.import_cookies_file` (which keys the profile by user id).
    """
    form = await read_form(request)
    upload = form.get("file")
    pasted = str(form.get("cookies_json") or "").strip()
    raw: str | None = None
    if upload is not None and getattr(upload, "filename", ""):
        raw = (await upload.read()).decode("utf-8-sig", errors="replace")
    elif pasted:
        raw = pasted
    if not raw:
        flash(request, "Upload or paste your LinkedIn cookie export first.", "error")
        return _to("linkedin")

    folder = get_settings().data_dir / "cookies" / str(ctx.view.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "linkedin.json"
    path.write_text(raw, encoding="utf-8")

    site = ctx.scope.site("linkedin")
    try:
        count = browser.import_cookies_file(ctx.view.id, "linkedin", path)
    except Exception as exc:  # noqa: BLE001 - bad export / expired cookies / playwright error
        if site is not None:
            site.login_status = "expired"
            ctx.session.add(site)
            ctx.session.commit()
        flash(request, f"Could not connect LinkedIn: {str(exc).splitlines()[0]}", "error")
        return _to("linkedin")
    if site is not None:
        site.login_status = "ok"
        site.last_login_at = utcnow()
        ctx.session.add(site)
        ctx.session.commit()
    flash(request, f"LinkedIn connected. Imported {count} cookies into your private profile.")
    return _to("linkedin")


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
