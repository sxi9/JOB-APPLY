"""Apply run: fill forms for approved/queued applications, per user, within caps and switches.

Outcome per application:
  dry_run  filled and screenshotted, waiting for a human on the Queue page
  applied  submitted for real (site auto-submit on, global dry-run off)
  paused   captcha, a question needing a human, or required fields left empty
  failed   the page had no form, the filler crashed, or submit reported errors
"""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobapply.ai.base import LLM, LLMError
from jobapply.ai.service import get_llm
from jobapply.collectors.detector import detect_from_url
from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.forms.fields import FormField
from jobapply.forms.runner import AnswerSource, FillResult, FormRunner, PlannedFill, vault_view_for
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, JobSource, User, UserSite, utcnow
from jobapply.notify import notify
from jobapply.repo import UserScope
from jobapply.switches import global_dry_run, kill_switch_on
from jobapply.users import ACCOUNT_SITES
from jobapply.users import SITES as SITE_DEFAULTS

log = get_logger(__name__)

# Synthetic site key for apply links on hosts that are not a known ATS. The navigator path
# (forms/navigator.py via FormRunner) handles them; this UserSite row carries their daily cap and auto-submit.
EXTERNAL_SITE = "external"
EMAIL_SITE = "email"

APPLY_STATUSES = (AppStatus.approved, AppStatus.queued)
# Sites with their own filler running inside the user's logged-in browser profile. `apply_user` leaves these
# alone: LinkedIn goes through `jobapply linkedin apply` (`sites.linkedin.easy_apply`), the others through
# `apply_assisted` below (`jobapply sites apply -s <site>`), which dispatches to `sites.<site>.apply.apply_job`.
ASSISTED_SITES = {"linkedin", "naukri", "instahyre", "cutshort", "wellfound", *ACCOUNT_SITES}
# Sites whose package exposes the uniform `apply_job(session, application, *, dry_run, auto_submit)`,
# `pick_<site>_applications`, `<Site>Session.check(page)` and `throttled(user, site)` interface. Workday is the
# account site among them (`users.ACCOUNT_SITES`): no cookie import, its package creates the per-company
# account; `COOKIE_SITES` are the ones connected through a cookie export (`sites.connect`).
SITE_APPLIERS = ("naukri", "instahyre", "cutshort", "wellfound", *ACCOUNT_SITES)
COOKIE_SITES = tuple(s for s in SITE_APPLIERS if s not in ACCOUNT_SITES)


@dataclass
class ApplyOutcome:
    app_id: int
    status: AppStatus
    detail: str


def site_for_job(job: Job) -> str:
    return job.ats_type or ("email" if job.apply_url.startswith("mailto:") else job.source)


def account_site_of(job: Job) -> str | None:
    """The account site (`workday`) that owns this job: by `ats_type`, else by the apply URL's host
    (`detect_from_url` wins over whatever the source said), else None."""
    if job.ats_type in ACCOUNT_SITES:
        return job.ats_type
    hit = detect_from_url(job.apply_url or job.url or "")
    return hit.ats if hit is not None and hit.ats in ACCOUNT_SITES else None


def hand_to_account_site(session: Session, user: User, app: Application, job: Job, site: str, *,
                         origin: str) -> ApplyOutcome:
    """A job on an account site's host, met on a generic path: stamp `ats_type`, leave the application where it is
    (approved / queued) and record that the site's own applier owns it. `apply_assisted_user(..., site)`
    (`jobapply sites apply -s <site>`) picks it up with the full gate stack; the generic FormRunner / navigator
    never touches it, so no generic sign-up can run on a Workday tenant."""
    if job.ats_type != site:
        job.ats_type = site
        session.add(job)
        session.flush()
    detail = f"{origin}: {site} job handed to the {site} applier (jobapply sites apply -s {site})"
    UserScope(session, user).set_status(app, app.status, detail)
    log.info("job handed to the account site applier", user=user.username, job=job.id, site=site, origin=origin)
    return ApplyOutcome(app.id, app.status, detail)


def applied_today(session: Session, user_id: int, site: str | None = None) -> int:
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(Application)
        .join(Job, Job.id == Application.job_id)
        .where(Application.user_id == user_id, Application.submitted_at >= today)
    )
    if site:
        # ATS jobs carry ats_type; assisted-site jobs (LinkedIn Easy Apply) have no ATS and keep source = site.
        stmt = stmt.where(or_(Job.ats_type == site, and_(Job.ats_type.is_(None), Job.source == site)))
    return session.exec(stmt).one()


def pick_applications(session: Session, user: User, limit: int) -> list[tuple[Application, Job, UserSite]]:
    """Approved first, then queued by score. Only sites the user enabled; queued rows on sites without
    auto-submit still get a dry run so the Queue page can show the filled form."""
    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    sites = {s.site: s for s in scope.sites()}
    out = []
    skipped_assisted = 0
    for app, job in rows:
        site_key = site_for_job(job)
        if site_key in ASSISTED_SITES:
            skipped_assisted += 1
            continue
        site = sites.get(site_key)
        if site is None or not site.enabled:
            continue
        if not job.apply_url:
            continue
        out.append((app, job, site))  # a mailto: apply link rides along under the "email" site
        if len(out) >= limit:
            break
    if skipped_assisted:
        log.info(
            "assisted-site applications left for their own filler",
            user=user.username, count=skipped_assisted,
            hint="run `jobapply linkedin apply` or `jobapply sites apply -s <site>` (no scheduler task yet)",
        )
    return out


