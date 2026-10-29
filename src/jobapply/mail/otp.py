"""Pull one-time codes and verification links out of mail.

Hardened against the usual false positives: phone numbers, order / ticket numbers, dates and years are
masked before a code is looked for; codes written as "123 456" or "123-456" are joined; HTML-only bodies
are flattened to text first. `wait_for_code` can be told to accept only mail that arrived after the
request was made (`after=`) and to skip codes that were already consumed (`CodeRegistry`, kept in the
`app_settings` table as salted hashes, never the codes themselves).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from jobapply.collectors.base import html_to_text
from jobapply.logs import get_logger
from jobapply.mail.imap import ImapReader, Mail, sender_matches

log = get_logger(__name__)

# --------------------------------------------------------------------------- code extraction

_SEP = r"[   \-–‑]"
# A run of 4 to 8 digits, optionally separated by single spaces or dashes ("123 456", "12-34-56").
_DIGIT_RUN = re.compile(rf"(?<![\d#])(?<!\d-)(\d(?:{_SEP}?\d){{3,7}})(?![\d\-])")
_CODE_WORDS = (
    r"\b(?:security|verification|verify|confirmation|confirm|login|log-in|sign-in|signin|access|authentication|"
    r"auth|one[- ]time|otp|2fa|two[- ]factor|passcode|pass code|pin|token|activation|validation)"
)
# "...code is 482913", "OTP: 7731", "security code 123 456"
_CODE_CONTEXT = re.compile(
    rf"(?:{_CODE_WORDS}\s*(?:code|password|pin|number)?|\bcode\b|\botp\b|\bpin\b|\bpasscode\b)"
    rf"[^0-9]{{0,60}}?{_DIGIT_RUN.pattern}",
    re.I | re.S,
)
# "771234 is your Naukri login code"
_CODE_BEFORE = re.compile(
    rf"{_DIGIT_RUN.pattern}[^0-9]{{0,40}}?(?:is your|is the|as your|as the)[^.]{{0,40}}?(?:code|otp|pin|passcode)",
    re.I | re.S,
)
_ANY_CODE = re.compile(r"(?<![\d\-])(\d{6})(?![\d\-])")
_HTML_HINT = re.compile(r"<(?:html|body|div|p|td|span|br|a|table)\b", re.I)

# Things that are not codes. Masked (replaced by spaces) before extraction.
_MASKS = [
    # ISO dates: 2026-10-28
    re.compile(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    # Times: 10:30, 10:30:00
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    # Order / invoice / ticket / reference / application numbers: "Order #12345678", "Ref: 445566", "Job ID 9981"
    re.compile(
        r"\b(?:order|invoice|receipt|ticket|case|ref(?:erence)?|req(?:uisition)?|job|application|candidate|"
        r"account|customer|transaction|tracking|confirmation number|id)\s*(?:id|no\.?|number|num|#)?\s*[:#]?\s*"
        r"[A-Z]*-?\d[\d\-]*",
        re.I,
    ),
    # A bare "#12345" is a reference, not a code.
    re.compile(r"#\s?\d+"),
    # Years in prose: "in 2026", "© 2026", "October 28, 2026", "Oct 2026"
    re.compile(
        r"(?:©|\(c\)|copyright|\bin|\bsince|\byear|\bof|\bjan\w*|\bfeb\w*|\bmar\w*|\bapr\w*|\bmay|\bjun\w*|\bjul\w*|"
        r"\baug\w*|\bsep\w*|\boct\w*|\bnov\w*|\bdec\w*)\.?\s*\d{0,2},?\s*((?:19|20)\d{2})\b",
        re.I,
    ),
    # Durations: "10 minutes", "600 seconds", "24 hours"
    re.compile(r"\b\d{1,4}\s*(?:minutes?|mins?|seconds?|secs?|hours?|hrs?|days?)\b", re.I),
]
# Anything that looks like a phone number: digits with at most two separator characters between them,
# 10+ digits in all (or 7+ behind a "+"/"00" country-code prefix). Newlines are not separators, so a code
# followed by a year on the next line is not glued into a "phone number".
_PHONE = re.compile(r"(?:\+|\b00)?\(?\d(?:[ .\- ()]{0,2}\d){6,}")


# Day-first or month-first numeric dates: 28/10/2026, 10.28.26, 1-5-2026. Only masked when the parts can be
# a date, so a dashed code like "12-34-56" survives.
_DMY = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|(?:19|20)\d{2})\b")


def _date_like(m: re.Match[str]) -> str:
    a, b = int(m.group(1)), int(m.group(2))
    plausible = (1 <= a <= 31 and 1 <= b <= 12) or (1 <= a <= 12 and 1 <= b <= 31)
    return " " * len(m.group(0)) if plausible else m.group(0)


def _mask(text: str) -> str:
    text = _DMY.sub(_date_like, text)
    for rx in _MASKS:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)

    def _phone(m: re.Match[str]) -> str:
        s = m.group(0)
        digits = sum(ch.isdigit() for ch in s)
        return " " * len(s) if digits >= 10 or (digits >= 7 and s.startswith(("+", "00"))) else s

    return _PHONE.sub(_phone, text)


def _join(raw: str) -> str:
    return re.sub(_SEP, "", raw)


def _plain(text: str) -> str:
    """HTML-only bodies become text first; entities are unescaped so `&nbsp;` between digits is a space."""
    if _HTML_HINT.search(text):
        return html_to_text(text)
    return text


def extract_code(text: str, min_len: int = 4, max_len: int = 8) -> str | None:
    """The one-time code in `text`, or None. Prefers a number next to a code-ish word, then "NNNNNN is your
    code", then a lone 6-digit number. Codes written with spaces or dashes are returned joined."""
    if not text:
        return None
    cleaned = _mask(_plain(text))
    for rx in (_CODE_CONTEXT, _CODE_BEFORE):
        for m in rx.finditer(cleaned):
            code = _join(m.group(1))
            if min_len <= len(code) <= max_len:
                return code
    if not min_len <= 6 <= max_len:
        return None
    m = _ANY_CODE.search(cleaned)
    if m:
        return m.group(1)
    # Last resort, same strength as the contiguous form: a lone 6-digit run written with spaces or dashes
    # ("Enter 123 456 to verify your email").
    for m in _DIGIT_RUN.finditer(cleaned):
        code = _join(m.group(1))
        if len(code) == 6:
            return code
    return None


