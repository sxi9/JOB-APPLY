"""Cutshort apply filler.

Runs inside the user's persistent, logged-in Cutshort browser profile (headless). The site's "Apply to this
job" is a logged-in one-click action that may open a screening questionnaire first. Because that click is the
final, irreversible step on jobs without a questionnaire, the runner:

- in a dry run, or while the site's auto-submit is off, opens the job page, checks the session and the
  button, screenshots and STOPS before clicking Apply (status `dry_run`);
- with `submit=True` (only the apply path passes it, and only when global dry-run is off and the user's site
  has auto-submit on) clicks Apply, fills a questionnaire from the profile vault / answer bank / LLM, and
  clicks its Submit; a question nobody can answer pauses the run, closes the dialog and leaves nothing sent.

Captcha, Cloudflare interstitials, OTP prompts and the login redirect pause the run with a screenshot and are
never retried. Result shape is the shared `FillResult`, so `pipeline.apply.record_outcome` and the Queue page
need nothing new; `apply_job` wraps it in the ``{status, screenshot, reason}`` dict of the uniform interface.
Live questionnaire markup is unverified (no logged-in session was available): selectors live in
``config/form_profiles/cutshort.yaml`` and DEFAULT_SELECTORS below so they can be fixed without a code change.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.forms.fields import FormField, detect_captcha, extract_fields, match_selector
from jobapply.forms.profiles import FormProfileSpec, VaultView, load_builtin_profiles, resolve_key
from jobapply.forms.runner import AnswerSource, FillResult, FormRunner, PlannedFill
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, User
from jobapply.sites.cutshort.session import SITE, CutshortSession, classify_page, throttled
from jobapply.sites.linkedin.easy_apply import key_for_label, skill_years

log = get_logger(__name__)

PROFILE_ORIGIN = "builtin:cutshort"
DEFAULT_COOLDOWN_HOURS = 6
MAX_STEPS = 6
MIN_ANSWER_CONFIDENCE = 60
_RATE_LIMIT_STATUSES = (403, 429, 503)
DIALOG_TAG = "data-jobapply-dialog"

# Fallbacks when config/form_profiles/cutshort.yaml is missing an entry. The YAML wins for submit / success.
DEFAULT_SELECTORS: dict[str, str] = {
    "apply_button": 'button:has-text("Apply to this job"), button:has-text("Apply now"), a:has-text("Apply now"), '
                    'button:has-text("Apply")',
    # Plain CSS only (Playwright's :has-text / :text-matches): a `text=` engine cannot share a comma list.
    "already_applied": 'button:has-text("Applied"), p:text-matches("(already|have) applied", "i"), '
                       'span:text-matches("(already|have) applied", "i"), '
                       'div:text-matches("^\\s*(you have )?already applied", "i")',
    "dialog": '[role=dialog], .ReactModal__Content, [class*="modal"], [class*="Modal"]',
    "next": 'button:has-text("Next"), button:has-text("Continue")',
    "submit": 'button[type=submit]:has-text("Submit"), button:has-text("Submit application"), '
              'button:has-text("Submit answers"), button:has-text("Submit"), button:has-text("Send application")',
    "close": 'button[aria-label="Close"], button[aria-label="close"], button:has-text("Cancel"), '
             'button:has-text("Close")',
    "error": '[role=alert], [class*="error"]',
    "login": 'button:has-text("Candidate login"), input[type="password"], [id*="auth-modal"], '
             'button:has-text("Get started")',
    "security_check": 'iframe[src*="captcha"], #challenge-form, [class*="cf-challenge"], input[name*="otp"], '
                      'input[autocomplete="one-time-code"]',
    "success": 'p:text-matches("application (sent|submitted|received)", "i"), '
               'div:text-matches("^\\s*application (sent|submitted|received)", "i"), '
               'span:text-matches("application (sent|submitted|received)", "i"), '
               'p:text-matches("successfully applied|you have applied", "i"), '
               'span:text-matches("successfully applied|you have applied", "i"), button:has-text("Applied")',
}
# Titles / URLs of interstitials. A job description may legitimately say "security check" or "unusual
# activity", so the body text is only matched against wording no posting uses (`_BODY_SECURITY_TEXT`).
_SECURITY_TEXT = re.compile(
    r"verify you are human|checking your browser|just a moment|enter the otp|one[- ]time password|"
    r"we sent a code|unusual activity|verify your identity|are you a robot|attention required|access denied",
    re.I,
)
_BODY_SECURITY_TEXT = re.compile(
    r"verify you are human|checking your browser before accessing|enter the otp|one[- ]time password|"
    r"enter the code we sent|we sent a code to",
    re.I,
)
_PLACEHOLDERS = {"", "select", "select an option", "choose", "please select", "-", "--"}
_TEXT_FIELD_KEYS = {"profile.notice_period", "profile.current_ctc", "profile.expected_ctc",
                    "profile.years_experience"}

PauseFn = Callable[[Any, int, int], None]


# ------------------------------------------------------------------ profile / selectors


def load_profile(folder: Path | None = None) -> FormProfileSpec | None:
    """The built-in `cutshort` form profile (questionnaire label map, submit, success markers), if present."""
    try:
        return load_builtin_profiles(folder or get_settings().profiles_dir).get(SITE)
    except Exception as exc:  # noqa: BLE001 - a broken YAML must not stop the run; defaults still apply
        log.warning("cutshort form profile not loaded", error=str(exc)[:200])
        return None


def selectors_from(spec: FormProfileSpec | None) -> dict[str, str]:
    out = dict(DEFAULT_SELECTORS)
    if spec is not None:
        if spec.submit and spec.submit != "button[type=submit]":
            out["submit"] = spec.submit
        if spec.success:
            out["success"] = ", ".join(spec.success)
    return out


# ------------------------------------------------------------------ lazy hooks


def _human_pause(page, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


def _launch_persistent(pw, user_id: int, site: str = SITE, headless: bool | None = True):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, site, headless=True if headless is None else headless)


def _cutshort_session(context, user: User, session: Session) -> CutshortSession:
    return CutshortSession(context, user, session)


def _is_throttled(user: User, site) -> bool:
    return bool(throttled(user, site))


def _between_applications(page) -> None:
    _human_pause(page, 6000, 15000)


def _classify(page) -> str:
    try:
        return classify_page(page)
    except Exception as exc:  # noqa: BLE001
        log.debug("classify failed", error=str(exc)[:120])
        return "unknown"


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


# ------------------------------------------------------------------ the runner


class CutshortApplyRunner:
    def __init__(
        self,
        session_db: Session,
        user: User,
        vault: VaultView,
        answers: AnswerSource | None = None,
        llm=None,
        resume_path: str | None = None,
        spec: FormProfileSpec | None = None,
        selectors: dict[str, str] | None = None,
        min_answer_confidence: int = MIN_ANSWER_CONFIDENCE,
        max_steps: int = MAX_STEPS,
        pause: PauseFn | None = None,
    ):
        self.session = session_db
        self.user = user
        self.vault = vault
        self.answers = answers
        self.llm = llm
        self.resume_path = resume_path or vault.resume_path
        self.spec = spec
        self.sel = {**selectors_from(spec), **(selectors or {})}
        self.min_answer_confidence = min_answer_confidence
        self.max_steps = max_steps
        self._pause: PauseFn = pause or _human_pause
        self._clicked_apply = False

    # ---------------------------------------------------------------- page helpers

    def _visible(self, page, key: str, within=None):
        selector = self.sel.get(key, "")
        if not selector:
            return None
        base = within if within is not None else page
        try:
            loc = base.locator(selector)
            n = loc.count()
            for i in range(min(n, 12)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception as exc:  # noqa: BLE001 - detached nodes mid-transition
            log.debug("locator failed", key=key, error=str(exc)[:120])
        return None

    def _dialog(self, page):
        """The visible questionnaire dialog, tagged for the in-page lookups, or None."""
        try:
            loc = page.locator(self.sel["dialog"])
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                if el.is_visible():
                    el.evaluate(f"(e) => e.setAttribute('{DIALOG_TAG}', '1')")
                    return el
        except Exception as exc:  # noqa: BLE001
            log.debug("dialog lookup failed", error=str(exc)[:120])
        return None

    def _blocked(self, page) -> str:
        """Captcha / Cloudflare / OTP detection. Returns the reason or ''."""
        try:
            cap = detect_captcha(page)
        except Exception:  # noqa: BLE001
            cap = ""
        if cap:
            return f"captcha on page ({cap})"
        if self._visible(page, "security_check") is not None:
            return "security check page"
        try:
            url, title = page.url or "", page.title() or ""
        except Exception:  # noqa: BLE001
            url, title = "", ""
        if re.search(r"/cdn-cgi/challenge|/verify-otp", url, re.I) or _SECURITY_TEXT.search(title):
            return "security check page"
        if (self._visible(page, "apply_button") is not None or self._dialog(page) is not None
                or self._succeeded(page)):
            return ""  # a rendered job page: its description may mention "unusual activity"
        try:
            text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
        except Exception:  # noqa: BLE001
            text = ""
        if _BODY_SECURITY_TEXT.search(text or ""):
            return "security check page"
        return ""

    def _login_needed(self, page) -> bool:
        return _classify(page) == "login" or self._visible(page, "login") is not None

    def _screenshot(self, page, path: Path | None, result: FillResult) -> None:
        if not path or result.screenshot_path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            result.screenshot_path = str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])

    def _fields(self, page) -> tuple[list[FormField], dict[int, Any]]:
        """Visible fields of the questionnaire (inside the dialog when there is one) plus their current values."""
        fields = [f for f in extract_fields(page) if not f.hidden]
        if not fields:
            return [], {}
        state = page.evaluate(
            """(args) => {
              const scope = document.querySelector(args.tag) || document;
              return args.sels.map((s) => {
                let el = null;
                try { el = scope.querySelector(s); } catch (e) { el = null; }
                if (!el || el.getClientRects().length === 0) return null;
                if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return o ? o.text.trim() : ''; }
                if (el.type === 'checkbox' || el.type === 'radio') return !!el.checked;
                return el.value || '';
              });
            }""",
            {"tag": f"[{DIALOG_TAG}]", "sels": [f.selector for f in fields]},
        )
        inside = [f for f, st in zip(fields, state, strict=True) if st is not None]
        values = {f.idx: st for f, st in zip(fields, state, strict=True) if st is not None}
        never: set[int] = set()
        for sel in self.spec.never if self.spec else []:
            never.update(f.idx for f in match_selector(inside, sel))
        inside = [f for f in inside if f.idx not in never and f.type not in ("search", "password", "submit", "button")]
        return inside, values

    # ---------------------------------------------------------------- planning

    def _profile_key(self, f: FormField) -> str | None:
        if self.spec is None:
            return None
        for selector, key in self.spec.fields.items():
            if any(m.idx == f.idx for m in match_selector([f], selector)):
                return key
        return None

    def _value_for(self, f: FormField, options: list[str] | None, is_text: bool) -> tuple[str, Any, str, int]:
        """(key, value, source, confidence). Profile YAML, then the label map, skills, then the answer source."""
        profile = self.vault.profile or {}
        question = f.question
        years = skill_years(question, profile.get("skills"))
        if years is not None:
            return "profile.skills", years, "builtin", 100
        key = self._profile_key(f) or key_for_label(question)
        if key and key != "skip":
            rv = resolve_key(key, self.vault)
            value = rv.value
            if key == "derived.work_authorized" and is_text:
                value = profile.get("work_authorization")
            if value not in (None, ""):
                return key, value, "builtin", 100
        if self.answers is not None and question:
            answer, conf = self.answers.answer(question, options)
            return f"answers.{question[:80]}", answer, "ai", conf
        return f"answers.{question[:80]}", None, "ai", 0

    def _plan_step(self, fields: list[FormField], current: dict[int, Any]) -> list[PlannedFill]:
        plan: list[PlannedFill] = []
        seen_groups: set[str] = set()
        for f in fields:
            if f.is_file:
                if self.resume_path:
                    plan.append(PlannedFill(f, "derived.resume_path", self.resume_path, "file", "builtin"))
                continue
            if f.is_choice:
                if f.name and f.name in seen_groups:
                    continue
                if f.name:
                    seen_groups.add(f.name)
                group = [g for g in fields if g.is_choice and g.name and g.name == f.name] or [f]
                options = [g.label or g.value for g in group]
                if len(group) == 1 and f.type == "checkbox":
                    if not f.required:
                        continue  # consent / newsletter boxes are left alone unless the step needs them
                    plan.append(PlannedFill(f, "const:Yes", True, "bool", "builtin"))
                    continue
                key, value, source, conf = self._value_for(f, options, is_text=False)
                plan.append(PlannedFill(f, key, value, "choice", source, conf))
                continue
            options = f.options if f.type == "select" else None
            key, value, source, conf = self._value_for(f, options, is_text=f.type != "select")
            existing = current.get(f.idx)
            if f.type == "select" and str(existing or "").strip().lower() in _PLACEHOLDERS:
                existing = None
            if value in (None, "") and existing not in (None, "", False):
                plan.append(PlannedFill(f, key or "prefilled", existing, "keep", "builtin", 100))
                continue
            kind = "select" if f.type == "select" else "text"
            plan.append(PlannedFill(f, key, value, kind, source, conf))
        return plan

    # ---------------------------------------------------------------- execution

    def _fill_one(self, page, p: PlannedFill, fields: list[FormField]) -> None:
        f = p.field
        scope = self._dialog(page)
        base = scope if scope is not None else page
        locator = base.locator(f.selector).first
        if p.kind == "keep":
            return
        if p.kind == "file":
            if not p.value or not Path(str(p.value)).exists():
                raise ValueError("resume file missing")
            locator.set_input_files(str(p.value))
            return
        if p.kind == "select":
            opt = FormRunner._pick_option(f.options, p.value)
            if opt is None:
                raise ValueError(f"no option matches the {p.key} value")
            locator.select_option(label=opt)
            p.value = opt
            return
        if p.kind == "bool":
            locator.set_checked(bool(p.value))
            p.value = "checked" if p.value else "unchecked"
            return
        if p.kind == "choice":
            group = [g for g in fields if g.is_choice and g.name and g.name == f.name] or [f]
            labels = [g.label or g.value for g in group]
            opt = FormRunner._pick_option(labels, p.value)
            if opt is None:
                raise ValueError(f"no choice matches the {p.key} value")
            target = next(g for g in group if (g.label or g.value) == opt)
            base.locator(target.selector).first.check()
            p.value = opt
            return
        if p.value is None:
            raise ValueError("no value")
        locator.fill(_as_text(p.value))

    def _retry_with_options(self, page, p: PlannedFill, fields: list[FormField]) -> bool:
        f = p.field
        if self.answers is None or p.source == "ai" or p.kind not in ("select", "choice"):
            return False
        options = (f.options if p.kind == "select"
                   else [g.label or g.value for g in fields if g.is_choice and g.name and g.name == f.name]
                   or [f.label or f.value])
        answer, conf = self.answers.answer(f.question, options)
        if answer is None:
            return False
        p.value, p.source, p.confidence = answer, "ai", conf
        try:
            self._fill_one(page, p, fields)
        except Exception as exc:  # noqa: BLE001
            p.error = str(exc)[:200]
            return False
        p.error = ""
        return True

    def _fill_step(self, page, plan: list[PlannedFill], fields: list[FormField], result: FillResult) -> None:
        for p in plan:
            try:
                if p.kind in ("text", "choice", "select") and p.value is None:
                    raise ValueError("no answer")
                self._fill_one(page, p, fields)
                p.done = True
            except Exception as exc:  # noqa: BLE001 - one bad field must not abort the step
                p.error = str(exc)[:200]
                if self._retry_with_options(page, p, fields):
                    p.done = True
                    if p.confidence < self.min_answer_confidence:
                        result.needs_human.append(f"{p.field.question[:100]}: low confidence answer, review it")
                    continue
                log.debug("field not filled", selector=p.field.selector, error=p.error)
            if p.done and p.source == "ai" and p.confidence < self.min_answer_confidence:
                result.needs_human.append(f"{p.field.question[:100]}: low confidence answer, review it")
            elif not p.done and p.source == "ai" and p.value is None:
                result.needs_human.append(f"{p.field.question[:100]}: no answer available")
        done_idx = {p.field.idx for p in plan if p.done}
        done_groups = {p.field.name for p in plan if p.done and p.field.is_choice and p.field.name}
        for f in fields:
            if f.required and f.idx not in done_idx and f.name not in done_groups:
                reason = next((p.error for p in plan if p.field.idx == f.idx and p.error), "")
                label = f.question[:100] or f.selector
                result.unfilled_required.append(f"{label} ({reason})" if reason and f.is_file else label)
        result.needs_human = list(dict.fromkeys(result.needs_human))
        result.unfilled_required = list(dict.fromkeys(result.unfilled_required))

    # ---------------------------------------------------------------- dialog navigation

    def _inline_error(self, page) -> str:
        loc = self._visible(page, "error", within=self._dialog(page))
        if loc is None:
            return ""
        try:
            return (loc.inner_text() or "validation error").strip()[:200] or "validation error"
        except Exception:  # noqa: BLE001
            return "validation error"

    def _close_dialog(self, page, result: FillResult | None = None) -> bool:
        """Close an open questionnaire so nothing half-answered is left behind. Never after a submit click."""
        note = "Cutshort questionnaire may be left open: close it by hand"
        try:
            dialog = self._dialog(page)
            if dialog is None:
                return True
            close = self._visible(page, "close", within=dialog) or self._visible(page, "close")
            if close is not None:
                close.click(timeout=5000)
            else:
                page.keyboard.press("Escape")
            self._pause(page, 300, 700)
            if self._dialog(page) is None:
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close cutshort questionnaire", error=str(exc)[:200])
        log.warning("cutshort questionnaire still open after close")
        if result is not None and note not in result.needs_human:
            result.needs_human.append(note)
        return False

    def _succeeded(self, page) -> bool:
        return self._visible(page, "success") is not None

    def _submit(self, page, result: FillResult) -> None:
        btn = self._visible(page, "submit", within=self._dialog(page)) or self._visible(page, "submit")
        if btn is None:
            result.error = "submit button not found"
            return
        btn.click(timeout=10000)
        self._clicked_submit = True
        self._pause(page, 800, 1600)
        err = self._inline_error(page)
        if err and self._dialog(page) is not None:
            result.error = f"Cutshort reported an error after submit: {err}"
            return
        if self._succeeded(page) or self._dialog(page) is None:
            result.submitted = True
            return
        result.error = "submit button clicked but the questionnaire stayed open"
        result.needs_human.append("submit was clicked but the dialog stayed open: verify on Cutshort")

    # ---------------------------------------------------------------- entry point

    def _wait_for_button(self, page, timeout_ms: int = 15000):
        try:
            page.locator(self.sel["apply_button"]).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        return self._visible(page, "apply_button")

    def _questionnaire(self, page, timeout_ms: int = 6000) -> tuple[list[FormField], dict[int, Any]]:
        """Fields of a questionnaire that opened after the Apply click, polled briefly (it renders client-side)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            fields, current = self._fields(page)
            if fields or self._succeeded(page) or time.monotonic() >= deadline:
                return fields, current
            self._pause(page, 250, 500)

    def run(self, page, job_url: str, submit: bool = False, screenshot_path: Path | None = None) -> FillResult:
        result = FillResult(url=job_url, profile_origin=PROFILE_ORIGIN)
        self._clicked_apply = False
        self._clicked_submit = False
        resp = page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
        status = getattr(resp, "status", None)
        if status in _RATE_LIMIT_STATUSES:
            result.paused_reason = f"cutshort answered {status}"
            return result
        self._pause(page, 800, 1800)

        blocked = self._blocked(page)
        if blocked:
            result.paused_reason = blocked
            self._screenshot(page, screenshot_path, result)
            return result
        if self._login_needed(page):
            result.paused_reason = "cutshort login needed"
            self._screenshot(page, screenshot_path, result)
            return result

        button = self._wait_for_button(page)
        if button is None:
            if self._visible(page, "already_applied") is not None:
                result.error = "already applied to this job on Cutshort"
            else:
                result.error = "Apply button not found (job closed or page changed)"
            self._screenshot(page, screenshot_path, result)
            return result
        if not submit:
            # The Apply click is the final action on one-click jobs: a dry run stops right here.
            self._screenshot(page, screenshot_path, result)
            log.info("cutshort dry run: stopped before the Apply click", url=job_url)
            return result

        button.click(timeout=10000)
        self._clicked_apply = True
        self._pause(page, 1000, 2000)
        try:
            blocked = self._blocked(page)
            if blocked:
                result.paused_reason = blocked
                self._screenshot(page, screenshot_path, result)
                return result
            if self._login_needed(page):
                result.paused_reason = "cutshort login needed"
                self._screenshot(page, screenshot_path, result)
                return result
            fields, current = self._questionnaire(page)
            if not fields:
                if self._succeeded(page) or self._visible(page, "apply_button") is None:
                    result.submitted = True
                else:
                    result.error = "Apply click did not go through (button still shown, no questionnaire)"
                self._screenshot(page, screenshot_path, result)
                return result

            previous_sig: tuple | None = None
            repeats = 0
            for step in range(self.max_steps):
                result.fields_total += len(fields)
                plan = self._plan_step(fields, current)
                self._fill_step(page, plan, fields, result)
                result.plan.extend(plan)
                log.debug("cutshort questionnaire step", step=step, fields=len(fields),
                          filled=sum(1 for p in plan if p.done))
                if result.needs_human:
                    result.paused_reason = "a question needs a human"
                elif result.unfilled_required:
                    result.paused_reason = "required fields left empty"
                if result.paused_reason:
                    self._screenshot(page, screenshot_path, result)
                    break
                if self._visible(page, "submit", within=self._dialog(page)) or self._visible(page, "submit"):
                    self._screenshot(page, screenshot_path, result)
                    self._submit(page, result)
                    break
                sig = tuple((f.selector, f.question) for f in fields)
                repeats = repeats + 1 if sig == previous_sig else 0
                if repeats >= 2:
                    result.paused_reason = "the questionnaire did not advance after clicking next"
                    self._screenshot(page, screenshot_path, result)
                    break
                previous_sig = sig
                nav = self._visible(page, "next", within=self._dialog(page)) or self._visible(page, "next")
                if nav is None:
                    result.error = "no next / submit button in the Cutshort questionnaire"
                    self._screenshot(page, screenshot_path, result)
                    break
                nav.click(timeout=10000)
                self._pause(page, 700, 1500)
                err = self._inline_error(page)
                if err:
                    result.unfilled_required.append(err)
                    result.unfilled_required = list(dict.fromkeys(result.unfilled_required))
                    result.paused_reason = "required fields left empty"
                    self._screenshot(page, screenshot_path, result)
                    break
                fields, current = self._fields(page)
            else:
                result.paused_reason = f"more than {self.max_steps} steps in the Cutshort questionnaire"
                self._screenshot(page, screenshot_path, result)
        finally:
            if self._clicked_submit and not result.submitted:
                log.warning("submit clicked but not confirmed; leaving the dialog alone", url=job_url)
            elif not result.submitted:
                self._close_dialog(page, result)
        return result