def record_outcome(
    session: Session, user: User, app: Application, job: Job, result: FillResult | None, error: str | None = None
) -> ApplyOutcome:
    """Turn a filler result (or a crash) into the application's status, events and alerts. Shared by the ATS
    `run_one` path and the LinkedIn Easy Apply path."""
    scope = UserScope(session, user)
    if result is None:
        app.error = (error or "filler failed")[:500]
        scope.set_status(app, AppStatus.failed, app.error)
        return ApplyOutcome(app.id, AppStatus.failed, app.error)

    answers = {**(app.answers or {}), **result.answers}
    # AI recovery notes ride along in the answers JSON under a reserved key the Queue page renders separately,
    # so no extra column is needed; replace last run's so stale notes do not linger.
    answers.pop("__ai_recoveries__", None)
    if result.recoveries:
        answers["__ai_recoveries__"] = result.recoveries
    app.answers = answers
    app.screenshot_path = result.screenshot_path
    app.error = None
    detail = result.summary()
    if result.error and not result.plan:
        app.error = result.error
        scope.set_status(app, AppStatus.failed, result.error)
        return ApplyOutcome(app.id, AppStatus.failed, result.error)
    if result.submitted:
        app.submitted_at = utcnow()
        app.follow_up_at = utcnow() + timedelta(days=7)
        scope.set_status(app, AppStatus.applied, detail)
        return ApplyOutcome(app.id, AppStatus.applied, detail)
    if result.paused_reason:
        app.error = "; ".join([result.paused_reason, *result.needs_human[:3], *result.unfilled_required[:3]])[:500]
        scope.set_status(app, AppStatus.paused, detail)
        try:
            notify(session, user, "paused", f"{job.title} at {job.company}: {result.paused_reason}")
        except Exception as exc:  # noqa: BLE001 - a notifier failure must not undo a recorded outcome
            log.warning("notification failed", user=user.username, error=str(exc)[:200])
        return ApplyOutcome(app.id, AppStatus.paused, detail)
    if result.error:
        app.error = result.error
        scope.set_status(app, AppStatus.failed, result.error)
        return ApplyOutcome(app.id, AppStatus.failed, result.error)
    scope.set_status(app, AppStatus.dry_run, detail)
    return ApplyOutcome(app.id, AppStatus.dry_run, detail)


def run_one(
    session: Session, user: User, app: Application, job: Job, site: UserSite, page, llm: LLM | None, dry_run: bool
) -> ApplyOutcome:
    s = get_settings()
    submit = (
        not dry_run
        and site.auto_submit
        and app.status == AppStatus.approved
        or (not dry_run and site.auto_submit and app.status == AppStatus.queued)
    )
    if (job.apply_url or "").lower().startswith("mailto:"):
        return apply_by_email(session, user, app, job, llm, dry_run=dry_run, auto_submit=bool(site.auto_submit))
    owner = account_site_of(job)
    if owner:
        return hand_to_account_site(session, user, app, job, owner, origin=site.site)
    vault = vault_view_for(user, app)
    answers = AnswerSource(session, llm, user, job, message=app.message or "")
    runner = FormRunner(session, vault, answers=answers, llm=llm, user=user)
    shot = s.screenshots_dir / str(user.id) / f"{app.id}-{utcnow().strftime('%Y%m%d%H%M%S')}.png"
    try:
        result: FillResult = runner.run(
            page, url=job.apply_url, ats_hint=job.ats_type, submit=submit, screenshot_path=shot
        )
    except Exception as exc:  # browser errors: timeouts, navigation failures
        return record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
    if result.mailto:
        # The navigator found that the "Apply" control is a mailto: link: apply by email instead.
        job.apply_url = result.mailto
        session.add(job)
        email_site = UserScope(session, user).site(EMAIL_SITE)
        return apply_by_email(session, user, app, job, llm, dry_run=dry_run,
                              auto_submit=bool(email_site is not None and email_site.enabled
                                               and email_site.auto_submit))
    return record_outcome(session, user, app, job, result)


# --------------------------------------------------------------------------- apply by email (mailto: links)


def parse_mailto(url: str) -> tuple[str, str, str]:
    """`(address, subject, body)` from a mailto: link. Only the first address is used."""
    from urllib.parse import parse_qs, unquote, urlparse

    raw = str(url or "").strip()
    if not raw.lower().startswith("mailto:"):
        return "", "", ""
    parsed = urlparse(raw)
    addr = unquote(parsed.path or "").split(",")[0].strip()
    q = parse_qs(parsed.query or "")
    if not addr and q.get("to"):
        addr = q["to"][0].strip()
    subject = (q.get("subject") or [""])[0].strip()
    body = (q.get("body") or [""])[0].strip()
    return addr, subject, body


def _email_plan(to: str, subject: str, body: str, resume: Path | None, draft: Path | None = None) -> list[PlannedFill]:
    rows = [("To", to, "text"), ("Subject", subject, "text"), ("Body", body, "text")]
    if resume is not None:
        rows.append(("Resume", str(resume), "file"))
    if draft is not None:
        rows.append(("Draft", str(draft), "text"))
    out = []
    for i, (label, value, kind) in enumerate(rows):
        f = FormField(i, f"email:{label.lower()}", "input", "textarea", label=label)
        out.append(PlannedFill(f, f"const:{label.lower()}", value, kind, "builtin", 100, done=True))
    return out


