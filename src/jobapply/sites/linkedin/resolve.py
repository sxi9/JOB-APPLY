"""Resolve a LinkedIn posting's real apply target from a logged-in job page.

The guest posting page (``sites.linkedin.search``) hides the direct apply URL of an offsite job behind a
sign-in modal, so a non-Easy-Apply posting is stored with ``apply_url = linkedin.com/jobs/view/<id>`` and
``ats_type = None`` and neither the ATS filler nor the Easy Apply filler can act on it. On a *logged-in*
job page the apply button is decisive:

- an **Easy Apply** button means the job stays on LinkedIn -> ``kind = "easy_apply"`` (hand to the Easy
  Apply filler);
- an external **Apply** button either carries the offsite URL in its markup (an ``<a href>``, a
  ``data-apply-url`` attribute, or the ``<code id="applyUrl">`` block) or only opens a new tab to it when
  clicked -> ``kind = "offsite"`` with the resolved URL;
- a **sign-in wall** (the guest contextual-sign-in modal, or a login form) exposes nothing -> ``kind =
  "signin"``.

Nothing is ever submitted here: ``parse_apply_button`` is pure HTML parsing (unit-tested on saved
fixtures) and ``resolve_external_url`` drives a live Playwright page, reading the ``href`` or intercepting
the popup URL and closing it again.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass

from jobapply.collectors.detector import detect_from_url
from jobapply.logs import get_logger
from jobapply.sites.linkedin.search import (
    _P_OFFSITE,
    _P_ONSITE,
    _normalize_apply_url,
)

log = get_logger(__name__)

# Kinds parse_apply_button / resolve_external_url can report.
EASY_APPLY = "easy_apply"
OFFSITE = "offsite"
SIGNIN = "signin"
NONE = "none"

# The apply control on a logged-in job page: <button class="... jobs-apply-button ..."> or the <a> form of
# the same on an offsite posting. Captures the tag, its attributes and its inner text.
_APPLY_CONTROL = re.compile(
    r'<(button|a)\b([^>]*\bclass="[^"]*jobs-apply-button[^"]*"[^>]*)>(.*?)</\1>', re.I | re.S
)
_EASY_APPLY_TEXT = re.compile(r"easy apply", re.I)
_TAGS = re.compile(r"<[^>]+>")

# Where the offsite URL can be read from the static HTML, most specific first.
_APPLY_URL_CODE = re.compile(r'<code id="applyUrl"[^>]*>\s*<!--\s*"?([^"\s]+?)"?\s*-->\s*</code>', re.S)
_APPLY_ANCHOR_HREF = re.compile(
    r'<a[^>]*\bclass="[^"]*jobs-apply-button[^"]*"[^>]*\bhref="([^"]+)"', re.S
)
_APPLY_HREF_ANCHOR = re.compile(
    r'<a[^>]*\bhref="([^"]+)"[^>]*\bclass="[^"]*jobs-apply-button[^"]*"', re.S
)
_DATA_APPLY_URL = re.compile(r'data-(?:offsite-apply-url|apply-url|job-url)="([^"]+)"', re.S)
_OFFSITE_TRACKED_HREF = re.compile(
    r'<a[^>]*data-tracking-control-name="[^"]*apply-link-offsite[^"]*"[^>]*href="([^"]+)"', re.S
)

# Markers that mean the page is a sign-in / sign-up wall rather than a real apply control.
_SIGNIN_WALL = re.compile(
    r"contextual-sign-in-modal|sign-up-modal|login__form|session_key|join-link|"
    r"sign in to (?:see|apply|continue)",
    re.I,
)


@dataclass
class ApplyResolution:
    """What a logged-in job page says about how to apply.

    ``kind`` is one of easy_apply / offsite / signin / none. ``apply_url`` and ``ats_type`` are filled only
    for an offsite posting whose URL could be read (``ats_type`` is None for a host that is not a known ATS).
    """

    kind: str
    apply_url: str | None = None
    ats_type: str | None = None

    @property
    def easy_apply(self) -> bool:
        return self.kind == EASY_APPLY

    @property
    def resolved(self) -> bool:
        """An offsite posting with a usable external URL (not LinkedIn's own, not empty)."""
        return self.kind == OFFSITE and bool(self.apply_url)


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", text or ""))).strip()


def _extract_offsite_url(html: str) -> str:
    """The direct apply URL from whatever the static markup exposes, normalized (externalApply unwrapped,
    LinkedIn's own sign-in/signup links dropped). Empty when nothing usable is present."""
    for pattern in (_APPLY_URL_CODE, _OFFSITE_TRACKED_HREF, _APPLY_ANCHOR_HREF, _APPLY_HREF_ANCHOR,
                    _DATA_APPLY_URL):
        m = pattern.search(html)
        if not m:
            continue
        url = _normalize_apply_url(html_lib.unescape(m.group(1)).strip())
        if url:
            return url
    return ""


def _has_easy_apply_button(html: str) -> bool:
    """True only when the PRIMARY apply control (the first ``jobs-apply-button``, i.e. the top-card CTA) is
    Easy Apply. Only the primary control is inspected on purpose: a logged-in offsite job page also renders
    ``jobs-apply-button`` controls for recommended / also-viewed jobs in a side rail, and some of those are
    Easy Apply; scanning every control would misclassify an offsite posting (external Apply primary) as
    Easy Apply and hand it to a filler that cannot apply to it. The primary CTA renders first in the top
    card, before any rail, so document order picks it out."""
    m = _APPLY_CONTROL.search(html)
    if m is None:
        return False
    attrs, inner = m.group(2), m.group(3)
    return bool(_EASY_APPLY_TEXT.search(attrs) or _EASY_APPLY_TEXT.search(_strip(inner)))


def parse_apply_button(html: str) -> tuple[str, str | None]:
    """Classify the apply control on a saved job page: ``(kind, url)``. Pure parsing, no browser.

    ``url`` is set only for an offsite posting whose direct link is in the markup; an offsite button whose
    URL only appears after a click comes back ``("offsite", None)`` for ``resolve_external_url`` to chase.
    """
    html = html or ""
    offsite_marker = bool(_P_OFFSITE.search(html))
    onsite_marker = bool(_P_ONSITE.search(html))
    has_control = bool(_APPLY_CONTROL.search(html))

    if (_has_easy_apply_button(html) or onsite_marker) and not offsite_marker:
        return EASY_APPLY, None

    url = _extract_offsite_url(html)
    if url:
        return OFFSITE, url
    if offsite_marker or has_control:
        return OFFSITE, None  # offsite, but the URL is only revealed on click
    if _SIGNIN_WALL.search(html):
        return SIGNIN, None
    return NONE, None


def _resolution_from(kind: str, url: str | None) -> ApplyResolution:
    if kind == OFFSITE and url:
        hit = detect_from_url(url)
        return ApplyResolution(OFFSITE, url, hit.ats if hit else None)
    return ApplyResolution(kind, None, None)


def _capture_popup_url(page, selector: str) -> str:
    """Click the apply control and read the URL of the tab it opens, then close that tab. Never submits
    anything: the offsite page is loaded by the company, and we only read its address."""
    try:
        control = page.locator(selector).first
        if not control.count():
            return ""
        with page.context.expect_page(timeout=15000) as popup_info:
            control.click(timeout=10000)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001 - the address is usable before full load
            pass
        url = popup.url or ""
        try:
            popup.close()
        except Exception:  # noqa: BLE001
            pass
        return url
    except Exception as exc:  # noqa: BLE001 - no popup, detached node, timeout
        log.debug("could not capture apply popup url", error=str(exc)[:160])
        return ""


def resolve_external_url(page, job_url: str, *, timeout_ms: int = 60000,
                         apply_selector: str = "button.jobs-apply-button, a.jobs-apply-button") -> ApplyResolution:
    """Open a LinkedIn job in a logged-in page and resolve how to apply, without submitting anything.

    Returns an :class:`ApplyResolution`. An Easy Apply button -> ``kind="easy_apply"``; an offsite button
    whose URL is in the markup -> ``kind="offsite"`` with the URL and detected ATS; an offsite button that
    only opens a new tab -> the popup's URL is read and the tab closed; a sign-in wall -> ``kind="signin"``.
    """
    page.goto(job_url, wait_until="domcontentloaded", timeout=timeout_ms)
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        try:
            waiter(1500)  # the top card renders client-side after domcontentloaded
        except Exception:  # noqa: BLE001
            pass
    html = page.content()
    kind, url = parse_apply_button(html)
    if kind == EASY_APPLY or url:
        return _resolution_from(kind, url)
    if kind == OFFSITE:
        popup_url = _normalize_apply_url(_capture_popup_url(page, apply_selector))
        if popup_url:
            return _resolution_from(OFFSITE, popup_url)
        return ApplyResolution(OFFSITE, None, None)
    return ApplyResolution(kind, None, None)


__all__ = [
    "ApplyResolution",
    "parse_apply_button",
    "resolve_external_url",
    "EASY_APPLY",
    "OFFSITE",
    "SIGNIN",
    "NONE",
]
