"""Playwright form runner.

Opens the application page, finds a profile (built-in by URL, learned by fingerprint, or the AI mapper),
fills every mapped field, uploads the resume, takes a screenshot and, in dry-run mode, stops there.
It never clicks submit unless `submit=True`, which only the apply pipeline passes, and only when the user's
site has auto-submit on and the global dry-run switch is off.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from sqlmodel import Session, select

from jobapply.ai.base import LLM, LLMError
from jobapply.config import get_settings
from jobapply.forms.fields import FormField, detect_captcha, extract_fields, match_selector
from jobapply.forms.fingerprint import domain_of, fingerprint
from jobapply.forms.mapper import map_fields, mapping_to_profile_fields
from jobapply.forms.navigator import STATUS_FORM, STATUS_NO_FORM, WIZARD_MAX_STEPS, Navigator
from jobapply.forms.profiles import (
    FormProfileSpec,
    VaultView,
    load_builtin_profiles,
    profile_for_url,
    resolve_key,
)
from jobapply.logs import get_logger
from jobapply.models import FormProfile, utcnow

log = get_logger(__name__)

# Share of a profile's selectors that must still match the page. Below this the site changed its form: the
# profile is stale and the AI mapper takes over (docs/FORM_PROFILES.md, Verification).
MIN_MATCH_RATE = 0.5

# What a custom dropdown shows as its chosen value: React-Select's single-value container (or a sibling with a
# similar class), the aria-selected option of the listbox it controls, else the input's own value.
SHOWN_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return '';
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').trim();
  let n = el;
  for (let d = 0; d < 6 && n && n !== document.body; d++, n = n.parentElement) {
    const sv = n.querySelector('[class*="single-value"], [class*="singleValue"], [class*="selected-value"], '
      + '[class*="SelectedValue"], [class*="multi-value__label"]');
    if (sv && txt(sv)) return txt(sv);
  }
  const ctl = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (ctl) {
    const box = document.getElementById(ctl);
    const s = box && box.querySelector('[role=option][aria-selected="true"]');
    if (s && txt(s)) return txt(s);
  }
  return el.value || '';
}
"""

# Dry-run safety net: swallows every `submit` event (a stray Enter in a text input, an "Apply" button that is
# the form's submit control) until `_submit` lifts the guard. Installed per page, re-run on every navigation.
SUBMIT_GUARD_JS = r"""
() => {
  if (window.__jobapplyGuard) return;
  window.__jobapplyGuard = true;
  document.addEventListener('submit', (e) => {
    if (window.__jobapplyAllowSubmit) return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);
}
"""

# Menu rows that are not options: React-Select's notices, Google Places' footer.
OPTION_NOTICES = {"no options", "no results", "no results found", "loading...", "loading", "type to search",
                  "start typing", "powered by google"}

# The visible validation message next to a field, if any: aria-describedby when aria-invalid, else the nearest
# visible .error / [role=alert] / "invalid" element that is not a container wrapping the field itself. Used only
# when an LLM is present, to decide whether a filled value needs an AI-guided correction.
FIELD_ERROR_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return '';
  const vis = (n) => { if (!n) return false; const s = window.getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').trim();
  if (el.getAttribute('aria-invalid') === 'true') {
    for (const id of (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)) {
      const d = document.getElementById(id);
      if (d && vis(d) && txt(d)) return txt(d);
    }
  }
  const SEL = '.error, [class*="error"], [class*="invalid"], .field-error, .invalid-feedback, .help-block, '
    + '[role=alert]';
  let n = el;
  for (let i = 0; i < 5 && n && n !== document.body; i++, n = n.parentElement) {
    for (const c of n.querySelectorAll(SEL)) {
      if (c === el || c.contains(el)) continue;  // a wrapper with a has-error class is not the message
      const t = txt(c);
      if (t && t.length <= 300 && vis(c)) return t;
    }
  }
  return '';
}
"""

# A trimmed snippet of the field's wrapper, for the recovery prompt's context.
DOM_SNIPPET_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return '';
  const n = el.closest('.field, .form-group, .application-question, fieldset, li') || el.parentElement || el;
  return ((n.outerHTML || '').replace(/\s+/g, ' ').trim()).slice(0, 2000);
}
"""


@dataclass
class PlannedFill:
    field: FormField
    key: str
    value: Any
    kind: str
    source: str  # builtin | learned | ai | bank
    confidence: int = 100
    done: bool = False
    error: str = ""


@dataclass
class FillResult:
    url: str
    profile_origin: str  # builtin | learned | ai | none
    fields_total: int = 0
    plan: list[PlannedFill] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    recoveries: list[str] = field(default_factory=list)  # AI-fixed fields: "Phone: added country code"
    screenshot_path: str | None = None
    submitted: bool = False
    paused_reason: str = ""
    error: str = ""
    # Set by the navigator: the hops taken to reach the form, and a mailto: apply link it found (the pipeline
    # then applies by email instead of filling a form).
    hops: str = ""
    mailto: str = ""

    @property
    def filled(self) -> int:
        return sum(1 for p in self.plan if p.done)

    @property
    def match_rate(self) -> float:
        required = [f for f in self.plan]
        return (self.filled / len(required)) if required else 0.0

    @property
    def answers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self.plan:
            if p.done and p.kind != "file":
                out[p.field.question[:120] or p.field.selector] = str(p.value)
            elif p.done:
                out[p.field.question[:120] or "Resume"] = Path(str(p.value)).name
        return out

    def summary(self) -> str:
        bits = [f"{self.filled}/{len(self.plan)} fields filled via {self.profile_origin}"]
        if self.unfilled_required:
            bits.append(f"{len(self.unfilled_required)} required unfilled")
        if self.needs_human:
            bits.append(f"{len(self.needs_human)} need a human")
        if self.paused_reason:
            bits.append(f"paused: {self.paused_reason}")
        return "; ".join(bits)


def _pick_bank_option(options: list[str], answer: str) -> str | None:
    from jobapply.ai.heuristics import _pick_option

    return _pick_option(options, answer)


