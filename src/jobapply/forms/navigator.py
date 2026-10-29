"""Universal external-link navigator: from any landing page to the application form.

An apply URL on an unknown host used to be paused ("apply manually"). The navigator gives every URL a real
attempt, without ever submitting anything itself:

1. dismiss cookie / consent banners;
2. a form embedded in an iframe is opened directly (so filling, screenshots and submit work on a plain page);
3. find the way to the form by heuristics: Apply / Apply now / Start application / Continue controls, scored on
   their text, aria label, href and data-* attributes, followed in the same tab, a popup (its URL is taken over
   and the popup closed) or a redirect; multi-step wizards (Next / Continue are not final submits) are walked;
4. when the heuristics find nothing, the LLM (``ai.service.structured``) picks from a NUMBERED list of the
   visible links and buttons; the answer must be an index into that list, never free text. Controls that look
   like a final submit (Submit, Send application, Finish, Confirm) are never in the list, so neither the
   heuristics nor the model can ever click one. Without an LLM (or with a FakeLLM that has no canned
   answer) only the heuristics run;
5. a sign-in / create-account wall is handled generically: a stored account for the host (vault site
   ``ext:<host>``, kind ``login``) is used to sign in; otherwise, and only when the caller allows account
   creation (``allow_final``: not dry run AND auto-submit on), an account is created with the profile email and a
   generated password that is stored in the vault before the form is sent, and the verification code / link is
   read from the user's mailbox (``mail.otp``). No mailbox, no code, dry run: pause with a clear reason.

Hops and total time are capped. Everything is heuristic and best effort: the result says exactly how far it
got (``NavOutcome.status`` / ``reason`` / ``hops``) so the pipeline can pause with a specific reason.
"""

from __future__ import annotations

import imaplib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel

from jobapply.collectors.detector import detect_from_url
from jobapply.forms.fields import FormField, detect_captcha, extract_fields
from jobapply.forms.profiles import profile_for_url
from jobapply.logs import get_logger
from jobapply.models import utcnow
from jobapply.users import ACCOUNT_SITES

log = get_logger(__name__)


def account_site_for_url(url: str) -> str | None:
    """The account site (``workday``) whose package owns this host, or None. Such a host's sign-in / create-account
    wall is never handled by the generic navigator: the site package creates the per-company account."""
    hit = detect_from_url(url or "")
    return hit.ats if hit is not None and hit.ats in ACCOUNT_SITES else None

MIN_FORM_FIELDS = 3  # fewer visible fields than this is "not the application form" (search box, newsletter)
MAX_HOPS = 6
MAX_SECONDS = 120.0
SETTLE_MS = 1500
MAX_CANDIDATES = 60
WIZARD_MAX_STEPS = 4

STATUS_FORM = "form"
STATUS_NO_FORM = "no_form"
STATUS_CAPTCHA = "captcha"
STATUS_ACCOUNT_WALL = "account_wall"
STATUS_MAILTO = "mailto"

# ------------------------------------------------------------------ browser-side helpers

# Every visible link / button, tagged with data-jobapply-nav="<i>" so it can be clicked later by index.
CANDIDATES_JS = r"""
(limit) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = window.getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const sel = 'a[href], button, [role=button], input[type=submit], input[type=button], [onclick]';
  document.querySelectorAll('[data-jobapply-nav]').forEach(e => e.removeAttribute('data-jobapply-nav'));
  const out = [];
  let i = 0;
  for (const e of Array.from(document.querySelectorAll(sel))) {
    if (!vis(e)) continue;
    const label = e.getAttribute('aria-label') || e.getAttribute('title') || '';
    const text = ((e.innerText || e.value || label || '') + '').replace(/\s+/g, ' ').trim();
    const href = e.getAttribute('href') || e.getAttribute('data-href') || e.getAttribute('data-url')
      || e.getAttribute('data-apply-url') || '';
    if (!text && !href) continue;
    const data = Array.from(e.attributes).filter(a => a.name.startsWith('data-') && a.name !== 'data-jobapply-nav')
      .map(a => a.name + '=' + (a.value || '').slice(0, 40)).join(' ');
    e.setAttribute('data-jobapply-nav', String(i));
    out.push({ idx: i, tag: e.tagName.toLowerCase(), text: text.slice(0, 120), href: href.slice(0, 500),
      type: ((e.type || e.getAttribute('type') || '') + '').toLowerCase(), in_form: !!e.closest('form'),
      attrs: ((e.id || '') + ' ' + ((e.className && e.className.toString) ? e.className.toString() : '')
        + ' ' + label + ' ' + data).slice(0, 300), target: e.getAttribute('target') || '' });
    i += 1;
    if (i >= limit) break;
  }
  return out;
}
"""

