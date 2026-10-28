"""AI form mapper: reads the extracted fields once and maps each to a vault key.

The mapping holds keys only (no personal data), so it is saved as a learned profile shared by all users.
Free-text questions map to `answers.<question>`; anything the model cannot place is `skip`, and anything
that needs a human (anti-bot puzzles, "upload a video") is flagged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobapply.ai.base import LLM
from jobapply.ai.service import structured
from jobapply.forms.fields import FormField
from jobapply.forms.profiles import MAPPABLE_KEYS

MAP_SYSTEM = """You map the fields of a job application form to keys in a candidate's answers vault, so an
automated filler can complete the form. You only see field metadata, never the candidate's data.

Available keys:
{keys}

Rules:
- Use `answers.<question>` for custom questions with free text, numbers or yes/no that are not covered by
  a profile key. Put the question text verbatim (trimmed) after `answers.`.
- For a radio or checkbox group, map the group once with the key that decides it; the filler picks the
  option whose label matches the value (for yes/no keys: Yes/No options). For consent checkboxes that are
  required to submit (privacy policy, terms, "I certify"), use `const:Yes`. Marketing or SMS/WhatsApp
  opt-ins and newsletter sign-ups are `skip`.
- Hidden fields, honeypots, captcha tokens and search boxes are `skip`.
- Set `needs_human` when the field asks for something no vault key can supply (a puzzle, a video, a code
  sent elsewhere, a document the candidate must create); explain in `note`.
- `confidence` is 0-100: how sure you are the key is right.
"""

MAP_PROMPT = """Form URL: {url}
Page title: {title}

Fields (index, type, label, group, options):
{fields}

Return one entry per field index."""


class FieldMapping(BaseModel):
    idx: int
    key: str
    confidence: int = 70
    needs_human: bool = False
    note: str = ""


class FormMapping(BaseModel):
    mappings: list[FieldMapping] = Field(default_factory=list)


def map_fields(session, llm: LLM, user, fields: list[FormField], url: str, title: str = "") -> FormMapping:
    visible = [f for f in fields if not f.hidden]
    prompt = MAP_PROMPT.format(url=url, title=title, fields="\n".join(f.brief() for f in visible))
    keys = "\n".join(f"- {k}: {v}" for k, v in MAPPABLE_KEYS.items())
    out = structured(
        session,
        llm,
        user,
        task="form_map",
        system=MAP_SYSTEM.format(keys=keys),
        prompt=prompt,
        schema=FormMapping,
        max_tokens=8000,
        context={"fields": [f.__dict__ for f in visible], "url": url},
    )
    valid_idx = {f.idx for f in visible}
    out.mappings = [m for m in out.mappings if m.idx in valid_idx]
    for m in out.mappings:
        m.confidence = max(0, min(100, int(m.confidence)))
        if not _valid_key(m.key):
            m.key, m.confidence = "skip", 0
    return out


def _valid_key(key: str) -> bool:
    key = (key or "").strip()
    if key in ("skip",) or key.startswith(("const:", "answers.")):
        return True
    return key in MAPPABLE_KEYS


def mapping_to_profile_fields(
    fields: list[FormField], mapping: FormMapping, min_confidence: int = 60
) -> dict[str, str]:
    """selector -> key, for saving as a learned profile. Low-confidence entries are left out so they
    go through the mapper again next time instead of being wrong forever."""
    by_idx = {f.idx: f for f in fields}
    out: dict[str, str] = {}
    for m in mapping.mappings:
        f = by_idx.get(m.idx)
        if f is None or m.key == "skip" or m.needs_human or m.confidence < min_confidence:
            continue
        selector = f"label={f.group or f.label}" if (f.group or f.label) and not _stable(f) else f.selector
        out[selector] = m.key
    return out


def _stable(f: FormField) -> bool:
    """Ids/names that look generated per posting are not reusable selectors."""
    import re

    return bool(f.name or f.id) and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}|\d{5,}", f.name + f.id)
