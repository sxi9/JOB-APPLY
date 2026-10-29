"""Canonical list of what the ATS forms ask a candidate for.

Derived from a live field audit of the five built-in ATS on 2026-10-29 (see docs/FORM_FIELD_CATALOG.md):
Greenhouse (Stripe), Lever (Palantir), Ashby (Ramp), Workable (Hugging Face) and SmartRecruiters (Experian,
from the standard form; the live posting was behind a bot wall).

This module is data only: no I/O, no DB, no Playwright. `profile-completeness` and the dashboard import it to
tell a user which vault fields to fill before applying, and to pre-seed the answer bank with the recurring
screening questions.

- REQUIRED_PROFILE_KEYS: the `profile.*` / `derived.*` vault keys the ATS commonly ask for, each with a human
  label and whether it is *essential* (nearly every form blocks on it) or *optional* (common but skippable).
- COMMON_QUESTIONS: the recurring free-text / dropdown screening questions, each with the vault key that
  answers it, or "free" when the answer is genuinely per-posting and must come from the user / answer bank.

Every `key` below is a valid key for `jobapply.forms.profiles.resolve_key` (a member of MAPPABLE_KEYS, or the
literal "free"). The unit test in tests/test_required_inputs.py enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass

FREE = "free"  # answer is per-posting: collect from the user / answer bank, no single vault key fits


@dataclass(frozen=True)
class RequiredKey:
    key: str  # a profile.* / derived.* key understood by resolve_key
    label: str  # human label for the dashboard
    essential: bool  # True = almost every form requires it; False = common but optional


@dataclass(frozen=True)
class CommonQuestion:
    question: str  # canonical, hash-free phrasing of a recurring screening question
    key: str  # vault key that answers it, or FREE


# ---------------------------------------------------------------- required profile keys
# Essential = seen as a required field on (almost) every ATS: contact basics + resume.
# Optional = common across forms but not universally required (links, employment, EEO, screening-backed keys).

REQUIRED_PROFILE_KEYS: list[RequiredKey] = [
    # --- contact basics: required on every form audited ---
    RequiredKey("profile.first_name", "First name", True),
    RequiredKey("profile.last_name", "Last name", True),
    RequiredKey("derived.full_name", "Full name (single-field forms: Lever, Ashby)", True),
    RequiredKey("profile.email", "Email address", True),
    RequiredKey("profile.phone", "Phone number", True),
    RequiredKey("profile.location", "Current location / city", True),
    RequiredKey("derived.resume_path", "Resume / CV file", True),
    # --- links: common, usually optional (Workable/HF made LinkedIn+GitHub required as custom fields) ---
    RequiredKey("profile.linkedin", "LinkedIn URL", False),
    RequiredKey("profile.github", "GitHub URL", False),
    RequiredKey("profile.portfolio", "Portfolio / website URL", False),
    RequiredKey("derived.links", "Other links (combined)", False),
    # --- employment context ---
    RequiredKey("profile.current_company", "Current / previous employer", False),
    RequiredKey("profile.current_title", "Current / previous job title", False),
    RequiredKey("profile.years_experience", "Total years of experience", False),
    RequiredKey("profile.education", "Highest education", False),
    RequiredKey("profile.summary", "Short professional summary", False),
    RequiredKey("derived.cover_letter", "Cover letter", False),
    # --- work eligibility: frequently required screening dropdowns ---
    RequiredKey("profile.work_authorization", "Work authorization / right to work", False),
    RequiredKey("derived.needs_sponsorship", "Needs visa sponsorship (yes/no)", False),
    RequiredKey("derived.willing_to_relocate", "Willing to relocate (yes/no)", False),
    RequiredKey("profile.notice_period", "Notice period / earliest start date", False),
    RequiredKey("profile.current_ctc", "Current salary / CTC", False),
    RequiredKey("profile.expected_ctc", "Expected salary / CTC", False),
    # --- EEO self-identification (voluntary; Greenhouse + Lever expose these as standard fields) ---
    RequiredKey("profile.gender", "Gender (EEO, voluntary)", False),
    RequiredKey("profile.veteran_status", "Veteran status (EEO, voluntary)", False),
    RequiredKey("profile.disability_status", "Disability status (EEO, voluntary)", False),
]

ESSENTIAL_KEYS: list[str] = [r.key for r in REQUIRED_PROFILE_KEYS if r.essential]


# ---------------------------------------------------------------- recurring screening questions
# Canonical phrasings of questions that repeat across companies. The filler answers these from the mapped key;
# FREE means there is no single vault value (per-company motivation / sourcing), so it goes to the answer bank.

COMMON_QUESTIONS: list[CommonQuestion] = [
    CommonQuestion("Will you now or in the future require visa sponsorship to work in this location?",
                   "derived.needs_sponsorship"),
    CommonQuestion("Are you legally authorized to work in this location?", "profile.work_authorization"),
    CommonQuestion("What is your current country of residence?", "profile.location"),
    CommonQuestion("Are you willing to relocate?", "derived.willing_to_relocate"),
    CommonQuestion("What is your notice period / earliest start date?", "profile.notice_period"),
    CommonQuestion("What is your current salary / CTC?", "profile.current_ctc"),
    CommonQuestion("What is your expected salary / CTC?", "profile.expected_ctc"),
    CommonQuestion("How many years of relevant experience do you have?", "profile.years_experience"),
    CommonQuestion("What is your highest level of education?", "profile.education"),
    CommonQuestion("Who is your current or previous employer?", "profile.current_company"),
    CommonQuestion("What is your current or previous job title?", "profile.current_title"),
    CommonQuestion("What is your LinkedIn profile URL?", "profile.linkedin"),
    CommonQuestion("What is your GitHub profile URL?", "profile.github"),
    CommonQuestion("Have you previously been employed by this company?", FREE),
    CommonQuestion("How did you hear about this role?", FREE),
    CommonQuestion("Why do you want to work here?", FREE),
]