def mask_digits(text: str) -> str:
    """`text` with every run of 4+ digits (spaces / dashes allowed between them) replaced by ****, so a
    subject line that carries the one-time code can be logged or shown without leaking it."""
    return re.sub(rf"\d(?:{_SEP}?\d){{3,}}", "****", text or "")


def mail_text(mail: Mail) -> str:
    """Subject plus body; falls back to the HTML part flattened when the text body is empty."""
    body = mail.body or (html_to_text(mail.html) if mail.html else "")
    return f"{mail.subject}\n{body}"


def code_from_mail(mail: Mail, min_len: int = 4, max_len: int = 8) -> str | None:
    return extract_code(mail_text(mail), min_len=min_len, max_len=max_len)


# --------------------------------------------------------------------------- links

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_VERIFY_WORDS = re.compile(r"verif|confirm|activate|validate|token=|magic|login|signin|sign-in|auth", re.I)
_SKIP_URL = re.compile(
    r"unsubscribe|privacy|terms|facebook|twitter|linkedin\.com/company|instagram|youtube|support", re.I
)


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


# --------------------------------------------------------------------------- sender / subject filters

# Named sender presets: `sender="workday"` matches whatever an ATS sends from. A value that is not a preset
# is used as-is (domain, address or regex; see `sender_matches`).
SENDER_PRESETS: dict[str, str] = {
    "workday": r"workday|myworkday",
    "greenhouse": r"greenhouse",
    "lever": r"lever\.co|hire\.lever",
    "ashby": r"ashbyhq",
    "smartrecruiters": r"smartrecruiters",
    "icims": r"icims",
    "taleo": r"taleo|oracle",
    "successfactors": r"successfactors|sap\.com",
    "linkedin": r"linkedin\.com",
    "naukri": r"naukri\.com|info\.naukri",
    "instahyre": r"instahyre",
    "cutshort": r"cutshort",
    "wellfound": r"wellfound|angel\.co",
    "google": r"google\.com|accounts\.google",
    "microsoft": r"microsoft|live\.com|outlook",
    "noreply": r"no-?reply|do-?not-?reply|donotreply|notification",
}
MailFilter = Callable[[Mail], bool]


def sender_pattern(value: str) -> str:
    """Resolve a preset name (case-insensitive) to its pattern; anything else passes through."""
    key = (value or "").strip().lower()
    return SENDER_PRESETS.get(key, value or "")


def sender_ok(mail: Mail, sender: str) -> bool:
    """True when `mail` comes from `sender`: a preset name (always a regex over address and display name),
    or a domain / address / regex as `sender_matches` understands them. Empty matches everything."""
    key = (sender or "").strip().lower()
    if not key:
        return True
    if key in SENDER_PRESETS:
        rx = re.compile(SENDER_PRESETS[key], re.I)
        return rx.search(mail.sender) is not None or rx.search(mail.sender_name or "") is not None
    return sender_matches(mail, sender)


