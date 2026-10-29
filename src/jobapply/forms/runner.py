"""Playwright form runner.

Opens the application page, finds a profile (built-in by URL, learned by fingerprint, or the AI mapper),
fills every mapped field, uploads the resume, takes a screenshot and, in dry-run mode, stops there.
It never clicks submit unless `submit=True`, which only the apply pipeline passes, and only when the user's
site has auto-submit on and the global dry-run switch is off.
"""

from __future__ import annotations

import re
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
            return banked.answer, banked.confidence
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
        for selector, key in mapping.items():
            for f in match_selector(fields, selector):
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

    def _fill_one(self, page, p: PlannedFill, fields: list[FormField]) -> None:
        f = p.field
        locator = page.locator(f.selector).first
        if p.kind == "file":
            if not p.value or not Path(str(p.value)).exists():
                raise ValueError("resume file missing")
            locator.set_input_files(str(p.value))
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

        captcha = detect_captcha(page)
        fields = extract_fields(page)
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
                self._fill_one(page, p, fields)
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

    def _retry_with_options(self, page, p: PlannedFill, fields: list[FormField], result: FillResult) -> bool:
        """A select or choice group whose vault value matches no option: let the answer source pick one."""
        f = p.field
        if self.answers is None or p.source == "ai" or not (f.type == "select" or f.is_choice):
            return False
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
            self._fill_one(page, p, fields)
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
