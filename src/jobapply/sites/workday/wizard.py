"""The Workday application wizard: My Information -> My Experience -> Application Questions ->
Voluntary Disclosures -> (Self Identify) -> Review.

Every step but Review is filled by the existing ``forms.runner.FormRunner`` (field extraction, builtin
label profile, answer bank / heuristics / LLM for the rest, custom-dropdown handling), with the step's own
selector map from ``config/form_profiles/workday.yaml`` (``workday_steps``) installed as the ``workday``
profile for that pass so the match rate is judged per step. Then *Save and Continue* / *Next*
(``bottom-navigation-next-button``) is clicked: that is never a submit, so it is clicked in dry run too. The
only final control is *Submit* on the Review step, clicked only when the caller passes ``submit=True``
(``apply_job`` does so only when the global dry run is off AND the site's ``auto_submit`` is on).

The wizard step markup could not be observed live (it sits behind sign-in), so its selectors are the standard
Workday ids verified on the inline test fixtures only.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobapply.config import get_settings
from jobapply.forms.fields import detect_captcha
from jobapply.forms.profiles import FormProfileSpec, VaultView
from jobapply.forms.runner import FillResult, FormRunner
from jobapply.logs import get_logger
from jobapply.sites.workday.account import (
    SELECTORS,
    STATE_CHALLENGE,
    STATE_SUBMITTED,
    STATE_WIZARD,
    active_step,
    classify_page,
    page_error,
)
from jobapply.sites.workday.tenant import SITE

log = get_logger(__name__)

STEP_MY_INFORMATION = "My Information"
STEP_MY_EXPERIENCE = "My Experience"
STEP_QUESTIONS = "Application Questions"
STEP_DISCLOSURES = "Voluntary Disclosures"
STEP_SELF_IDENTIFY = "Self Identify"
STEP_REVIEW = "Review"
STEPS = (STEP_MY_INFORMATION, STEP_MY_EXPERIENCE, STEP_QUESTIONS, STEP_DISCLOSURES, STEP_SELF_IDENTIFY, STEP_REVIEW)
# Progress bar labels vary a little per tenant ("Voluntary Disclosures" / "Voluntary Self-Identification").
_STEP_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"my information|personal information|contact", re.I), STEP_MY_INFORMATION),
    (re.compile(r"my experience|experience", re.I), STEP_MY_EXPERIENCE),
    (re.compile(r"application questions|questions|questionnaire", re.I), STEP_QUESTIONS),
    (re.compile(r"voluntary|disclosure|eeo", re.I), STEP_DISCLOSURES),
    (re.compile(r"self[- ]identif", re.I), STEP_SELF_IDENTIFY),
    (re.compile(r"review", re.I), STEP_REVIEW),
]
MAX_STEPS = 8
SUBMIT_TEXT = re.compile(r"\bsubmit\b", re.I)
WIZARD_SELECTORS: dict[str, str] = {
    **SELECTORS,
    "resume_input": 'input[data-automation-id="file-upload-input-ref"], input[type="file"]',
    "add_work": ('[data-automation-id="workExperienceSection"] button[data-automation-id="Add"], '
                 'button[aria-label*="Add Work Experience"], button[data-automation-id="Add Work Experience"]'),
    "add_education": ('[data-automation-id="educationSection"] button[data-automation-id="Add"], '
                      'button[aria-label*="Add Education"], button[data-automation-id="Add Education"]'),
}


def canonical_step(label: str) -> str:
    for rx, name in _STEP_ALIASES:
        if rx.search(label or ""):
            return name
    return (label or "").strip()


# ------------------------------------------------------------------ the per-step profile


def profile_path() -> Path:
    p = get_settings().profiles_dir / "workday.yaml"
    if p.exists():
        return p
    return Path(__file__).resolve().parents[4] / "config" / "form_profiles" / "workday.yaml"


def load_step_profiles(path: Path | None = None) -> tuple[FormProfileSpec, dict[str, dict[str, str]]]:
    """The base ``workday`` profile plus ``{step: {selector: key}}`` from ``workday_steps``."""
    p = path or profile_path()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    spec = FormProfileSpec.model_validate({"origin": "builtin", **{k: v for k, v in data.items()
                                                                    if k != "workday_steps"}})
    steps: dict[str, dict[str, str]] = {}
    for name, mapping in (data.get("workday_steps") or {}).items():
        steps[canonical_step(str(name))] = {str(k): str(v) for k, v in (mapping or {}).items()}
    return spec, steps


def step_spec(base: FormProfileSpec, fields: dict[str, str]) -> FormProfileSpec:
    return FormProfileSpec(ats=base.ats, detect=base.detect, steps=[], fields=dict(fields), submit=base.submit,
                           success=list(base.success), never=list(base.never), origin="builtin")


# ------------------------------------------------------------------ the vault view


def _first(items: Any) -> dict[str, Any]:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _month_year(value: Any) -> tuple[str, str]:
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?", str(value or ""))
    if not m:
        return "", ""
    return (m.group(2) or "").zfill(2) if m.group(2) else "", m.group(1)


def workday_vault_view(user: Any, app: Any = None) -> VaultView:
    """``vault_view_for`` plus the ``derived.*`` extras the Workday profile asks for (address pieces, work
    history and education from the master JSON resume, skills as text, today's date)."""
    from jobapply.forms.runner import vault_view_for
    from jobapply.models import utcnow

    view = vault_view_for(user, app)
    p = view.profile
    resume = dict(p.get("resume_json") or {})
    basics = resume.get("basics") or {}
    loc = basics.get("location") if isinstance(basics.get("location"), dict) else {}
    location = str(p.get("location") or "")
    parts = [s.strip() for s in location.split(",") if s.strip()]
    work = _first(resume.get("work"))
    edu = _first(resume.get("education"))
    skills = p.get("skills")
    if isinstance(skills, dict):
        skills_text = ", ".join(str(k) for k in skills)
    elif isinstance(skills, list):
        skills_text = ", ".join(str(k) for k in skills)
    else:
        skills_text = str(skills or "")
    start_m, start_y = _month_year(work.get("startDate"))
    end_m, end_y = _month_year(work.get("endDate"))
    view.extra.update({
        "address_line_1": str(p.get("address") or loc.get("address") or ""),
        "city": str(p.get("city") or loc.get("city") or (parts[0] if parts else "")),
        "postal_code": str(p.get("postal_code") or loc.get("postalCode") or ""),
        "country": str(p.get("country") or (parts[-1] if len(parts) > 1 else "")),
        "phone_country": str(p.get("phone_country") or (parts[-1] if len(parts) > 1 else "")),
        "source": str(p.get("how_heard") or "Job Board"),
        "role_description": "\n".join(str(h) for h in (work.get("highlights") or [])) or str(work.get("summary") or ""),
        "work_start_month": start_m, "work_start_year": start_y, "work_end_month": end_m, "work_end_year": end_y,
        "currently_work_here": not work.get("endDate"),
        "school": str(edu.get("institution") or p.get("school") or ""),
        "degree": str(edu.get("studyType") or p.get("education") or ""),
        "field_of_study": str(edu.get("area") or ""),
        "graduation_year": str(edu.get("endDate") or "")[:4],
        "skills_text": skills_text,
        "today": utcnow().strftime("%m/%d/%Y"),
        "full_name": view.full_name,
    })
    return view


# ------------------------------------------------------------------ results


@dataclass
class StepResult:
    step: str
    profile_origin: str = "none"
    filled: int = 0
    fields_total: int = 0
    unfilled_required: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    recoveries: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    screenshot: str | None = None
    advanced: bool = False  # Save and Continue clicked and the progress bar moved on


@dataclass
class WizardResult:
    steps: list[StepResult] = field(default_factory=list)
    final_step: str = ""
    submitted: bool = False
    reached_review: bool = False
    submit_clicked: bool = False
    paused_reason: str = ""
    error: str = ""
    screenshot: str | None = None
    login_status: str | None = None

    @property
    def answers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in self.steps:
            out.update(s.answers)
        return out

    @property
    def needs_human(self) -> list[str]:
        return list(dict.fromkeys(h for s in self.steps for h in s.needs_human))

    @property
    def unfilled_required(self) -> list[str]:
        return list(dict.fromkeys(u for s in self.steps for u in s.unfilled_required))

    @property
    def recoveries(self) -> list[str]:
        return [r for s in self.steps for r in s.recoveries]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(answers=self.answers, needs_human=self.needs_human, unfilled_required=self.unfilled_required,
                 recoveries=self.recoveries)
        return d


# ------------------------------------------------------------------ the wizard


PauseFn = Any


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


class WorkdayWizard:
    """``run(page, submit=False, screenshot_path=...)`` -> ``WizardResult``; ``runner`` is a configured
    ``FormRunner`` (vault view from ``workday_vault_view``). ``submit`` lets Review's *Submit* be clicked."""

    def __init__(self, runner: FormRunner, *, profile: Path | None = None, selectors: dict[str, str] | None = None,
                 pause: PauseFn | None = None, max_steps: int = MAX_STEPS, settle_ms: int = 2_000):
        self.runner = runner
        self.base, self.step_fields = load_step_profiles(profile)
        self.sel = {**WIZARD_SELECTORS, **(selectors or {})}
        self._pause = pause or _human_pause
        self.max_steps = max_steps
        self.settle_ms = settle_ms

    # -- helpers

    def _visible(self, key: str) -> Any:
        from jobapply.sites.workday.account import _visible

        return _visible(self.page, self.sel[key])

    def _shot(self, path: Path | None, suffix: str = "") -> str | None:
        if not path:
            return None
        target = Path(path)
        if suffix:
            target = target.with_name(f"{target.stem}-{suffix}{target.suffix}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target), full_page=True)
            return str(target)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])
            return None

    def _next_button(self) -> tuple[Any, str]:
        loc = self._visible("next_button")
        if loc is None:
            return None, ""
        try:
            text = (loc.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        return loc, text

    def _install_step_profile(self, step: str) -> None:
        fields = self.step_fields.get(step)
        spec = step_spec(self.base, fields if fields is not None else self.base.fields)
        self.runner.profiles = {**self.runner.profiles, SITE: spec}

    def _expand_sections(self, step: str) -> None:
        """My Experience: open one Work Experience and one Education block when the section only shows an
        Add button (the inputs exist only after the click). Best effort; a missing button is fine."""
        if step != STEP_MY_EXPERIENCE:
            return
        for key, probe in (("add_work", "Job Title"), ("add_education", "School")):
            try:
                if self.page.locator(f'label:has-text("{probe}")').count():
                    continue  # the block is already open
                btn = self._visible(key)
                if btn is not None:
                    btn.click(timeout=5_000)
                    self._pause(self.page, 400, 900)
            except Exception as exc:  # noqa: BLE001
                log.debug("section not expanded", key=key, error=str(exc)[:120])

    def _fill_step(self, step: str, shot: Path | None) -> tuple[StepResult, FillResult | None]:
        self._install_step_profile(step)
        self._expand_sections(step)
        sr = StepResult(step=step)
        try:
            fill = self.runner.run(self.page, url=None, html=None, ats_hint=SITE, submit=False,
                                   screenshot_path=None)
        except Exception as exc:  # noqa: BLE001 - one broken step must not crash the run
            sr.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            sr.screenshot = self._shot(shot, _slug(step))
            return sr, None
        sr.profile_origin = fill.profile_origin
        sr.filled, sr.fields_total = fill.filled, fill.fields_total
        sr.unfilled_required = list(fill.unfilled_required)
        sr.needs_human = list(fill.needs_human)
        sr.recoveries = list(fill.recoveries)
        sr.answers = dict(fill.answers)
        sr.error = "" if fill.error == "no form fields found on the page" else fill.error
        sr.screenshot = self._shot(shot, _slug(step))
        return sr, fill

    def _click_next(self, loc: Any) -> None:
        """Save and Continue / Next: not a submit, but the dry-run submit guard would swallow a React form's
        submit event, so it is lifted for this one click and restored at once."""
        try:
            self.page.evaluate("() => { window.__jobapplyAllowSubmit = true; }")
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.click(timeout=10_000)
        finally:
            try:
                self.page.evaluate("() => { window.__jobapplyAllowSubmit = false; }")
            except Exception:  # noqa: BLE001
                pass

    # -- entry point

    def run(self, page: Any, *, submit: bool = False, screenshot_path: Path | None = None) -> WizardResult:
        self.page = page
        result = WizardResult()
        seen: list[str] = []
        for _ in range(self.max_steps):
            state = classify_page(page, self.sel)
            if state == STATE_CHALLENGE:
                result.paused_reason, result.login_status = "captcha / security check in the wizard", "challenge"
                result.screenshot = self._shot(screenshot_path)
                return result
            if state == STATE_SUBMITTED:
                result.submitted = True
                result.final_step = STEP_REVIEW
                result.screenshot = self._shot(screenshot_path)
                return result
            if state != STATE_WIZARD:
                result.error = f"wizard not on the page (state {state})"
                result.screenshot = self._shot(screenshot_path)
                return result
            label = active_step(page, self.sel)
            step = canonical_step(label)
            result.final_step = step
            if step == STEP_REVIEW:
                result.reached_review = True
                result.screenshot = self._shot(screenshot_path)
                if result.needs_human:
                    result.paused_reason = "a question needs a human"
                    return result
                if not submit:
                    return result
                return self._submit(result, screenshot_path)
            if seen.count(step) >= 2:
                result.paused_reason = f"stuck on the {step} step: {page_error(page, self.sel) or 'no progress'}"
                result.screenshot = self._shot(screenshot_path)
                return result
            seen.append(step)
            sr, fill = self._fill_step(step, screenshot_path)
            result.steps.append(sr)
            if fill is not None and fill.paused_reason.startswith("captcha"):
                result.paused_reason, result.login_status = fill.paused_reason, "challenge"
                result.screenshot = sr.screenshot
                return result
            if sr.unfilled_required:
                result.paused_reason = f"{step}: required fields left empty"
                result.screenshot = sr.screenshot
                return result
            if sr.error:
                result.error = f"{step}: {sr.error}"
                result.screenshot = sr.screenshot
                return result
            loc, text = self._next_button()
            if loc is None:
                result.paused_reason = f"{step}: no Save and Continue button"
                result.screenshot = sr.screenshot
                return result
            if SUBMIT_TEXT.search(text):
                # A tenant whose last step doubles as the review: treat it like Review.
                result.reached_review = True
                result.screenshot = sr.screenshot
                if result.needs_human:
                    result.paused_reason = "a question needs a human"
                    return result
                if not submit:
                    return result
                return self._submit(result, screenshot_path)
            self._click_next(loc)
            self._pause(page, self.settle_ms // 2, self.settle_ms)
            after = canonical_step(active_step(page, self.sel))
            if after == step and classify_page(page, self.sel) == STATE_WIZARD:
                err = page_error(page, self.sel)
                if err:
                    result.paused_reason = f"{step}: {err[:160]}"
                    result.screenshot = self._shot(screenshot_path, _slug(step) + "-error")
                    return result
                self._pause(page, self.settle_ms, self.settle_ms * 2)
                after = canonical_step(active_step(page, self.sel))
            sr.advanced = after != step
        result.paused_reason = f"more than {self.max_steps} wizard steps"
        result.screenshot = self._shot(screenshot_path)
        return result

    def _submit(self, result: WizardResult, screenshot_path: Path | None) -> WizardResult:
        """The one final click. Only reached with ``submit=True`` on the Review step."""
        loc, text = self._next_button()
        if loc is None or not SUBMIT_TEXT.search(text):
            result.paused_reason = "Review reached but no Submit button found"
            return result
        try:
            self.page.evaluate("() => { window.__jobapplyAllowSubmit = true; }")
        except Exception:  # noqa: BLE001
            pass
        loc.click(timeout=15_000)
        result.submit_clicked = True
        self._pause(self.page, self.settle_ms, self.settle_ms * 2)
        state = classify_page(self.page, self.sel)
        if state == STATE_SUBMITTED:
            result.submitted = True
        elif state == STATE_CHALLENGE:
            result.paused_reason, result.login_status = "captcha / security check after Submit", "challenge"
        else:
            try:
                detect_captcha(self.page)
            except Exception:  # noqa: BLE001
                pass
            err = page_error(self.page, self.sel)
            result.paused_reason = ("Submit clicked but no confirmation seen: verify on the Candidate Home"
                                    + (f" ({err[:120]})" if err else ""))
        result.screenshot = self._shot(screenshot_path, "submitted" if result.submitted else "after-submit")
        return result


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "step"


__all__ = ["STEPS", "STEP_MY_INFORMATION", "STEP_MY_EXPERIENCE", "STEP_QUESTIONS", "STEP_DISCLOSURES",
           "STEP_SELF_IDENTIFY", "STEP_REVIEW", "WIZARD_SELECTORS", "canonical_step", "load_step_profiles",
           "step_spec", "workday_vault_view", "StepResult", "WizardResult", "WorkdayWizard", "profile_path"]