# ------------------------------------------------------------------ uniform interface


def _status_of(result: FillResult | None, error: str | None = None) -> tuple[str, str]:
    """``(status, reason)`` in the pipeline vocabulary: applied | dry_run | paused | failed."""
    if result is None:
        return AppStatus.failed.value, error or "filler failed"
    if result.error and not result.plan:
        return AppStatus.failed.value, result.error
    if result.submitted:
        return AppStatus.applied.value, result.summary()
    if result.paused_reason:
        return AppStatus.paused.value, "; ".join([result.paused_reason, *result.needs_human[:3],
                                                  *result.unfilled_required[:3]])[:500]
    if result.error:
        return AppStatus.failed.value, result.error
    return AppStatus.dry_run.value, "stopped before the Apply click (dry run or auto-submit off)"


def _outcome(result: FillResult | None, error: str | None = None, reason: str | None = None) -> dict[str, Any]:
    status, detail = _status_of(result, error)
    return {"status": status, "screenshot": result.screenshot_path if result else None,
            "reason": reason or detail, "result": result}


def _run_for(session_db: Session, user: User, app: Application, job: Job, page, llm=None,
             submit: bool = False) -> FillResult:
    from jobapply.forms.runner import vault_view_for

    vault = vault_view_for(user, app)
    runner = CutshortApplyRunner(session_db, user, vault, answers=AnswerSource(session_db, llm, user, job,
                                                                                 message=app.message or ""),
                                 llm=llm, resume_path=vault.resume_path, spec=load_profile())
    shot = get_settings().screenshots_dir / str(user.id) / f"{app.id}-{time.strftime('%Y%m%d%H%M%S')}.png"
    return runner.run(page, job.apply_url, submit=submit, screenshot_path=shot)


