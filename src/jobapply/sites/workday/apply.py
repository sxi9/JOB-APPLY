"""Workday apply: the uniform ``apply_job(session, application, *, dry_run, auto_submit) -> dict`` and the gated
per-user orchestrator (``apply_workday_user`` / ``apply_workday``), mirroring ``sites.naukri.apply``.

Flow for one application (``apply_job``):

1. ``tenant.parse_tenant_url`` on the job URL (not a Workday URL -> ``failed``).
2. ``account.WorkdayAccount.open_apply``: job page -> Apply -> Apply Manually -> step 1 of the flow.
3. ``login`` / ``create``: stored credentials (vault ``workday:<tenant>``) -> ``sign_in``; none ->
   under the global dry run nothing is created (``dry_run``, screenshot); otherwise the user's mailbox must be
   configured (``mail.inbox.mailbox_configs``; missing -> ``paused`` "mailbox not configured", run stops), a
   password is generated into the vault, the account is created with the profile email and the one required
   checkbox, and the verification code / link is read from the mailbox with a bounded timeout
   (``paused`` "verification code not received" when nothing arrives; never retried in a loop).
4. ``wizard.WorkdayWizard``: every step filled by the FormRunner, *Save and Continue* clicked (never a
   submit), screenshots per step under ``data/screenshots/<user_id>/``. *Submit* on Review is clicked only
   when ``dry_run`` is False AND ``auto_submit`` is True.

Result vocabulary (``ApplyResult.status``): ``applied | dry_run | needs_human | paused | skipped | failed``,
mapped onto the pipeline's ``FillResult`` by ``ApplyResult.to_fill_result``. A CAPTCHA / block sets
``login_status="challenge"`` (the orchestrator records a cooldown and stops); ``stop_run`` marks a result
that would repeat for every job of the run (mailbox / vault not configured).
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jobapply.config import get_settings
from jobapply.forms.fields import FormField
from jobapply.forms.runner import FillResult, FormRunner, PlannedFill
from jobapply.logs import get_logger
from jobapply.mail.imap import ImapReader
from jobapply.models import Application, AppStatus, Job, User, UserSite, utcnow
from jobapply.sites.workday.account import (
    MAILBOX_LOGIN_FAILED,
    STATE_CHALLENGE,
    STATE_CREATE,
    STATE_LOGIN,
    STATE_SUBMITTED,
    STATE_UNKNOWN,
    STATE_VERIFY,
    STATE_WIZARD,
    WorkdayAccount,
    page_error,
)
from jobapply.sites.workday.session import DEFAULT_COOLDOWN_HOURS, SITE, WorkdaySession, throttled
from jobapply.sites.workday.tenant import is_workday_url, parse_tenant_url
from jobapply.sites.workday.wizard import WorkdayWizard, workday_vault_view
from jobapply.vault import Vault, VaultError

log = get_logger(__name__)

PROFILE_ORIGIN = "builtin:workday"
MAIL_TIMEOUT_S = 180
STATUS_APPLIED = "applied"
STATUS_DRY_RUN = "dry_run"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_PAUSED = "paused"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUSES = (STATUS_APPLIED, STATUS_DRY_RUN, STATUS_NEEDS_HUMAN, STATUS_PAUSED, STATUS_SKIPPED, STATUS_FAILED)

PauseFn = Any


@dataclass
class ApplyResult:
    url: str
    status: str = STATUS_FAILED
    reason: str = ""
    screenshot: str | None = None
    tenant: str = ""
    account_state: str = ""  # the account step's final classify_page state
    account_created: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    needs_human: list[str] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    recoveries: list[str] = field(default_factory=list)
    reached_review: bool = False
    submit_clicked: bool = False
    submitted: bool = False
    login_status: str | None = None  # "challenge" when a CAPTCHA / block stopped the run
    stop_run: bool = False  # a per-user problem every job would hit (mailbox / vault missing)
    apply_url: str | None = None  # kept for the uniform dict shape; Workday never routes elsewhere
    ats_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_fill_result(self) -> FillResult:
        r = FillResult(url=self.url, profile_origin=PROFILE_ORIGIN, screenshot_path=self.screenshot,
                       needs_human=list(self.needs_human), unfilled_required=list(self.unfilled_required),
                       recoveries=list(self.recoveries))
        for idx, (question, value) in enumerate(self.answers.items()):
            f = FormField(idx, f"{SITE}:{idx}", "textarea", "textarea", label=str(question))
            r.plan.append(PlannedFill(f, "answers." + str(question)[:80], value, "text", "builtin", 100, done=True))
            r.fields_total += 1
        if self.status == STATUS_APPLIED:
            r.submitted = True
        elif self.status == STATUS_DRY_RUN:
            pass
        elif self.status in (STATUS_NEEDS_HUMAN, STATUS_PAUSED, STATUS_SKIPPED):
            r.paused_reason = self.reason or self.status
        else:
            r.error = self.reason or "workday apply failed"
        return r


# ------------------------------------------------------------------ helpers


def _job_of(db: Any, application: Application) -> Job:
    job = db.get(Job, application.job_id)
    if job is None:
        raise ValueError(f"application {application.id} has no job")
    return job


def _vault_or_none() -> Vault | None:
    try:
        return Vault()
    except VaultError as exc:
        log.warning("vault unavailable for workday accounts", error=str(exc)[:120])
        return None


def mailbox_reader(db: Any, user: User, client_factory: Any = None) -> ImapReader | None:
    """An ``ImapReader`` over the user's vault mailbox (site ``mailbox``, kind ``imap``), or None."""
    from jobapply.mail.inbox import mailbox_configs

    imap_cfg, _ = mailbox_configs(db, user)
    if imap_cfg is None:
        return None
    return ImapReader(imap_cfg, client_factory=client_factory)


