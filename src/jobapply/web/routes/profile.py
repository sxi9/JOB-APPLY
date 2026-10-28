from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import select

from jobapply.config import get_settings
from jobapply.models import AnswerBank, utcnow
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

# Profile vault fields shown on the Profile page; anything else in users.profile is kept as is.
PROFILE_FIELDS: list[tuple[str, str, str]] = [
    ("first_name", "First name", "text"), ("last_name", "Last name", "text"),
    ("email", "Email (dedicated mailbox)", "email"), ("phone", "Phone", "tel"),
    ("location", "Current location", "text"), ("linkedin", "LinkedIn URL", "url"),
    ("github", "GitHub URL", "url"), ("portfolio", "Portfolio URL", "url"),
    ("work_authorization", "Work authorization", "text"), ("needs_sponsorship", "Needs visa sponsorship", "yesno"),
    ("notice_period", "Notice period", "text"), ("current_ctc", "Current CTC", "text"),
    ("expected_ctc", "Expected CTC", "text"), ("years_experience", "Total years of experience", "number"),
    ("willing_to_relocate", "Willing to relocate", "yesno"), ("current_company", "Current company", "text"),
    ("current_title", "Current title", "text"), ("education", "Highest education", "text"),
    ("gender", "Gender (EEO, optional)", "text"), ("veteran_status", "Veteran status (EEO, optional)", "text"),
    ("disability_status", "Disability status (EEO, optional)", "text"),
    ("summary", "Short professional summary", "textarea"),
]
_SAFE_NAME = re.compile(r"[^\w.-]+")


def parse_skills(text: str) -> dict[str, float]:
    skills: dict[str, float] = {}
    for line in text.splitlines():
        if ":" in line:
            name, years = line.rsplit(":", 1)
            try:
                skills[name.strip()] = float(years)
            except ValueError:
                continue
        elif line.strip():
            skills[line.strip()] = 0
    return skills


@router.get("/profile")
def profile_page(ctx: Ctx = Depends(get_ctx)):
    answers = ctx.session.exec(select(AnswerBank).where(AnswerBank.user_id == ctx.view.id)
                               .order_by(AnswerBank.approved, AnswerBank.updated_at.desc())).all()
    profile = ctx.view.profile or {}
    skills = "\n".join(f"{k}: {v:g}" for k, v in (profile.get("skills") or {}).items())
    resume_json = json.dumps(profile.get("resume_json") or {}, indent=2)
    return ctx.render("profile.html", fields=PROFILE_FIELDS, profile=profile, skills=skills,
                      resume_json=resume_json, answers=answers)


@router.post("/profile")
async def profile_save(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    profile = dict(ctx.view.profile or {})
    for key, _label, _kind in PROFILE_FIELDS:
        if key in form:
            profile[key] = str(form[key]).strip()
    if "skills" in form:
        profile["skills"] = parse_skills(str(form["skills"]))
    if "resume_json" in form:
        try:
            profile["resume_json"] = json.loads(str(form["resume_json"]) or "{}")
        except json.JSONDecodeError as exc:
            flash(request, f"Master resume is not valid JSON: {exc}", "error")
            return RedirectResponse("/profile", status_code=303)
    ctx.view.profile = profile
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, "Profile saved")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/resume")
async def resume_upload(request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        flash(request, "Choose a file", "error")
        return RedirectResponse("/profile", status_code=303)
    name = _SAFE_NAME.sub("_", upload.filename)[-120:]
    if not name.lower().endswith((".pdf", ".docx")):
        flash(request, "Upload a PDF or DOCX", "error")
        return RedirectResponse("/profile", status_code=303)
    folder = get_settings().resumes_dir / str(ctx.view.id) / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(await upload.read())
    profile = dict(ctx.view.profile or {})
    resumes = [r for r in profile.get("resumes", []) if r.get("name") != name]
    resumes.append({"name": name, "path": str(path), "role_type": str(form.get("role_type") or "default"),
                    "uploaded_at": utcnow().isoformat()})
    profile["resumes"] = resumes
    ctx.view.profile = profile
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, f"Uploaded {name}")
    return RedirectResponse("/profile", status_code=303)


@router.get("/files/resume/{name}")
def resume_file(name: str, ctx: Ctx = Depends(get_ctx)):
    entry = next((r for r in (ctx.view.profile or {}).get("resumes", []) if r.get("name") == name), None)
    if entry is None:
        raise HTTPException(404)
    return FileResponse(entry["path"], filename=name)


@router.post("/profile/answers")
async def answers_save(request: Request, ctx: Ctx = Depends(get_ctx)):
    from jobapply.forms.answers import question_hash

    form = await read_form(request)
    if question := str(form.get("new_question") or "").strip():
        qh = question_hash(question)
        row = ctx.scope.answer(qh) or AnswerBank(user_id=ctx.view.id, question_hash=qh, question=question)
        row.answer, row.source, row.approved, row.updated_at = str(form.get("new_answer") or ""), "user", True, utcnow()
        ctx.session.add(row)
    for key, value in form.items():
        if key.startswith("answer__"):
            row = ctx.scope.get(AnswerBank, int(key.removeprefix("answer__")))
            if row and (row.answer != value or not row.approved):
                row.answer, row.source, row.approved, row.updated_at = str(value), "user", True, utcnow()
                ctx.session.add(row)
    ctx.session.commit()
    flash(request, "Answers saved")
    return RedirectResponse("/profile#answers", status_code=303)
