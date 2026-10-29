"""Contact discovery, referral drafting, and the gated send path for LinkedIn referrals.

DRAFT-ONLY by default. `parse_hiring_team` / `find_contacts` read the logged-in job page's "Meet the hiring
team" block and the job poster; `generate_referral_drafts` writes one `ReferralDraft` row per contact (status
`draft`) with an AI/template message. None of that contacts anyone.

`send_referral` is the only function that can actually reach a person, and it does so ONLY when every gate
passes: the draft belongs to the acting user, the global referral send switch is on
(`switches.referral_send_enabled`, default off), the global dry-run master switch is off
(`switches.global_dry_run`, on by default), the draft is `approved`, the per-user daily cap
(`config.referral_daily_cap`) is not reached, the kill switch is off and we are inside the LinkedIn active
hours. A disabled/ungated call leaves the draft untouched and returns a reason.
The actual browser fill is behind `_perform_send` so tests never open a browser or touch the network.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlmodel import Session, select

from jobapply.ai.service import referral_message
from jobapply.config import get_settings
from jobapply.logs import get_logger
from jobapply.models import Job, ReferralDraft, User, utcnow
from jobapply.repo import UserScope
from jobapply.switches import global_dry_run, kill_switch_on, referral_send_enabled

log = get_logger(__name__)

SITE = "linkedin"

# ------------------------------------------------------------------ Contact


@dataclass
class Contact:
    """One person on a job's hiring team (or the job poster), parsed from a logged-in LinkedIn job page."""

    name: str
    headline: str = ""
    profile_url: str = ""
    connect_url: str | None = None  # set when a Connect action is available (i.e. not already connected)
    is_recruiter: bool = False


# ------------------------------------------------------------------ parsing (pure)

_IN_HREF = re.compile(r'href="([^"]*linkedin\.com/in/[^"#?]+[^"]*)"', re.I)
_IN_HREF_REL = re.compile(r'href="(/in/[^"#?]+[^"]*)"', re.I)
_ARIA_NAME = re.compile(r'aria-hidden="true"[^>]*>(.*?)</span>', re.I | re.S)
_STRONG_NAME = re.compile(r"<strong[^>]*>(.*?)</strong>", re.I | re.S)
_ALT_NAME = re.compile(r'alt="([^"]+)"', re.I)
_HEADLINE = re.compile(
    r'class="[^"]*(?:hirer-card__hirer-job-title|hirer-card__subtitle|job-title|t-black[^"]*t-normal|'
    r'job-poster__headline|artdeco-entity-lockup__subtitle)[^"]*"[^>]*>(.*?)</(?:div|span|p)>',
    re.I | re.S,
)
_RECRUITER = re.compile(r"recruit|talent|sourcer|staffing|\bhr\b|people team|people ops|hiring", re.I)
_CONNECT = re.compile(r'(?:aria-label|data-control-name)="[^"]*\bconnect\b[^"]*"', re.I)
_TAGS = re.compile(r"<[^>]+>")

# Where cards live on a job page. `hirer-card` is specific to the "Meet the hiring team" block; the poster
# sits in a container whose class carries `job-poster` (the outer div, before the name/anchor). Splitting on
# the hirer marker bounds each person's card; the poster is read from a window at its container marker (not
# split, so the name's own `jobs-poster__name` class cannot separate the anchor from the headline).
_HIRER_SPLIT = re.compile(r"hirer-card__container|hirer-card ", re.I)
_POSTER_MARKER = re.compile(r'class="[^"]*(?:job-poster|jobs-poster__container)[^"]*"', re.I)


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", fragment))).strip()


def _normalize_profile_url(url: str) -> str:
    """A clean `https://www.linkedin.com/in/<slug>` URL, query and fragment dropped. Empty when not a
    profile link."""
    url = html_lib.unescape(url or "").strip()
    if not url:
        return ""
    if url.startswith("/in/"):
        url = "https://www.linkedin.com" + url
    parts = urlsplit(url)
    if "linkedin.com" not in parts.netloc.lower():
        return ""
    path = parts.path.rstrip("/")
    if "/in/" not in path:
        return ""
    slug = path.split("/in/", 1)[1].split("/")[0]
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def _slug_name(url: str) -> str:
    """`.../in/jane-recruiter-12ab` -> `Jane Recruiter`: a readable name from the slug when no name text is
    present (trailing hash ids and digits dropped)."""
    slug = url.rstrip("/").split("/in/", 1)[-1].split("/")[0]
    parts = [p for p in slug.split("-") if p and not p.isdigit() and not re.fullmatch(r"[0-9a-f]{4,}", p)]
    return " ".join(p.capitalize() for p in parts) if parts else ""


