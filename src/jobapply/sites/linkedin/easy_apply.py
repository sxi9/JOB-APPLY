"""LinkedIn Easy Apply filler.

Runs inside the user's persistent, logged-in LinkedIn browser profile (assisted mode, docs/PLAN.md). It opens
the job page, clicks Easy Apply, walks the modal step by step, fills every control from the profile vault
(matched by label text), answers the rest through the answer bank / LLM, uploads the resume, and stops on
the review step. In dry run it screenshots the review step and discards the draft so LinkedIn keeps nothing;
with `submit=True` (only the apply pipeline passes it, and only when the site has auto-submit on and the
global dry-run switch is off) it clicks Submit.

Selectors come from `config/sites/linkedin.yaml` so they can be fixed without a code change. The result is
the same `FillResult` the ATS `FormRunner` returns, so the Queue page and the apply pipeline do not care which
filler produced it.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.forms.fields import FormField, detect_captcha, extract_fields
from jobapply.forms.profiles import VaultView, resolve_key
from jobapply.forms.runner import AnswerSource, FillResult, FormRunner, PlannedFill
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, JobSource, User

log = get_logger(__name__)

SITE = "linkedin"
PROFILE_ORIGIN = "builtin:linkedin"
DEFAULT_COOLDOWN_HOURS = 6
MAX_STEPS = 12
MIN_ANSWER_CONFIDENCE = 60

# Fallbacks when config/sites/linkedin.yaml is missing an entry. The YAML wins.
DEFAULT_SELECTORS: dict[str, str] = {
    "easy_apply_button": 'button.jobs-apply-button[aria-label*="Easy Apply"], button[aria-label*="Easy Apply"]',
    "modal": "div.jobs-easy-apply-modal, div.jobs-easy-apply-modal[data-test-modal]",
    "next": 'button[aria-label="Continue to next step"], footer button[data-easy-apply-next-button]',
    "review": 'button[aria-label="Review your application"]',
    "submit": 'button[aria-label="Submit application"]',
    "dismiss": 'button[aria-label="Dismiss"]',
    "discard": 'button[data-control-name="discard_application_confirm_btn"], button[data-test-dialog-primary-btn]',
    "resume_upload": "input[type=file]",
    "uploaded_resume_card": ".jobs-document-upload-redesign-card__container",
    "error": ".artdeco-inline-feedback--error",
    "follow_company_checkbox": "input#follow-company-checkbox",
    "progress": "progress, [role=progressbar]",
    "security_check": 'iframe[src*="captcha"], #captcha-internal, form#captcha-challenge',
    "post_submit_done": 'button[aria-label="Dismiss"]',
    "already_applied": ".artdeco-inline-feedback--success",
}

# Label text -> vault key. First match wins, so the more specific patterns come first.
LABEL_KEYS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), key)
    for p, key in [
        (r"e-?mail", "profile.email"),
        (r"country code|phone code|dial(ing)? code", "derived.phone_country_code"),
        (r"phone|mobile", "profile.phone"),
        (r"first name|given name", "profile.first_name"),
        (r"last name|family name|surname", "profile.last_name"),
        (r"^(your )?(full )?name\b", "derived.full_name"),
        (r"\bcity\b|\blocation\b|where (are you|do you) (based|live|located)", "profile.location"),
        (r"linkedin", "profile.linkedin"),
        (r"github", "profile.github"),
        (r"website|portfolio|personal site", "profile.portfolio"),
        (r"notice period|earliest (start|join)|when can you (start|join)|how soon|"
         r"(expected|tentative|possible) (date of )?join|joining date|date of joining", "profile.notice_period"),
        (r"current (ctc|salary|compensation|pay)", "profile.current_ctc"),
        (r"(expected|desired).*(ctc|salary|compensation|pay)|salary expectation|\bctc\b|compensation",
         "profile.expected_ctc"),
        (r"sponsor|visa", "derived.needs_sponsorship"),
        (r"relocat", "derived.willing_to_relocate"),
        (r"authori[sz]ed|authori[sz]ation|right to work|legally (able|allowed|permitted)", "derived.work_authorized"),
        (r"current (company|employer|organi[sz]ation)", "profile.current_company"),
        (r"current (title|role|designation|position)", "profile.current_title"),
        (r"(highest|level of) education|degree|qualification", "profile.education"),
        (r"total.*years|years of (total |overall |professional |work )*experience\s*\??$", "profile.years_experience"),
    ]
]
_YEARS = re.compile(r"\b(years?|yrs)\b", re.I)
_SECURITY_TEXT = re.compile(
    r"security check|unusual activity|verify (it's|it is|its) you|quick security|checkpoint/challenge|"
    r"let's make sure (it's|it is) you|are you a robot",
    re.I,
)
_YES = {"yes", "y", "true", "1"}
_PLACEHOLDERS = {"", "select", "select an option", "choose", "please select", "-", "--"}
_EASY_APPLY_TEXT = re.compile(r"easy apply", re.I)
_RATE_LIMIT_STATUSES = (429, 999, 403)
MODAL_TAG = "data-jobapply-modal"

PauseFn = Callable[[Any, int, int], None]


# ------------------------------------------------------------------ selectors


def selectors_path() -> Path:
    return get_settings().profiles_dir.parent / "sites" / "linkedin.yaml"


def load_selectors(path: Path | None = None) -> dict[str, str]:
    """DEFAULT_SELECTORS overlaid with `config/sites/linkedin.yaml` (utf-8)."""
    out = dict(DEFAULT_SELECTORS)
    p = path or selectors_path()
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k, v in (data.get("selectors") or {}).items():
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    return out


# ------------------------------------------------------------------ lazy hooks (module B's code)


def _human_pause(page, lo_ms: int, hi_ms: int) -> None:
    try:
        from jobapply.sites.browser import human_pause
    except ImportError:  # module B not merged yet
        page.wait_for_timeout(random.randint(lo_ms, hi_ms))
        return
    human_pause(page, lo_ms, hi_ms)


def _launch_persistent(pw, user_id: int, site: str, headless: bool | None = None):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, site, headless=headless)


def _linkedin_session(context, user: User, session: Session):
    from jobapply.sites.linkedin.session import LinkedInSession

    return LinkedInSession(context, user, session)


def _is_throttled(user: User, site) -> bool:
    """Cooldown after a challenge (sites.linkedin.session.throttled). Without that module: never throttled."""
    try:
        from jobapply.sites.linkedin.session import throttled
    except ImportError:
        return False
    return bool(throttled(user, site))


def _between_applications(page) -> None:
    _human_pause(page, 8000, 20000)


# ------------------------------------------------------------------ helpers


def phone_country_code(phone: str | None) -> str | None:
    m = re.match(r"\s*(\+\d{1,3})", phone or "")
    return m.group(1) if m else None


def local_phone(phone: str) -> str:
    """`+91 98765 43210` -> `9876543210`: the number without its dial code, spaces or dashes."""
    rest = re.sub(r"^\s*\+\d{1,3}[\s-]*", "", phone or "")
    return re.sub(r"[\s\-().]", "", rest)


def _dial_code_option(options: list[str], code: Any) -> str | None:
    """`+1` -> `United States (+1)`: literal match on the bracketed code (fuzzy matching ignores 2-char values)."""
    code = str(code or "").strip()
    if not code:
        return None
    needle = f"({code})"
    for opt in options:
        if needle in opt:
            return opt
    return None


def _classify(page) -> str:
    """sites.linkedin.session.classify_page, or 'unknown' when it cannot be evaluated."""
    try:
        from jobapply.sites.linkedin.session import classify_page

        return classify_page(page)
    except Exception as exc:  # noqa: BLE001
        log.debug("classify failed", error=str(exc)[:120])
        return "unknown"


def skill_years(question: str, skills: dict[str, Any] | None) -> str | None:
    """`How many years of experience do you have with Python?` -> the years from profile.skills."""
    if not skills or not _YEARS.search(question):
        return None
    best: tuple[int, Any] | None = None
    for name, years in skills.items():
        if not name or years in (None, ""):
            continue
        if re.search(rf"(?<![\w+#]){re.escape(str(name))}(?![\w+#])", question, re.I):
            if best is None or len(str(name)) > best[0]:
                best = (len(str(name)), years)
    if best is None:
        return None
    years = best[1]
    try:
        return f"{float(years):g}"
    except (TypeError, ValueError):
        return str(years)


def key_for_label(question: str) -> str | None:
    q = question.strip()
    for rx, key in LABEL_KEYS:
        if rx.search(q):
            return key
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


# ------------------------------------------------------------------ the runner


class EasyApplyRunner:
    def __init__(
        self,
        session_db: Session,
        user: User,
        vault: VaultView,
        answers: AnswerSource | None = None,
        llm=None,
        resume_path: str | None = None,
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
        self.sel = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.min_answer_confidence = min_answer_confidence
        self.max_steps = max_steps
        self._pause: PauseFn = pause or _human_pause
        profile = vault.profile or {}
        vault.extra.setdefault("phone_country_code", phone_country_code(profile.get("phone")))
        auth = str(profile.get("work_authorization") or "").strip()
        if auth:
            vault.extra.setdefault("work_authorized", not re.search(r"\b(no|not|need|require)\b", auth, re.I))

    # ---------------------------------------------------------------- page helpers

    def _visible(self, page, key: str, within: str | None = None):
        """First visible element for selector `key` (optionally inside the modal), else None."""
        selector = self.sel.get(key, "")
        if not selector:
            return None
        base = page.locator(within) if within else page
        try:
            loc = base.locator(selector)
            n = loc.count()
            for i in range(n):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception as exc:  # detached nodes mid-transition
            log.debug("locator failed", key=key, error=str(exc)[:120])
        return None

    def _modal(self, page):
        """The visible Easy Apply modal (not the first hidden artdeco container in the DOM), tagged with
        `data-jobapply-modal` so the in-page lookups scope to the same element."""
        loc = page.locator(self.sel["modal"])
        try:
            n = loc.count()
            for i in range(n):
                el = loc.nth(i)
                if el.is_visible():
                    el.evaluate(f"(e) => e.setAttribute('{MODAL_TAG}', '1')")
                    return el
        except Exception as exc:  # detached nodes mid-transition
            log.debug("modal lookup failed", error=str(exc)[:120])
        return loc.first

    @property
    def _modal_scope(self) -> str:
        return f"[{MODAL_TAG}], {self.sel['modal']}"

    def _modal_open(self, page) -> bool:
        try:
            return self._modal(page).is_visible()
        except Exception:
            return False

    def _looks_like_job_page(self, page) -> bool:
        """A rendered job page (apply button or open modal): its body text is content, not a challenge."""
        return self._visible(page, "easy_apply_button") is not None or self._modal_open(page)

    def _blocked(self, page) -> str:
        """Captcha / security-check / unusual-activity detection. Returns the reason or ''."""
        try:
            cap = detect_captcha(page)
        except Exception:
            cap = ""
        if cap:
            return f"captcha on page ({cap})"
        if self._visible(page, "security_check") is not None:
            return "security check page"
        try:
            url, title = page.url or "", page.title() or ""
        except Exception:
            url, title = "", ""
        if re.search(r"checkpoint|challenge|security-verification", url, re.I) or _SECURITY_TEXT.search(title):
            return "security check page"
        if self._looks_like_job_page(page):
            return ""  # job descriptions may mention "security check"; only an un-rendered page is suspect
        try:
            text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
        except Exception:
            text = ""
        if _SECURITY_TEXT.search(text or ""):
            return "unusual activity / security check page"
        return ""

    def _modal_fields(self, page) -> tuple[list[FormField], dict[int, Any]]:
        """Visible fields inside the modal plus their current values (LinkedIn prefills contact info)."""
        fields = [f for f in extract_fields(page) if not f.hidden]
        if not fields:
            return [], {}
        state = page.evaluate(
            """(args) => {
              const m = document.querySelector(args.tag) || document.querySelector(args.modal);
              return args.sels.map((s) => {
                let el = null;
                try { el = m ? m.querySelector(s) : null; } catch (e) { el = null; }
                if (!el || el.getClientRects().length === 0) return null;  // not inside, or in a hidden step
                if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return o ? o.text.trim() : ''; }
                if (el.type === 'checkbox' || el.type === 'radio') return !!el.checked;
                return el.value || '';
              });
            }""",
            {"modal": self.sel["modal"], "tag": f"[{MODAL_TAG}]", "sels": [f.selector for f in fields]},
        )
        inside = [f for f, st in zip(fields, state, strict=True) if st is not None]
        values = {f.idx: st for f, st in zip(fields, state, strict=True) if st is not None}
        return inside, values

    def _is(self, page, f: FormField, key: str) -> bool:
        """Does this extracted field match a configured selector (checked inside the modal)?"""
        selector = self.sel.get(key, "")
        if not selector:
            return False
        try:
            return bool(
                page.evaluate(
                    "(a) => { const m = document.querySelector(a.tag) || document.querySelector(a.modal);"
                    " const el = m && m.querySelector(a.f); return !!(el && el.matches(a.s)); }",
                    {"modal": self.sel["modal"], "tag": f"[{MODAL_TAG}]", "f": f.selector, "s": selector},
                )
            )
        except Exception:
            return False

    # ---------------------------------------------------------------- planning

    def _value_for(self, question: str, options: list[str] | None, is_text: bool) -> tuple[str, Any, str, int]:
        """(key, value, source, confidence) for a question. Vault first, then skills, then the answer source."""
        profile = self.vault.profile or {}
        years = skill_years(question, profile.get("skills"))
        if years is not None:
            return "profile.skills", years, "builtin", 100
        key = key_for_label(question)
        if key:
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

    def _plan_step(self, page, fields: list[FormField], current: dict[int, Any]) -> list[PlannedFill]:
        plan: list[PlannedFill] = []
        seen_groups: set[str] = set()
        has_country_code = any(key_for_label(f.question) == "derived.phone_country_code" for f in fields)
        for f in fields:
            if f.is_file:
                if self._visible(page, "uploaded_resume_card", within=self._modal_scope) is not None:
                    continue  # LinkedIn already holds a resume for this application
                if not self.resume_path:
                    continue
                plan.append(PlannedFill(f, "derived.resume_path", self.resume_path, "file", "builtin"))
                continue
            if f.type == "checkbox" and self._is(page, f, "follow_company_checkbox"):
                plan.append(PlannedFill(f, "const:unfollow", False, "bool", "builtin"))
                continue
            if f.type in ("search", "password", "submit", "button"):
                continue
            if f.is_choice:
                if f.name and f.name in seen_groups:
                    continue
                if f.name:
                    seen_groups.add(f.name)
                group = [g for g in fields if g.is_choice and g.name and g.name == f.name] or [f]
                question = f.question
                options = [g.label or g.value for g in group]
                if len(group) == 1 and f.type == "checkbox":
                    if not f.required:
                        continue  # consent / newsletter boxes are left alone unless the step needs them
                    plan.append(PlannedFill(f, "const:Yes", True, "bool", "builtin"))
                    continue
                if any(current.get(g.idx) is True for g in group) and not self._is_decidable(question):
                    continue  # LinkedIn remembered an earlier answer we have nothing better for
                key, value, source, conf = self._value_for(question, options, is_text=False)
                plan.append(PlannedFill(f, key, value, "choice", source, conf))
                continue
            question = f.question
            options = f.options if f.type == "select" else None
            key, value, source, conf = self._value_for(question, options, is_text=f.type != "select")
            if key == "profile.phone" and has_country_code and value:
                value = local_phone(str(value))  # the country code goes in its own select
            existing = current.get(f.idx)
            if f.type == "select" and str(existing or "").strip().lower() in _PLACEHOLDERS:
                existing = None  # "Select an option" is not an answer
            if value in (None, "") and existing not in (None, "", False):
                plan.append(PlannedFill(f, key or "prefilled", existing, "keep", "builtin", 100))
                continue
            kind = "select" if f.type == "select" else "text"
            plan.append(PlannedFill(f, key, value, kind, source, conf))
        return plan

    def _is_decidable(self, question: str) -> bool:
        return skill_years(question, (self.vault.profile or {}).get("skills")) is not None or bool(
            key_for_label(question)
        )

    # ---------------------------------------------------------------- execution

    def _fill_one(self, page, p: PlannedFill, fields: list[FormField], current: dict[int, Any]) -> None:
        f = p.field
        modal = self._modal(page)
        locator = modal.locator(f.selector).first
        if p.kind == "keep":
            return
        if p.kind == "file":
            if not p.value or not Path(str(p.value)).exists():
                raise ValueError("resume file missing")
            locator.set_input_files(str(p.value))
            return
        if p.kind == "select":
            opt = _dial_code_option(f.options, p.value) if p.key == "derived.phone_country_code" else None
            opt = opt or FormRunner._pick_option(f.options, p.value)
            if opt is None:
                existing = current.get(f.idx)
                if existing and str(existing).strip().lower() not in _PLACEHOLDERS:
                    p.value = existing
                    return
                raise ValueError(f"no option matches the {p.key} value")  # value kept out of logs
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
            modal.locator(target.selector).first.check()
            p.value = opt
            return
        if p.value is None:
            raise ValueError("no value")
        locator.fill(_as_text(p.value))

    def _retry_with_options(self, page, p: PlannedFill, fields: list[FormField], current: dict[int, Any]) -> bool:
        f = p.field
        if self.answers is None or p.source == "ai" or p.kind not in ("select", "choice"):
            return False
        options = (
            f.options
            if p.kind == "select"
            else [g.label or g.value for g in fields if g.is_choice and g.name and g.name == f.name]
            or [f.label or f.value]
        )
        answer, conf = self.answers.answer(f.question, options)
        if answer is None:
            return False
        p.value, p.source, p.confidence = answer, "ai", conf
        try:
            self._fill_one(page, p, fields, current)
        except Exception as exc:
            p.error = str(exc)[:200]
            return False
        p.error = ""
        return True

    def _fill_step(
        self, page, plan: list[PlannedFill], fields: list[FormField], current: dict[int, Any], result: FillResult
    ) -> None:
        for p in plan:
            try:
                if p.kind in ("text", "choice", "select") and p.value is None:
                    raise ValueError("no answer")
                self._fill_one(page, p, fields, current)
                p.done = True
            except Exception as exc:
                p.error = str(exc)[:200]
                if self._retry_with_options(page, p, fields, current):
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
            if f.required and f.idx not in done_idx and f.name not in done_groups and not f.is_file:
                result.unfilled_required.append(f.question[:100] or f.selector)
            elif f.is_file and f.required and f.idx not in done_idx:
                reason = next((p.error for p in plan if p.field.idx == f.idx and p.error), "")
                label = f.question[:100] or "Resume"
                result.unfilled_required.append(f"{label} ({reason})" if reason else label)
        result.needs_human = list(dict.fromkeys(result.needs_human))
        result.unfilled_required = list(dict.fromkeys(result.unfilled_required))

    # ---------------------------------------------------------------- modal navigation

    def _inline_error(self, page) -> str:
        loc = self._visible(page, "error", within=self._modal_scope)
        if loc is None:
            return ""
        try:
            return (loc.inner_text() or "validation error").strip()[:200] or "validation error"
        except Exception:
            return "validation error"

    def _discard(self, page, result: FillResult | None = None) -> bool:
        """Close the modal and discard the draft so LinkedIn keeps nothing of a dry run.

        The Dismiss button is looked up inside the modal (toasts and banners carry the same aria-label).
        Returns True when the modal is gone; otherwise the result is flagged so the Queue page shows it.
        """
        note = "Easy Apply draft may be left open on LinkedIn: discard it by hand"
        try:
            if not self._modal_open(page):
                return True
            dismiss = self._visible(page, "dismiss", within=self._modal_scope) or self._visible(page, "dismiss")
            if dismiss is not None:
                dismiss.click(timeout=5000)
                self._pause(page, 400, 900)
                discard = self._visible(page, "discard")
                if discard is not None:
                    discard.click(timeout=5000)
                    self._pause(page, 300, 700)
            try:
                page.locator(self._modal_scope).first.wait_for(state="hidden", timeout=5000)
            except Exception:
                pass
            if not self._modal_open(page):
                return True
        except Exception as exc:
            log.warning("could not discard easy apply draft", error=str(exc)[:200])
        log.warning("easy apply modal still open after discard")
        if result is not None and note not in result.needs_human:
            result.needs_human.append(note)
        return False

    def _screenshot(self, page, path: Path | None, result: FillResult) -> None:
        if not path or result.screenshot_path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            result.screenshot_path = str(path)
        except Exception as exc:
            log.warning("screenshot failed", error=str(exc)[:200])

    def _submit(self, page, result: FillResult) -> None:
        btn = self._visible(page, "submit")
        if btn is None:
            result.error = "submit button not found"
            return
        btn.click(timeout=10000)
        self._clicked_submit = True
        # The button stays in the DOM (disabled) while the POST runs; wait for it to go rather than guess.
        try:
            page.locator(self.sel["submit"]).first.wait_for(state="hidden", timeout=15000)
        except Exception:
            pass
        self._pause(page, 500, 1200)
        err = self._inline_error(page) if self._modal_open(page) else ""
        if err:
            result.error = f"LinkedIn reported an error after submit: {err}"
            return
        if self._visible(page, "submit") is not None:
            result.error = "submit button still shown after clicking it"
            result.needs_human.append("submit was clicked but the modal stayed open: verify on LinkedIn")
            return
        result.submitted = True
        done = self._visible(page, "post_submit_done")
        if done is not None:
            try:
                done.click(timeout=3000)
            except Exception:
                pass

    # ---------------------------------------------------------------- entry point

    def _wait_for_button(self, page, timeout_ms: int = 15000):
        """The top card renders client-side after domcontentloaded: wait for the button, then pick it."""
        try:
            page.locator(self.sel["easy_apply_button"]).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            pass
        return self._visible(page, "easy_apply_button")

    @staticmethod
    def _button_is_easy_apply(button) -> bool:
        try:
            label = (button.get_attribute("aria-label") or "") + " " + (button.inner_text() or "")
        except Exception:
            return True  # cannot read it: trust the selector
        return bool(_EASY_APPLY_TEXT.search(label))

    def run(self, page, job_url: str, submit: bool = False, screenshot_path: Path | None = None) -> FillResult:
        result = FillResult(url=job_url, profile_origin=PROFILE_ORIGIN)
        self._clicked_submit = False
        resp = page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
        status = getattr(resp, "status", None)
        if status in _RATE_LIMIT_STATUSES:
            result.paused_reason = f"linkedin answered {status}"
            return result
        self._pause(page, 800, 1800)

        blocked = self._blocked(page)
        if blocked:
            result.paused_reason = blocked
            self._screenshot(page, screenshot_path, result)
            return result
        session_state = _classify(page)
        if session_state == "expired":
            result.paused_reason = "linkedin session expired"
            return result
        if session_state == "challenge" and not self._looks_like_job_page(page):
            result.paused_reason = "security check page"
            self._screenshot(page, screenshot_path, result)
            return result

        button = self._wait_for_button(page)
        if button is None:
            if self._visible(page, "already_applied") is not None:
                result.error = "already applied to this job on LinkedIn"
            else:
                result.error = "Easy Apply button not found (external apply or job closed)"
            return result
        if not self._button_is_easy_apply(button):
            result.error = "external apply (the Apply button opens the company site), not Easy Apply"
            return result
        button.click(timeout=10000)
        try:
            self._modal(page).wait_for(state="visible", timeout=10000)
        except Exception:
            result.error = "Easy Apply modal did not open"
            return result
        self._pause(page, 600, 1400)

        previous_sig: tuple | None = None
        repeats = 0
        reached_review = False
        try:
            for step in range(self.max_steps):
                blocked = self._blocked(page)
                if blocked:
                    result.paused_reason = blocked
                    self._screenshot(page, screenshot_path, result)
                    break
                if not self._modal_open(page):
                    result.error = "Easy Apply modal closed unexpectedly"
                    break
                fields, current = self._modal_fields(page)
                result.fields_total += len(fields)
                plan = self._plan_step(page, fields, current)
                self._fill_step(page, plan, fields, current, result)
                result.plan.extend(plan)
                log.debug("easy apply step", step=step, fields=len(fields), filled=sum(1 for p in plan if p.done))

                if self._visible(page, "submit") is not None:
                    reached_review = True
                    self._screenshot(page, screenshot_path, result)
                    break
                sig = tuple((f.selector, f.question) for f in fields)
                if sig == previous_sig:
                    repeats += 1
                    if repeats >= 2:
                        result.paused_reason = "the step did not advance after clicking next"
                        self._screenshot(page, screenshot_path, result)
                        break
                else:
                    repeats = 0
                previous_sig = sig
                nav = self._visible(page, "next") or self._visible(page, "review")
                if nav is None:
                    result.error = "no next / review button in the Easy Apply modal"
                    self._screenshot(page, screenshot_path, result)
                    break
                nav.click(timeout=10000)
                self._pause(page, 700, 1600)
                err = self._inline_error(page)
                if err:
                    result.unfilled_required.append(err)
                    result.unfilled_required = list(dict.fromkeys(result.unfilled_required))
                    result.paused_reason = "required fields left empty"
                    self._screenshot(page, screenshot_path, result)
                    break
            else:
                result.paused_reason = f"more than {self.max_steps} steps in the Easy Apply modal"
                self._screenshot(page, screenshot_path, result)

            if reached_review and not result.paused_reason:
                if result.needs_human:
                    result.paused_reason = "a question needs a human"
                elif result.unfilled_required:
                    result.paused_reason = "required fields left empty"
            if reached_review and submit and not result.paused_reason:
                self._submit(page, result)
        finally:
            if self._clicked_submit and not result.submitted:
                # Never discard after a submit click: the application may already be on its way.
                log.warning("submit clicked but not confirmed; leaving the modal alone", url=job_url)
            elif not result.submitted:
                self._discard(page, result)
        return result


# ------------------------------------------------------------------ pipeline integration


def pick_linkedin_applications(session: Session, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications on LinkedIn Easy Apply jobs (JobSource.raw.easy_apply true)."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != SITE or not job.apply_url:
            continue
        sources = session.exec(select(JobSource).where(JobSource.job_id == job.id, JobSource.source == SITE)).all()
        if not any((src.raw or {}).get("easy_apply") for src in sources):
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


_CHALLENGE_REASON = re.compile(r"captcha|security|unusual activity|linkedin answered \d+", re.I)


def _notify(session: Session, user: User, kind: str, message: str) -> None:
    """notify() that never breaks a run: a notifier failure is logged, the outcome is already recorded."""
    import jobapply.notify as notify_mod

    try:
        notify_mod.notify(session, user, kind, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", user=user.username, kind=kind, error=str(exc)[:200])


def _stop_reason(result) -> str:
    """'challenge' | 'expired' | '' from a run result: the whole run stops on these, not just the job."""
    reason = getattr(result, "paused_reason", "") or ""
    if reason == "linkedin session expired":
        return "expired"
    if _CHALLENGE_REASON.search(reason):
        return "challenge"
    return ""


def _mark_challenge(li, session: Session) -> None:
    recorder = getattr(li, "record_challenge", None)
    if callable(recorder):
        recorder()
    else:  # a session object without the helper: at least set the cooldown
        setter = getattr(li, "set_cooldown", None)
        if callable(setter):
            setter()
    session.commit()


def apply_linkedin_user(pw, session: Session, user: User, llm=None, limit: int = 3, dry_run: bool = True) -> list:
    """One user's LinkedIn run inside their persistent profile. Returns ApplyOutcome rows.

    Honours the kill switch and the global dry-run switch itself, so any caller (CLI, scheduler, web) gets the
    same guarantees as the ATS path; a challenge, block or logout seen mid-run ends the run for the user.
    """
    from jobapply.forms.runner import vault_view_for
    from jobapply.pipeline.apply import applied_today, record_outcome
    from jobapply.repo import UserScope
    from jobapply.switches import global_dry_run, kill_switch_on

    if kill_switch_on(session):
        log.warning("kill switch on, not applying on linkedin", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(session)
    scope = UserScope(session, user)
    site = scope.site(SITE)
    if site is None or not site.enabled:
        log.info("linkedin disabled for user", user=user.username)
        return []
    if _is_throttled(user, site):
        log.info("linkedin throttled (cooldown after a challenge), skipping", user=user.username)
        return []
    outcomes: list = []
    context = _launch_persistent(pw, user.id, SITE)
    try:
        li = _linkedin_session(context, user, session)
        page = context.new_page()
        status = li.check(page)
        if status == "unknown":
            # A half-rendered feed or a timeout is not a logout: retry on the next run, no alert.
            log.warning("linkedin session state unclear, retry later", user=user.username)
            return []
        if status != "ok":
            # LinkedInSession.check records expired/challenge itself; cover a check that did not.
            if site.login_status in ("ok", "unknown", "n/a"):
                site.login_status = status if status in ("expired", "challenge") else "unknown"
            session.add(site)
            session.commit()
            if status == "challenge":
                _notify(session, user, "paused",
                        "LinkedIn is showing a security check: open the browser profile, complete it, then run "
                        f"jobapply linkedin login -u {user.username}")
            else:
                _notify(session, user, "paused",
                        f"LinkedIn login needed ({status}): run jobapply linkedin login -u {user.username}")
            log.warning("linkedin session not ok", user=user.username, status=status)
            return []
        if site.login_status != "ok":
            site.login_status = "ok"
            session.add(site)
        s = get_settings()
        cap = int((user.preferences or {}).get("daily_cap", 40))
        done_today = applied_today(session, user.id)
        picks = pick_linkedin_applications(session, user, limit)
        random.shuffle(picks)
        selectors = load_selectors()
        for i, (app, job) in enumerate(picks):
            if not dry_run and (done_today >= cap or applied_today(session, user.id, SITE) >= site.daily_cap):
                log.info("daily cap reached", user=user.username, site=SITE)
                break
            if i:
                _between_applications(page)
            vault = vault_view_for(user, app)
            runner = EasyApplyRunner(
                session, user, vault, answers=AnswerSource(session, llm, user, job), llm=llm,
                resume_path=vault.resume_path, selectors=selectors,
            )
            shot = s.screenshots_dir / str(user.id) / f"{app.id}-{time.strftime('%Y%m%d%H%M%S')}.png"
            submit = not dry_run and site.auto_submit
            result = None
            try:
                result = runner.run(page, job.apply_url, submit=submit, screenshot_path=shot)
            except Exception as exc:  # navigation / browser failures
                outcome = record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
            else:
                outcome = record_outcome(session, user, app, job, result)
            session.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info("linkedin apply outcome", user=user.username, job=job.id, status=outcome.status.value,
                     detail=outcome.detail)
            stop = _stop_reason(result)
            if stop == "challenge":
                _mark_challenge(li, session)
                _notify(session, user, "paused",
                        f"LinkedIn blocked or challenged the Easy Apply run ({result.paused_reason}); "
                        f"paused for {DEFAULT_COOLDOWN_HOURS} h. Resolve it in the browser profile.")
                log.warning("linkedin challenge mid-run, stopping", user=user.username, reason=result.paused_reason)
                break
            if stop == "expired":
                site.login_status = "expired"
                session.add(site)
                session.commit()
                _notify(session, user, "paused",
                        f"LinkedIn session expired mid-run: run jobapply linkedin login -u {user.username}")
                log.warning("linkedin session expired mid-run, stopping", user=user.username)
                break
    finally:
        try:
            context.close()
        except Exception:
            pass
    return outcomes


def apply_linkedin(username: str | None = None, limit: int = 3, dry_run: bool = True, engine=None) -> list[dict]:
    """`jobapply linkedin apply` and the scheduler: Easy Apply for every active user (or one), within caps,
    switches and the LinkedIn active hours (`JOBAPPLY_LINKEDIN_ACTIVE_HOURS`)."""
    from playwright.sync_api import sync_playwright

    from jobapply.ai.base import LLMError
    from jobapply.ai.service import get_llm
    from jobapply.db import session_scope
    from jobapply.sites.browser import is_active_hours
    from jobapply.switches import global_dry_run, kill_switch_on

    hours = get_settings().linkedin_active_hours
    if not is_active_hours(spec=hours):
        log.info("outside linkedin active hours, nothing done", active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as session:
        if kill_switch_on(session):
            log.warning("kill switch on, not applying on linkedin")
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
                    outcomes = apply_linkedin_user(pw, session, user, llm, limit=limit, dry_run=dry_run)
                except Exception as exc:  # one user's browser trouble must not stop the others
                    log.exception("linkedin run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


__all__ = [
    "EasyApplyRunner",
    "apply_linkedin",
    "apply_linkedin_user",
    "key_for_label",
    "load_selectors",
    "pick_linkedin_applications",
    "skill_years",
]
