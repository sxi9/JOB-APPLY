"""Pull one-time codes and verification links out of mail."""

from __future__ import annotations

import re

from jobapply.mail.imap import ImapReader, Mail, sender_matches

_CODE_CONTEXT = re.compile(
    r"(?:code|otp|passcode|pin|verification|verify|confirm(?:ation)?|token|2fa|one[- ]time)[^0-9]{0,60}?(\b\d{4,8}\b)",
    re.I | re.S,
)
_CODE_BEFORE = re.compile(
    r"(\b\d{4,8}\b)[^0-9]{0,40}?(?:is your|is the|as your)[^.]{0,30}?(?:code|otp|pin)", re.I | re.S
)
_ANY_CODE = re.compile(r"(?<![\d-])(\d{6})(?![\d-])")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_VERIFY_WORDS = re.compile(r"verif|confirm|activate|validate|token=|magic|login|signin|sign-in|auth", re.I)
_SKIP_URL = re.compile(
    r"unsubscribe|privacy|terms|facebook|twitter|linkedin\.com/company|instagram|youtube|support", re.I
)


def extract_code(text: str, min_len: int = 4, max_len: int = 8) -> str | None:
    if not text:
        return None
    for rx in (_CODE_CONTEXT, _CODE_BEFORE):
        for m in rx.finditer(text):
            code = m.group(1)
            if min_len <= len(code) <= max_len:
                return code
    m = _ANY_CODE.search(text)
    return m.group(1) if m and min_len <= 6 <= max_len else None


def extract_links(text: str, html: str = "", verify_only: bool = True) -> list[str]:
    seen: list[str] = []
    for source in (html, text):
        for url in _URL.findall(source or ""):
            url = url.rstrip(".,;")
            if url in seen or _SKIP_URL.search(url):
                continue
            if verify_only and not _VERIFY_WORDS.search(url):
                continue
            seen.append(url)
    return seen


def verification_link(mail: Mail) -> str | None:
    links = extract_links(mail.body, mail.html)
    return links[0] if links else None


def wait_for_code(
    reader: ImapReader, sender: str = "", subject_words: tuple[str, ...] = (), timeout: float = 120, poll: float = 5
) -> str | None:
    def pred(m: Mail) -> bool:
        if not sender_matches(m, sender):
            return False
        if subject_words and not any(w.lower() in m.subject.lower() for w in subject_words):
            return False
        return extract_code(m.subject + "\n" + m.body) is not None

    mail = reader.wait_for(pred, timeout=timeout, poll=poll)
    return extract_code(mail.subject + "\n" + mail.body) if mail else None


def wait_for_link(reader: ImapReader, sender: str = "", timeout: float = 180, poll: float = 5) -> str | None:
    mail = reader.wait_for(
        lambda m: sender_matches(m, sender) and verification_link(m) is not None, timeout=timeout, poll=poll
    )
    return verification_link(mail) if mail else None