# Cookie / consent banners: click the least permissive button in a container that looks like one.
BANNER_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const boxSel = '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], ' +
    '[id*="gdpr" i], [class*="gdpr" i], #onetrust-banner-sdk, #didomi-host, [class*="cc-window" i], ' +
    '[id*="cmp" i], [aria-label*="cookie" i]';
  const prefer = [/^(reject|decline|refuse)( all)?( cookies)?$/i, /^(only )?(necessary|essential)( only| cookies)?$/i,
    /^(accept|allow|agree|i agree|got it|ok|okay|understood|continue|close|dismiss)( all| cookies)?( ?cookies)?!?$/i];
  for (const box of Array.from(document.querySelectorAll(boxSel))) {
    if (!vis(box)) continue;
    const btns = Array.from(box.querySelectorAll('button, a[role=button], [role=button], input[type=button]'))
      .filter(vis);
    for (const rx of prefer) {
      const hit = btns.find(b => rx.test(((b.innerText || b.value || b.getAttribute('aria-label') || '') + '')
        .replace(/\s+/g, ' ').trim()));
      if (hit) { hit.click(); return (hit.innerText || hit.value || '').trim().slice(0, 40) || 'button'; }
    }
  }
  const known = ['#onetrust-accept-btn-handler', '#didomi-notice-agree-button', '.cc-dismiss', '.cc-btn.cc-allow'];
  for (const s of known) { const b = document.querySelector(s); if (b && vis(b)) { b.click(); return s; } }
  return '';
}
"""

# What the page looks like as an account wall: visible password inputs, visible inputs overall, page text.
WALL_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = window.getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const skip = ['hidden', 'submit', 'button', 'image'];
  const inputs = Array.from(document.querySelectorAll('input')).filter(e => vis(e) && !skip.includes(e.type));
  const pw = inputs.filter(e => e.type === 'password').length;
  const text = ((document.body && document.body.innerText) || '').slice(0, 5000);
  return { password: pw, inputs: inputs.length, text };
}
"""

# Visible inputs with everything needed to fill a sign-up / sign-in form by heuristics.
WALL_INPUTS_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = window.getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const labelOf = (e) => {
    if (e.labels && e.labels[0]) return e.labels[0].innerText || '';
    if (e.id) { const l = document.querySelector('label[for="' + esc(e.id) + '"]'); if (l) return l.innerText; }
    return '';
  };
  const skip = ['hidden', 'submit', 'button', 'image', 'file'];
  document.querySelectorAll('[data-jobapply-wall]').forEach(e => e.removeAttribute('data-jobapply-wall'));
  const out = [];
  Array.from(document.querySelectorAll('input')).forEach((e, i) => {
    if (!vis(e) || skip.includes(e.type)) return;
    e.setAttribute('data-jobapply-wall', String(i));
    out.push({ i, type: (e.type || 'text').toLowerCase(), required: !!e.required,
      hint: [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), e.autocomplete, labelOf(e)]
        .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, 200).toLowerCase() });
  });
  return out;
}
"""

# ------------------------------------------------------------------ text heuristics

APPLY_STRONG = re.compile(
    r"^(apply( now| here| online| today| for this (job|position|role|opening)| to this (job|position|role))?|"
    r"apply (for|to) (the |this )?(job|position|role|opening|vacancy)|"
    r"start (your |the |an |my )?application|begin (your |the |an )?application|"
    r"i'?m interested|quick apply|easy apply|apply with (linkedin|indeed|seek|resume|cv)|"
    r"continue (to )?(the )?application|continue to apply)\W*$", re.I)
APPLY_ANY = re.compile(r"\bapply\b|\bapplication\b|\bcandidature\b|\bbewerb", re.I)
NEXT = re.compile(
    r"^(next|continue|proceed|get started|start|let'?s (go|start|begin)|save (and|&) continue|next step|"
    r"continue application|go to (the )?application)\W*$", re.I)
FINAL = re.compile(
    r"\b(submit|send|finish|complete|confirm|place order|pay|purchase|buy|delete|remove|unsubscribe|withdraw|"
    r"sign out|log ?out)\b", re.I)
NEGATIVE = re.compile(
    r"\b(share|save (this )?job|bookmark|refer( a)?|print|email (this|a) friend|tell a friend|back|cancel|close|"
    r"cookie|privacy|terms|search|menu|home|about|contact|facebook|twitter|instagram|youtube|similar jobs|"
    r"view all|all jobs|job alert|subscribe|language|settings|help|faq|report|skip|previous)\b", re.I)
ACCOUNT_LINK = re.compile(r"\b(sign in|log ?in|create (an |your )?account|sign up|register|join( now)?)\b", re.I)
SIGNUP_LINK = re.compile(r"\b(create (an |your |a new )?account|sign up|register|join( now)?|new (user|here))\b", re.I)
SIGNIN_BUTTON = re.compile(r"\b(sign in|log ?in|continue|next|submit|go)\b", re.I)
SIGNUP_BUTTON = re.compile(r"\b(create|sign up|register|continue|next|get started|submit|join)\b", re.I)
VERIFY_BUTTON = re.compile(r"\b(verify|confirm|continue|submit|next|activate)\b", re.I)
SIGNUP_PAGE = re.compile(r"create (an |your |a new )?account|sign up|register|registration", re.I)
CODE_INPUT = re.compile(r"\b(code|otp|one[- ]?time|verif|pin|token)\b", re.I)
HEURISTIC_MIN = 50
MAX_HOP_SECONDS = 60_000  # navigation timeout (ms)
MAILBOX_LOGIN_FAILED = "mailbox login failed: fix the app password in Settings"


class NavPick(BaseModel):
    """The LLM's choice from the numbered candidate list: 1-based index, 0 when nothing applies."""

    choice: int = 0
    reason: str = ""


