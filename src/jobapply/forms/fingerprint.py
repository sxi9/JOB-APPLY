"""Form fingerprint: a hash of the form's shape, so a learned profile is reused on the same form
and discarded when the site changes it."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from jobapply.forms.fields import FormField

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUM = re.compile(r"\d{3,}")


def _norm(text: str) -> str:
    text = _UUID.sub("<uuid>", text or "")
    text = _NUM.sub("<n>", text)
    return re.sub(r"\s+", " ", text.lower()).strip()


def fingerprint(fields: list[FormField]) -> str:
    """Stable across job postings on the same ATS tenant: labels and types, not ids or values."""
    parts = sorted(f"{f.tag}|{f.type}|{_norm(f.label or f.group or f.name)}" for f in fields if not f.hidden)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
