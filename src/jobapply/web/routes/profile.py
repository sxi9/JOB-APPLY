from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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
_FIELD_LABELS = {key: label for key, label, _kind in PROFILE_FIELDS}

# Used only when the sibling module (built by the forms agent) is not importable. Keeps this page working
# and the tests self-contained. Shape: {vault_key: "essential" | "optional"}.
_FALLBACK_REQUIRED: dict[str, str] = {
    "first_name": "essential", "last_name": "essential", "email": "essential",
    "phone": "essential", "location": "essential", "work_authorization": "essential",
    "linkedin": "optional", "github": "optional", "years_experience": "optional", "summary": "optional",
}


def _tier(value: Any) -> str:
    """Normalize however the required-keys table flags a field to 'essential' or 'optional'."""
    if isinstance(value, bool):
        return "essential" if value else "optional"
    text = str(value).strip().lower()
    if text in ("optional", "amber", "nice", "false", "0", "no"):
        return "optional"
    return "essential"


# Namespaced `derived.*` catalog keys that map to a profile vault field edited on this page (the input id is
# the bare name). Other derived keys (full_name is first+last, resume_path has its own upload section,
# links/cover_letter are composed) have no field on this page, so they are dropped from the completeness card.
_DERIVED_TO_FIELD: dict[str, str] = {
    "needs_sponsorship": "needs_sponsorship",
    "willing_to_relocate": "willing_to_relocate",
}


def _vault_field(key: str) -> str | None:
    """The bare profile-vault field id a catalog key refers to (so it lines up with `_FIELD_LABELS` and the
    template's `#<id>` anchors), or None when the key has no editable field on this page."""
    key = str(key).strip()
    if key.startswith("profile."):
        return key[len("profile."):] or None
    if key.startswith("derived."):
        return _DERIVED_TO_FIELD.get(key[len("derived."):])
    return key or None


def _normalize_required(raw: Any) -> list[tuple[str, str]]:
    """Accept the required-keys table in any plausible shape (dict, list of keys, list of tuples, or list of
    RequiredKey-style objects) and reduce each entry to a (vault field id, tier) pair, dropping keys with no
    editable field on this page and de-duplicating."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(k), _tier(v)) for k, v in raw.items()]
    else:
        for entry in raw or []:
            if isinstance(entry, str):
                pairs.append((entry, "essential"))
            elif isinstance(entry, (list, tuple)) and entry:
                pairs.append((str(entry[0]), _tier(entry[-1]) if len(entry) > 1 else "essential"))
            elif hasattr(entry, "key"):  # RequiredKey dataclass (or any object exposing .key / .essential)
                pairs.append((str(entry.key), _tier(getattr(entry, "essential", True))))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, tier in pairs:
        field_id = _vault_field(key)
        if field_id is None or field_id in seen:
            continue
        seen.add(field_id)
        out.append((field_id, tier))
    return out


def _required_profile_keys() -> list[tuple[str, str]]:
    """The required vault keys from the forms module, or the inline fallback if it is not present yet."""
    try:
        from jobapply.forms.required_inputs import REQUIRED_PROFILE_KEYS as raw
    except Exception:  # noqa: BLE001 - the sibling module may not exist yet; the page must still work
        raw = _FALLBACK_REQUIRED
    return _normalize_required(raw)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def missing_fields(profile: dict) -> list[dict[str, str]]:
    """Required vault fields this user has not filled, each with a display label and its tier."""
    out: list[dict[str, str]] = []
    for key, tier in _required_profile_keys():
        if _is_empty(profile.get(key)):
            out.append({"key": key, "label": _FIELD_LABELS.get(key, key.replace("_", " ").capitalize()),
                        "tier": tier})
    return out


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
    resume_file = str(profile.get("resume_file") or "")
    resume_file_name = Path(resume_file).name if resume_file else ""
    return ctx.render("profile.html", fields=PROFILE_FIELDS, profile=profile, skills=skills,
                      resume_json=resume_json, answers=answers, missing=missing_fields(profile),
                      pending_answers=sum(1 for a in answers if not a.approved),
                      resume_file_name=resume_file_name)


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


@router.post("/profile/resume-file")
async def resume_file_upload(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Store the user's own resume as `<resumes_dir>/<user id>/uploaded.<ext>` and remember its path.

    The apply pipeline (via `master_resume_pdf`) then sends this file instead of the generated one.
    """
    form = await read_form(request)
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        flash(request, "Choose a file", "error")
        return RedirectResponse("/profile#resume-file", status_code=303)
    ext = Path(str(upload.filename)).suffix.lower()
    if ext not in (".pdf", ".docx"):
        flash(request, "Upload a PDF or DOCX", "error")
        return RedirectResponse("/profile#resume-file", status_code=303)
    folder = get_settings().resumes_dir / str(ctx.view.id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("uploaded.*"):  # keep exactly one uploaded resume per user
        old.unlink(missing_ok=True)
    path = folder / f"uploaded{ext}"
    path.write_bytes(await upload.read())
    profile = dict(ctx.view.profile or {})
    profile["resume_file"] = str(path)
    ctx.view.profile = profile
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, f"Uploaded {path.name}")
    return RedirectResponse("/profile#resume-file", status_code=303)


@router.post("/profile/resume-file/remove")
async def resume_file_remove(request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)  # CSRF check
    profile = dict(ctx.view.profile or {})
    path = profile.pop("resume_file", None)
    if path:
        Path(str(path)).unlink(missing_ok=True)
    ctx.view.profile = profile
    ctx.session.add(ctx.view)
    ctx.session.commit()
    flash(request, "Removed uploaded resume")
    return RedirectResponse("/profile#resume-file", status_code=303)


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