def _name_in(window: str) -> str:
    for pat in (_ARIA_NAME, _STRONG_NAME):
        m = pat.search(window)
        if m:
            name = _text(m.group(1))
            if name and "…" not in name and len(name) <= 80:
                return name
    m = _ALT_NAME.search(window)
    if m:
        name = _text(m.group(1))
        # alt text is sometimes "Photo of Jane" or a generic label; keep only plausible person names
        name = re.sub(r"^(photo of|picture of)\s+", "", name, flags=re.I).strip()
        if name and 2 <= len(name) <= 80 and "logo" not in name.lower():
            return name
    return ""


def _headline_in(window: str) -> str:
    m = _HEADLINE.search(window)
    return _text(m.group(1)) if m else ""


def _contact_from_card(card: str) -> Contact | None:
    """One Contact from a card chunk: its first profile anchor, plus the nearest name and headline. A card is
    one person, so only the first anchor is used; name/headline are read from a bounded window so a chunk that
    runs long (the last card) cannot pull in unrelated profiles."""
    m = _IN_HREF.search(card) or _IN_HREF_REL.search(card)
    if not m:
        return None
    url = _normalize_profile_url(m.group(1))
    if not url:
        return None
    window = card[max(0, m.start() - 200): m.start() + 2000]
    name = _name_in(window) or _slug_name(url) or "LinkedIn member"
    headline = _headline_in(window)
    is_recruiter = bool(_RECRUITER.search(headline))
    connect_url = url if _CONNECT.search(window) else None
    return Contact(name=name, headline=headline, profile_url=url, connect_url=connect_url,
                   is_recruiter=is_recruiter)


def _add(out: list[Contact], seen: dict[str, Contact], contact: Contact | None) -> None:
    if contact is None or not contact.profile_url:
        return
    existing = seen.get(contact.profile_url)
    if existing is None:
        seen[contact.profile_url] = contact
        out.append(contact)
        return
    # merge: keep the richer values across duplicate cards (e.g. a photo anchor then a name anchor)
    if (not existing.headline) and contact.headline:
        existing.headline = contact.headline
        existing.is_recruiter = existing.is_recruiter or contact.is_recruiter
    if existing.name in ("", "LinkedIn member") and contact.name not in ("", "LinkedIn member"):
        existing.name = contact.name
    existing.connect_url = existing.connect_url or contact.connect_url


def parse_hiring_team(html: str) -> list[Contact]:
    """Parse the "Meet the hiring team" cards and the job poster from a logged-in LinkedIn job page.

    Pure HTML parsing, no browser. Returns contacts in page order (hiring-team cards first, then the poster),
    deduplicated by profile URL. Returns `[]` when the page has neither."""
    html = html or ""
    out: list[Contact] = []
    seen: dict[str, Contact] = {}
    for card in _HIRER_SPLIT.split(html)[1:]:
        _add(out, seen, _contact_from_card(card))
    poster = _POSTER_MARKER.search(html)
    if poster is not None:
        _add(out, seen, _contact_from_card(html[poster.start(): poster.start() + 2500]))
    return out


# ------------------------------------------------------------------ live discovery (guarded by a hook)


def _page_html(page: Any, job_url: str, timeout_ms: int = 60000) -> str:
    """Open the job page and return its rendered HTML. The hook `find_contacts` calls, so tests pass a fake
    page (goto / wait_for_timeout / content) and never open a real browser."""
    page.goto(job_url, wait_until="domcontentloaded", timeout=timeout_ms)
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        try:
            waiter(1500)  # the hiring-team card renders client-side after domcontentloaded
        except Exception:  # noqa: BLE001
            pass
    try:
        return page.content() or ""
    except Exception as exc:  # noqa: BLE001 - page mid-navigation
        log.debug("could not read job page html", error=str(exc)[:160])
        return ""


def find_contacts(page: Any, job_url: str) -> list[Contact]:
    """Navigate the logged-in job page and parse its hiring team. Reading only; contacts no one.

    The window is visible unless the browser was launched headless (`JOBAPPLY_LINKEDIN_HEADLESS` /
    `linkedin_headless`); the referral CLI commands launch headless explicitly, so on a machine that cannot
    open a window the discovery still runs. In tests `page` is a fake, so no browser opens at all."""
    return parse_hiring_team(_page_html(page, job_url))