def mail_filter(
    sender: str = "", subject_words: tuple[str, ...] | list[str] = (), after: datetime | None = None, skew: float = 120
) -> MailFilter:
    """A predicate over Mail: sender preset / domain / regex, any-of subject words, and (with `after`) only mail
    that arrived after that instant, with `skew` seconds of tolerance for clocks that disagree."""
    words = tuple(w.lower() for w in subject_words if w)
    cutoff = None
    if after is not None:
        after = after if after.tzinfo else after.replace(tzinfo=UTC)
        cutoff = after.astimezone(UTC) - timedelta(seconds=skew)

    def pred(m: Mail) -> bool:
        if not sender_ok(m, sender):
            return False
        if words and not any(w in m.subject.lower() for w in words):
            return False
        if cutoff is not None:
            when = m.received_at if m.received_at.tzinfo else m.received_at.replace(tzinfo=UTC)
            if when < cutoff:
                return False
        return True

    return pred


# --------------------------------------------------------------------------- used-code registry

USED_TTL = timedelta(hours=24)
_USED_KEY = "otp_used:{user_id}"


class CodeRegistry:
    """Codes already consumed for one user, so a stale OTP mail is never typed twice. Stored in
    `app_settings` under `otp_used:<user id>` as salted SHA-256 digests with a timestamp; entries expire
    after 24 hours. The code itself never touches the database or the logs."""

    def __init__(self, session: Any, user_id: int):
        self.session = session
        self.user_id = int(user_id)

    @property
    def key(self) -> str:
        return _USED_KEY.format(user_id=self.user_id)

    def _digest(self, code: str) -> str:
        return hashlib.sha256(f"{self.user_id}:{code.strip()}".encode()).hexdigest()

    def _load(self) -> list[dict[str, Any]]:
        from jobapply.switches import get_value

        raw = get_value(self.session, self.key)
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except ValueError:
            return []
        cutoff = datetime.now(UTC) - USED_TTL
        keep = []
        for row in rows if isinstance(rows, list) else []:
            try:
                at = datetime.fromisoformat(row["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if (at if at.tzinfo else at.replace(tzinfo=UTC)) >= cutoff:
                keep.append(row)
        return keep

    def is_used(self, code: str) -> bool:
        digest = self._digest(code)
        return any(row.get("h") == digest for row in self._load())

    def mark_used(self, code: str) -> None:
        from jobapply.switches import set_value

        rows = [r for r in self._load() if r.get("h") != self._digest(code)]
        rows.append({"h": self._digest(code), "at": datetime.now(UTC).isoformat()})
        set_value(self.session, self.key, json.dumps(rows[-200:]))
        self.session.commit()

    def __len__(self) -> int:
        return len(self._load())


# --------------------------------------------------------------------------- waiting

def wait_for_code(
    reader: ImapReader,
    sender: str = "",
    subject_words: tuple[str, ...] = (),
    timeout: float = 120,
    poll: float = 5,
    after: datetime | None = None,
    registry: CodeRegistry | None = None,
    min_len: int = 4,
    max_len: int = 8,
) -> str | None:
    """Poll the mailbox for a one-time code.

    `sender` is a preset name (workday, greenhouse, noreply, ...), a domain, an address or a regex;
    `subject_words` any-of words the subject must contain; `after` rejects mail older than the request
    (pass the moment you clicked "send code"); `registry` skips codes already consumed and records the
    returned one. Returns the code or None on timeout; the code is never logged."""
    base = mail_filter(sender, subject_words, after=after)

    def pred(m: Mail) -> bool:
        if not base(m):
            return False
        code = code_from_mail(m, min_len, max_len)
        if code is None:
            return False
        if registry is not None and registry.is_used(code):
            log.info("otp already used, waiting for a newer one", sender=m.sender_domain,
                     subject=mask_digits(m.subject)[:60])
            return False
        return True

    since = after - timedelta(minutes=5) if after is not None else None
    mail = reader.wait_for(pred, timeout=timeout, poll=poll, since=since)
    if mail is None:
        return None
    code = code_from_mail(mail, min_len, max_len)
    if code is not None and registry is not None:
        registry.mark_used(code)
    if code is not None:
        # Most providers put the code in the subject line: digit runs are masked before anything is logged.
        log.info("otp received", sender=mail.sender_domain, subject=mask_digits(mail.subject)[:60], length=len(code))
    return code


def wait_for_link(
    reader: ImapReader, sender: str = "", timeout: float = 180, poll: float = 5, after: datetime | None = None
) -> str | None:
    base = mail_filter(sender, after=after)
    since = after - timedelta(minutes=5) if after is not None else None
    mail = reader.wait_for(
        lambda m: base(m) and verification_link(m) is not None, timeout=timeout, poll=poll, since=since
    )
    return verification_link(mail) if mail else None
