"""Wellfound apply: the logged-in "Apply" modal on a job page (a short note plus optional questions).

Runs inside the user's persistent, headless Wellfound profile (logged in through `session.import_cookies`).
It opens the job page, checks the session state, clicks Apply, writes the note (the application's stored
message, else `jobapply.ai.service.application_message`, which falls back to the template without an LLM),
answers extra questions through the answer source, screenshots the filled modal and stops there. Only when
every gate allows it does it click the final submit:

- kill switch off, global dry-run off (`switches`), the user's `wellfound` site enabled with auto_submit on,
- no challenge cooldown (`session.throttled`), inside the active hours,
- the per-user and the per-site daily caps not reached.

Any CAPTCHA / Cloudflare challenge pauses the application, records `login_status=challenge` with a cooldown
and is never retried by this run; a login page records `expired`. Statuses follow `pipeline/apply.py`:
dry_run | applied | paused | failed, plus `skipped` when a gate stopped the run before the browser opened.

Selectors live in `DEFAULT_SELECTORS`; `config/sites/wellfound.yaml` (optional, `selectors:` map) overrides
them without a code change. The logged-in modal markup could not be recorded (no session available), so the
modal selectors are deliberately broad and documented as unverified in docs/sites/WELLFOUND.md.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any

import yaml

from jobapply.config import get_settings
from jobapply.forms.fields import FormField, detect_captcha, extract_fields
from jobapply.forms.runner import FillResult, PlannedFill
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, User
from jobapply.sites.wellfound.session import BLOCK_STATUSES, SITE, WellfoundSession, classify_page, throttled

log = get_logger(__name__)

PROFILE_ORIGIN = "builtin:wellfound"
NOTE_MAX_CHARS = 1000
MIN_ANSWER_CONFIDENCE = 60
NOTE_LABEL = "Note to the hiring team"
_JOB_URL = re.compile(r"^https?://(www\.)?wellfound\.com/jobs/\d+", re.I)
_SECURITY_TEXT = re.compile(r"verify you are human|checking your browser|security check|are you a robot", re.I)
# Which of the extracted fields sit inside the (first matching, visible) modal container.
_IN_MODAL_JS = """(a) => {
  let m = null;
  for (const s of a.modal.split(',')) {
    try { m = document.querySelector(s.trim()); } catch (e) { m = null; }
    if (m) break;
  }
  return a.sels.map((s) => {
    let el = null;
    try { el = document.querySelector(s); } catch (e) { el = null; }
    return !!(el && m && m.contains(el) && el.getClientRects().length > 0);
  });
}"""

# Verified logged out (2026-10-29): the job page's Apply button carries data-test=JobApplicationApplyButton on
# the Next.js role pages and reads "Apply Now" on the server-rendered job page. Everything inside the modal is
# inferred and overridable from config/sites/wellfound.yaml.
DEFAULT_SELECTORS: dict[str, str] = {
    "apply_button": "[data-test='JobApplicationApplyButton'], button:has-text('Apply Now'), "
                    "a:has-text('Apply Now'), button:has-text('Apply')",
    "applied_marker": "[data-test='JobApplicationApplied'], button:has-text('Applied'), "
                      "button:has-text('Application sent')",
    "modal": "[data-test='JobApplicationModal'], [data-test='ApplicationModal'], [role='dialog']",
    "note": "[role='dialog'] textarea[name='note'], [role='dialog'] textarea[placeholder*='note' i], "
            "[role='dialog'] textarea[placeholder*='interested' i], [data-test='JobApplicationNote'], "
            "[role='dialog'] textarea",
    "submit": "[data-test='JobApplicationSubmitButton'], [role='dialog'] button[type='submit'], "
              "[role='dialog'] button:has-text('Send application'), [role='dialog'] button:has-text('Send'), "
              "[role='dialog'] button:has-text('Submit')",
    "close": "[role='dialog'] button[aria-label='Close'], [role='dialog'] button[aria-label='close'], "
             "[data-test='CloseButton'], [role='dialog'] button:has-text('Cancel')",
    "success": "[data-test='JobApplicationSuccess'], text=Application sent, text=Your application has been sent",
    "error": "[role='dialog'] [role='alert'], [role='dialog'] .error, [role='dialog'] [class*='error']",
    "security_check": "iframe[src*='challenges.cloudflare.com'], #challenge-form, #turnstile-wrapper, "
                      "iframe[src*='captcha']",
}


# ------------------------------------------------------------------ selectors


def selectors_path() -> Path:
    return get_settings().profiles_dir.parent / "sites" / "wellfound.yaml"


def load_selectors(path: Path | None = None) -> dict[str, str]:
    """DEFAULT_SELECTORS overlaid with `config/sites/wellfound.yaml` when it exists (utf-8)."""
    out = dict(DEFAULT_SELECTORS)
    p = path or selectors_path()
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k, v in (data.get("selectors") or {}).items():
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    return out


# ------------------------------------------------------------------ helpers


def active_hours_spec() -> str:
    """`JOBAPPLY_WELLFOUND_ACTIVE_HOURS` when the settings grow it; the LinkedIn window otherwise."""
    s = get_settings()
    return str(getattr(s, "wellfound_active_hours", None) or s.linkedin_active_hours)


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


def is_wellfound_job_url(url: str) -> bool:
    return bool(_JOB_URL.match(url or ""))


def outcome(status: str, reason: str = "", screenshot: str | None = None, url: str = "", **extra: Any) -> dict:
    """The uniform result: status (dry_run | applied | paused | failed | skipped), reason, screenshot, url."""
    base = {"status": status, "reason": reason, "screenshot": screenshot, "url": url, "submitted": status == "applied",
            "answers": {}, "needs_human": [], "unfilled_required": [], "site": SITE}
    base.update(extra)
    return base


def as_fill_result(result: dict) -> FillResult:
    """Translate an `apply_job` dict into the `FillResult` `pipeline.apply.record_outcome` consumes."""
    fr = FillResult(url=result.get("url") or "", profile_origin=PROFILE_ORIGIN)
    fr.screenshot_path = result.get("screenshot")
    fr.submitted = bool(result.get("submitted"))
    fr.needs_human = list(result.get("needs_human") or [])
    fr.unfilled_required = list(result.get("unfilled_required") or [])
    status, reason = result.get("status"), result.get("reason") or ""
    for idx, (question, value) in enumerate((result.get("answers") or {}).items()):
        f = FormField(idx, f"wellfound:{idx}", "textarea", "textarea", label=question)
        fr.plan.append(PlannedFill(f, "answers." + question[:80], value, "text", "builtin", 100, done=True))
        fr.fields_total += 1
    if status == "paused":
        fr.paused_reason = reason or "paused"
    elif status in ("failed", "skipped"):
        fr.error = reason or status
        fr.plan = []  # record_outcome: an error without a plan is a failure
    return fr


def note_for(session: WellfoundSession, application: Application, job: Job) -> str:
    """The application's stored message, else the AI/template application message. Capped for the modal."""
    from jobapply.ai.heuristics import cap_text

    text = (application.message or "").strip()
    if not text:
        from jobapply.ai.service import application_message

        text = application_message(session.db, session.llm, session.user, job, application)
    return cap_text(text, NOTE_MAX_CHARS)


