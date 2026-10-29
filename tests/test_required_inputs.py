"""The canonical required-inputs catalog stays importable and consistent with the resolver vocabulary."""

from jobapply.forms.profiles import MAPPABLE_KEYS, VaultView, resolve_key
from jobapply.forms.required_inputs import (
    COMMON_QUESTIONS,
    ESSENTIAL_KEYS,
    FREE,
    REQUIRED_PROFILE_KEYS,
    CommonQuestion,
    RequiredKey,
)


def _valid_vault_key(key: str) -> bool:
    return key in MAPPABLE_KEYS or key.startswith(("profile.", "derived."))


def test_required_profile_keys_are_resolvable_and_shaped():
    assert REQUIRED_PROFILE_KEYS, "catalog must not be empty"
    seen = set()
    for r in REQUIRED_PROFILE_KEYS:
        assert isinstance(r, RequiredKey)
        assert r.key not in seen, f"duplicate key {r.key}"
        seen.add(r.key)
        assert _valid_vault_key(r.key), f"{r.key} is not a resolvable vault key"
        assert r.label and isinstance(r.essential, bool)
    # The essential set is the contact-plus-resume basics every form needs.
    for must in ("profile.email", "profile.first_name", "derived.resume_path"):
        assert must in ESSENTIAL_KEYS


def test_common_questions_map_to_vault_keys_or_free():
    assert COMMON_QUESTIONS
    questions = set()
    for q in COMMON_QUESTIONS:
        assert isinstance(q, CommonQuestion)
        assert q.question and q.question not in questions, "questions must be unique and non-empty"
        questions.add(q.question)
        assert q.key == FREE or _valid_vault_key(q.key), f"{q.question!r} -> bad key {q.key}"


def test_mapped_keys_actually_resolve_against_a_vault():
    """Every non-FREE key resolves without error against a populated demo vault."""
    from jobapply.seed import DEMO_PROFILE

    vault = VaultView(profile=DEMO_PROFILE, resume_path="/r.pdf", cover_letter="CL")
    for r in REQUIRED_PROFILE_KEYS:
        rv = resolve_key(r.key, vault)
        assert rv.kind in ("text", "file", "bool", "question", "skip")
    for q in COMMON_QUESTIONS:
        if q.key != FREE:
            resolve_key(q.key, vault)  # must not raise