def apply_by_email(
    session: Session, user: User, app: Application, job: Job, llm: LLM | None, *, dry_run: bool,
    auto_submit: bool, smtp_factory=None,
) -> ApplyOutcome:
    """Apply to a job whose apply link is `mailto:`: the tailored resume attached, `ai.service.application_message`
    (template without an LLM) as the body. The mail is SENT only when `dry_run` is False AND `auto_submit` is
    on (the "email" site's flag) AND the user's mailbox has SMTP settings; every other case writes the full
    draft as an `.eml` under data/screenshots/<user_id>/ and pauses the application for review."""
    from jobapply.ai.service import application_message
    from jobapply.mail.inbox import mailbox_configs
    from jobapply.mail.smtp import SmtpConfig, SmtpSender

    s = get_settings()
    profile = dict(user.profile or {})
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or user.username
    to, subject, preset_body = parse_mailto(job.apply_url)
    if not to:
        return record_outcome(session, user, app, job, FillResult(
            url=job.apply_url, profile_origin="email", paused_reason="apply by email: the mailto link has no address"))
    job.ats_type = EMAIL_SITE  # so the per-site cap (applied_today(site="email")) counts this application
    session.add(job)
    subject = subject or f"Application for {job.title} at {job.company} - {name}"
    note = application_message(session, llm, user, job, app)
    contact = [str(profile.get(k) or "") for k in ("phone", "linkedin") if profile.get(k)]
    body = "\n".join(filter(None, [
        "Hello,", "", preset_body, note, "", "My resume is attached; I would be glad to share anything else.",
        "", "Best regards,", name, *contact,
    ]))
    resume = Path(app.resume_variant) if app.resume_variant and Path(app.resume_variant).exists() else None
    if resume is None:
        resume = Path(vault_view_for(user, app).resume_path or "")
        resume = resume if str(resume) and resume.exists() else None
    attachments = [resume] if resume is not None else []
    _imap_cfg, smtp_cfg = mailbox_configs(session, user)
    from_addr = str(profile.get("email") or user.email or "")
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    if dry_run or not auto_submit:
        cfg = smtp_cfg or SmtpConfig(host="", username=from_addr, password="", from_name=name)
        msg = SmtpSender(cfg).build(to, subject, body, attachments=attachments)
        draft = s.screenshots_dir / str(user.id) / f"{app.id}-{stamp}.eml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(msg.as_bytes())
        why = "dry run" if dry_run else "auto-submit off for email"
        result = FillResult(url=job.apply_url, profile_origin="email",
                            paused_reason=f"email draft written for review ({why}): {draft.name}",
                            plan=_email_plan(to, subject, body, resume, draft))
        log.info("application email drafted, not sent", user=user.username, job=job.id, to=to, draft=str(draft))
        return record_outcome(session, user, app, job, result)
    if smtp_cfg is None:
        return record_outcome(session, user, app, job, FillResult(
            url=job.apply_url, profile_origin="email",
            paused_reason="mailbox not configured: add the mailbox (imap/smtp) credential to send application emails"))
    try:
        SmtpSender(smtp_cfg, client_factory=smtp_factory).send(to, subject, body, attachments=attachments)
    except Exception as exc:  # noqa: BLE001 - SMTP refused / network
        return record_outcome(session, user, app, job, None, error=f"email send failed: {type(exc).__name__}: {exc}")
    result = FillResult(url=job.apply_url, profile_origin="email", submitted=True,
                        plan=_email_plan(to, subject, body, resume))
    log.info("application email sent", user=user.username, job=job.id, to=to)
    return record_outcome(session, user, app, job, result)


def external_site(session: Session, user: User) -> UserSite:
    """The user's `external` UserSite row (caps / auto-submit for unknown hosts), created from the SITE
    defaults for accounts that predate it."""
    scope = UserScope(session, user)
    row = scope.site(EXTERNAL_SITE)
    if row is None:
        cfg = SITE_DEFAULTS[EXTERNAL_SITE]
        row = UserSite(user_id=user.id, site=EXTERNAL_SITE, daily_cap=cfg["daily_cap"], enabled=True,
                       login_status="n/a")
        session.add(row)
        session.flush()
    return row