def _shot(page: Any, path: Path | None, result: ApplyResult, suffix: str = "") -> None:
    if not path or result.screenshot:
        return
    target = Path(path)
    if suffix:
        target = target.with_name(f"{target.stem}-{suffix}{target.suffix}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target), full_page=True)
        result.screenshot = str(target)
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot failed", error=str(exc)[:200])


# ------------------------------------------------------------------ uniform entry point


def apply_job(session: WorkdaySession, application: Application, *, dry_run: bool, auto_submit: bool,
              llm: Any = None, answers: Any = None, screenshot_path: Path | None = None, vault: Vault | None = None,
              client_factory: Any = None, mail_timeout: float = MAIL_TIMEOUT_S, selectors: dict[str, str] | None = None,
              pause: PauseFn | None = None, profile: Path | None = None) -> dict[str, Any]:
    """Apply to one application's job inside ``session`` (a ``WorkdaySession``: page + user + db).

    Gates honoured here: ``dry_run`` -> no account is created and Submit is never clicked (the wizard is still
    walked to Review and screenshotted); ``auto_submit`` False -> same, Submit never clicked. The caller owns
    the kill switch / caps / active hours (``apply_workday_user`` does). ``client_factory`` injects the IMAP
    client (tests); ``vault`` the credential vault (default: ``Vault()`` from ``JOBAPPLY_VAULT_KEY``).
    """
    from jobapply.forms.runner import AnswerSource

    job = _job_of(session.db, application)
    user = session.user
    url = job.apply_url or job.url
    result = ApplyResult(url=url)
    tenant = parse_tenant_url(url)
    if tenant is None:
        result.reason = f"not a Workday job URL: {url[:120]}"
        return result.as_dict()
    result.tenant = tenant.tenant
    if job.ats_type != SITE:
        # A bare *.myworkdayjobs.com posting from a non-ATS collector (ats_type None): stamp it, as
        # hand_to_account_site does, so applied_today(site="workday") counts it toward the site's daily cap.
        job.ats_type = SITE
        session.db.add(job)
        session.db.flush()
    if screenshot_path is None:
        screenshot_path = get_settings().screenshots_dir / str(user.id) / (
            f"{application.id}-{utcnow().strftime('%Y%m%d%H%M%S')}.png")
    vault = vault if vault is not None else _vault_or_none()
    page = session.page
    account = WorkdayAccount(page, user, session.db, tenant, vault=vault, selectors=selectors, pause=pause)
    if not account.email:
        result.status, result.reason, result.stop_run = STATUS_PAUSED, "profile has no email address", True
        return result.as_dict()

    try:
        state = account.open_apply(url)
    except Exception as exc:  # noqa: BLE001 - navigation failure
        result.reason = f"apply page did not load: {str(exc).splitlines()[0][:160]}"
        return result.as_dict()
    result.account_state = state
    if state == STATE_CHALLENGE:
        result.status, result.login_status = STATUS_PAUSED, "challenge"
        result.reason = "captcha / security check on the apply page"
        _shot(page, screenshot_path, result)
        return result.as_dict()
    if state == STATE_SUBMITTED:
        result.status, result.reason = STATUS_SKIPPED, f"already applied on {tenant.tenant} (Workday)"
        _shot(page, screenshot_path, result)
        return result.as_dict()
    if state == STATE_UNKNOWN:
        result.status = STATUS_PAUSED
        result.reason = "apply page did not render the sign-in step or the wizard (closed posting or new layout?)"
        _shot(page, screenshot_path, result)
        return result.as_dict()

    if state in (STATE_LOGIN, STATE_CREATE):
        creds = account.credentials()
        # A row with `created` False never reached Create Account (the form could not be filled last time):
        # there is no account to sign in to, so creation is tried again. Rows without the flag are taken as real.
        retry_create = False
        if creds and creds.get("password") and creds.get("created", True):
            state = account.sign_in(str(creds.get("username") or account.email), str(creds["password"]))
            result.account_state = state
            if state in (STATE_LOGIN, STATE_CREATE):
                err = page_error(page, account.sel)
                if creds.get("verified", True) is False:
                    # The account was never confirmed by mail: the Create Account click most likely never reached
                    # Workday (its submit sits under a click filter), so there is nothing to sign in to yet.
                    # Create it again with the same stored password; a real duplicate surfaces as Workday's
                    # "already in use" error below.
                    log.info("workday sign-in rejected on an unverified account, retrying creation",
                             tenant=tenant.tenant, error=err[:80])
                    retry_create = True
                else:
                    result.status = STATUS_PAUSED
                    result.reason = (f"workday sign-in rejected on {tenant.tenant}: "
                                     f"{err or 'the stored password was not accepted; reset it on the Candidate Home'}")
                    _shot(page, screenshot_path, result)
                    return result.as_dict()
        if retry_create or not (creds and creds.get("password") and creds.get("created", True)):
            if vault is None:
                result.status, result.reason, result.stop_run = (
                    STATUS_PAUSED, "vault not configured: set JOBAPPLY_VAULT_KEY to store Workday account passwords",
                    True)
                _shot(page, screenshot_path, result)
                return result.as_dict()
            if dry_run:
                result.status = STATUS_DRY_RUN
                result.reason = (f"global dry run: no account on {tenant.tenant} yet; it would be created with the "
                                 "profile email (not created, nothing submitted)")
                _shot(page, screenshot_path, result)
                return result.as_dict()
            reader = mailbox_reader(session.db, user, client_factory)
            if reader is None:
                result.status, result.reason, result.stop_run = STATUS_PAUSED, "mailbox not configured", True
                _shot(page, screenshot_path, result)
                return result.as_dict()
            if not retry_create:
                creds = account.new_credentials()
            state = account.create_account(account.email, str(creds["password"]))
            result.account_state, result.account_created = state, account.created
            if state in (STATE_CREATE, STATE_LOGIN):
                err = page_error(page, account.sel)
                result.status = STATUS_PAUSED
                result.reason = f"workday account creation rejected on {tenant.tenant}: {err or 'form not accepted'}"
                _shot(page, screenshot_path, result)
                return result.as_dict()
            if state == STATE_VERIFY:
                state, reason = account.verify_email(reader, timeout=mail_timeout)
                result.account_state = state
                if reason:
                    result.status, result.reason = STATUS_PAUSED, reason
                    result.stop_run = reason == MAILBOX_LOGIN_FAILED  # every job of the run would hit it
                    _shot(page, screenshot_path, result)
                    return result.as_dict()
    if state == STATE_VERIFY:
        # Signing in with a known account can still ask for a mailed code (new device).
        reader = mailbox_reader(session.db, user, client_factory)
        if reader is None:
            result.status, result.reason, result.stop_run = STATUS_PAUSED, "mailbox not configured", True
            _shot(page, screenshot_path, result)
            return result.as_dict()
        state, reason = account.verify_email(reader, timeout=mail_timeout)
        result.account_state = state
        if reason:
            result.status, result.reason = STATUS_PAUSED, reason
            result.stop_run = reason == MAILBOX_LOGIN_FAILED
            _shot(page, screenshot_path, result)
            return result.as_dict()
    if state == STATE_CHALLENGE:
        result.status, result.login_status = STATUS_PAUSED, "challenge"
        result.reason = "captcha / security check at sign-in"
        _shot(page, screenshot_path, result)
        return result.as_dict()
    if state == STATE_SUBMITTED:
        result.status, result.reason = STATUS_SKIPPED, f"already applied on {tenant.tenant} (Workday)"
        _shot(page, screenshot_path, result)
        return result.as_dict()
    if state != STATE_WIZARD:
        result.status, result.reason = STATUS_PAUSED, f"unexpected page after the account step (state {state})"
        _shot(page, screenshot_path, result)
        return result.as_dict()

    vault_view = workday_vault_view(user, application)
    answers = answers if answers is not None else AnswerSource(session.db, llm, user, job,
                                                               message=application.message or "")
    # navigate=False: the WorkdayWizard owns every Save and Continue click. With the generic navigator on, the
    # runner's own wizard walker would click the same Next button after a clean fill (a React type=button Next
    # is not stopped by the dry-run submit guard) and advance the SPA out from under the wizard.
    runner = FormRunner(session.db, vault_view, answers=answers, llm=llm, user=user, learn=False, navigate=False)
    wizard = WorkdayWizard(runner, profile=profile, selectors=selectors, pause=pause)
    submit = (not dry_run) and bool(auto_submit)
    wr = wizard.run(page, submit=submit, screenshot_path=screenshot_path)
    result.steps = [asdict(s) for s in wr.steps]
    result.answers = wr.answers
    result.needs_human = wr.needs_human
    result.unfilled_required = wr.unfilled_required
    result.recoveries = wr.recoveries
    result.reached_review, result.submit_clicked, result.submitted = wr.reached_review, wr.submit_clicked, wr.submitted
    result.screenshot = wr.screenshot or result.screenshot
    if wr.submitted:
        result.status, result.reason = STATUS_APPLIED, f"applied on {tenant.tenant} (Workday)"
    elif wr.login_status == "challenge":
        result.status, result.reason, result.login_status = STATUS_PAUSED, wr.paused_reason, "challenge"
    elif wr.paused_reason:
        result.status = STATUS_NEEDS_HUMAN if "needs a human" in wr.paused_reason else STATUS_PAUSED
        result.reason = wr.paused_reason
    elif wr.error:
        result.status, result.reason = STATUS_FAILED, wr.error
    else:
        result.status = STATUS_DRY_RUN
        why = "global dry run" if dry_run else "auto_submit off for workday"
        where = "Review reached" if wr.reached_review else f"stopped at {wr.final_step or 'the wizard'}"
        result.reason = f"{why}: {where}, Submit not clicked"
    log.info("workday apply", user=user.username, job=job.id, tenant=tenant.tenant, status=result.status,
             reason=result.reason[:120], submit=submit)
    return result.as_dict()


# ------------------------------------------------------------------ pipeline integration (gated)


def pick_workday_applications(db: Any, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications on Workday postings (``ats_type == "workday"`` or a bare
    ``*.myworkdayjobs.com`` apply URL)."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(db, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        url = job.apply_url or job.url
        if not url:
            continue
        if site_for_job(job) != SITE and not (job.ats_type is None and is_workday_url(url)):
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


def _notify(db: Any, user: User, kind: str, message: str) -> None:
    import jobapply.notify as notify_mod

    try:
        notify_mod.notify(db, user, kind, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", user=user.username, kind=kind, error=str(exc)[:200])


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


def apply_workday_user(pw: Any, db: Any, user: User, llm: Any = None, limit: int = 3, dry_run: bool = True, *,
                       context: Any = None, page: Any = None, between: Any = None, client_factory: Any = None,
                       selectors: dict[str, str] | None = None, pause: PauseFn | None = None,
                       vault: Vault | None = None) -> list[Any]:
    """One user's Workday run inside their persistent (headless) profile. Returns ``ApplyOutcome`` rows.

    Gates, in order: kill switch; global dry-run switch (forces dry run: no account creation, no Submit);
    site enabled; cooldown after a challenge; per-user and per-site daily caps; the site's ``auto_submit``
    flag. A challenge mid-run records the cooldown and stops; a per-user problem (mailbox / vault missing,
    ``stop_run``) stops after the first application instead of failing every one the same way.
    """
    from jobapply.pipeline.apply import applied_today, record_outcome
    from jobapply.repo import UserScope
    from jobapply.switches import global_dry_run, kill_switch_on

    if kill_switch_on(db):
        log.warning("kill switch on, not applying on workday", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(db)
    scope = UserScope(db, user)
    site: UserSite | None = scope.site(SITE)
    if site is None or not site.enabled:
        log.info("workday disabled for user", user=user.username)
        return []
    if throttled(user, site):
        log.info("workday throttled (cooldown after a challenge), skipping", user=user.username)
        return []
    picks = pick_workday_applications(db, user, limit)
    if not picks:
        return []
    outcomes: list[Any] = []
    own_context = context is None and page is None
    if own_context:
        from jobapply.sites.browser import launch_persistent

        context = launch_persistent(pw, user.id, SITE, headless=True)
    wd = WorkdaySession(context, user, db, page=page)
    try:
        if wd.check() != "ok":
            log.warning("workday session throttled", user=user.username)
            return []
        s = get_settings()
        cap = int((user.preferences or {}).get("daily_cap", 40))
        done_today = applied_today(db, user.id)
        random.shuffle(picks)
        for i, (app, job) in enumerate(picks):
            if not dry_run and (done_today >= cap or applied_today(db, user.id, SITE) >= site.daily_cap):
                log.info("daily cap reached", user=user.username, site=SITE)
                break
            if i:
                (between or (lambda pg: _human_pause(pg, 8000, 20000)))(wd.page)
            shot = s.screenshots_dir / str(user.id) / f"{app.id}-{time.strftime('%Y%m%d%H%M%S')}.png"
            try:
                result = apply_job(wd, app, dry_run=dry_run, auto_submit=bool(site.auto_submit), llm=llm,
                                   screenshot_path=shot, client_factory=client_factory, selectors=selectors,
                                   pause=pause, vault=vault)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                outcome = record_outcome(db, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
                db.commit()
                outcomes.append(outcome)
                continue
            fill = ApplyResult(**result).to_fill_result()
            outcome = record_outcome(db, user, app, job, fill)
            db.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info("workday apply outcome", user=user.username, job=job.id, status=outcome.status.value,
                     detail=outcome.detail)
            if result.get("login_status") == "challenge":
                wd.record_challenge()
                _notify(db, user, "paused", f"Workday blocked or challenged the apply run ({result['reason']}); "
                                            f"paused for {DEFAULT_COOLDOWN_HOURS} h.")
                log.warning("workday challenge mid-run, stopping", user=user.username, reason=result["reason"])
                break
            if result.get("stop_run"):
                _notify(db, user, "paused", f"Workday run stopped: {result['reason']}")
                log.warning("workday run stopped", user=user.username, reason=result["reason"])
                break
    finally:
        if own_context:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    return outcomes


def apply_workday(username: str | None = None, limit: int = 3, dry_run: bool = True, engine: Any = None) -> list[dict]:
    """Entry point for a CLI / scheduler task: Workday apply for every active user (or one), within the active
    hours (``JOBAPPLY_WORKDAY_ACTIVE_HOURS`` once settings grow it, else the LinkedIn window), caps and
    switches."""
    from playwright.sync_api import sync_playwright
    from sqlmodel import select

    from jobapply.ai.base import LLMError
    from jobapply.ai.service import get_llm
    from jobapply.db import session_scope
    from jobapply.sites.browser import is_active_hours
    from jobapply.switches import global_dry_run, kill_switch_on

    settings = get_settings()
    hours = getattr(settings, "workday_active_hours", None) or settings.linkedin_active_hours
    if not is_active_hours(spec=hours):
        log.info("outside workday active hours, nothing done", active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as db:
        if kill_switch_on(db):
            log.warning("kill switch on, not applying on workday")
            return []
        dry_run = dry_run or global_dry_run(db)
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        users = db.exec(stmt).all()
        with sync_playwright() as pw:
            for user in users:
                try:
                    llm = get_llm(db, user)
                except LLMError as exc:
                    log.warning("no llm, custom questions may be left for review", user=user.username,
                                error=str(exc))
                    llm = None
                try:
                    outcomes = apply_workday_user(pw, db, user, llm, limit=limit, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop the others
                    log.exception("workday run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


__all__ = ["ApplyResult", "apply_job", "apply_workday_user", "apply_workday", "pick_workday_applications",
           "mailbox_reader", "PROFILE_ORIGIN", "MAIL_TIMEOUT_S", "STATUSES", "STATUS_APPLIED", "STATUS_DRY_RUN",
           "STATUS_NEEDS_HUMAN", "STATUS_PAUSED", "STATUS_SKIPPED", "STATUS_FAILED"]
