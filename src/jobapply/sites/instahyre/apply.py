"""Instahyre one-click apply ("Apply" / "Interested" on an opportunity page).

Runs inside the user's persistent, logged-in Instahyre profile (always HEADLESS on this machine). The site has
no application form: a signed-in candidate's Apply button fires one request
(``POST /api/v1/candidate_opportunities/candidate_matching/apply/`` with ``{"job_id", "is_interested": true}``,
seen in the ``opportunitiesListCtrl`` / ``candidateOpportunityCtrl`` code on 2026-10-29) and the page shows a
"Application sent to <company>!" toast. After a successful apply the app may open a modal: an "active check",
a "go premium" / "follow us" prompt, or a bulk-apply prompt for the employer's other openings; a job can also
carry screening questions. Any modal holding form controls is treated as a questionnaire: the run pauses with
``needs_human`` and nothing in it is filled.

Safety gates (``apply_job`` enforces them itself, whoever calls it): the kill switch, the global dry-run
switch, the per-site auto-submit flag (off by default: the run stops BEFORE the click and records a
screenshot), the per-user and per-site daily caps, active hours for real submits, and a Cloudflare challenge /
captcha / login page => pause, record login_status, never retry. The result follows the status vocabulary of
``pipeline/apply.py`` (dry_run | applied | paused | failed, plus skipped for a gate) and carries a
``FillResult`` so ``pipeline.apply.record_outcome`` can book it like any other filler result.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.forms.runner import FillResult
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, User, UserSite
from jobapply.sites.instahyre.session import (
    BLOCK_STATUSES,
    DEFAULT_COOLDOWN_HOURS,
    SITE,
    InstahyreSession,
    throttled,
)

log = get_logger(__name__)

PROFILE_ORIGIN = "builtin:instahyre"
APPLY_ENDPOINT = "/api/v1/candidate_opportunities/candidate_matching/apply/"
STATUSES = ("dry_run", "applied", "paused", "failed", "skipped")
CONFIRM_TIMEOUT_MS = 12_000
BUTTON_TIMEOUT_MS = 15_000

# Selectors, most specific first. The public page's button is ``button#job_details_top`` ("Apply now");
# the signed-in markup binds the same control to ``submitChoice`` / ``submitChoiceNonMatching`` (recorded from
# the app code, not from a logged-in page: no account was available). Every entry may list alternatives.
DEFAULT_SELECTORS: dict[str, str] = {
    "apply_button": (
        "button#job_details_top, #apply-bar button, [ng-click*='submitChoiceNonMatching'], "
        "[ng-click*='submitChoice('], button.btn-interested, button.btn-apply, "
        "button:has-text('Apply now'), button:has-text('Interested'), button:has-text('Apply')"
    ),
    # The public page's button only sends a visitor to sign-up: seeing it means logged out.
    "logged_out_apply": "[ng-click*='setPostAuthRedirectionUrl']",
    "already_applied": (
        ".application-status-box, span.status-label:has-text('Applied'), button:has-text('Applied'), "
        "button:has-text('Application sent')"
    ),
    "success_toast": (
        "#toast-container .toast-success, .toast-success, .toast-message:has-text('Application sent'), "
        ".growl-item.alert-success"
    ),
    "error_message": ".growl-item.alert-danger, .growl-item.alert-error, .growl-message, .field-error, .error-text",
    "modal": ".modal.in, .modal.show, .modal[style*='display: block'], [role='dialog'], .modal-dialog",
    "questionnaire": (
        ".modal.in input, .modal.in select, .modal.in textarea, .modal.show input, .modal.show select, "
        ".modal.show textarea, [role='dialog'] input, [role='dialog'] select, [role='dialog'] textarea, "
        ".questionnaire, [ng-controller*='uestionnaire'], form.screening-questions"
    ),
    "modal_close": (
        ".modal.in button.close, .modal.show button.close, [role='dialog'] button.close, "
        ".modal.in button:has-text('Skip'), .modal.in button:has-text('Not now'), "
        ".modal.in button:has-text('Maybe later'), .modal.in button:has-text('Close'), "
        ".modal.in [ng-click*='close']"
    ),
}
_APPLIED_TEXT = re.compile(r"\bapplied\b|application sent", re.I)
_SECURITY_TEXT = re.compile(r"just a moment|verify you are human|checking your browser|attention required", re.I)

PauseFn = Callable[[Any, int, int], None]


# ------------------------------------------------------------------ result


@dataclass
class ApplyResult:
    status: str
    reason: str = ""
    screenshot: str | None = None
    needs_human: list[str] = field(default_factory=list)
    # True when the interest request went through (the success toast was seen). A paused result can carry it:
    # the application IS on Instahyre's side even though a questionnaire was left for a human.
    interest_sent: bool = False
    url: str = ""

    def to_fill_result(self) -> FillResult:
        """The same shape the ATS and LinkedIn fillers return, for ``pipeline.apply.record_outcome``."""
        r = FillResult(url=self.url, profile_origin=PROFILE_ORIGIN, screenshot_path=self.screenshot,
                       needs_human=list(self.needs_human))
        if self.status == "applied":
            r.submitted = True
        elif self.status == "paused":
            r.paused_reason = self.reason
        elif self.status in ("failed", "skipped"):
            r.error = self.reason
        return r

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "screenshot": self.screenshot, "reason": self.reason,
                "needs_human": list(self.needs_human), "interest_sent": self.interest_sent, "url": self.url,
                "result": self.to_fill_result()}


# ------------------------------------------------------------------ lazy hooks (patched in tests)


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


def _launch_persistent(pw: Any, user_id: int):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, SITE, headless=True)  # headful spawn fails on this machine


def _instahyre_session(context: Any, user: User, session: Session) -> InstahyreSession:
    return InstahyreSession(context, user, session)


def _is_throttled(user: User, site: UserSite | None) -> bool:
    return bool(throttled(user, site))


def _between_applications(page: Any) -> None:
    _human_pause(page, 8000, 20000)


def active_hours_spec() -> str:
    """``JOBAPPLY_INSTAHYRE_ACTIVE_HOURS`` when the settings grow it, else the LinkedIn window."""
    s = get_settings()
    return str(getattr(s, "instahyre_active_hours", None) or s.linkedin_active_hours)


def _in_active_hours() -> bool:
    from jobapply.sites.browser import is_active_hours

    return is_active_hours(spec=active_hours_spec())


# ------------------------------------------------------------------ the runner


class OneClickApplyRunner:
    """Drives one opportunity page: finds the Apply control, stops before it (dry run / auto-submit off) or
    clicks it and reads the outcome. Never fills a questionnaire."""

    def __init__(self, selectors: dict[str, str] | None = None, pause: PauseFn | None = None,
                 confirm_timeout_ms: int = CONFIRM_TIMEOUT_MS, button_timeout_ms: int = BUTTON_TIMEOUT_MS):
        self.sel = {**DEFAULT_SELECTORS, **(selectors or {})}
        self._pause: PauseFn = pause or _human_pause
        self.confirm_timeout_ms = confirm_timeout_ms
        self.button_timeout_ms = button_timeout_ms

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

    def _text_of(self, locator: Any) -> str:
        try:
            return " ".join(filter(None, [locator.inner_text() or "", locator.get_attribute("aria-label") or ""]))
        except Exception:  # noqa: BLE001
            return ""

    def _wait_for_button(self, page: Any):
        try:
            page.locator(self.sel["apply_button"]).first.wait_for(state="visible", timeout=self.button_timeout_ms)
        except Exception:  # noqa: BLE001 - not there: decided below
            pass
        return self._visible(page, "apply_button")

    def _questionnaire_open(self, page: Any) -> bool:
        return self._visible(page, "questionnaire") is not None

    def _screenshot(self, page: Any, path: Path | None, result: ApplyResult) -> None:
        if not path or result.screenshot:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            result.screenshot = str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])

    def _close_modal(self, page: Any) -> None:
        btn = self._visible(page, "modal_close")
        if btn is None:
            return
        try:
            btn.click(timeout=3000)
            self._pause(page, 300, 700)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not close post-apply modal", error=str(exc)[:120])

    # -- after the click

    def _confirm(self, page: Any, result: ApplyResult) -> None:
        """Poll for the outcome of the click: success toast, applied marker, error, questionnaire, challenge."""
        deadline = time.monotonic() + self.confirm_timeout_ms / 1000
        while True:
            if InstahyreSession.safe_classify(page) == "challenge":
                result.status, result.reason = "paused", "challenge page after clicking apply"
                return
            err = self._visible(page, "error_message")
            if err is not None:
                text = (err.inner_text() or "").strip()[:200] if hasattr(err, "inner_text") else ""
                result.status, result.reason = "failed", f"instahyre reported an error: {text or 'unknown'}"
                return
            toast = self._visible(page, "success_toast")
            applied = self._visible(page, "already_applied")
            if toast is not None or applied is not None:
                result.interest_sent = True
                break
            if self._questionnaire_open(page):
                # Questions before the interest is registered: a human must answer them on the site.
                result.status = "paused"
                result.reason = "screening questionnaire needs a human"
                result.needs_human.append("Instahyre shows screening questions for this job: answer them on the site")
                return
            if time.monotonic() >= deadline:
                break
            self._pause(page, 400, 700)
        if not result.interest_sent:
            result.status = "paused"
            result.reason = "apply clicked but no confirmation seen: verify on instahyre"
            result.needs_human.append(
                "apply was clicked but no 'Application sent' toast appeared: check the job on Instahyre")
            return
        if self._questionnaire_open(page):
            result.status = "paused"
            result.reason = "post-apply questionnaire needs a human"
            result.needs_human.append(
                "application sent; Instahyre then opened a questionnaire: complete it on the site")
            return
        if self._visible(page, "modal") is not None:
            self._close_modal(page)  # premium / follow / bulk-apply prompts: nothing to answer
        result.status, result.reason = "applied", "interest sent"

    # -- entry point

    def run(self, page: Any, job_url: str, *, submit: bool, screenshot_path: Path | None = None,
            stop_reason: str = "") -> ApplyResult:
        """`submit=False` stops before the click (`stop_reason` says why: dry run or auto-submit off)."""
        result = ApplyResult(status="dry_run", url=job_url)
        resp = page.goto(job_url, wait_until="domcontentloaded", timeout=60_000)
        status = getattr(resp, "status", None)
        if status in BLOCK_STATUSES:
            result.status, result.reason = "paused", f"instahyre answered {status}"
            return result
        self._pause(page, 800, 1800)

        state = InstahyreSession.safe_classify(page)
        if state == "challenge":
            result.status, result.reason = "paused", "challenge page"
            self._screenshot(page, screenshot_path, result)
            return result
        if state == "login" or self._visible(page, "logged_out_apply") is not None:
            result.status, result.reason = "paused", "instahyre login needed"
            return result

        if self._visible(page, "already_applied") is not None:
            result.status, result.reason = "failed", "already applied to this job on instahyre"
            return result
        button = self._wait_for_button(page)
        if button is None:
            result.status, result.reason = "failed", "apply button not found (job closed or inactive)"
            self._screenshot(page, screenshot_path, result)
            return result
        if _APPLIED_TEXT.search(self._text_of(button)):
            result.status, result.reason = "failed", "already applied to this job on instahyre"
            return result
        if self._questionnaire_open(page):
            result.status, result.reason = "paused", "screening questionnaire needs a human"
            result.needs_human.append("Instahyre shows screening questions for this job: answer them on the site")
            self._screenshot(page, screenshot_path, result)
            return result

        if not submit:
            self._screenshot(page, screenshot_path, result)
            result.status = "dry_run"
            result.reason = stop_reason or "stopped before the apply click"
            return result

        try:
            button.click(timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            result.status, result.reason = "failed", f"could not click apply: {str(exc).splitlines()[0][:160]}"
            return result
        self._pause(page, 600, 1200)
        self._confirm(page, result)
        self._screenshot(page, screenshot_path, result)
        return result


# ------------------------------------------------------------------ gates + one application


def _job_of(db: Session, application: Application) -> Job | None:
    return db.get(Job, application.job_id)


def _site_of(db: Session, user: User) -> UserSite | None:
    return db.exec(select(UserSite).where(UserSite.user_id == user.id, UserSite.site == SITE)).first()


def _cap_reached(db: Session, user: User, site: UserSite) -> str:
    from jobapply.pipeline.apply import applied_today

    user_cap = int((user.preferences or {}).get("daily_cap", 40))
    if applied_today(db, user.id) >= user_cap:
        return f"daily cap reached for user ({user_cap})"
    if applied_today(db, user.id, SITE) >= int(site.daily_cap):
        return f"daily cap reached for instahyre ({site.daily_cap})"
    return ""


def apply_job(
    session: InstahyreSession,
    application: Application,
    *,
    dry_run: bool,
    auto_submit: bool,
    page: Any = None,
    selectors: dict[str, str] | None = None,
    screenshot_path: Path | None = None,
    pause: PauseFn | None = None,
) -> dict[str, Any]:
    """Apply (one click) to `application`'s job inside `session` (an :class:`InstahyreSession`: browser context,
    user and DB session). Returns ``{"status", "screenshot", "reason", "needs_human", "interest_sent", "result"}``.

    `dry_run` is OR-ed with the global dry-run switch; `auto_submit` is AND-ed with the site row's flag; the
    kill switch, a cooldown, a disabled site and the daily caps make it ``skipped``. The browser is touched only
    once every gate is passed. A challenge or a login page records login_status and pauses.
    """
    from jobapply.switches import global_dry_run, kill_switch_on

    db, user = session.db, session.user
    job = _job_of(db, application)
    url = job.apply_url if job else ""
    if job is None or not url:
        return ApplyResult("failed", "application has no job / apply url", url=url).as_dict()
    if kill_switch_on(db):
        log.warning("kill switch on, not applying on instahyre", user=user.username)
        return ApplyResult("skipped", "kill switch on", url=url).as_dict()
    site = _site_of(db, user)
    if site is None or not site.enabled:
        return ApplyResult("skipped", "instahyre disabled for user", url=url).as_dict()
    if _is_throttled(user, site):
        return ApplyResult("skipped", "instahyre cooldown active after a challenge", url=url).as_dict()
    dry_run = bool(dry_run) or global_dry_run(db)
    submit = (not dry_run) and bool(auto_submit) and bool(site.auto_submit)
    stop_reason = ""
    if dry_run:
        stop_reason = "dry run: apply button found, not clicked"
    elif not submit:
        stop_reason = "auto-submit off for instahyre: stopped before the apply click"
    if submit:
        capped = _cap_reached(db, user, site)
        if capped:
            log.info("daily cap reached", user=user.username, site=SITE, detail=capped)
            return ApplyResult("skipped", capped, url=url).as_dict()
        if not _in_active_hours():
            return ApplyResult("skipped", f"outside instahyre active hours ({active_hours_spec()})",
                               url=url).as_dict()

    s = get_settings()
    shot = screenshot_path or (s.screenshots_dir / str(user.id)
                               / f"{application.id}-{time.strftime('%Y%m%d%H%M%S')}.png")
    runner = OneClickApplyRunner(selectors=selectors, pause=pause)
    own_page = page is None
    if own_page:
        page = session.context.new_page()
    try:
        result = runner.run(page, url, submit=submit, screenshot_path=shot, stop_reason=stop_reason)
    finally:
        if own_page:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
    if result.status == "paused" and ("challenge" in result.reason or "answered" in result.reason):
        session.record_challenge()
    elif result.status == "paused" and result.reason == "instahyre login needed":
        session.record_login_needed()
    log.info("instahyre apply", user=user.username, job=job.id, status=result.status, reason=result.reason,
             interest_sent=result.interest_sent, submit=submit)
    return result.as_dict()


# ------------------------------------------------------------------ pipeline integration


def pick_instahyre_applications(session: Session, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications on Instahyre opportunity pages."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != SITE or "instahyre.com" not in (job.apply_url or ""):
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


def _notify(session: Session, user: User, kind: str, message: str) -> None:
    """notify() that never breaks a run."""
    import jobapply.notify as notify_mod

    try:
        notify_mod.notify(session, user, kind, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", user=user.username, kind=kind, error=str(exc)[:200])


def _stop_reason(outcome: dict[str, Any] | None) -> str:
    """'challenge' | 'login' | '' from an apply_job result: the whole run stops on these."""
    if not outcome or outcome.get("status") != "paused":
        return ""
    reason = str(outcome.get("reason") or "")
    if reason == "instahyre login needed":
        return "login"
    if "challenge" in reason or re.search(r"answered \d+", reason):
        return "challenge"
    return ""


def apply_instahyre_user(pw: Any, session: Session, user: User, limit: int = 3, dry_run: bool = True) -> list:
    """One user's Instahyre run inside their persistent (headless) profile. Returns ApplyOutcome rows.

    Honours the kill switch, the global dry-run switch, the site flags, the cooldown and the daily caps; a
    challenge or a logout seen mid-run ends the run for the user.
    """
    from jobapply.pipeline.apply import record_outcome
    from jobapply.repo import UserScope
    from jobapply.switches import global_dry_run, kill_switch_on

    if kill_switch_on(session):
        log.warning("kill switch on, not applying on instahyre", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(session)
    scope = UserScope(session, user)
    site = scope.site(SITE)
    if site is None or not site.enabled:
        log.info("instahyre disabled for user", user=user.username)
        return []
    if _is_throttled(user, site):
        log.info("instahyre throttled (cooldown after a challenge), skipping", user=user.username)
        return []
    picks = pick_instahyre_applications(session, user, limit)
    if not picks:
        return []
    outcomes: list = []
    context = _launch_persistent(pw, user.id)
    try:
        ih = _instahyre_session(context, user, session)
        page = context.new_page()
        status = ih.check(page)
        if status == "unknown":
            log.warning("instahyre session state unclear, retry later", user=user.username)
            return []
        if status != "ok":
            if status == "challenge":
                _notify(session, user, "paused",
                        "Instahyre is showing a security check: open the site in a browser, complete it, then "
                        f"re-import the cookies for {user.username}")
            else:
                _notify(session, user, "paused",
                        f"Instahyre login needed ({status}): export the site's cookies and import them for "
                        f"{user.username}")
            log.warning("instahyre session not ok", user=user.username, status=status)
            return []
        random.shuffle(picks)
        interest_count = 0
        for i, (app, job) in enumerate(picks):
            if not dry_run and _cap_reached(session, user, site):
                log.info("daily cap reached", user=user.username, site=SITE)
                break
            if i:
                _between_applications(page)
            try:
                outcome = apply_job(ih, app, dry_run=dry_run, auto_submit=site.auto_submit, page=page)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                booked = record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
                outcome = None
            else:
                if outcome["status"] == "skipped":
                    log.info("instahyre apply skipped", user=user.username, job=job.id, reason=outcome["reason"])
                    break
                booked = record_outcome(session, user, app, job, outcome["result"])
                if outcome.get("interest_sent"):
                    interest_count += 1
            session.commit()
            outcomes.append(booked)
            log.info("instahyre apply outcome", user=user.username, job=job.id, status=booked.status.value,
                     detail=booked.detail)
            stop = _stop_reason(outcome)
            if stop == "challenge":
                _notify(session, user, "paused",
                        f"Instahyre blocked or challenged the apply run ({outcome['reason']}); paused for "
                        f"{DEFAULT_COOLDOWN_HOURS} h.")
                log.warning("instahyre challenge mid-run, stopping", user=user.username, reason=outcome["reason"])
                break
            if stop == "login":
                _notify(session, user, "paused",
                        f"Instahyre session expired mid-run: re-import the cookies for {user.username}")
                log.warning("instahyre session expired mid-run, stopping", user=user.username)
                break
        if interest_count:
            log.info("instahyre interests sent", user=user.username, count=interest_count)
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
    return outcomes


def apply_instahyre(username: str | None = None, limit: int = 3, dry_run: bool = True,
                    engine: Any = None) -> list[dict]:
    """Entry point for a CLI / scheduler: one-click apply for every active user (or one), within caps,
    switches and the active hours."""
    from playwright.sync_api import sync_playwright

    from jobapply.db import session_scope
    from jobapply.switches import global_dry_run, kill_switch_on

    if not _in_active_hours():
        log.info("outside instahyre active hours, nothing done", active_hours=active_hours_spec())
        return []
    results: list[dict] = []
    with session_scope(engine) as session:
        if kill_switch_on(session):
            log.warning("kill switch on, not applying on instahyre")
            return []
        dry_run = dry_run or global_dry_run(session)
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        users = session.exec(stmt).all()
        with sync_playwright() as pw:
            for user in users:
                try:
                    outcomes = apply_instahyre_user(pw, session, user, limit=limit, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop the others
                    log.exception("instahyre run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


__all__ = ["ApplyResult", "OneClickApplyRunner", "apply_job", "apply_instahyre", "apply_instahyre_user",
           "pick_instahyre_applications", "active_hours_spec", "DEFAULT_SELECTORS", "APPLY_ENDPOINT",
           "PROFILE_ORIGIN", "STATUSES"]