class AnswerSource:
    """Answers custom questions: answer bank first, then the profile heuristics, then the LLM when there is
    one. Injected so tests can stub it. `(None, 0)` means nobody could answer.

    A heuristic answer is stored in the answer bank like an AI draft (`source="heuristic"`, unapproved) so the
    user can see, approve or correct it on the Answers page instead of it being repeated on every form."""

    # Free-text "anything else / message to the team" boxes the per-application fit blurb is a good default
    # answer for. Kept deliberately narrow: a specific free-text prompt ("Tell us about a time you disagreed
    # with a manager", "Why would you like to relocate to Berlin?") must fall through to the tailored LLM
    # answer, not get the generic message. So no bare "tell us about ..." and no bare "why would you like ...".
    MESSAGE_Q = re.compile(
        r"anything else|additional (information|info|comments?|details)|cover (note|letter)|"
        r"why do you want to (work|join|be (a )?part)|"
        r"tell us (anything|more)|tell us about yourself|"
        r"(message|note) (to|for) (the )?(hiring|recruit|team|us)|is there anything|"
        r"what (makes you|interests you|draws you)",
        re.I,
    )

    def __init__(self, session: Session, llm: LLM | None, user, job=None, message: str = ""):
        self.session, self.llm, self.user, self.job = session, llm, user, job
        self.message = message or ""

    def answer(self, question: str, options: list[str] | None = None) -> tuple[str | None, int]:
        from jobapply.ai.heuristics import heuristic_answer
        from jobapply.ai.service import answer_question, bank_answer

        if self.user is None:
            return None, 0
        banked = bank_answer(self.session, self.user, question)
        if banked is not None:
            if not options:
                return banked.answer, banked.confidence
            picked = _pick_bank_option(options, banked.answer)
            if picked is not None:
                return picked, banked.confidence
            # Banked free text ("B.Tech CSE") that is not one of the dropdown's options: ask again with them.
        if self.message and not options and self.MESSAGE_Q.search(question or ""):
            # A free-text "anything else / why do you want" box: offer the tailored per-application message
            # before falling back to the generic heuristic / LLM answer.
            return self.message, 85
        answer, conf = heuristic_answer(question, options, self.user)
        if answer is not None:
            self._bank_heuristic(question, answer)
            return answer, conf
        if self.llm is None:
            return None, 0
        try:
            result = answer_question(self.session, self.llm, self.user, question, self.job, use_bank=False,
                                     options=options)
        except LLMError as exc:
            log.warning("answer failed", question=question[:80], error=str(exc))
            return None, 0
        if not result.answer:
            return None, 0
        return result.answer, result.confidence

    def _bank_heuristic(self, question: str, answer: str) -> None:
        from jobapply.forms.answers import question_hash
        from jobapply.models import AnswerBank

        qh = question_hash(question)
        exists = self.session.exec(
            select(AnswerBank).where(AnswerBank.user_id == self.user.id, AnswerBank.question_hash == qh)
        ).first()
        if exists is None:
            self.session.add(AnswerBank(user_id=self.user.id, question_hash=qh, question=question, answer=answer,
                                        source="heuristic", approved=False))
            self.session.flush()
        elif exists.source == "heuristic" and not exists.approved and exists.answer != answer:
            # Re-asked with the dropdown's options ("6" became "Yes"): keep the draft current for review.
            exists.answer = answer
            self.session.add(exists)
            self.session.flush()


