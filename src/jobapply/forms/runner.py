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
    screenshot_path: str | None = None
    submitted: bool = False
    paused_reason: str = ""
    error: str = ""

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

    def __init__(self, session: Session, llm: LLM | None, user, job=None):
        self.session, self.llm, self.user, self.job = session, llm, user, job

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
    ):
        self.session = session
        self.vault = vault
        self.answers = answers
        self.llm = llm
        self.user = user
        self.learn = learn
        self.min_answer_confidence = min_answer_confidence
        self.profiles = load_builtin_profiles(profiles_dir or get_settings().profiles_dir)

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
            s = fuzz.partial_ratio(want, o) if len(want) > 2 else 0
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
                if len(needle) >= 4:
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
        loc.press("ArrowDown")
        loc.press("Enter")
        page.wait_for_timeout(300)
        shown = self._shown_value(page, f)
        if not shown or shown.strip().lower() == text.lower():
            self._clear_text(loc)
            self._close(loc)
            raise ValueError(f"no suggestion matches {text!r}")
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
        loc.press("Control+a")
        loc.press("Backspace")

    @staticmethod
    def _close(loc) -> None:
        try:
            loc.press("Escape", timeout=1000)
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _options_locator(self, page, f: FormField, loc):
        """The option elements of the open menu that belongs to this input, or None while it is closed.
        aria-controls / aria-owns first, then React-Select's `react-select-<id>-listbox`, then any visible
        listbox (hidden ones, like a phone widget's country list, are skipped)."""
        try:
            ctl = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns") or f.controls
        except Exception:  # noqa: BLE001 - detached input
            ctl = f.controls
        candidates = []
        if ctl:
            candidates.append(page.locator(f'[id="{ctl}"]'))
        if f.id:
            candidates.append(page.locator(f'[id="react-select-{f.id}-listbox"]'))
        candidates += [
            page.locator('[id$="-listbox"]:visible'),
            page.locator("[role=listbox]:visible"),
            page.locator('[class*="menu-list"]:visible, [class*="__menu"]:visible, .pac-container:visible'),
        ]
        for cand in candidates:
            try:
                if not cand.count() or not cand.first.is_visible():
                    continue
                opts = cand.first.locator("[role=option]")
                if not opts.count():
                    opts = cand.first.locator('li, [class*="option"], .pac-item')
                if opts.count():
                    return opts
            except Exception:  # noqa: BLE001 - menu re-rendered under us; poll again
                continue
        return None

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
                if any(texts):
                    return texts
            if time.monotonic() >= deadline:
                return []
            page.wait_for_timeout(self.LISTBOX_POLL_MS)

    def _choose(self, page, f: FormField, loc, options: list[str], pick: str) -> None:
        idx = options.index(pick)
        opts = self._options_locator(page, f, loc)
        try:
            if opts is not None and opts.count() > idx:
                target = opts.nth(idx)
                target.scroll_into_view_if_needed(timeout=1000)
                target.click(timeout=2000)
                return
        except Exception as exc:  # noqa: BLE001 - menu closed or option not clickable: use the keyboard
            log.debug("option click failed", selector=f.selector, error=str(exc)[:120])
        for _ in range(idx):
            loc.press("ArrowDown")
        loc.press("Enter")

    def _verify_shown(self, page, f: FormField, loc, pick: str, typed: str) -> str:
        page.wait_for_timeout(200)
        shown = self._shown_value(page, f)
        if not self._shows(shown, pick, typed):
            loc.press("Enter")  # the click missed a re-rendered menu: Enter commits the focused option
            page.wait_for_timeout(200)
            shown = self._shown_value(page, f)
        if not self._shows(shown, pick, typed):
            self._close(loc)
            raise ValueError(f"picked {pick!r} but the control shows {shown!r}")
        return shown

    @staticmethod
    def _shows(shown: str, pick: str, typed: str) -> bool:
        s, k = shown.strip().lower(), pick.strip().lower()
        if not s:
            return False
        if s == typed.strip().lower() and k != s:
            return False  # the input only echoes what we typed; nothing was selected
        return k == s or k in s or s in k or fuzz.ratio(s, k) >= FormRunner.FUZZY_MIN

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
        if html is not None:
            page.set_content(html)
            url = url or "file://fixture"
        elif url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
        result = FillResult(url=url or page.url, profile_origin="none")

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

        fields, hop = self._locate_form(page, spec)
        if hop:
            url = page.url
            result.url = url
            spec = profile_for_url(url, self.profiles) or spec or (self.profiles.get(ats_hint) if ats_hint else None)
            log.info("form located", hop=hop, url=url, ats=spec.ats if spec else None)
        captcha = detect_captcha(page)
        result.fields_total = len([f for f in fields if not f.hidden])
        never = set()
        for sel in spec.never if spec else []:
            never.update(f.idx for f in match_selector(fields, sel))
        fields = [f for f in fields if f.idx not in never]
        if not fields:
            result.error = "no form fields found on the page"
            return result

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
        result.plan = plan

        for p in plan:
            try:
                if p.kind == "question" and p.value is None:
                    raise ValueError("no answer")
                self._fill_one(page, p, fields, result)
                p.done = True
            except Exception as exc:  # one bad field must not abort the run
                p.error = str(exc)[:200]
                if self._retry_with_options(page, p, fields, result):
                    continue
                log.debug("field not filled", selector=p.field.selector, error=p.error)
        done_idx = {p.field.idx for p in plan if p.done}
        choice_names_done = {p.field.name for p in plan if p.done and p.field.is_choice}
        for f in fields:
            if f.required and not f.hidden and f.idx not in done_idx and f.name not in choice_names_done:
                result.unfilled_required.append(f.question[:100] or f.selector)
        result.unfilled_required = list(dict.fromkeys(result.unfilled_required))
        result.needs_human = list(dict.fromkeys(result.needs_human))

        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            result.screenshot_path = str(screenshot_path)

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

    # Fewer visible fields than this on a page means "not the application form" (newsletter box, search).
    MIN_FORM_FIELDS = 3
    APPLY_LINK = "a:has-text('Apply'), button:has-text('Apply')"

    def _usable(self, fields: list[FormField]) -> bool:
        return len([f for f in fields if not f.hidden]) >= self.MIN_FORM_FIELDS

    def _locate_form(self, page, spec: FormProfileSpec | None) -> tuple[list[FormField], str]:
        """Find the application form: on the page itself, inside an embedded ATS frame (navigated to directly
        so filling, screenshots and submit work on a plain page), or one "Apply" link away. At most two hops.
        Returns the fields and a short description of the hop taken ("" when the page already had the form)."""
        hops: list[str] = []
        for _ in range(3):
            fields = extract_fields(page)
            if self._usable(fields):
                return fields, " -> ".join(hops)
            frame_url = self._form_frame_url(page)
            if frame_url:
                page.goto(frame_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                hops.append(f"frame {frame_url[:80]}")
                continue
            if len(hops) >= 2 or not self._follow_apply_link(page):
                return fields, " -> ".join(hops)
            hops.append("apply link")
        return extract_fields(page), " -> ".join(hops)

    def _form_frame_url(self, page) -> str | None:
        """URL of the child frame that holds the application form, preferring one a profile recognises."""
        best: tuple[int, int, str] | None = None
        for frame in getattr(page, "frames", [])[1:]:
            url = frame.url or ""
            if not url.startswith("http"):
                continue
            try:
                count = frame.locator("input, select, textarea").count()
            except Exception:  # noqa: BLE001 - detached or cross-origin frame
                continue
            if count < self.MIN_FORM_FIELDS:
                continue
            known = 1 if profile_for_url(url, self.profiles) else 0
            cand = (known, count, url)
            if best is None or cand[:2] > best[:2]:
                best = cand
        return best[2] if best else None

    def _follow_apply_link(self, page) -> bool:
        loc = page.locator(self.APPLY_LINK).first
        try:
            if not loc.count() or not loc.is_visible():
                return False
            href = loc.get_attribute("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                page.goto(href if href.startswith("http") else page.url.split("/", 3)[0] + "//" +
                          page.url.split("/", 3)[2] + href, wait_until="domcontentloaded", timeout=60000)
            else:
                loc.click(timeout=5000)
            page.wait_for_timeout(2500)
            return True
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