# ------------------------------------------------------------------ drafting (no contact made)


def _channel_for(contact: Contact) -> str:
    """A connection note when the contact is not yet connected (a Connect action is available), otherwise a
    direct message. When no Connect affordance was parsed (unknown state, or already connected) this defaults
    to a direct message; a connection note is only chosen when a Connect action was actually detected."""
    return "message" if not contact.connect_url else "connection_note"


def generate_referral_drafts(session: Session, user: User, job: Job, contacts: list[Contact],
                             llm: Any = None) -> list[ReferralDraft]:
    """Create one `ReferralDraft` (status `draft`) per contact, deduped so the same contact is not drafted
    twice for the same job. Drafting only: this contacts no one. The caller commits."""
    scope = UserScope(session, user)
    existing = {d.contact_profile_url for d in scope.all(ReferralDraft, ReferralDraft.job_id == job.id)
                if d.contact_profile_url}
    out: list[ReferralDraft] = []
    for contact in contacts:
        if not contact.profile_url or contact.profile_url in existing:
            continue
        existing.add(contact.profile_url)
        channel = _channel_for(contact)
        body = referral_message(session, llm, user, job, contact, channel=channel)
        draft = ReferralDraft(
            user_id=user.id, job_id=job.id, contact_name=contact.name, contact_headline=contact.headline,
            contact_profile_url=contact.profile_url, channel=channel, body=body, status="draft",
        )
        session.add(draft)
        out.append(draft)
    session.flush()
    log.info("referral drafts generated", user=user.username, job=job.id, created=len(out),
             contacts=len(contacts))
    return out


# ------------------------------------------------------------------ gated send


def referrals_sent_today(session: Session, user_id: int, now: datetime | None = None) -> int:
    """How many referrals this user has actually sent since midnight UTC today."""
    now = now or utcnow()
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(session.exec(
        select(func.count()).select_from(ReferralDraft).where(
            ReferralDraft.user_id == user_id,
            ReferralDraft.status == "sent",
            ReferralDraft.sent_at >= start,
        )
    ).one())


def _within_active_hours() -> bool:
    from jobapply.sites.browser import is_active_hours

    return is_active_hours(spec=get_settings().linkedin_active_hours)


def send_referral(session: Session, user: User, draft: ReferralDraft, page: Any,
                  now: datetime | None = None) -> tuple[bool, str]:
    """Actually send ONE approved referral, behind every safety gate. Returns `(sent, reason)`.

    Sends only when ALL hold: the draft belongs to `user`, the draft is `approved`, the global referral send
    switch is on, the global dry-run master switch is off, the kill switch is off, we are inside the LinkedIn
    active hours and the per-user daily cap (`config.referral_daily_cap`) is not reached. If any gate fails
    the draft is left completely untouched and a reason is returned (a dry-run / disabled call never mutates a
    draft). A draft that is not `approved`, or that belongs to another user, is NEVER sent.
    """
    # 0. Ownership: never send a draft that belongs to another user, regardless of how the caller scoped it
    #    (mirrors UserScope.set_status). The cap is computed against user.id, so a mismatched draft would also
    #    be charged to the wrong user. Checked before anything else.
    if draft.user_id != user.id:
        return False, "draft belongs to another user; not sending"
    # 1. The draft must be approved. Checked hard: never send anything else.
    if draft.status != "approved":
        return False, f"draft is {draft.status!r}, not approved; not sending"
    # 2. The global referral send switch (off by default).
    if not referral_send_enabled(session):
        return False, "referral sending is disabled (global switch off); draft left as-is"
    # 3. The global dry-run master switch (on by default). The same switch that blocks real job applies also
    #    blocks real referral sends, so "dry-run on" means nothing goes out even if referral_send was enabled.
    if global_dry_run(session):
        return False, "global dry-run is on; not sending"
    # 4. Kill switch.
    if kill_switch_on(session):
        return False, "kill switch is on; not sending"
    # 5. Active hours.
    if not _within_active_hours():
        return False, f"outside LinkedIn active hours ({get_settings().linkedin_active_hours}); not sending"
    # 6. Per-user daily cap.
    cap = int(get_settings().referral_daily_cap)
    sent_today = referrals_sent_today(session, user.id, now)
    if sent_today >= cap:
        return False, f"daily referral cap reached ({sent_today}/{cap}); not sending"

    # All gates passed: perform the send (behind a hook so tests never open a browser / network).
    now = now or utcnow()
    try:
        ok = _perform_send(page, draft)
    except Exception as exc:  # noqa: BLE001 - any browser failure is recorded, not raised
        draft.status = "failed"
        draft.error = f"{type(exc).__name__}: {exc}"[:500]
        draft.updated_at = now
        session.add(draft)
        log.warning("referral send failed", user=user.username, draft=draft.id, error=draft.error)
        return False, draft.error
    if ok:
        draft.status = "sent"
        draft.sent_at = now
        draft.error = None
    else:
        draft.status = "failed"
        draft.error = "send did not complete (the Connect/Message control was not found)"
    draft.updated_at = now
    session.add(draft)
    log.info("referral send", user=user.username, draft=draft.id, channel=draft.channel, sent=ok)
    return ok, "sent" if ok else (draft.error or "not sent")