def _answers_for(session: WellfoundSession, application: Application, job: Job) -> Any:
    if session.answers is not None:
        return session.answers
    try:
        from jobapply.forms.runner import AnswerSource

        return AnswerSource(session.db, session.llm, session.user, job, message=application.message or "")
    except Exception as exc:  # noqa: BLE001 - no answer source: custom questions are left for a human
        log.debug("no answer source", error=str(exc)[:120])
        return None


def resume_path_for(user: User, application: Application) -> str | None:
    for candidate in (application.resume_variant, (user.profile or {}).get("resume_file")):
        if candidate and Path(str(candidate)).exists():
            return str(candidate)
    return None


# ------------------------------------------------------------------ the runner


class WellfoundApplyRunner:
    def __init__(self, session: WellfoundSession, selectors: dict[str, str] | None = None,
                 min_answer_confidence: int = MIN_ANSWER_CONFIDENCE, pause: Any = None):
        self.session = session
        self.sel = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.min_answer_confidence = min_answer_confidence
        self._pause = pause or _human_pause

    # -- page helpers
    def _visible(self, page: Any, key: str):
        selector = self.sel.get(key, "")
        if not selector:
            return None
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception as exc:  # noqa: BLE001 - detached nodes mid-transition
            log.debug("locator failed", key=key, error=str(exc)[:120])
        return None

    def _modal_open(self, page: Any) -> bool:
        return self._visible(page, "modal") is not None or self._visible(page, "note") is not None

    def _blocked(self, page: Any) -> str:
        try:
            cap = detect_captcha(page)
        except Exception:  # noqa: BLE001
            cap = ""
        if cap:
            return f"captcha on page ({cap})"
        if self._visible(page, "security_check") is not None:
            return "security check page"
        try:
            title = page.title() or ""
        except Exception:  # noqa: BLE001
            title = ""
        if _SECURITY_TEXT.search(title):
            return "security check page"
        return ""

    def _screenshot(self, page: Any, path: Path | None, result: dict) -> None:
        if not path or result.get("screenshot"):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            result["screenshot"] = str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])

    def _close_modal(self, page: Any) -> bool:
        """Close the modal without submitting (Escape, then the close button). True when it is gone."""
        try:
            if not self._modal_open(page):
                return True
            page.keyboard.press("Escape")
            self._pause(page, 300, 700)
            if self._modal_open(page):
                close = self._visible(page, "close")
                if close is not None:
                    close.click(timeout=5000)
                    self._pause(page, 300, 700)
            return not self._modal_open(page)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close the wellfound apply modal", error=str(exc)[:200])
            return False

    def _fields_in_modal(self, page: Any) -> list[FormField]:
        fields = [f for f in extract_fields(page) if not f.hidden]
        if not fields:
            return []
        try:
            inside = page.evaluate(_IN_MODAL_JS, {"modal": self.sel["modal"], "sels": [f.selector for f in fields]})
        except Exception as exc:  # noqa: BLE001
            log.debug("modal field scoping failed", error=str(exc)[:120])
            return []
        return [f for f, ok in zip(fields, inside, strict=True) if ok]

    # -- filling
    def _fill_note(self, page: Any, note: str, result: dict) -> bool:
        box = self._visible(page, "note")
        if box is None:
            return False
        box.fill(note)
        result["answers"][NOTE_LABEL] = note
        return True

    def _fill_questions(self, page: Any, answers: Any, resume: str | None, result: dict) -> None:
        for f in self._fields_in_modal(page):
            if f.type in ("hidden", "submit", "button", "search", "password") or not f.question:
                continue
            try:
                loc = page.locator(f.selector).first
                if f.is_file:
                    if resume:
                        loc.set_input_files(resume)
                        result["answers"][f.question or "Resume"] = Path(resume).name
                    elif f.required:
                        result["unfilled_required"].append(f.question or "Resume")
                    continue
                if f.value and f.type not in ("checkbox", "radio"):
                    continue  # prefilled by Wellfound (profile data)
                if answers is None:
                    if f.required:
                        result["unfilled_required"].append(f.question[:100])
                    continue
                options = f.options if f.type == "select" else None
                answer, conf = answers.answer(f.question, options)
                if answer in (None, ""):
                    if f.required:
                        result["unfilled_required"].append(f.question[:100])
                    continue
                if f.type == "select":
                    loc.select_option(label=str(answer))
                elif f.is_choice:
                    if str(answer).strip().lower() in ("yes", "true", "1", (f.label or "").strip().lower()):
                        loc.check()
                    else:
                        continue
                else:
                    loc.fill(str(answer))
                result["answers"][f.question[:120]] = str(answer)
                if conf < self.min_answer_confidence:
                    result["needs_human"].append(f"{f.question[:100]}: low confidence answer, review it")
            except Exception as exc:  # noqa: BLE001 - one odd control must not abort the whole modal
                log.debug("field not filled", selector=f.selector, error=str(exc)[:120])
                if f.required:
                    result["unfilled_required"].append(f.question[:100])
        result["needs_human"] = list(dict.fromkeys(result["needs_human"]))
        result["unfilled_required"] = list(dict.fromkeys(result["unfilled_required"]))

    def _submit(self, page: Any, result: dict) -> None:
        btn = self._visible(page, "submit")
        if btn is None:
            result.update(status="failed", reason="submit button not found in the apply modal")
            return
        btn.click(timeout=10000)
        self._clicked_submit = True
        try:
            page.locator(self.sel["modal"]).first.wait_for(state="hidden", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        self._pause(page, 500, 1200)
        err = self._visible(page, "error") if self._modal_open(page) else None
        if err is not None:
            try:
                text = (err.inner_text() or "validation error").strip()[:200]
            except Exception:  # noqa: BLE001
                text = "validation error"
            result.update(status="failed", reason=f"wellfound reported an error after submit: {text}")
            return
        if self._visible(page, "success") is not None or not self._modal_open(page):
            result.update(status="applied", reason="submitted", submitted=True)
            return
        result.update(status="paused", reason="submit was clicked but the modal stayed open: verify on Wellfound")
        result["needs_human"].append("submit was clicked but the modal stayed open: verify on Wellfound")

    # -- entry point
    def run(self, page: Any, application: Application, job: Job, *, submit: bool, screenshot_path: Path | None,
            dry_reason: str) -> dict:
        result = outcome("dry_run", dry_reason, url=job.apply_url)
        self._clicked_submit = False
        resp = page.goto(job.apply_url, wait_until="domcontentloaded", timeout=60000)
        status = getattr(resp, "status", None)
        if status in BLOCK_STATUSES:
            self.session.record_challenge()
            self._screenshot(page, screenshot_path, result)
            result.update(status="paused", reason=f"wellfound answered {status} (challenge / block)")
            return result
        self._pause(page, 800, 1800)
        blocked = self._blocked(page)
        state = classify_page(page)
        if blocked or state == "challenge":
            self.session.record_challenge()
            self._screenshot(page, screenshot_path, result)
            result.update(status="paused", reason=blocked or "security check page")
            return result
        if state == "login":
            self.session.record("login")
            result.update(status="paused", reason="wellfound login required: import cookies and retry")
            return result
        if self._visible(page, "applied_marker") is not None:
            result.update(status="failed", reason="already applied to this job on wellfound")
            return result
        try:
            page.locator(self.sel["apply_button"]).first.wait_for(state="visible", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        button = self._visible(page, "apply_button")
        if button is None:
            result.update(status="failed", reason="Apply button not found (job closed or not a Wellfound apply)")
            return result
        button.click(timeout=10000)
        self._pause(page, 700, 1500)
        try:
            page.locator(self.sel["note"]).first.wait_for(state="visible", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        after = classify_page(page)
        if after == "login":
            self.session.record("login")
            result.update(status="paused", reason="wellfound login required: import cookies and retry")
            return result
        blocked = self._blocked(page)
        if blocked or after == "challenge":
            self.session.record_challenge()
            self._screenshot(page, screenshot_path, result)
            result.update(status="paused", reason=blocked or "security check page")
            return result
        if not self._modal_open(page):
            result.update(status="failed", reason="the apply modal did not open")
            return result
        if self.session.site.login_status != "ok":
            self.session.record("ok")  # the modal opened: the session is live
        try:
            note = note_for(self.session, application, job)
            if not self._fill_note(page, note, result):
                result["needs_human"].append("no note box found in the apply modal")
            answers = _answers_for(self.session, application, job)
            self._fill_questions(page, answers, resume_path_for(self.session.user, application), result)
            self._screenshot(page, screenshot_path, result)
            if result["unfilled_required"]:
                result.update(status="paused", reason="required fields left empty")
            elif result["needs_human"]:
                result.update(status="paused", reason="a question needs a human")
            elif submit:
                self._submit(page, result)
        finally:
            if self._clicked_submit and not result.get("submitted"):
                log.warning("submit clicked but not confirmed; leaving the modal alone", url=job.apply_url)
            elif not result.get("submitted") and not self._close_modal(page):
                result["needs_human"].append("apply modal may be left open on Wellfound: close it by hand")
        return result


# ------------------------------------------------------------------ gated entry point


def apply_job(session: WellfoundSession, application: Application, *, dry_run: bool, auto_submit: bool,
              job: Job | None = None, selectors: dict[str, str] | None = None, screenshot_path: Path | None = None,
              pause: Any = None, now: Any = None) -> dict:
    """Apply (or dry-run) one application on Wellfound inside `session` (a `WellfoundSession`).

    Returns `{"status": dry_run|applied|paused|failed|skipped, "reason": ..., "screenshot": ..., "url": ...,
    "submitted": bool, "answers": {...}, "needs_human": [...], "unfilled_required": [...]}`. It never submits
    when the global dry-run switch is on, the kill switch is on, `auto_submit` is false, the user's site has
    auto_submit off, a cooldown is active, the active hours are over or a daily cap is reached.
    """
    from jobapply.pipeline.apply import applied_today
    from jobapply.sites.browser import is_active_hours
    from jobapply.switches import global_dry_run, kill_switch_on

    db, user = session.db, session.user
    if kill_switch_on(db):
        log.warning("kill switch on, not applying on wellfound", user=user.username)
        return outcome("skipped", "kill switch on")
    dry_run = bool(dry_run) or global_dry_run(db)
    site = session.scope.site(SITE)
    if site is None or not site.enabled:
        log.info("wellfound disabled for user", user=user.username)
        return outcome("skipped", "wellfound disabled for this user")
    if throttled(user, site, now=now):
        log.info("wellfound throttled (cooldown after a challenge), skipping", user=user.username)
        return outcome("skipped", "wellfound throttled: cooldown after a challenge")
    hours = active_hours_spec()
    if not is_active_hours(spec=hours):
        log.info("outside wellfound active hours, nothing done", active_hours=hours)
        return outcome("skipped", f"outside active hours ({hours})")
    if not dry_run:
        cap = int((user.preferences or {}).get("daily_cap", 40))
        if applied_today(db, user.id) >= cap:
            log.info("daily cap reached", user=user.username, cap=cap)
            return outcome("skipped", "daily cap reached")
        if applied_today(db, user.id, SITE) >= site.daily_cap:
            log.info("daily cap reached", user=user.username, site=SITE, cap=site.daily_cap)
            return outcome("skipped", "wellfound daily cap reached")
    job = job or db.get(Job, application.job_id)
    if job is None:
        return outcome("failed", "application has no job")
    if not is_wellfound_job_url(job.apply_url):
        return outcome("failed", "not a wellfound job page", url=job.apply_url)
    submit = (not dry_run) and bool(auto_submit) and bool(site.auto_submit)
    if dry_run:
        dry_reason = "dry run: global dry-run on" if global_dry_run(db) else "dry run requested"
    elif not submit:
        dry_reason = "auto-submit off for wellfound: filled and screenshotted, not submitted"
    else:
        dry_reason = "filled"
    shot = screenshot_path or (get_settings().screenshots_dir / str(user.id)
                               / f"{application.id}-{time.strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}.png")
    runner = WellfoundApplyRunner(session, selectors=selectors or load_selectors(), pause=pause)
    page = session.page()
    try:
        result = runner.run(page, application, job, submit=submit, screenshot_path=shot, dry_reason=dry_reason)
    except Exception as exc:  # noqa: BLE001 - navigation / browser failures
        log.warning("wellfound apply crashed", user=user.username, job=job.id, error=f"{type(exc).__name__}: {exc}")
        result = outcome("failed", f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}", url=job.apply_url)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("wellfound apply outcome", user=user.username, job=job.id, status=result["status"],
             reason=result.get("reason"))
    return result


def pick_wellfound_applications(session: Any, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications on Wellfound job pages (for the wiring agent's runner)."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != SITE or not is_wellfound_job_url(job.apply_url):
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


__all__ = ["apply_job", "as_fill_result", "WellfoundApplyRunner", "DEFAULT_SELECTORS", "load_selectors",
           "note_for", "outcome", "is_wellfound_job_url", "pick_wellfound_applications", "active_hours_spec",
           "PROFILE_ORIGIN", "NOTE_MAX_CHARS", "NOTE_LABEL"]