def apply_user(
    session: Session, user: User, llm: LLM | None, browser, limit: int = 5, dry_run: bool = True
) -> list[ApplyOutcome]:
    if kill_switch_on(session):
        log.warning("kill switch on, not applying", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(session)
    cap = int((user.preferences or {}).get("daily_cap", 40))
    done_today = applied_today(session, user.id)
    outcomes: list[ApplyOutcome] = []
    context = browser.new_context(
        viewport={"width": 1280, "height": 1800},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    )
    try:
        for app, job, site in pick_applications(session, user, limit):
            if not dry_run and (done_today >= cap or applied_today(session, user.id, site.site) >= site.daily_cap):
                log.info("daily cap reached", user=user.username, site=site.site)
                break
            page = context.new_page()
            try:
                outcome = run_one(session, user, app, job, site, page, llm, dry_run)
            finally:
                page.close()
            session.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info(
                "apply outcome", user=user.username, job=job.id, status=outcome.status.value, detail=outcome.detail
            )
    finally:
        context.close()
    return outcomes


def apply_all(
    username: str | None = None, limit: int = 5, dry_run: bool = True, engine: Engine | None = None
) -> dict[str, list[dict]]:
    from playwright.sync_api import sync_playwright

    s = get_settings()
    results: dict[str, list[dict]] = {}
    with sync_playwright() as pw:
        launch = {"headless": True}
        if s.chromium_executable:
            launch["executable_path"] = s.chromium_executable
        browser = pw.chromium.launch(**launch)
        try:
            with session_scope(engine) as session:
                stmt = select(User).where(User.is_active)
                if username:
                    stmt = stmt.where(User.username == username)
                for user in session.exec(stmt).all():
                    try:
                        llm = get_llm(session, user)
                    except LLMError as exc:
                        log.warning(
                            "no llm, custom questions will be left for review", user=user.username, error=str(exc)
                        )
                        llm = None
                    outcomes = apply_user(session, user, llm, browser, limit=limit, dry_run=dry_run)
                    results[user.username] = [o.__dict__ for o in outcomes]
        finally:
            browser.close()
    return results


# --------------------------------------------------------------------------- LinkedIn offsite routing
#
# A non-Easy-Apply LinkedIn posting arrives with apply_url = linkedin.com/jobs/view/<id> and ats_type None
# (the guest page hides the direct URL behind a sign-in modal), so `pick_applications` skips it as an
# assisted site and `pick_linkedin_applications` skips it as not-Easy-Apply: it never gets applied. This
# path opens each such posting in the user's logged-in LinkedIn profile, resolves the real apply URL, writes
# it (and the detected ATS) back onto the Job, and runs the normal FormRunner on it. A posting that turns
# out to be Easy Apply is flagged so the Easy Apply filler takes it; one that resolves to LinkedIn itself or
# a host with no fillable form is paused for a human and never retried.

LINKEDIN_SITE = "linkedin"
NO_FORM_ERROR = "no form fields found on the page"


def _launch_persistent_linkedin(pw, user_id: int):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, LINKEDIN_SITE)


def _linkedin_session(context, user: User, session: Session):
    from jobapply.sites.linkedin.session import LinkedInSession

    return LinkedInSession(context, user, session)


def _linkedin_throttled(user: User, site: UserSite) -> bool:
    from jobapply.sites.linkedin.session import throttled

    return bool(throttled(user, site))


def _resolve_external_url(page, job_url: str):
    from jobapply.sites.linkedin.resolve import resolve_external_url

    return resolve_external_url(page, job_url)


def _is_linkedin_easy_apply(session: Session, job: Job) -> bool:
    sources = session.exec(
        select(JobSource).where(JobSource.job_id == job.id, JobSource.source == LINKEDIN_SITE)
    ).all()
    return any((src.raw or {}).get("easy_apply") for src in sources)


def _mark_linkedin_easy_apply(session: Session, job: Job) -> None:
    """Flag the LinkedIn source row as Easy Apply so the Easy Apply filler picks it up next run."""
    src = session.exec(
        select(JobSource).where(JobSource.job_id == job.id, JobSource.source == LINKEDIN_SITE)
    ).first()
    if src is not None:
        raw = dict(src.raw or {})
        raw["easy_apply"] = True
        src.raw = raw
        session.add(src)


