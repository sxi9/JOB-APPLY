"""Per-question answer bank: custom questions are stored by a hash of their normalized text."""

from __future__ import annotations

import hashlib
import re

_NOISE = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    text = _NOISE.sub(" ", (text or "").lower())
    text = re.sub(r"\b(please|kindly)\b|\(optional\)|\brequired\b", " ", text)
    return _SPACE.sub(" ", text).strip()


def question_hash(text: str) -> str:
    return hashlib.sha1(normalize_question(text).encode()).hexdigest()[:20]