class FormRunner:
    def __init__(
        self,
        session: Session,
        vault: VaultView,
        answers: AnswerSource | None = None,
        llm: LLM | None = None,
        user=None,
        profiles_dir: Path | None = None,
        learn: bool = True,
        min_answer_confidence: int = 50,
        navigate: bool = True,
        nav_options: dict[str, Any] | None = None,
    ):
        self.session = session
        self.vault = vault
        self.answers = answers
        self.llm = llm
        self.user = user
        self.learn = learn
        self.min_answer_confidence = min_answer_confidence
        self.profiles = load_builtin_profiles(profiles_dir or get_settings().profiles_dir)
        # The navigator (forms/navigator.py) finds the form from any landing page: banners, apply links,
        # popups, iframes, wizards, account walls, an LLM pick from a numbered list. `nav_options` are passed
        # to it (tests inject a fake IMAP client and short OTP timeouts there).
        self.navigate = navigate
        self.nav_options = dict(nav_options or {})

    # ------------------------------------------------------------ profile lookup

    def learned_profile(self, domain: str, fp: str) -> FormProfile | None:
        return self.session.exec(
            select(FormProfile).where(FormProfile.domain == domain, FormProfile.fingerprint == fp)
        ).first()

    def save_learned(self, domain: str, fp: str, ats: str | None, fields: dict[str, str], submit: str) -> None:
        row = self.learned_profile(domain, fp) or FormProfile(domain=domain, fingerprint=fp)
        row.ats_type, row.fields, row.submit, row.origin, row.updated_at = ats, fields, submit, "learned", utcnow()
        self.session.add(row)
        self.session.flush()

    # ------------------------------------------------------------ planning

    def plan_from_spec(self, fields: list[FormField], mapping: dict[str, str], source: str) -> list[PlannedFill]:
        plan: list[PlannedFill] = []
        used: set[int] = set()
        by_selector = {f.selector: f for f in fields}
        for selector, key in mapping.items():
            for f in match_selector(fields, selector):
                if f.twin and f.twin in by_selector:
                    # A profile that names the hidden question_<n> input: fill its visible combobox instead.
                    f = by_selector[f.twin]
                if f.idx in used or f.hidden:
                    continue
                rv = resolve_key(key, self.vault)
                if rv.kind == "skip":
                    used.add(f.idx)
                    continue
                plan.append(PlannedFill(f, key, rv.value, rv.kind, source))
                used.add(f.idx)
                if rv.kind == "file":
                    # A file value fills one input only: a broad selector (e.g. `input[type=file]`, used when
                    # the resume dropzone has a generated name) must not copy the resume into every file input
                    # on the page (a separate portfolio / cover-letter attachment slot).
                    break
        return plan

    def fill_questions(
        self, plan: list[PlannedFill], fields: list[FormField], result: FillResult, ai_mapping=None
    ) -> None:
        """Resolve `answers.*` entries and, for fields nobody mapped, ask the answer source by label."""
        mapped = {p.field.idx for p in plan}
        skip_idx = set()
        if ai_mapping is not None:
            for m in ai_mapping.mappings:
                if m.needs_human:
                    f = next((x for x in fields if x.idx == m.idx), None)
                    if f is not None:
                        result.needs_human.append(f"{f.question[:100]}: {m.note}".strip(": "))
                        skip_idx.add(m.idx)
                elif m.key == "skip":
                    skip_idx.add(m.idx)
        for p in plan:
            if p.kind == "question" and p.value is None and self.answers is not None:
                answer, conf = self.answers.answer(p.field.question or p.key[8:])
                p.value, p.confidence, p.source = answer, conf, "ai"
                if answer is not None and conf < self.min_answer_confidence:
                    result.needs_human.append(f"{p.field.question[:100]}: low confidence answer, review it")
        for f in fields:
            if f.idx in mapped or f.idx in skip_idx or f.hidden or not f.required:
                continue
            if f.is_file or f.type in ("search", "password"):
                continue
            if self.answers is None or not f.question:
                continue
            options = f.options or None
            if f.is_choice:
                group = [g for g in fields if g.is_choice and g.name and g.name == f.name] or [f]
                if any(g.idx in mapped for g in group):
                    continue
                options = [g.label or g.value for g in group]
                mapped.update(g.idx for g in group)
            answer, conf = self.answers.answer(f.question, options)
            if answer is None:
                if f.is_combobox:
                    # The options are only in the DOM once the menu is open: planned without an answer, the
                    # fill step reads them from the dropdown and asks again (`_retry_with_options`).
                    plan.append(PlannedFill(f, f"answers.{f.question[:80]}", None, "question", "pending", 0))
                    mapped.add(f.idx)
                continue
            plan.append(PlannedFill(f, f"answers.{f.question[:80]}", answer, "question", "ai", conf))
            mapped.add(f.idx)
            if conf < self.min_answer_confidence:
                result.needs_human.append(f"{f.question[:100]}: low confidence answer, review it")

    # ------------------------------------------------------------ execution

    @staticmethod
    def _pick_option(options: list[str], value: Any) -> str | None:
        if value is None or not options:
            return None
        want = str(value).strip().lower()
        if isinstance(value, bool):
            want = "yes" if value else "no"
        best, score = None, 0
        for opt in options:
            o = opt.strip().lower()
            if o == want:
                return opt
            # partial_ratio against a 2-3 letter option is 100 for any value containing it ("Unknown" -> "No"):
            # short options only match whole.
            s = fuzz.partial_ratio(want, o) if len(want) > 2 and len(o) >= 4 else 0
            if s > score:
                best, score = opt, s
        return best if score >= 80 else None

    # ------------------------------------------------------------ custom dropdowns (React-Select, Ashby, Workday)

    LISTBOX_WAIT_MS = 3000
    LISTBOX_POLL_MS = 150
    FUZZY_MIN = 80

    @classmethod
    def _best_option(cls, options: list[str], value: str) -> str | None:
        """exact > starts-with > fuzzy >= FUZZY_MIN, tried for the whole value and then for each of its
        comma / slash segments ("Bengaluru, India" also matches "India +91")."""
        want = value.strip().lower()
        if not want or not options:
            return None
        segments = [s.strip().lower() for s in re.split(r"[,/|(]", want)]
        needles = [want] + [s for s in segments if len(s) > 2 and s != want]
        lowered = [(o, o.strip().lower()) for o in options if o.strip()]
        for needle in needles:
            for o, lo in lowered:
                if lo == needle:
                    return o
            if len(needle) >= 3:
                for o, lo in lowered:
                    if lo.startswith(needle):
                        return o
            best, score = None, (0, 0)
            for o, lo in lowered:
                s = fuzz.ratio(needle, lo)
                if len(needle) >= 4 and len(lo) >= 4:
                    # Never partial-match a 2-3 letter option: "Unknown" / "I don't know" contain "no".
                    s = max(s, fuzz.partial_ratio(needle, lo))
                if (s, -len(lo)) > score:
                    best, score = o, (s, -len(lo))
            if best is not None and score[0] >= cls.FUZZY_MIN:
                return best
        return None

    def _fill_combobox(self, page, p: PlannedFill) -> str:
        """Click the input, type the value, pick the option whose text best matches it and verify the control
        now shows that text. A value no fixed option matches is handed back (ValueError) with `field.options`
        filled from the opened dropdown so `_retry_with_options` can ask the answer source. An autocomplete
        (no options until something is typed: city, school) takes the first suggestion containing the
        value's first token, else the first suggestion, which is returned as a note for the reviewer."""
        f = p.field
        text = ("Yes" if p.value else "No") if isinstance(p.value, bool) else str(p.value).strip()
        if not text:
            raise ValueError("no value")
        loc = page.locator(f.selector).first
        # Opened empty, a fixed list (Yes/No, degrees, countries) shows its options; an autocomplete shows
        # nothing until something is typed. Decided before typing, since a search widget keeps its last
        # results on screen after the query is cleared.
        fixed = f.options or self._open_empty(page, f, loc)
        options = self._type_and_wait(page, f, loc, text)
        pick = self._best_option(options, text)
        if pick is not None:
            self._choose(page, f, loc, options, pick)
            p.value = self._verify_shown(page, f, loc, pick, text)
            return ""
        if fixed:
            f.options = fixed
            self._clear_text(loc)
            self._close(loc)
            raise ValueError(f"no option matches {text!r}")
        first_segment = re.split(r"[,/|(]", text)[0].strip()
        if not options and first_segment and first_segment.lower() != text.lower():
            # "Bengaluru, India" found nothing in time: the bare city name is what a places lookup wants.
            options = self._type_and_wait(page, f, loc, first_segment)
            pick = self._best_option(options, text)
            if pick is not None:
                self._choose(page, f, loc, options, pick)
                p.value = self._verify_shown(page, f, loc, pick, first_segment)
                return ""
        if not options:
            self._clear_text(loc)
            self._close(loc)
            raise ValueError(f"no suggestions for {text!r}")
        token = next((t for t in re.findall(r"[^\W_]+", text.lower()) if len(t) > 1), text.lower())
        guess = next((o for o in options if token in o.lower()), None)
        if guess is not None:
            self._choose(page, f, loc, options, guess)
            p.value = self._verify_shown(page, f, loc, guess, text)
            return ""
        # Commit the first suggestion by clicking it (ArrowDown+Enter would move React-Select's focus from the
        # already focused first row to the second, and Enter on a closed menu submits the form).
        first = next(o for o in options if o)
        try:
            self._choose(page, f, loc, options, first)
            shown = self._verify_shown(page, f, loc, first, text)
        except ValueError:
            self._clear_text(loc)
            self._close(loc)
            raise ValueError(f"no suggestion matches {text!r}") from None
        p.value = shown
        return f"picked the first suggestion {shown!r} for {text!r}, review it"

    def _type_and_wait(self, page, f: FormField, loc, text: str) -> list[str]:
        loc.click(timeout=5000)
        self._clear_text(loc)
        loc.press_sequentially(text, delay=15)
        return self._wait_options(page, f, loc)

    def _open_empty(self, page, f: FormField, loc) -> list[str]:
        """The options a dropdown shows with nothing typed (empty for an autocomplete)."""
        loc.click(timeout=5000)
        self._clear_text(loc)
        opts = self._wait_options(page, f, loc, self.LISTBOX_WAIT_MS // 3)
        if not opts:
            loc.press("ArrowDown")
            opts = self._wait_options(page, f, loc, self.LISTBOX_WAIT_MS // 6)
        return [o for o in opts if o]

    @staticmethod
    def _clear_text(loc) -> None:
        """Select-all (Control on Windows/Linux, Meta on macOS) and delete. Nothing is pressed on an empty
        input: Backspace there removes a React-Select value that is already chosen."""
        try:
            if not loc.input_value(timeout=1000):
                return
        except Exception:  # noqa: BLE001 - not a text input; clear anyway
            pass
        loc.press("ControlOrMeta+a")
        loc.press("Backspace")

    @staticmethod
    def _menu_open(loc) -> bool:
        try:
            return loc.get_attribute("aria-expanded", timeout=1000) == "true"
        except Exception:  # noqa: BLE001 - detached input
            return False

    def _enter_commits(self, page, f: FormField, loc) -> bool:
        """True only when Enter would choose a focused option: the menu is open and a row is focused. On a
        closed menu (or one without focus) Enter is the browser's implicit form submission."""
        if not self._menu_open(loc):
            return False
        opts = self._options_locator(page, f, loc)
        if opts is None:
            return False
        try:
            focused = opts.and_(page.locator(
                '[aria-selected="true"], [class*="focused"], [class*="selected"], [class*="highlight"], '
                '[class*="active"]'
            ))
            return focused.count() > 0
        except Exception:  # noqa: BLE001 - menu re-rendered under us
            return False

    @staticmethod
    def _close(loc) -> None:
        try:
            loc.press("Escape", timeout=1000)
        except Exception:  # noqa: BLE001 - best effort
            pass

    # Rows of an open menu that can be chosen. Menu notices (React-Select's `__menu-notice--no-options`),
    # disabled rows and list items that are links (a site nav mistaken for a menu) are not options.
    OPTION_ROWS = '[role=option]:not([aria-disabled="true"])'
    OPTION_ROWS_FALLBACK = (
        'li:not(:has(a)), [class*="option"]:not([class*="notice"]):not([class*="no-options"]):not([role=listbox]), '
        '.pac-item'
    )

    def _options_locator(self, page, f: FormField, loc):
        """The option elements of the open menu that belongs to this input, or None while it is closed.
        aria-controls / aria-owns first, then React-Select's `react-select-<id>-listbox`. Any other visible
        listbox is only trusted while the input reports its menu open (aria-expanded), so a site's nav menu
        or another widget's open list is never read as this field's options."""
        try:
            ctl = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns") or f.controls
            expanded = loc.get_attribute("aria-expanded")
        except Exception:  # noqa: BLE001 - detached input
            ctl, expanded = f.controls, None
        candidates = []
        if ctl:
            candidates.append(page.locator(f'[id="{ctl}"]'))
        if f.id:
            candidates.append(page.locator(f'[id="react-select-{f.id}-listbox"]'))
        if expanded != "false":
            candidates += [
                page.locator('[id$="-listbox"]:visible'),
                page.locator("[role=listbox]:visible"),
                page.locator('[class*="menu-list"]:visible, .pac-container:visible'),
            ]
        for cand in candidates:
            try:
                if not cand.count() or not cand.first.is_visible():
                    continue
                opts = cand.first.locator(self.OPTION_ROWS)
                if not opts.count():
                    opts = cand.first.locator(self.OPTION_ROWS_FALLBACK)
                if opts.count():
                    return opts
            except Exception:  # noqa: BLE001 - menu re-rendered under us; poll again
                continue
        return None

    @staticmethod
    def _real_options(texts: list[str]) -> bool:
        """False for an empty list or one made only of notices ("No options", "Loading...")."""
        return any(t and t.lower() not in OPTION_NOTICES for t in texts)

    def _wait_options(self, page, f: FormField, loc, wait_ms: int | None = None) -> list[str]:
        """Option texts (in menu order, empty strings kept so indexes line up) within `wait_ms`."""
        deadline = time.monotonic() + (wait_ms or self.LISTBOX_WAIT_MS) / 1000
        while True:
            opts = self._options_locator(page, f, loc)
            if opts is not None:
                try:
                    texts = [t.strip() for t in opts.all_inner_texts()]
                except Exception:  # noqa: BLE001 - menu re-rendered under us
                    texts = []
                if self._real_options(texts):
                    return texts
            if time.monotonic() >= deadline:
                return []
            page.wait_for_timeout(self.LISTBOX_POLL_MS)

    def _choose(self, page, f: FormField, loc, options: list[str], pick: str) -> None:
        """Click the option whose text is `pick`, located by text on the live menu (an async autocomplete may
        have re-ordered its rows since they were read); the index it had is only a fallback. The keyboard is
        used when the click fails, and Enter only while the menu is open with a focused row."""
        idx = options.index(pick)
        opts = self._options_locator(page, f, loc)
        try:
            if opts is not None:
                live = [t.strip() for t in opts.all_inner_texts()]
                if pick.strip() in live:
                    idx = live.index(pick.strip())
                elif live != [o.strip() for o in options]:
                    log.debug("menu changed before the click", selector=f.selector, pick=pick)
                if opts.count() > idx:
                    target = opts.nth(idx)
                    target.scroll_into_view_if_needed(timeout=1000)
                    target.click(timeout=2000)
                    return
        except Exception as exc:  # noqa: BLE001 - menu closed or option not clickable: use the keyboard
            log.debug("option click failed", selector=f.selector, error=str(exc)[:120])
        if not self._menu_open(loc):
            return
        for _ in range(idx):
            loc.press("ArrowDown", timeout=1000)
        if self._enter_commits(page, f, loc):
            loc.press("Enter", timeout=1000)

    def _verify_shown(self, page, f: FormField, loc, pick: str, typed: str) -> str:
        page.wait_for_timeout(200)
        shown = self._shown_value(page, f)
        if not self._shows(shown, pick, typed) and self._enter_commits(page, f, loc):
            loc.press("Enter", timeout=1000)  # the click missed a re-rendered menu: Enter commits the focused row
            page.wait_for_timeout(200)
            shown = self._shown_value(page, f)
        if not self._shows(shown, pick, typed):
            self._close(loc)
            raise ValueError(f"picked {pick!r} but the control shows {shown!r}")
        return shown

    @staticmethod
    def _shows(shown: str, pick: str, typed: str) -> bool:
        """Does the control show the picked option? Equal, or one contains the other as whole words (never for
        a pick shorter than 4 characters: "No" is in "Norway"), or near-equal by fuzzy ratio."""
        s, k = shown.strip().lower(), pick.strip().lower()
        if not s:
            return False
        if s == typed.strip().lower() and k != s:
            return False  # the input only echoes what we typed; nothing was selected
        if k == s:
            return True
        if len(k) >= 4:
            whole = lambda part, text: re.search(rf"(?<!\w){re.escape(part)}(?!\w)", text) is not None  # noqa: E731
            if whole(k, s) or whole(s, k):
                return True
        return fuzz.ratio(s, k) >= FormRunner.FUZZY_MIN

    @staticmethod
    def _shown_value(page, f: FormField) -> str:
        return (page.evaluate(SHOWN_JS, f.selector) or "").strip()

    def _combobox_options(self, page, f: FormField) -> list[str]:
        """Open the dropdown with an empty input and read its options (a fixed list), then close it."""
        loc = page.locator(f.selector).first
        try:
            opts = self._open_empty(page, f, loc)
        except Exception as exc:  # noqa: BLE001
            log.debug("combobox options not read", selector=f.selector, error=str(exc)[:120])
            opts = []
        self._close(loc)
        return opts

    def _fill_one(self, page, p: PlannedFill, fields: list[FormField], result: FillResult | None = None) -> None:
        f = p.field
        locator = page.locator(f.selector).first
        if p.kind == "file":
            if not p.value or not Path(str(p.value)).exists():
                raise ValueError("resume file missing")
            locator.set_input_files(str(p.value))
            return
        if f.is_combobox:
            if p.value is None:
                raise ValueError("no answer")
            note = self._fill_combobox(page, p)
            if note and result is not None:
                result.needs_human.append(f"{f.question[:100]}: {note}")
            return
        if f.type == "select":
            opt = self._pick_option(f.options, p.value)
            if opt is None:
                raise ValueError(f"no option matches {p.value!r}")
            locator.select_option(label=opt)
            p.value = opt
            return
        if f.is_choice:
            group = [g for g in fields if g.is_choice and g.name and g.name == f.name] or [f]
            labels = [g.label or g.value for g in group]
            opt = self._pick_option(labels, p.value) if len(group) > 1 else labels[0]
            if opt is None:
                raise ValueError(f"no choice matches {p.value!r}")
            target = next(g for g in group if (g.label or g.value) == opt)
            want_checked = True
            if len(group) == 1 and isinstance(p.value, bool):
                want_checked = p.value
            elif len(group) == 1 and isinstance(p.value, str):
                want_checked = p.value.strip().lower() in ("yes", "true", "1", "y")
            page.locator(target.selector).first.set_checked(want_checked)
            p.value = opt if want_checked else "unchecked"
            return
        if p.value is None:
            raise ValueError("no value")
        text = str(p.value)
        if isinstance(p.value, bool):
            text = "Yes" if p.value else "No"
        locator.fill(text)
        if f.autocomplete == "off" and f.type == "text" and f.options == []:
            # Some react-select style inputs need Enter to commit the typed value.
            try:
                locator.press("Enter", timeout=1000)
            except Exception:  # pragma: no cover - best effort
                pass

    def run(
        self,
        page,
        url: str | None = None,
        html: str | None = None,
        ats_hint: str | None = None,
        submit: bool = False,
        screenshot_path: Path | None = None,
    ) -> FillResult:
        """Fill the form on `page`. Pass `url` to navigate, or `html` to load a saved page (tests)."""
        if not submit:
            # Dry run: no `submit` event may go through, whatever a stray Enter or a mis-clicked button does.
            page.add_init_script(f"({SUBMIT_GUARD_JS})()")
        if html is not None:
            page.set_content(html)
            url = url or "file://fixture"
        elif url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
        result = FillResult(url=url or page.url, profile_origin="none")
        self._recovery_calls = 0  # AI recovery budget is per form (per run call)

        spec = profile_for_url(url or "", self.profiles) or (self.profiles.get(ats_hint) if ats_hint else None)
        for step in spec.steps if spec else []:
            for sel in step.click.split(","):
                loc = page.locator(sel.strip()).first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000)
                        page.wait_for_timeout(step.wait_ms)
                        break
                except Exception:
                    if not step.optional:
                        raise

        nav = self._navigator(page, submit) if self.navigate else None
        fields, hop, stop = self._locate_form(page, spec, nav)
        if not submit:
            page.evaluate(SUBMIT_GUARD_JS)  # the init script covers navigations; this covers set_content
        if hop:
            url = page.url
            result.url, result.hops = url, hop
            spec = profile_for_url(url, self.profiles) or spec or (self.profiles.get(ats_hint) if ats_hint else None)
            log.info("form located", hop=hop, url=url, ats=spec.ats if spec else None)
        if stop is not None:
            # The navigator could not reach a form: captcha, account wall, a mailto: apply link, or nothing to
            # fill after the hop budget. Pause with its reason; the pipeline shows it on the Queue page.
            result.url = page.url
            result.paused_reason, result.mailto = stop.reason, stop.mailto
            self._screenshot(page, screenshot_path, result)
            log.info("navigator stopped", status=stop.status, reason=stop.reason, hops=hop)
            return result
        captcha = detect_captcha(page)
        result.fields_total = len([f for f in fields if not f.hidden])
        fields = self._drop_never(fields, spec)
        if not fields:
            result.error = "no form fields found on the page"
            return result

        plan, learned = self._plan(page, fields, url or "", spec, ats_hint, result)
        result.plan = plan
        self._execute(page, plan, fields, result)
        self._report_unfilled(plan, fields, result)
        if nav is not None and not captcha and not result.needs_human and not result.unfilled_required:
            captcha = self._wizard(page, nav, fields, spec, ats_hint, result)

        self._screenshot(page, screenshot_path, result)

        if captcha:
            result.paused_reason = f"captcha on page ({captcha})"
        elif result.needs_human:
            result.paused_reason = "a question needs a human"
        elif result.unfilled_required:
            result.paused_reason = "required fields left empty"
        if learned is not None and result.profile_origin == "learned":
            self._bump(learned, success=not result.unfilled_required)

        if submit and not result.paused_reason:
            self._submit(page, spec, result)
        return result

    # ------------------------------------------------------------ run() pieces (shared with the wizard loop)

    @staticmethod
    def _screenshot(page, screenshot_path: Path | None, result: FillResult) -> None:
        if not screenshot_path:
            return
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            result.screenshot_path = str(screenshot_path)
        except Exception as exc:  # noqa: BLE001 - a lost screenshot must not lose the outcome
            log.warning("screenshot failed", error=str(exc)[:160])

    @staticmethod
    def _drop_never(fields: list[FormField], spec: FormProfileSpec | None) -> list[FormField]:
        never: set[int] = set()
        for sel in spec.never if spec else []:
            never.update(f.idx for f in match_selector(fields, sel))
        return [f for f in fields if f.idx not in never]

    def _plan(self, page, fields: list[FormField], url: str, spec: FormProfileSpec | None, ats_hint: str | None,
              result: FillResult) -> tuple[list[PlannedFill], FormProfile | None]:
        """Profile selection: built-in by URL, learned by fingerprint, else the AI mapper (whose mapping is
        saved as a learned profile). Custom questions are resolved on top. Returns the plan and the learned
        row that was used (for the success/failure tally)."""
        domain, fp = domain_of(url or ""), fingerprint(fields)
        plan: list[PlannedFill] = []
        ai_mapping = None
        rate = 0.0
        if spec:
            rate = self._profile_match(spec.fields, fields)
            if rate >= MIN_MATCH_RATE:
                plan = self.plan_from_spec(fields, spec.fields, "builtin")
                result.profile_origin = "builtin"
            else:
                log.warning("builtin profile looks stale", ats=spec.ats, match_rate=round(rate, 2), url=url)
        learned = self.learned_profile(domain, fp)
        if learned and rate < MIN_MATCH_RATE:
            rate = self._profile_match(learned.fields, fields)
            if rate >= MIN_MATCH_RATE:
                plan = self.plan_from_spec(fields, learned.fields, "learned")
                result.profile_origin = "learned"
        if rate < MIN_MATCH_RATE and self.llm is not None:
            try:
                ai_mapping = map_fields(self.session, self.llm, self.user, fields, url or "", page.title())
                ai_fields = mapping_to_profile_fields(fields, ai_mapping)
                plan = self.plan_from_spec(fields, ai_fields, "ai")
                result.profile_origin = "ai"
                if self.learn and ai_fields:
                    self.save_learned(
                        domain,
                        fp,
                        spec.ats if spec else ats_hint,
                        ai_fields,
                        spec.submit if spec else "button[type=submit]",
                    )
            except LLMError as exc:
                log.warning("ai mapper failed", url=url, error=str(exc))
                if not plan:
                    result.error = f"no profile and the AI mapper failed: {exc}"
        self.fill_questions(plan, fields, result, ai_mapping)
        return plan, learned

    def _execute(self, page, plan: list[PlannedFill], fields: list[FormField], result: FillResult) -> None:
        for p in plan:
            if p.done:
                continue
            try:
                if p.kind == "question" and p.value is None:
                    raise ValueError("no answer")
                self._fill_one(page, p, fields, result)
                p.done = True
            except Exception as exc:  # one bad field must not abort the run
                p.error = str(exc)[:200]
                try:
                    if self._retry_with_options(page, p, fields, result):
                        continue
                    # A value was tried and the form rejected it (bad option, wrong format): one AI-guided fix.
                    if p.value is not None and self._ai_recover(page, p, fields, result, p.error):
                        continue
                except Exception as retry_exc:  # noqa: BLE001 - the answer source / recovery failed
                    p.error = f"{p.error}; retry failed: {str(retry_exc)[:120]}"[:200]
                log.debug("field not filled", selector=p.field.selector, error=p.error)
                continue
            # Filled: an inline validation error next to the field means the value was wrong. Try one AI fix.
            err = self._field_error_safe(page, p.field)
            if err:
                if not self._ai_recover(page, p, fields, result, err):
                    result.needs_human.append(f"{p.field.question[:100]}: form error {err[:80]!r}")

    @staticmethod
    def _report_unfilled(plan: list[PlannedFill], fields: list[FormField], result: FillResult) -> None:
        done_idx = {p.field.idx for p in plan if p.done}
        choice_names_done = {p.field.name for p in plan if p.done and p.field.is_choice}
        for f in fields:
            if f.required and not f.hidden and f.idx not in done_idx and f.name not in choice_names_done:
                result.unfilled_required.append(f.question[:100] or f.selector)
        result.unfilled_required = list(dict.fromkeys(result.unfilled_required))
        result.needs_human = list(dict.fromkeys(result.needs_human))

    # ------------------------------------------------------------ locating the form

    # Fewer visible fields than this on a page means "not the application form" (newsletter box, search).
    MIN_FORM_FIELDS = 3
    APPLY_LINK = "a:has-text('Apply'), button:has-text('Apply')"

    def _usable(self, fields: list[FormField]) -> bool:
        return len([f for f in fields if not f.hidden]) >= self.MIN_FORM_FIELDS

    def _navigator(self, page, submit: bool) -> Navigator:
        opts = {"session": self.session, "llm": self.llm, "user": self.user, "profile": self.vault.profile,
                "profiles": self.profiles, "allow_final": bool(submit), **self.nav_options}
        return Navigator(page, **opts)

    def _locate_form(self, page, spec: FormProfileSpec | None, nav=None):
        """Find the application form. With a navigator (the default) any landing page gets the full treatment
        (forms/navigator.py); without one, the legacy two-hop search: an embedded ATS frame (navigated to
        directly so filling, screenshots and submit work on a plain page), or one "Apply" link away.

        Returns `(fields, hops, stop)`: `stop` is the navigator's outcome when it could not reach a form that
        the caller should pause on (captcha, account wall, mailto, nothing fillable after the hop budget);
        otherwise None and `fields` are what to fill (a page with a few fields but no better target is still
        filled, as before)."""
        fields = extract_fields(page)
        # A sign-in wall with three visible inputs (email, password, "remember me") counts as usable here, but
        # it is the navigator's job (stored-account sign-in / generic sign-up), never a form to fill.
        if self._usable(fields) and (nav is None or not nav.is_account_wall()):
            return fields, "", None
        if nav is not None:
            outcome = nav.find_form()
            hops = " -> ".join(outcome.hops)
            if outcome.status == STATUS_FORM:
                return outcome.fields, hops, None
            if outcome.status == STATUS_NO_FORM and any(not f.hidden for f in outcome.fields):
                return outcome.fields, hops, None  # a short form (quick apply) is still worth filling
            return outcome.fields, hops, outcome
        hops: list[str] = []
        for _ in range(2):
            frame_url = self._form_frame_url(page)
            if frame_url:
                page.goto(frame_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                hops.append(f"frame {frame_url[:80]}")
            elif not self._follow_apply_link(page):
                break
            else:
                hops.append("apply link")
            fields = extract_fields(page)
            if self._usable(fields):
                break
        return fields, " -> ".join(hops), None

    # ------------------------------------------------------------ multi-step wizards

    def _wizard(self, page, nav, fields: list[FormField], spec: FormProfileSpec | None, ats_hint: str | None,
                result: FillResult) -> str:
        """After the visible step is filled, walk a wizard: click Next / Continue (never a final submit, see
        navigator.FINAL), plan and fill whatever new fields appear, up to `WIZARD_MAX_STEPS`. In a dry run the
        submit guard stops a Next that posts the form, so a server-side wizard ends at step one; a JS wizard
        advances either way. Returns a captcha marker when one shows up on a later step, else ""."""
        seen = {(f.selector, f.label, f.type) for f in fields if not f.hidden}
        for _ in range(WIZARD_MAX_STEPS):
            nxt = nav.next_control()
            if nxt is None:
                return ""
            before = page.url
            nav.visited.add((before, nxt.key))
            if nav.follow(nxt) == "failed":
                return ""
            nav.dismiss_banners()
            captcha = detect_captcha(page)
            if captcha:
                return captcha
            new_fields = [f for f in extract_fields(page)
                          if not f.hidden and (f.selector, f.label, f.type) not in seen]
            new_fields = self._drop_never(new_fields, spec)
            if not new_fields:
                return ""
            seen.update((f.selector, f.label, f.type) for f in new_fields)
            if page.url != before:
                result.url = page.url
            result.hops = " -> ".join(h for h in (result.hops, f"wizard {nxt.label!r}") if h)
            log.info("wizard step", control=nxt.text[:40], new_fields=len(new_fields), url=page.url[:80])
            result.fields_total += len([f for f in new_fields if not f.hidden])
            plan, _ = self._plan(page, new_fields, page.url, spec, ats_hint, result)
            result.plan.extend(plan)
            self._execute(page, plan, new_fields, result)
            self._report_unfilled(plan, new_fields, result)
            if result.needs_human or result.unfilled_required:
                return ""
        return ""

    def _form_frame_url(self, page) -> str | None:
        """URL of the child frame that holds the application form, preferring one a profile recognises."""
        best: tuple[int, int, str] | None = None
        for frame in getattr(page, "frames", [])[1:]:
            url = frame.url or ""
            if not url.startswith("http"):
                continue
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=5000)
                count = frame.locator("input, select, textarea").count()
            except Exception:  # noqa: BLE001 - detached, cross-origin or still loading frame
                continue
            if count < self.MIN_FORM_FIELDS:
                continue
            known = 1 if profile_for_url(url, self.profiles) else 0
            cand = (known, count, url)
            if best is None or cand[:2] > best[:2]:
                best = cand
        return best[2] if best else None

    # A button is only an "Apply" link when it cannot submit anything: not type=submit and not inside a form
    # (a short quick-apply form's own submit button says "Apply" too).
    APPLY_BUTTON_OK_JS = (
        "e => e.tagName !== 'BUTTON' || ((e.type || '').toLowerCase() !== 'submit' && !e.closest('form'))"
    )

    def _follow_apply_link(self, page) -> bool:
        links = page.locator(self.APPLY_LINK)
        try:
            for i in range(min(links.count(), 5)):
                loc = links.nth(i)
                if not loc.is_visible() or not loc.evaluate(self.APPLY_BUTTON_OK_JS):
                    continue
                href = loc.get_attribute("href")
                if href and not href.startswith(("#", "javascript:", "mailto:")):
                    page.goto(href if href.startswith("http") else page.url.split("/", 3)[0] + "//" +
                              page.url.split("/", 3)[2] + href, wait_until="domcontentloaded", timeout=60000)
                else:
                    loc.click(timeout=5000)
                page.wait_for_timeout(2500)
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - a dead link must not abort the run
            log.debug("apply link not followed", error=str(exc)[:120])
            return False

    def _retry_with_options(self, page, p: PlannedFill, fields: list[FormField], result: FillResult) -> bool:
        """A select, choice group or combobox whose value matches no option: let the answer source pick one.
        For a combobox the options come from the opened dropdown, which the planner never saw, so the answer
        source gets a second ask even when it already answered without them."""
        f = p.field
        if self.answers is None:
            return False
        if f.is_combobox:
            options = f.options or self._combobox_options(page, f)
            if not options:
                return False
            f.options = options
        elif p.source == "ai" or not (f.type == "select" or f.is_choice):
            return False
        else:
            options = (
                f.options
                if f.type == "select"
                else [g.label or g.value for g in fields if g.is_choice and g.name and g.name == f.name]
                or [f.label or f.value]
            )
        answer, conf = self.answers.answer(f.question, options)
        if answer is None:
            return False
        p.value, p.source, p.confidence = answer, "ai", conf
        try:
            self._fill_one(page, p, fields, result)
        except Exception as exc:
            p.error = str(exc)[:200]
            return False
        p.done, p.error = True, ""
        if conf < self.min_answer_confidence:
            result.needs_human.append(f"{f.question[:100]}: low confidence answer, review it")
        return True

    # ------------------------------------------------------------ AI-assisted error recovery

    # Most AI recovery calls one form may make. Bounds cost when a page rejects many fields.
    AI_RECOVERY_CAP = 6

    def _field_error_safe(self, page, f: FormField) -> str:
        """The visible validation message next to this field, or "". Probed only with an LLM present, so the
        no-AI path never touches the DOM here and behaves exactly as before."""
        if self.llm is None or self.user is None:
            return ""
        try:
            return (page.evaluate(FIELD_ERROR_JS, f.selector) or "").strip()
        except Exception:  # noqa: BLE001 - detached node / navigation
            return ""

    def _recovery_options(self, page, f: FormField, fields: list[FormField]) -> list[str]:
        if f.type == "select":
            return list(f.options or [])
        if f.is_choice:
            return [g.label or g.value for g in fields if g.is_choice and g.name and g.name == f.name] or [
                f.label or f.value
            ]
        if f.is_combobox:
            return list(f.options) if f.options else self._combobox_options(page, f)
        return []

    def _ai_recover(self, page, p: PlannedFill, fields: list[FormField], result: FillResult,
                    error_text: str) -> bool:
        """One LLM-guided correction for a field that failed to fill or shows an inline error: ask
        `ai.service.recover_field` for a corrected value/action, apply it once and re-verify. A no-op without an
        LLM (so the no-AI behaviour is unchanged) and capped per form. Fills only; never clicks submit."""
        if self.llm is None or self.user is None or self._recovery_calls >= self.AI_RECOVERY_CAP:
            return False
        self._recovery_calls += 1
        from jobapply.ai.service import recover_field

        f = p.field
        options = self._recovery_options(page, f, fields)
        try:
            snippet = (page.evaluate(DOM_SNIPPET_JS, f.selector) or "").strip()
        except Exception:  # noqa: BLE001 - detached node
            snippet = ""
        tried = "" if p.value is None else ("Yes" if p.value is True else "No" if p.value is False else str(p.value))
        job = getattr(self.answers, "job", None) if self.answers is not None else None
        try:
            rec = recover_field(self.session, self.llm, self.user, question=f.question or p.key,
                                value_tried=tried, field_type=f.type, options=options,
                                error_text=error_text or "", dom_snippet=snippet, job=job)
        except LLMError as exc:
            log.warning("field recovery failed", selector=f.selector, error=str(exc)[:120])
            return False
        if rec.action == "skip" or not rec.value:
            log.debug("recovery suggested skip", selector=f.selector, reason=rec.reason[:120])
            return False
        prev = p.value
        p.value = rec.value
        try:
            self._fill_one(page, p, fields, result)
        except Exception as exc:  # noqa: BLE001 - the corrected value still would not fill
            p.value = prev
            p.error = f"recovery value rejected: {str(exc)[:120]}"[:200]
            return False
        err = self._field_error_safe(page, f)
        if err:
            p.error = f"recovery still invalid: {err[:120]}"[:200]
            return False
        p.done, p.error, p.source = True, "", "ai"
        note = (rec.reason or f"set to {rec.value}")[:120]
        result.recoveries.append(f"{(f.question[:60] or f.selector)}: {note}")
        log.info("ai recovered field", selector=f.selector, action=rec.action, note=note)
        return True

    @staticmethod
    def _profile_match(mapping: dict[str, str], fields: list[FormField]) -> float:
        """Share of the profile's selectors that still find a field on the page."""
        selectors = [sel for sel, key in mapping.items() if key != "skip"]
        if not selectors:
            return 0.0
        visible = [f for f in fields if not f.hidden]
        hit = sum(1 for sel in selectors if match_selector(visible, sel))
        return hit / len(selectors)

    def _bump(self, row: FormProfile, success: bool) -> None:
        if success:
            row.success_count += 1
        else:
            row.fail_count += 1
        self.session.add(row)
        self.session.flush()

    def _submit(self, page, spec: FormProfileSpec | None, result: FillResult) -> None:
        selector = spec.submit if spec else "button[type=submit]"
        for sel in selector.split(","):
            loc = page.locator(sel.strip()).first
            if loc.count():
                page.evaluate("() => { window.__jobapplyAllowSubmit = true; }")  # lift the dry-run guard
                loc.click(timeout=10000)
                page.wait_for_timeout(3000)
                break
        else:
            result.error = "submit button not found"
            return
        success_markers = spec.success if spec else []
        for marker in success_markers:
            try:
                loc = page.locator(marker).first
                if loc.count() and loc.is_visible():
                    result.submitted = True
                    return
            except Exception:
                continue
        # No marker matched: treat a page change or an empty form as success, otherwise flag for review.
        if not re.search(r"error|required|invalid", page.content()[:20000], re.I):
            result.submitted = True
        else:
            result.error = "form reported validation errors after submit"


def vault_view_for(user, app=None) -> VaultView:
    profile = dict(user.profile or {})
    resume = app.resume_variant if app and app.resume_variant else None
    if not resume:
        uploads = profile.get("resumes") or []
        resume = uploads[-1]["path"] if uploads else None
    return VaultView(
        profile=profile, resume_path=resume, cover_letter=(app.cover_letter if app else "") or "", answers={}
    )