def pick_external_linkedin_applications(
    session: Session, user: User, limit: int
) -> list[tuple[Application, Job]]:
    """Approved then queued applications on LinkedIn postings that are NOT Easy Apply and have no resolved
    ATS apply URL yet (ats_type None, still a /jobs/view link)."""
    scope = UserScope(session, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != LINKEDIN_SITE:
            continue  # already resolved to an ATS, or not a LinkedIn job
        if not job.apply_url or "/jobs/view/" not in job.apply_url:
            continue
        if _is_linkedin_easy_apply(session, job):
            continue  # the Easy Apply filler handles these
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


def _paused_result(job: Job, reason: str) -> FillResult:
    return FillResult(url=job.apply_url, profile_origin="none", paused_reason=reason)


def _run_resolved_external(
    session: Session, user: User, app: Application, job: Job, gov_site: UserSite | None,
    page, llm: LLM | None, dry_run: bool
) -> ApplyOutcome:
    """Run the FormRunner on a job whose apply_url/ats_type have just been resolved to an ATS or any other
    external site. An unknown host goes through the navigator (`gov_site` is then the synthetic `external`
    site, see `external_site`); a page where no form can be reached is paused with the navigator's reason
    (captcha, account wall, no form after N hops), not failed, so it is not retried blindly. A mailto: apply
    link the navigator finds is applied to by email. A host owned by an account site (a Workday tenant) is
    handed to that site's applier instead: the navigator's generic sign-up never runs there."""
    owner = account_site_of(job)
    if owner:
        return hand_to_account_site(session, user, app, job, owner,
                                    origin=gov_site.site if gov_site is not None else "resolved")
    s = get_settings()
    vault = vault_view_for(user, app)
    answers = AnswerSource(session, llm, user, job, message=app.message or "")
    runner = FormRunner(session, vault, answers=answers, llm=llm, user=user)
    shot = s.screenshots_dir / str(user.id) / f"{app.id}-{utcnow().strftime('%Y%m%d%H%M%S')}.png"
    # A real submit needs the resolved site enabled AND auto-submit on, mirroring pick_applications, which
    # skips sites where `not site.enabled`: disabling a site must stop real submits through this path too.
    submit = not dry_run and gov_site is not None and gov_site.enabled and gov_site.auto_submit
    try:
        result: FillResult = runner.run(
            page, url=job.apply_url, ats_hint=job.ats_type, submit=submit, screenshot_path=shot
        )
    except Exception as exc:  # browser errors: timeouts, navigation failures
        return record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
    if result.mailto:
        job.apply_url = result.mailto
        session.add(job)
        email_site = UserScope(session, user).site(EMAIL_SITE)
        return apply_by_email(session, user, app, job, llm, dry_run=dry_run,
                              auto_submit=bool(email_site is not None and email_site.enabled
                                               and email_site.auto_submit))
    if not result.plan and not result.submitted and result.error == NO_FORM_ERROR:
        result.error = ""
        result.paused_reason = "no application form found on the company site: apply manually"
    return record_outcome(session, user, app, job, result)


def apply_external_linkedin_user(
    pw, session: Session, user: User, llm: LLM | None = None, limit: int = 3, dry_run: bool = True
) -> list[ApplyOutcome]:
    """Resolve and apply to one user's non-Easy-Apply LinkedIn postings inside their logged-in profile.

    Honours the kill switch, the global dry-run switch and the per-user / per-site daily caps itself; skips a
    disabled or throttled LinkedIn site; an unclear session is retried later, an expired/challenged one alerts
    and stops with no applications.
    """
    if kill_switch_on(session):
        log.warning("kill switch on, not resolving linkedin jobs", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(session)
    cap = int((user.preferences or {}).get("daily_cap", 40))
    done_today = applied_today(session, user.id)
    scope = UserScope(session, user)
    site = scope.site(LINKEDIN_SITE)
    if site is None or not site.enabled:
        log.info("linkedin disabled for user", user=user.username)
        return []
    if _linkedin_throttled(user, site):
        log.info("linkedin throttled, skipping external resolve", user=user.username)
        return []
    picks = pick_external_linkedin_applications(session, user, limit)
    if not picks:
        return []
    outcomes: list[ApplyOutcome] = []
    context = _launch_persistent_linkedin(pw, user.id)
    try:
        li = _linkedin_session(context, user, session)
        page = context.new_page()
        status = li.check(page)
        if status == "unknown":
            log.warning("linkedin session state unclear, retry later", user=user.username)
            return []
        if status != "ok":
            msg = (f"LinkedIn login needed ({status}) to resolve offsite jobs: "
                   f"run jobapply linkedin login -u {user.username}")
            try:
                notify(session, user, "paused", msg)
            except Exception as exc:  # noqa: BLE001 - a notifier failure must not break the run
                log.warning("notification failed", user=user.username, error=str(exc)[:200])
            log.warning("linkedin session not ok", user=user.username, status=status)
            return []
        for app, job in picks:
            if not dry_run and done_today >= cap:
                log.info("daily cap reached, stopping linkedin external run", user=user.username, cap=cap)
                break
            try:
                resolution = _resolve_external_url(page, job.apply_url)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                outcome = record_outcome(session, user, app, job, None,
                                         error=f"{type(exc).__name__}: {exc}")
                outcomes.append(outcome)
                session.commit()
                continue
            if resolution.kind == "easy_apply":
                _mark_linkedin_easy_apply(session, job)
                session.commit()
                log.info("linkedin job is easy apply, left for the easy apply filler",
                         user=user.username, job=job.id)
                continue
            if not resolution.resolved:
                outcome = record_outcome(
                    session, user, app, job,
                    _paused_result(job, "could not resolve an external apply URL: apply on LinkedIn manually"),
                )
                outcomes.append(outcome)
                session.commit()
                log.info("linkedin job not resolvable, paused", user=user.username, job=job.id,
                         kind=resolution.kind)
                continue
            job.apply_url = resolution.apply_url
            job.ats_type = resolution.ats_type
            session.add(job)
            session.flush()
            # A known ATS is governed by its own site row; any other host by the synthetic `external` site,
            # and the navigator finds its form (no more "apply manually" for unknown hosts).
            gov_site = scope.site(resolution.ats_type) if resolution.ats_type else external_site(session, user)
            # Per-site daily cap: once the resolved site is at its cap, fill for review (dry run) instead of
            # submitting for real, so this path cannot exceed site.daily_cap. The per-user cap is checked at
            # the top of the loop. Mirrors apply_user's cap handling (lines 174-185).
            site_capped = (
                not dry_run and gov_site is not None
                and applied_today(session, user.id, gov_site.site) >= gov_site.daily_cap
            )
            if site_capped:
                log.info("site daily cap reached, filling for review only", user=user.username,
                         site=gov_site.site)
            outcome = _run_resolved_external(session, user, app, job, gov_site, page, llm,
                                             dry_run or site_capped)
            session.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info("linkedin external apply outcome", user=user.username, job=job.id,
                     ats=resolution.ats_type, status=outcome.status.value, detail=outcome.detail)
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
    return outcomes


def apply_external_linkedin(
    username: str | None = None, limit: int = 3, dry_run: bool = True, engine: Engine | None = None
) -> list[dict]:
    """`jobapply` entry point: resolve and apply to non-Easy-Apply LinkedIn postings for every active user
    (or one), within the LinkedIn active hours, caps and switches."""
    from playwright.sync_api import sync_playwright

    from jobapply.sites.browser import is_active_hours

    hours = get_settings().linkedin_active_hours
    if not is_active_hours(spec=hours):
        log.info("outside linkedin active hours, nothing done", active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as session:
        if kill_switch_on(session):
            log.warning("kill switch on, not resolving linkedin jobs")
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
                    outcomes = apply_external_linkedin_user(pw, session, user, llm, limit=limit,
                                                            dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop others
                    log.exception("linkedin external run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


# --------------------------------------------------------------------------- other assisted sites
#
# Naukri, Instahyre, Cutshort and Wellfound share one interface (see SITE_APPLIERS): `apply_job(session,
# application, *, dry_run, auto_submit) -> {"status", "reason", "screenshot", ...}` inside the user's persistent
# headless profile. This section is the one gate stack in front of all of them, mirroring the LinkedIn paths:
# kill switch, global dry-run, site enabled, challenge cooldown, session check, per-user and per-site daily
# caps, the site's auto_submit flag (passed through; every apply_job stops before its submit click when it is
# off) and the site's active hours (in `apply_assisted`). A challenge or logout seen mid-run ends the user's
# run and is never retried here. A Naukri "apply on company site" result is re-routed through the same
# resolved-ATS path the LinkedIn offsite flow uses.

# Matched against the HEAD of a paused result's reason only (the part before the first `;` / `:`): the tail
# carries user-facing question text ("chatbot question needs you: Describe a challenge you solved"), which
# must never read as a site challenge. The alternatives are the fixed strings the site appliers emit.
_CHALLENGE_REASON = re.compile(
    r"captcha|security check|challenge page|challenged|unusual activity|\botp\b|answered \d+|blocked by|"
    r"rate[ -]?limited", re.I)
_LOGIN_REASON = re.compile(r"login (?:needed|required)|session expired|not logged in|sign in", re.I)
STOP_STATUSES = ("paused", "needs_human")
# A `skipped` result that names a run-level gate ends the run; any other skip (e.g. Naukri's "already applied")
# is per job and is booked as paused so the application leaves the pick set. A site may also say `gate: True`.
_GATE_REASON = re.compile(
    r"kill switch|dry[ -]?run|disabled|cooldown|throttled|daily cap|cap reached|active hours|session (?:login|"
    r"challenge|unknown|expired)", re.I)


def _reason_head(reason: str) -> str:
    return re.split(r"[;:]", reason, maxsplit=1)[0]


def is_gate_skip(result: dict) -> bool:
    """True when a `skipped` apply_job result comes from a gate (switch, cap, hours, cooldown), not the job."""
    if result.get("gate") is not None:
        return bool(result.get("gate"))
    return bool(_GATE_REASON.search(str(result.get("reason") or "")))


def site_registered(result: dict) -> bool:
    """True when the site took the application even though the result is not `applied`: Naukri's Apply click IS
    the submit (`clicked_apply`), Instahyre's interest toast was seen (`interest_sent`). Such outcomes count
    toward the daily caps."""
    return bool(result.get("clicked_apply") or result.get("interest_sent"))


def _site_module(site: str, name: str):
    if site not in SITE_APPLIERS:
        raise ValueError(f"not an assisted site with a uniform applier: {site!r}")
    return importlib.import_module(f"jobapply.sites.{site}.{name}")


def _launch_persistent_site(pw, user_id: int, site: str):
    from jobapply.sites.browser import launch_persistent

    return launch_persistent(pw, user_id, site, headless=True)  # this machine only runs headless Chromium


def _site_session(site: str, context, user: User, session: Session, llm: LLM | None = None):
    """`<Site>Session(context, user, db)`; the LLM is handed over when the class takes one (Wellfound)."""
    cls = getattr(_site_module(site, "session"), f"{site.capitalize()}Session")
    params = inspect.signature(cls).parameters
    return cls(context, user, session, llm=llm) if "llm" in params else cls(context, user, session)


def _site_throttled(site: str, user: User, user_site: UserSite | None) -> bool:
    return bool(_site_module(site, "session").throttled(user, user_site))


def _site_pick(site: str, session: Session, user: User, limit: int) -> list[tuple[Application, Job]]:
    return getattr(_site_module(site, "apply"), f"pick_{site}_applications")(session, user, limit)


def _site_apply_job(site: str, site_session, application: Application, *, dry_run: bool, auto_submit: bool,
                    llm: LLM | None = None) -> dict:
    fn = _site_module(site, "apply").apply_job
    extra = {"llm": llm} if "llm" in inspect.signature(fn).parameters else {}
    return fn(site_session, application, dry_run=dry_run, auto_submit=auto_submit, **extra)


def _between_applications(page) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, 8000, 20000)


def site_active_hours(site: str) -> str:
    """`JOBAPPLY_<SITE>_ACTIVE_HOURS`, falling back to the LinkedIn window."""
    s = get_settings()
    return str(getattr(s, f"{site}_active_hours", None) or s.linkedin_active_hours)


def fill_result_from(result: dict, job: Job, site: str) -> FillResult:
    """Translate a site `apply_job` dict (status / reason / screenshot / answers ...) into the `FillResult`
    `record_outcome` consumes. A site may hand its own FillResult over under `result`."""
    inner = result.get("result")
    if isinstance(inner, FillResult):
        return inner
    fr = FillResult(url=str(result.get("url") or job.apply_url or ""), profile_origin=f"builtin:{site}",
                    screenshot_path=result.get("screenshot"),
                    needs_human=[str(x) for x in (result.get("needs_human") or [])],
                    unfilled_required=[str(x) for x in (result.get("unfilled_required") or [])])
    answers = result.get("answers") or {}
    if isinstance(answers, dict):
        for idx, (question, value) in enumerate(answers.items()):
            f = FormField(idx, f"{site}:{idx}", "textarea", "textarea", label=str(question))
            fr.plan.append(PlannedFill(f, "answers." + str(question)[:80], value, "text", "builtin", 100, done=True))
            fr.fields_total += 1
    status, reason = str(result.get("status") or ""), str(result.get("reason") or "")
    if status == "applied" or result.get("submitted"):
        fr.submitted = True
    elif status == "dry_run":
        pass
    elif status in ("paused", "needs_human", "skipped", "external"):
        fr.paused_reason = reason or status
    else:
        fr.error = reason or f"{site} apply failed"
    return fr


def stop_reason_for(result: dict) -> str:
    """'challenge' | 'login' | '' from an apply_job result: the whole run stops on these, not just the job."""
    login_status = str(result.get("login_status") or "")
    if login_status in ("challenge", "login"):
        return login_status
    if str(result.get("status") or "") not in STOP_STATUSES:
        return ""
    head = _reason_head(str(result.get("reason") or ""))
    if _CHALLENGE_REASON.search(head):
        return "challenge"
    if _LOGIN_REASON.search(head):
        return "login"
    return ""


def route_external_result(
    session: Session, user: User, app: Application, job: Job, result: dict, site: str, *,
    context=None, llm: LLM | None = None, dry_run: bool = True,
) -> ApplyOutcome | None:
    """A site answered "apply on the company site": write the resolved URL / ATS onto the job and send it
    down the resolved-site path (`_run_resolved_external`, the LinkedIn offsite flow) when a browser context
    is at hand: a known ATS under its own site row, any other host under the synthetic `external` site with the
    navigator finding the form. Without a context a known ATS is left queued for the ATS filler, and an
    unknown host (or a URL only revealed by a click that was not made) is paused for a human."""
    url = str(result.get("apply_url") or "")
    ats = result.get("ats_type")
    if url and not ats:
        hit = detect_from_url(url)
        ats = hit.ats if hit else None
    if url:
        job.apply_url = url
        job.ats_type = ats
        session.add(job)
        session.flush()
    scope = UserScope(session, user)
    if url and ats in ACCOUNT_SITES:
        # A Workday tenant: never the FormRunner / navigator. Its own applier (jobapply sites apply -s workday)
        # creates the per-company account and walks the wizard under the same gate stack as this run.
        hand_to_account_site(session, user, app, job, ats, origin=site)
        return None
    gov_site = scope.site(ats) if ats and ats not in ASSISTED_SITES else None
    if url and gov_site is None and not ats and context is not None:
        gov_site = external_site(session, user)  # unknown host: the navigator path
    if url and gov_site is not None:
        if context is not None:
            site_capped = (not dry_run and applied_today(session, user.id, gov_site.site) >= gov_site.daily_cap)
            page = context.new_page()
            try:
                outcome = _run_resolved_external(session, user, app, job, gov_site, page, llm, dry_run or site_capped)
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("external apply routed through the ats path", user=user.username, job=job.id, ats=ats,
                     status=outcome.status.value, site=site)
            return outcome
        scope.set_status(app, app.status, f"{site}: apply on company site -> {ats} ({url}); handed to the ATS filler")
        log.info("external apply left for the ats filler", user=user.username, job=job.id, ats=ats, site=site)
        return None
    reason = (f"apply on company site manually: {url}" if url
              else "apply on company site: URL only revealed by clicking (not clicked)")
    fill = FillResult(url=job.apply_url, profile_origin=f"builtin:{site}", paused_reason=reason,
                      screenshot_path=result.get("screenshot"))
    return record_outcome(session, user, app, job, fill)


def _notify_safe(session: Session, user: User, kind: str, message: str) -> None:
    try:
        notify(session, user, kind, message)
    except Exception as exc:  # noqa: BLE001 - a notifier failure must not break the run
        log.warning("notification failed", user=user.username, error=str(exc)[:200])


def apply_assisted_user(
    pw, session: Session, user: User, site: str, llm: LLM | None = None, limit: int = 3, dry_run: bool = True,
) -> list[ApplyOutcome]:
    """One user's run on one assisted site inside their persistent headless profile. Returns ApplyOutcome rows.

    Honours the kill switch and the global dry-run switch itself, so any caller (CLI, scheduler, web) gets the
    same guarantees as the ATS path; a disabled or throttled site is skipped; an unclear session is retried
    later, an expired or challenged one alerts and stops with no applications. `site.auto_submit` is passed to
    the site's `apply_job`, which stops before its submit click (and screenshots) when the flag is off.
    """
    if site not in SITE_APPLIERS:
        raise ValueError(f"not an assisted site with a uniform applier: {site!r}")
    if kill_switch_on(session):
        log.warning("kill switch on, not applying", user=user.username, site=site)
        return []
    dry_run = dry_run or global_dry_run(session)
    scope = UserScope(session, user)
    user_site = scope.site(site)
    if user_site is None or not user_site.enabled:
        log.info("site disabled for user", user=user.username, site=site)
        return []
    if _site_throttled(site, user, user_site):
        log.info("site throttled (cooldown after a challenge), skipping", user=user.username, site=site)
        return []
    picks = _site_pick(site, session, user, limit)
    if not picks:
        return []
    cap = int((user.preferences or {}).get("daily_cap", 40))
    done_today = applied_today(session, user.id)
    outcomes: list[ApplyOutcome] = []
    context = _launch_persistent_site(pw, user.id, site)
    try:
        site_session = _site_session(site, context, user, session, llm)
        page = context.new_page()
        status = site_session.check(page)
        if status == "unknown":
            log.warning("session state unclear, retry later", user=user.username, site=site)
            return []
        if status != "ok":
            if site in ACCOUNT_SITES:
                msg = (f"{site} is in a challenge cooldown ({status}); the run is retried after it ends "
                       "(accounts are created per company, nothing to import)")
            else:
                hint = f"export {site} cookies from a logged-in browser and import them (Settings -> Sites)"
                msg = (f"{site} is showing a security check / OTP: complete it in your browser, then {hint}"
                       if status == "challenge" else f"{site} login needed ({status}): {hint}")
            _notify_safe(session, user, "paused", msg)
            log.warning("session not ok", user=user.username, site=site, status=status)
            return []
        for i, (app, job) in enumerate(picks):
            if not dry_run and (done_today >= cap or applied_today(session, user.id, site) >= user_site.daily_cap):
                log.info("daily cap reached", user=user.username, site=site)
                break
            if i:
                _between_applications(page)
            try:
                result = _site_apply_job(site, site_session, app, dry_run=dry_run,
                                         auto_submit=bool(user_site.auto_submit), llm=llm)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                outcome = record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
                session.commit()
                outcomes.append(outcome)
                continue
            result = dict(result or {})
            result_status = str(result.get("status") or "")
            if result_status == "skipped" and is_gate_skip(result):
                # a gate inside apply_job (cap, hours, switch) said no: nothing to record, stop the run
                log.info("apply skipped by a site gate", user=user.username, site=site, job=job.id,
                         reason=result.get("reason"))
                break
            if result_status == "external":
                outcome = route_external_result(session, user, app, job, result, site, context=context, llm=llm,
                                                dry_run=dry_run)
                session.commit()
                if outcome is not None:
                    outcomes.append(outcome)
                    if outcome.status == AppStatus.applied:
                        done_today += 1  # a real submit on the resolved ATS counts toward the user's cap
                continue
            outcome = record_outcome(session, user, app, job, fill_result_from(result, job, site))
            if outcome.status == AppStatus.applied:
                done_today += 1
            elif site_registered(result):
                # The site already took the application (post-click chatbot / questionnaire left for a human):
                # stamp it so applied_today and the caps count it even though the status stays paused.
                app.submitted_at = utcnow()
                session.add(app)
                done_today += 1
                log.info("application registered on the site, counted toward the caps", user=user.username,
                         site=site, job=job.id, status=outcome.status.value)
            session.commit()
            outcomes.append(outcome)
            log.info("assisted apply outcome", user=user.username, site=site, job=job.id,
                     status=outcome.status.value, detail=outcome.detail)
            stop = stop_reason_for(result)
            if stop == "challenge":
                record_challenge = getattr(site_session, "record_challenge", None)
                if callable(record_challenge):
                    record_challenge()
                session.commit()
                _notify_safe(session, user, "paused",
                             f"{site} blocked or challenged the apply run ({result.get('reason')}); paused.")
                log.warning("challenge mid-run, stopping", user=user.username, site=site)
                break
            if stop == "login":
                user_site.login_status = "expired"
                session.add(user_site)
                session.commit()
                _notify_safe(session, user, "paused",
                             f"{site} session expired mid-run: re-import the cookies for {user.username}")
                log.warning("session expired mid-run, stopping", user=user.username, site=site)
                break
            if result.get("stop_run"):
                # A per-user problem every job of this run would hit the same way (Workday: mailbox / vault not
                # configured, no profile email): book this one, alert once, stop; never loop over the queue.
                _notify_safe(session, user, "paused", f"{site} run stopped: {result.get('reason')}")
                log.warning("run stopped by the site", user=user.username, site=site, reason=result.get("reason"))
                break
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
    return outcomes


def apply_assisted(
    site: str, username: str | None = None, limit: int = 3, dry_run: bool = True, engine: Engine | None = None,
) -> list[dict]:
    """`jobapply sites apply -s <site>` entry point: one assisted site for every active user (or one), within
    that site's active hours, caps and switches."""
    from playwright.sync_api import sync_playwright

    from jobapply.sites.browser import is_active_hours

    if site not in SITE_APPLIERS:
        raise ValueError(f"not an assisted site with a uniform applier: {site!r}")
    hours = site_active_hours(site)
    if not is_active_hours(spec=hours):
        log.info("outside active hours, nothing done", site=site, active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as session:
        if kill_switch_on(session):
            log.warning("kill switch on, not applying", site=site)
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
                    outcomes = apply_assisted_user(pw, session, user, site, llm, limit=limit, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop others
                    log.exception("assisted run failed", user=user.username, site=site, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, "site": site, **o.__dict__} for o in outcomes)
    return results


def apply_assisted_all(
    username: str | None = None, limit: int = 3, dry_run: bool = True, engine: Engine | None = None,
) -> dict[str, list[dict]]:
    """Every uniform assisted site in turn (each inside its own active hours / caps / switches)."""
    return {site: apply_assisted(site, username, limit=limit, dry_run=dry_run, engine=engine)
            for site in SITE_APPLIERS}


__all__ = ["apply_all", "apply_user", "run_one", "record_outcome", "pick_applications", "ASSISTED_SITES",
           "SITE_APPLIERS", "ACCOUNT_SITES", "COOKIE_SITES", "account_site_of", "hand_to_account_site",
           "apply_external_linkedin", "apply_external_linkedin_user",
           "pick_external_linkedin_applications", "apply_assisted", "apply_assisted_user", "apply_assisted_all",
           "fill_result_from", "route_external_result", "stop_reason_for", "site_active_hours", "Path",
           "apply_by_email", "parse_mailto", "external_site", "EXTERNAL_SITE", "EMAIL_SITE"]