def apply_job(session, application: Application, *, dry_run: bool, auto_submit: bool, llm=None) -> dict[str, Any]:
    """Apply (or dry-run) one application. Returns ``{status, screenshot, reason, result}``.

    `session` is either a `CutshortSession` (open persistent context + DB session, as the per-user run
    builds it) or a plain DB `Session`, in which case the user's persistent profile is opened headless for
    this one job and closed again. Every gate holds here regardless of the caller: the global dry-run switch
    and the kill switch are read from the DB, `auto_submit` must be on, and a login / challenge pauses.
    Status vocabulary: applied | dry_run | paused | failed | skipped.
    """
    from jobapply.switches import global_dry_run, kill_switch_on

    db = session.db if isinstance(session, CutshortSession) else session
    if kill_switch_on(db):
        log.warning("kill switch on, not applying on cutshort", app=application.id)
        return {"status": AppStatus.skipped.value, "screenshot": None, "reason": "kill switch on", "result": None}
    dry_run = dry_run or global_dry_run(db)
    submit = (not dry_run) and bool(auto_submit)
    job = db.get(Job, application.job_id)
    user = session.user if isinstance(session, CutshortSession) else db.get(User, application.user_id)
    if job is None or user is None or not job.apply_url:
        return {"status": AppStatus.failed.value, "screenshot": None, "reason": "application has no job / apply url",
                "result": None}

    def _one(context) -> dict[str, Any]:
        page = context.new_page()
        try:
            try:
                result = _run_for(db, user, application, job, page, llm=llm, submit=submit)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                return _outcome(None, error=f"{type(exc).__name__}: {exc}")
            return _outcome(result)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    if isinstance(session, CutshortSession):
        return _one(session.context)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = _launch_persistent(pw, user.id, SITE)
        try:
            cs = _cutshort_session(context, user, db)
            status = cs.check(context.new_page())
            if status != "ok":
                return {"status": AppStatus.paused.value if status != "unknown" else AppStatus.skipped.value,
                        "screenshot": None, "reason": f"cutshort session {status}", "result": None}
            return _one(context)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ per-user run (mirrors linkedin.easy_apply)