# Selectors for the live send (kept here; the whole path is untested against real LinkedIn). The YAML
# selectors file could override these later, like the Easy Apply filler.
_SEND_SELECTORS = {
    "connect": 'button[aria-label*="Invite"][aria-label*="connect"], button[aria-label^="Connect"], '
               'button.pvs-profile-actions__action[aria-label*="Connect"]',
    "more": 'button[aria-label="More actions"], button[aria-label*="More"]',
    "add_note": 'button[aria-label="Add a note"]',
    "note_box": 'textarea[name="message"], #custom-message',
    "send_invite": 'button[aria-label="Send invitation"], button[aria-label="Send now"], '
                   'button[aria-label="Send"]',
    "message": 'button[aria-label*="Message"]',
    "message_box": 'div.msg-form__contenteditable, div[contenteditable="true"][role="textbox"]',
    "message_send": 'button.msg-form__send-button, button[type="submit"][aria-label*="Send"]',
}


def _perform_send(page: Any, draft: ReferralDraft) -> bool:
    """Open the contact's profile and send the drafted note/message, with human-like pauses. This is the ONLY
    place that reaches a real person; `send_referral` has already confirmed every gate. Returns True on a
    confirmed send. Behind this hook so tests stub it out — no live LinkedIn run has been exercised here yet.
    """
    from jobapply.sites.browser import human_pause

    url = draft.contact_profile_url
    if not url:
        return False
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    human_pause(page, 1500, 3500)
    if draft.channel == "connection_note":
        return _send_connection_note(page, draft.body, human_pause)
    return _send_message(page, draft.body, human_pause)


def _first_visible(page: Any, selector: str):
    try:
        loc = page.locator(selector)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    except Exception as exc:  # noqa: BLE001
        log.debug("locator failed", selector=selector, error=str(exc)[:120])
    return None


def _send_connection_note(page: Any, body: str, pause) -> bool:
    btn = _first_visible(page, _SEND_SELECTORS["connect"])
    if btn is None:
        more = _first_visible(page, _SEND_SELECTORS["more"])
        if more is not None:
            more.click(timeout=8000)
            pause(page, 500, 1200)
            btn = _first_visible(page, _SEND_SELECTORS["connect"])
    if btn is None:
        return False
    btn.click(timeout=8000)
    pause(page, 600, 1400)
    add_note = _first_visible(page, _SEND_SELECTORS["add_note"])
    if add_note is not None:
        add_note.click(timeout=8000)
        pause(page, 400, 1000)
        box = _first_visible(page, _SEND_SELECTORS["note_box"])
        if box is not None:
            box.fill(body[:300])
            pause(page, 500, 1200)
    send = _first_visible(page, _SEND_SELECTORS["send_invite"])
    if send is None:
        return False
    send.click(timeout=8000)
    pause(page, 800, 1600)
    return True


def _send_message(page: Any, body: str, pause) -> bool:
    btn = _first_visible(page, _SEND_SELECTORS["message"])
    if btn is None:
        return False
    btn.click(timeout=8000)
    pause(page, 800, 1600)
    box = _first_visible(page, _SEND_SELECTORS["message_box"])
    if box is None:
        return False
    box.click(timeout=5000)
    box.fill(body)
    pause(page, 600, 1400)
    send = _first_visible(page, _SEND_SELECTORS["message_send"])
    if send is None:
        return False
    send.click(timeout=8000)
    pause(page, 800, 1600)
    return True


__all__ = [
    "Contact",
    "parse_hiring_team",
    "find_contacts",
    "generate_referral_drafts",
    "send_referral",
    "referrals_sent_today",
    "SITE",
]