@dataclass
class Candidate:
    idx: int
    tag: str
    text: str = ""
    href: str = ""
    type: str = ""
    in_form: bool = False
    attrs: str = ""
    target: str = ""

    @property
    def label(self) -> str:
        return self.text or self.href[:80] or self.attrs[:40]

    @property
    def key(self) -> tuple[str, str]:
        return (self.text.lower(), self.href)


@dataclass
class NavOutcome:
    status: str
    reason: str = ""
    hops: list[str] = field(default_factory=list)
    fields: list[FormField] = field(default_factory=list)
    url: str = ""
    mailto: str = ""


def score_candidate(c: Candidate, *, allow_form_submit: bool = False) -> int:
    """How much a control looks like the way to the application form. 0 = never click it."""
    text = c.text.strip()
    blob = f"{c.href} {c.attrs}".lower()
    if FINAL.search(text) or NEGATIVE.search(text):
        return 0
    if c.tag in ("button", "input") and c.type == "submit" and c.in_form and not allow_form_submit:
        return 0  # a form's own submit button is never a link to the form (a quick-apply form says "Apply" too)
    if ACCOUNT_LINK.search(text):
        return 0  # handled by the account-wall fallback, not as a normal hop
    score = 0
    if APPLY_STRONG.match(text):
        score = 100
    elif APPLY_ANY.search(text):
        score = 80
    elif NEXT.match(text):
        score = 60
    if re.search(r"apply|application|candidat|bewerb", blob):
        score += 15
    if c.tag == "a" and c.href and not c.href.startswith(("javascript:", "#")):
        score += 3  # a real link beats a script button at equal text
    return score


def generate_password() -> str:
    """A fresh password per site: 20+ url-safe characters plus one of each class the usual rules want."""
    return secrets.token_urlsafe(16) + "Aa1!"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