def pick_cutshort_applications(session: Session, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications on Cutshort postings (ats_type / source `cutshort`)."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != SITE or not job.apply_url:
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


_CHALLENGE_REASON = re.compile(r"captcha|security|unusual activity|cutshort answered \d+", re.I)


def _notify(session: Session, user: User, kind: str, message: str) -> None:
    import jobapply.notify as notify_mod

    try:
        notify_mod.notify(session, user, kind, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", user=user.username, kind=kind, error=str(exc)[:200])


def _stop_reason(result) -> str:
    """'challenge' | 'login' | '' from a run result: the whole run stops on these, not just the job."""
    reason = getattr(result, "paused_reason", "") or ""
    if reason == "cutshort login needed":
        return "login"
    if _CHALLENGE_REASON.search(reason):
        return "challenge"
    return ""


def apply_cutshort_user(pw, session: Session, user: User, llm=None, limit: int = 3, dry_run: bool = True) -> list:
    """One user's Cutshort run inside their persistent (headless) profile. Returns ApplyOutcome rows.

    Honours the kill switch, the global dry-run switch, the per-user and per-site daily caps, the site's
    enabled / auto-submit flags and the challenge cooldown; a challenge or logout seen mid-run ends the run.
    """
    from jobapply.pipeline.apply import applied_today, record_outcome
    from jobapply.repo import UserScope
    from jobapply.switches import global_dry_run, kill_switch_on

    if kill_switch_on(session):
        log.warning("kill switch on, not applying on cutshort", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(session)
    scope = UserScope(session, user)
    site = scope.site(SITE)
    if site is None or not site.enabled:
        log.info("cutshort disabled for user", user=user.username)
        return []
    if _is_throttled(user, site):
        log.info("cutshort throttled (cooldown after a challenge), skipping", user=user.username)
        return []
    picks = pick_cutshort_applications(session, user, limit)
    if not picks:
        return []
    outcomes: list = []
    context = _launch_persistent(pw, user.id, SITE)
    try:
        cs = _cutshort_session(context, user, session)
        page = context.new_page()
        status = cs.check(page)
        if status == "unknown":
            log.warning("cutshort session state unclear, retry later", user=user.username)
            return []
        if status != "ok":
            if status == "challenge":
                _notify(session, user, "paused",
                        "Cutshort is showing a security check or OTP prompt: complete it in a browser, export the "
                        f"cookies again and re-import them for {user.username}")
            else:
                _notify(session, user, "paused",
                        f"Cutshort login needed ({status}): export cutshort.io cookies from a logged-in browser and "
                        f"import them for {user.username}")
            log.warning("cutshort session not ok", user=user.username, status=status)
            return []
        if site.login_status != "ok":
            site.login_status = "ok"
            session.add(site)
        cap = int((user.preferences or {}).get("daily_cap", 40))
        done_today = applied_today(session, user.id)
        random.shuffle(picks)
        for i, (app, job) in enumerate(picks):
            if not dry_run and (done_today >= cap or applied_today(session, user.id, SITE) >= site.daily_cap):
                log.info("daily cap reached", user=user.username, site=SITE)
                break
            if i:
                _between_applications(page)
            submit = not dry_run and site.auto_submit
            result = None
            try:
                result = _run_for(session, user, app, job, page, llm=llm, submit=submit)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                outcome = record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
            else:
                outcome = record_outcome(session, user, app, job, result)
            session.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info("cutshort apply outcome", user=user.username, job=job.id, status=outcome.status.value,
                     detail=outcome.detail)
            stop = _stop_reason(result)
            if stop == "challenge":
                cs.record_challenge()
                session.commit()
                _notify(session, user, "paused",
                        f"Cutshort blocked or challenged the apply run ({result.paused_reason}); paused for "
                        f"{DEFAULT_COOLDOWN_HOURS} h.")
                log.warning("cutshort challenge mid-run, stopping", user=user.username, reason=result.paused_reason)
                break
            if stop == "login":
                site.login_status = "expired"
                session.add(site)
                session.commit()
                _notify(session, user, "paused",
                        f"Cutshort session expired mid-run: re-import cutshort.io cookies for {user.username}")
                log.warning("cutshort session expired mid-run, stopping", user=user.username)
                break
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
    return outcomes


def apply_cutshort(username: str | None = None, limit: int = 3, dry_run: bool = True, engine=None) -> list[dict]:
    """Entry point for a CLI / scheduler task: the Cutshort run for every active user (or one), within the
    active hours (`JOBAPPLY_CUTSHORT_ACTIVE_HOURS`, falling back to the LinkedIn window), caps and switches."""
    from playwright.sync_api import sync_playwright

    from jobapply.ai.base import LLMError
    from jobapply.ai.service import get_llm
    from jobapply.db import session_scope
    from jobapply.sites.browser import is_active_hours
    from jobapply.switches import global_dry_run, kill_switch_on

    settings = get_settings()
    hours = getattr(settings, "cutshort_active_hours", None) or settings.linkedin_active_hours
    if not is_active_hours(spec=hours):
        log.info("outside cutshort active hours, nothing done", active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as session:
        if kill_switch_on(session):
            log.warning("kill switch on, not applying on cutshort")
            return []
        dry_run = dry_run or global_dry_run(session)
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        users = session.exec(stmt).all()
        with sync_playwright() as pw:
            for user in users:
                try:
                    llm = get_llm(session, user)
                except LLMError as exc:
                    log.warning("no llm, custom questions will be left for review", user=user.username,
                                error=str(exc))
                    llm = None
                try:
                    outcomes = apply_cutshort_user(pw, session, user, llm, limit=limit, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop the others
                    log.exception("cutshort run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


__all__ = [
    "DEFAULT_SELECTORS", "CutshortApplyRunner", "apply_cutshort", "apply_cutshort_user", "apply_job",
    "load_profile", "pick_cutshort_applications", "selectors_from",
]
