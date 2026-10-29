"""Form profiles: which selector gets which vault value.

Built-in profiles live in `config/form_profiles/*.yaml` (see docs/FORM_PROFILES.md). Learned profiles are
rows in `form_profiles`. Both map a *field selector* to a *value key*:

    profile.first_name          a key in the user's profile vault
    derived.full_name           computed from the vault (full_name, resume_path, cover_letter, today)
    answers.<question text>     answer bank, then the AI, for custom questions
    const:Yes                   a literal
    skip                        leave the field alone

A selector is CSS, or `label=<text>` to match the field's visible label (case-insensitive, substring).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProfileStep(BaseModel):
    """Something to click before the form is visible (cookie banner, "Apply" button)."""

    click: str
    optional: bool = True
    wait_ms: int = 1500


class FormProfileSpec(BaseModel):
    ats: str
    detect: str = ""  # regex on the URL
    steps: list[ProfileStep] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)  # selector -> value key
    submit: str = "button[type=submit]"
    # Selectors whose presence means the form is already submitted / confirmation page.
    success: list[str] = Field(default_factory=list)
    # Fields to never touch even if the AI mapper wants to (hidden anti-bot inputs etc.).
    never: list[str] = Field(default_factory=list)
    origin: str = "builtin"

    def detect_re(self) -> re.Pattern[str] | None:
        return re.compile(self.detect, re.I) if self.detect else None


def load_builtin_profiles(folder: Path) -> dict[str, FormProfileSpec]:
    out: dict[str, FormProfileSpec] = {}
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec = FormProfileSpec.model_validate({"origin": "builtin", **data})
        out[spec.ats] = spec
    return out


def profile_for_url(url: str, profiles: dict[str, FormProfileSpec]) -> FormProfileSpec | None:
    for spec in profiles.values():
        rx = spec.detect_re()
        if rx and rx.search(url):
            return spec
    return None


# ---------------------------------------------------------------- value resolution


@dataclass
class VaultView:
    """Everything a value key can point at, for one user and one application."""

    profile: dict[str, Any]
    resume_path: str | None = None
    cover_letter: str = ""
    answers: dict[str, str] = field(default_factory=dict)  # question -> answer (already resolved)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.profile.get('first_name', '')} {self.profile.get('last_name', '')}".strip()


@dataclass
class ResolvedValue:
    key: str
    value: Any  # str for text, True/False for checkbox, path for file, None when unknown
    kind: str  # text | file | bool | question | skip
    question: str = ""  # for kind == question


YES = {"yes", "y", "true", "1"}


def resolve_key(key: str, vault: VaultView) -> ResolvedValue:
    key = (key or "").strip()
    if not key or key == "skip":
        return ResolvedValue(key, None, "skip")
    if key.startswith("const:"):
        return ResolvedValue(key, key[6:], "text")
    if key.startswith("profile."):
        name = key[8:]
        value = vault.profile.get(name)
        if name == "skills" and isinstance(value, dict):
            value = ", ".join(value)
        return ResolvedValue(key, "" if value is None else str(value), "text")
    if key.startswith("derived."):
        name = key[8:]
        if name == "full_name":
            return ResolvedValue(key, vault.full_name, "text")
        if name == "resume_path":
            return ResolvedValue(key, vault.resume_path, "file")
        if name == "cover_letter":
            return ResolvedValue(key, vault.cover_letter, "text")
        if name in ("needs_sponsorship", "willing_to_relocate"):
            return ResolvedValue(key, str(vault.profile.get(name, "")).lower() in YES, "bool")
        if name == "links":
            links = [vault.profile.get(k) for k in ("linkedin", "github", "portfolio") if vault.profile.get(k)]
            return ResolvedValue(key, "\n".join(links), "text")
        return ResolvedValue(key, vault.extra.get(name), "text")
    if key.startswith("answers."):
        question = key[8:]
        return ResolvedValue(key, vault.answers.get(question), "question", question=question)
    return ResolvedValue(key, None, "skip")


# Keys the AI mapper may choose from. Keeping the vocabulary closed keeps learned profiles safe to share.
MAPPABLE_KEYS: dict[str, str] = {
    "profile.first_name": "first / given name",
    "profile.last_name": "last / family name",
    "derived.full_name": "full name in one field",
    "profile.email": "email address",
    "profile.phone": "phone number",
    "profile.location": "current city / location",
    "profile.linkedin": "LinkedIn URL",
    "profile.github": "GitHub URL",
    "profile.portfolio": "portfolio / website URL",
    "derived.links": "any 'other links' field",
    "profile.current_company": "current employer",
    "profile.current_title": "current job title",
    "profile.years_experience": "total years of experience",
    "profile.notice_period": "notice period / earliest start",
    "profile.current_ctc": "current salary / CTC",
    "profile.expected_ctc": "expected salary / CTC",
    "profile.work_authorization": "work authorization / right to work",
    "derived.needs_sponsorship": "yes/no: needs visa sponsorship",
    "derived.willing_to_relocate": "yes/no: willing to relocate",
    "profile.education": "highest education",
    "profile.summary": "short professional summary / 'about you'",
    "profile.gender": "gender (EEO)",
    "profile.veteran_status": "veteran status (EEO)",
    "profile.disability_status": "disability status (EEO)",
    "derived.resume_path": "resume / CV file upload",
    "derived.cover_letter": "cover letter text",
    "answers.<question>": "a custom question: answer bank, then AI",
    "const:<value>": "a literal, e.g. const:Yes for a consent checkbox",
    "skip": "leave untouched (newsletter opt-ins, hidden anti-bot fields)",
}