class Navigator:
    """Drives one Playwright page from a landing URL to the application form. See the module docstring."""

    def __init__(self, page: Any, *, session: Any = None, llm: Any = None, user: Any = None,
                 profile: dict | None = None, profiles: dict | None = None, allow_final: bool = False,
                 max_hops: int = MAX_HOPS, max_seconds: float = MAX_SECONDS, settle_ms: int = SETTLE_MS,
                 imap_factory: Any = None, otp_timeout: float = 120.0, otp_poll: float = 5.0,
                 vault: Any = None, imap_config: Any = None):
        self.page = page
        self.session, self.llm, self.user = session, llm, user
        self.profile = dict(profile if profile is not None else (getattr(user, "profile", None) or {}))
        self.profiles = profiles or {}
        self.allow_final = bool(allow_final)
        self.max_hops, self.max_seconds, self.settle_ms = int(max_hops), float(max_seconds), int(settle_ms)
        self.imap_factory, self.otp_timeout, self.otp_poll = imap_factory, float(otp_timeout), float(otp_poll)
        self._vault, self._imap_config = vault, imap_config
        self.visited: set[tuple[str, tuple[str, str]]] = set()
        self.llm_calls = 0

    # ------------------------------------------------------------ main loop

    def find_form(self) -> NavOutcome:
        page = self.page
        start = time.monotonic()
        out = NavOutcome(status=STATUS_NO_FORM, url=page.url)
        for hop in range(self.max_hops + 1):
            if time.monotonic() - start > self.max_seconds:
                out.reason = f"time budget exhausted after {len(out.hops)} hops"
                break
            banner = self.dismiss_banners()
            if banner:
                out.hops.append(f"banner {banner}")
            captcha = self._captcha()
            if captcha:
                out.status, out.reason = STATUS_CAPTCHA, f"captcha on page ({captcha})"
                break
            frame_url = self.form_frame_url()
            if frame_url:
                self._goto(frame_url)
                out.hops.append(f"frame {frame_url[:80]}")
                continue
            if self.is_account_wall():
                stop = self.handle_account_wall()
                if stop is not None:
                    stop.hops = out.hops + stop.hops
                    return stop
                out.hops.append(f"account {host_of(page.url)}")
                continue
            fields = self._extract()
            if self._usable(fields):
                out.status, out.fields, out.url = STATUS_FORM, fields, page.url
                return out
            out.fields = fields
            if hop >= self.max_hops:
                out.reason = f"no application form found after {self.max_hops} hops"
                break
            cands = self.candidates()
            pick = self.heuristic_pick(cands) or self.llm_pick(cands) or self.account_link(cands)
            if pick is None:
                out.reason = (f"no apply control found on {page.url[:80]}" if not out.hops
                              else f"no application form found after {len(out.hops)} hops")
                break
            self.visited.add((page.url, pick.key))
            how = self.follow(pick)
            if how == "mailto":
                out.status, out.mailto, out.reason = STATUS_MAILTO, pick.href, f"apply by email: {pick.href}"
                return out
            if how == "failed":
                out.hops.append(f"{pick.label!r} (not followed)")
                continue
            out.hops.append(f"{pick.label!r} -> {page.url[:80]}")
        out.url = page.url
        if not out.reason:
            out.reason = f"no application form found after {len(out.hops)} hops"
        return out

    # ------------------------------------------------------------ page probes

    def _extract(self) -> list[FormField]:
        try:
            return extract_fields(self.page)
        except Exception as exc:  # noqa: BLE001 - mid-navigation
            log.debug("extract failed", error=str(exc)[:120])
            return []

    @staticmethod
    def _usable(fields: list[FormField]) -> bool:
        return len([f for f in fields if not f.hidden and f.type != "password"]) >= MIN_FORM_FIELDS

    def _captcha(self) -> str:
        try:
            return detect_captcha(self.page)
        except Exception:  # noqa: BLE001
            return ""

    def dismiss_banners(self) -> str:
        try:
            hit = self.page.evaluate(BANNER_JS) or ""
        except Exception:  # noqa: BLE001
            return ""
        if hit:
            self.page.wait_for_timeout(min(self.settle_ms, 700))
        return str(hit)

    def form_frame_url(self) -> str | None:
        """URL of the child frame that holds the application form, preferring one a profile recognises."""
        best: tuple[int, int, str] | None = None
        for frame in getattr(self.page, "frames", [])[1:]:
            url = frame.url or ""
            if not url.startswith("http"):
                continue
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=5000)
                count = frame.locator("input:not([type=hidden]), select, textarea").count()
            except Exception:  # noqa: BLE001 - detached, cross-origin or still loading frame
                continue
            if count < MIN_FORM_FIELDS:
                continue
            known = 1 if profile_for_url(url, self.profiles) else 0
            if best is None or (known, count) > best[:2]:
                best = (known, count, url)
        return best[2] if best else None

    def candidates(self, allow_form_submit: bool = False) -> list[Candidate]:
        try:
            raw = self.page.evaluate(CANDIDATES_JS, MAX_CANDIDATES) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("candidate scan failed", error=str(exc)[:120])
            return []
        out = []
        for d in raw:
            c = Candidate(**{k: d.get(k, "") for k in Candidate.__dataclass_fields__})
            c.in_form = bool(d.get("in_form"))
            if c.tag in ("button", "input") and c.type == "submit" and c.in_form and not allow_form_submit:
                continue
            out.append(c)
        return out

    def _unvisited(self, cands: list[Candidate]) -> list[Candidate]:
        here = self.page.url
        return [c for c in cands if (here, c.key) not in self.visited]

    # ------------------------------------------------------------ choosing

    def heuristic_pick(self, cands: list[Candidate]) -> Candidate | None:
        best, best_score = None, 0
        for c in self._unvisited(cands):
            s = score_candidate(c)
            if s > best_score:
                best, best_score = c, s
        return best if best_score >= HEURISTIC_MIN else None

    def next_control(self) -> Candidate | None:
        """A Next / Continue control for a wizard step: NEXT-looking text only, never the form's own submit
        button (a quick-apply form whose submit says "Apply now" / "Continue" would post the application, and
        a JS click handler is not covered by the dry-run submit guard) and never an Apply link (those are hops
        that `find_form` follows before the fill, not steps of the form that was just filled)."""
        for c in self._unvisited(self.candidates(allow_form_submit=False)):
            text = c.text.strip()
            if FINAL.search(text) or NEGATIVE.search(text) or ACCOUNT_LINK.search(text):
                continue
            if NEXT.match(text):
                return c
        return None

    def llm_pick(self, cands: list[Candidate]) -> Candidate | None:
        """Ask the model to choose from a numbered list of the safe candidates. Final-looking and clearly
        irrelevant controls are not even offered. Any failure (no LLM, FakeLLM without a canned answer, a bad
        index) means "no pick" and the heuristics' verdict stands."""
        if self.llm is None or self.session is None or self.user is None:
            return None
        offered = [c for c in self._unvisited(cands)
                   if not FINAL.search(c.text) and not NEGATIVE.search(c.text) and (c.text or c.href)]
        if not offered:
            return None
        from jobapply.ai import prompts
        from jobapply.ai.service import structured

        try:
            title = self.page.title()
        except Exception:  # noqa: BLE001
            title = ""
        rows = [f"{i}. [{c.tag}] {c.text[:80]!r}" + (f" -> {c.href[:100]}" if c.href else "")
                for i, c in enumerate(offered, start=1)]
        self.llm_calls += 1
        try:
            pick = structured(self.session, self.llm, self.user, task="navigate", system=prompts.NAVIGATE_SYSTEM,
                              prompt=prompts.navigate_prompt(self.page.url, title, rows), schema=NavPick,
                              context={"url": self.page.url, "title": title, "candidates": rows}, max_tokens=256)
        except Exception as exc:  # noqa: BLE001 - LLMError, FakeLLM without a handler, provider failure
            log.info("navigator llm pick unavailable", error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return None
        choice = int(getattr(pick, "choice", 0) or 0)
        if not 1 <= choice <= len(offered):
            return None
        chosen = offered[choice - 1]
        log.info("navigator llm pick", choice=choice, text=chosen.text[:60], reason=str(pick.reason)[:80])
        return chosen

    def account_link(self, cands: list[Candidate]) -> Candidate | None:
        """When nothing else leads anywhere, a "Sign in to apply" / "Create account" link is the way in."""
        for c in self._unvisited(cands):
            if ACCOUNT_LINK.search(c.text) and not NEGATIVE.search(c.text):
                if not (c.tag in ("button", "input") and c.type == "submit" and c.in_form):
                    return c
        return None

    # ------------------------------------------------------------ following

    def _goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=MAX_HOP_SECONDS)
        self.page.wait_for_timeout(self.settle_ms)

    def follow(self, c: Candidate) -> str:
        """Follow one control: "mailto" | "navigated" | "clicked" | "failed". A link with a real href is loaded
        directly (works for target=_blank too); anything else is clicked, and a popup it opens is taken over
        (its URL loaded in this page, the popup closed) so the rest of the run stays on one page."""
        href = (c.href or "").strip()
        if href.lower().startswith("mailto:"):
            return "mailto"
        page = self.page
        if href and not href.lower().startswith(("javascript:", "#", "tel:")):
            try:
                self._goto(urljoin(page.url, href))
                return "navigated"
            except Exception as exc:  # noqa: BLE001 - dead link; try the click below
                log.debug("navigator goto failed", href=href[:100], error=str(exc)[:120])
        popups: list[Any] = []
        ctx = getattr(page, "context", None)

        def handler(new_page: Any) -> None:  # a plain function: Playwright cannot wrap a bound builtin
            popups.append(new_page)

        before = page.url
        try:
            if ctx is not None:
                ctx.on("page", handler)
            loc = page.locator(f'[data-jobapply-nav="{c.idx}"]').first
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=5000)
            page.wait_for_timeout(self.settle_ms)
        except Exception as exc:  # noqa: BLE001 - detached / covered control
            log.debug("navigator click failed", text=c.text[:60], error=str(exc)[:120])
            return "failed"
        finally:
            if ctx is not None:
                try:
                    ctx.remove_listener("page", handler)
                except Exception:  # noqa: BLE001
                    pass
        if popups:
            popup = popups[0]
            url = ""
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001 - the address is usable before full load
                pass
            try:
                url = popup.url or ""
                popup.close()
            except Exception:  # noqa: BLE001
                pass
            if url.startswith("http") and url != before:
                try:
                    self._goto(url)
                except Exception as exc:  # noqa: BLE001
                    log.debug("popup url not loaded", url=url[:100], error=str(exc)[:120])
                    return "failed"
                return "navigated"
        return "clicked"

    # ------------------------------------------------------------ account walls

    def _wall_state(self) -> dict:
        try:
            return self.page.evaluate(WALL_JS) or {}
        except Exception:  # noqa: BLE001
            return {}

    def is_account_wall(self) -> bool:
        st = self._wall_state()
        return int(st.get("password") or 0) >= 1 and int(st.get("inputs") or 0) <= 8

    def _wall_kind(self, st: dict | None = None) -> str:
        st = st or self._wall_state()
        if int(st.get("password") or 0) >= 2:
            return "signup"
        text = str(st.get("text") or "")
        if SIGNUP_PAGE.search(text[:1500]) and not re.search(r"\b(sign in|log ?in)\b.{0,40}\bto\b", text[:300], re.I):
            inputs = self._wall_inputs()
            if any(re.search(r"first|last|given|family|surname|full ?name|\bname\b", i["hint"]) for i in inputs):
                return "signup"
        return "login"

    def _wall_inputs(self) -> list[dict]:
        try:
            return self.page.evaluate(WALL_INPUTS_JS) or []
        except Exception:  # noqa: BLE001
            return []

    def _vault_or_none(self):
        if self._vault is not None:
            return self._vault
        from jobapply.vault import Vault, VaultError

        try:
            self._vault = Vault()
        except VaultError:
            return None
        return self._vault

    def _imap_cfg(self):
        if self._imap_config is not None:
            return self._imap_config
        if self.session is None or self.user is None:
            return None
        from jobapply.mail.inbox import mailbox_configs

        imap_cfg, _smtp = mailbox_configs(self.session, self.user)
        return imap_cfg

    def _email(self) -> str:
        return str(self.profile.get("email") or getattr(self.user, "email", "") or "").strip()

    def handle_account_wall(self) -> NavOutcome | None:
        """Sign in with the stored account for this host, or create one. None means "carry on navigating";
        a NavOutcome is a stop with the reason the pipeline should show."""
        page = self.page
        host = host_of(page.url) or "unknown-host"
        owner = account_site_for_url(page.url)
        if owner:
            # A Workday tenant (or any other account site) has its own package that creates the per-company
            # account; the generic sign-up must never run there. The pipeline routes such jobs by ats_type.
            return NavOutcome(STATUS_ACCOUNT_WALL,
                              f"{host} is a {owner} site: its account is handled by the {owner} applier "
                              f"(jobapply sites apply -s {owner}), not the generic navigator")
        site_key = f"ext:{host}"
        email = self._email()
        if self.session is None or self.user is None or not email:
            return NavOutcome(STATUS_ACCOUNT_WALL, f"{host} needs an account; no user profile email to sign up with")
        vault = self._vault_or_none()
        if vault is None:
            return NavOutcome(STATUS_ACCOUNT_WALL,
                              f"{host} needs an account; vault not configured (JOBAPPLY_VAULT_KEY)")
        try:
            cred = vault.load(self.session, self.user.id, site_key, "login")
        except Exception as exc:  # noqa: BLE001 - undecryptable row
            log.warning("stored account unreadable", site=site_key, error=str(exc)[:120])
            cred = None
        kind = self._wall_kind()
        # A row whose sign-up form was never sent (`created` False: the fill or the button failed last time) is
        # not an account: creating it is retried instead of signing in forever with credentials the site never
        # saw. Rows without the flag (older, or stored by hand) are taken as existing accounts.
        if cred and cred.get("password") and cred.get("created", True):
            if kind == "signup":
                clicked = self._click_link(ACCOUNT_LINK, exclude=SIGNUP_LINK)
                if not clicked:
                    return NavOutcome(STATUS_ACCOUNT_WALL, f"{host}: stored account exists but no sign-in form found")
            if self.sign_in(str(cred.get("username") or email), str(cred["password"])):
                log.info("signed in with the stored account", host=host)
                return None
            return NavOutcome(STATUS_ACCOUNT_WALL, f"sign-in on {host} with the stored account did not succeed")
        if not self.allow_final:
            return NavOutcome(STATUS_ACCOUNT_WALL,
                              f"{host} needs an account; not created (dry run or auto-submit off)")
        imap_cfg = self._imap_cfg()
        if imap_cfg is None:
            return NavOutcome(STATUS_ACCOUNT_WALL,
                              f"{host} needs an account; mailbox not configured for the email verification")
        if kind == "login":
            if not self._click_link(SIGNUP_LINK):
                return NavOutcome(STATUS_ACCOUNT_WALL, f"{host} needs an account; no sign-up link found")
            if not self.is_account_wall():
                return NavOutcome(STATUS_ACCOUNT_WALL, f"{host}: the sign-up link did not lead to a sign-up form")
        password = generate_password()
        secret = {"password": password, "created_at": utcnow().isoformat(), "verified": False, "created": False}
        vault.store(self.session, self.user.id, site_key, "login", email, dict(secret))
        self.session.flush()
        if not self.fill_signup(email, password):
            return NavOutcome(STATUS_ACCOUNT_WALL, f"{host}: sign-up form could not be filled")
        started = utcnow()  # only mail that arrives after this click can be this host's verification
        if not self._click_button(SIGNUP_BUTTON):
            return NavOutcome(STATUS_ACCOUNT_WALL, f"{host}: sign-up button not found")
        secret["created"] = True
        vault.store(self.session, self.user.id, site_key, "login", email, dict(secret))
        self.session.flush()
        page.wait_for_timeout(self.settle_ms)
        log.info("account created, waiting for the verification email", host=host)
        verified = self.verify_email(imap_cfg, host, after=started)
        if verified != "ok":
            return NavOutcome(STATUS_ACCOUNT_WALL, f"account created on {host} but {verified}")
        secret["verified"] = True
        vault.store(self.session, self.user.id, site_key, "login", email, dict(secret))
        self.session.flush()
        if self.is_account_wall() and self._wall_kind() == "login":
            self.sign_in(email, password)
        return None

    def _click_link(self, rx: re.Pattern[str], exclude: re.Pattern[str] | None = None) -> bool:
        for c in self.candidates(allow_form_submit=False):
            if rx.search(c.text) and not (exclude and exclude.search(c.text)):
                return self.follow(c) in ("navigated", "clicked")
        return False

    def _allow_submit(self, allow: bool) -> None:
        """Lift (or restore) the runner's dry-run submit guard for an account form: signing in, creating the
        account and entering the verification code all post a form that is not the application. The flag
        resets on every navigation, so the application form itself stays guarded."""
        try:
            self.page.evaluate("(v) => { window.__jobapplyAllowSubmit = v; }", bool(allow))
        except Exception:  # noqa: BLE001 - mid-navigation
            pass

    def _click_button(self, rx: re.Pattern[str]) -> bool:
        """Click the first visible button whose text matches, preferring one inside the form that holds the
        password input. Used only on sign-up / sign-in / verification pages, where the page's own submit is
        the point."""
        for scope in ("form:has(input[type=password]) ", "form:has([data-jobapply-wall]) ", ""):
            loc = self.page.locator(f"{scope}button, {scope}input[type=submit], {scope}[role=button]")
            try:
                n = loc.count()
            except Exception:  # noqa: BLE001
                n = 0
            for i in range(min(n, 30)):
                b = loc.nth(i)
                try:
                    if not b.is_visible():
                        continue
                    text = (b.inner_text() or b.get_attribute("value") or b.get_attribute("aria-label") or "").strip()
                    if rx.search(text) and not NEGATIVE.search(text):
                        self._allow_submit(True)
                        try:
                            b.click(timeout=5000)
                            self.page.wait_for_timeout(self.settle_ms)
                        finally:
                            self._allow_submit(False)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _fill_wall_input(self, i: int, value: str) -> bool:
        try:
            self.page.locator(f'[data-jobapply-wall="{i}"]').first.fill(value, timeout=5000)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("wall input not filled", idx=i, error=str(exc)[:100])
            return False

    def sign_in(self, username: str, password: str) -> bool:
        inputs = self._wall_inputs()
        user_box = next((i for i in inputs if i["type"] == "email"), None) or next(
            (i for i in inputs if i["type"] in ("text", "") and re.search(r"e-?mail|user|login|account", i["hint"])),
            None) or next((i for i in inputs if i["type"] in ("text", "")), None)
        pw_box = next((i for i in inputs if i["type"] == "password"), None)
        if pw_box is None:
            return False
        if user_box is not None:
            self._fill_wall_input(user_box["i"], username)
        self._fill_wall_input(pw_box["i"], password)
        if not self._click_button(SIGNIN_BUTTON):
            self._allow_submit(True)
            try:
                self.page.locator(f'[data-jobapply-wall="{pw_box["i"]}"]').first.press("Enter")
                self.page.wait_for_timeout(self.settle_ms)
            except Exception:  # noqa: BLE001
                return False
            finally:
                self._allow_submit(False)
        return not self.is_account_wall()

    def fill_signup(self, email: str, password: str) -> bool:
        """Fill a sign-up form by heuristics: email, every password box, names and phone from the profile,
        required consent checkboxes. True when an email and a password box were filled."""
        p = self.profile
        filled_email = filled_pw = False
        for i in self._wall_inputs():
            t, hint = i["type"], i["hint"]
            if t == "password":
                filled_pw |= self._fill_wall_input(i["i"], password)
            elif t == "email" or re.search(r"e-?mail", hint):
                filled_email |= self._fill_wall_input(i["i"], email)
            elif t == "checkbox":
                if i["required"] or re.search(r"terms|privacy|agree|consent|policy", hint):
                    try:
                        self.page.locator(f'[data-jobapply-wall="{i["i"]}"]').first.check(timeout=3000)
                    except Exception:  # noqa: BLE001
                        pass
            elif t in ("text", "tel", ""):
                value = ""
                if re.search(r"first|given|fname", hint):
                    value = str(p.get("first_name") or "")
                elif re.search(r"last|family|surname|lname", hint):
                    value = str(p.get("last_name") or "")
                elif re.search(r"full ?name|your name|\bname\b", hint):
                    value = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                elif re.search(r"phone|mobile", hint) or t == "tel":
                    value = str(p.get("phone") or "")
                elif re.search(r"user ?name|login", hint):
                    value = email
                if value:
                    self._fill_wall_input(i["i"], value)
        return filled_email and filled_pw

    def _code_box(self) -> dict | None:
        return next((i for i in self._wall_inputs() if CODE_INPUT.search(i["hint"])), None)

    def verify_email(self, imap_cfg: Any, host: str, after: Any = None) -> str:
        """Read the verification code or link from the mailbox and apply it. "ok", or what went wrong.

        Only mail that arrived after `after` (the moment the sign-up was sent) counts, and a code already
        consumed for this user (`mail.otp.CodeRegistry`) is never typed again, so an OTP from another site in
        the same ten minutes (Naukri, a bank, the previous host of this run) is not mistaken for this host's.
        After the code is sent the page is checked again: a code box still asking is "not accepted", never "ok".
        A mailbox login the server refuses is reported once, not retried per poll."""
        from jobapply.mail.imap import ImapReader
        from jobapply.mail.otp import CodeRegistry, wait_for_code, wait_for_link

        registry = (CodeRegistry(self.session, self.user.id)
                    if self.session is not None and getattr(self.user, "id", None) is not None else None)
        reader = ImapReader(imap_cfg, client_factory=self.imap_factory)
        try:
            code_box = self._code_box()
            if code_box is not None:
                try:
                    code = wait_for_code(reader, timeout=self.otp_timeout, poll=self.otp_poll, after=after,
                                         registry=registry)
                except imaplib.IMAP4.error:
                    return MAILBOX_LOGIN_FAILED
                if not code:
                    return f"no verification code arrived within {int(self.otp_timeout)}s"
                before = self.page.url
                self._fill_wall_input(code_box["i"], code)
                if not self._click_button(VERIFY_BUTTON):
                    self._allow_submit(True)
                    try:
                        self.page.locator(f'[data-jobapply-wall="{code_box["i"]}"]').first.press("Enter")
                        self.page.wait_for_timeout(self.settle_ms)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        self._allow_submit(False)
                still = self._code_box()
                if self.page.url == before and still is not None and still["hint"] == code_box["hint"]:
                    return "the verification code was not accepted"
                return "ok"
            try:
                link = wait_for_link(reader, timeout=self.otp_timeout, poll=self.otp_poll, after=after)
            except imaplib.IMAP4.error:
                return MAILBOX_LOGIN_FAILED
            if not link:
                return f"no verification email arrived within {int(self.otp_timeout)}s"
            try:
                self._goto(link)
            except Exception as exc:  # noqa: BLE001
                return f"the verification link did not load: {str(exc)[:100]}"
            return "ok"
        finally:
            reader.close()


def forget_external_account(db: Any, user_id: int, host: str) -> bool:
    """Delete the stored account for one external host (the vault row ``ext:<host>``, kind ``login``). The
    account on the site itself stays. Returns True when a row was removed."""
    from jobapply.repo import UserScope

    key = f"ext:{host_of(host) or (host or '').strip().lower()}"
    cred = UserScope(db, int(user_id)).credential(key, "login")
    if cred is None:
        return False
    db.delete(cred)
    db.commit()
    log.info("external account forgotten", user_id=user_id, site=key)
    return True


__all__ = ["Navigator", "NavOutcome", "NavPick", "Candidate", "score_candidate", "generate_password", "host_of",
           "account_site_for_url", "forget_external_account", "MAILBOX_LOGIN_FAILED",
           "STATUS_FORM", "STATUS_NO_FORM", "STATUS_CAPTCHA", "STATUS_ACCOUNT_WALL",
           "STATUS_MAILTO", "MIN_FORM_FIELDS", "MAX_HOPS", "WIZARD_MAX_STEPS"]
