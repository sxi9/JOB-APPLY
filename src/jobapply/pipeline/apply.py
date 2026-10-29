"""Apply run: fill forms for approved/queued applications, per user, within caps and switches.

Outcome per application:
  dry_run  filled and screenshotted, waiting for a human on the Queue page
  applied  submitted for real (site auto-submit on, global dry-run off)
  paused   captcha, a question needing a human, or required fields left empty
  failed   the page had no form, the filler crashed, or submit reported errors
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobapply.ai.base import LLM, LLMError
from jobapply.ai.service import get_llm
from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.forms.runner import AnswerSource, FillResult, FormRunner, vault_view_for
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, JobSource, User, UserSite, utcnow
from jobapply.notify import notify
from jobapply.repo import UserScope
from jobapply.switches import global_dry_run, kill_switch_on

log = get_logger(__name__)

APPLY_STATUSES = (AppStatus.approved, AppStatus.queued)
# Sites with their own filler running inside the user's logged-in browser profile. `apply_user` leaves these
# alone; `jobapply linkedin apply` (the only entry point until a scheduler task lands) calls
# `jobapply.sites.linkedin.easy_apply`.
ASSISTED_SITES = {"linkedin"}


@dataclass
class ApplyOutcome:
    app_id: int
    status: AppStatus
    detail: str


def site_for_job(job: Job) -> str:
    return job.ats_type or ("email" if job.apply_url.startswith("mailto:") else job.source)


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
        if not job.apply_url or job.apply_url.startswith("mailto:"):
            continue
        out.append((app, job, site))
        if len(out) >= limit:
            break
    if skipped_assisted:
        log.info(
            "assisted-site applications left for their own filler",
            user=user.username, count=skipped_assisted,
            hint="run `jobapply linkedin apply` (no scheduler task yet)",
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
    vault = vault_view_for(user, app)
    answers = AnswerSource(session, llm, user, job, message=app.message or "")
    runner = FormRunner(session, vault, answers=answers, llm=llm, user=user)
    shot = s.screenshots_dir / str(user.id) / f"{app.id}-{utcnow().strftime('%Y%m%d%H%M%S')}.png"
    submit = (
        not dry_run
        and site.auto_submit
        and app.status == AppStatus.approved
        or (not dry_run and site.auto_submit and app.status == AppStatus.queued)
    )
    try:
        result: FillResult = runner.run(
            page, url=job.apply_url, ats_hint=job.ats_type, submit=submit, screenshot_path=shot
        )
    except Exception as exc:  # browser errors: timeouts, navigation failures
        return record_outcome(session, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
    return record_outcome(session, user, app, job, result)


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
    """Run the FormRunner on a LinkedIn job whose apply_url/ats_type have just been resolved to an ATS or
    other external site. A host with no fillable form is paused (apply manually), not failed, so it is not
    retried."""
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
            gov_site = scope.site(resolution.ats_type) if resolution.ats_type else None
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


__all__ = ["apply_all", "apply_user", "run_one", "record_outcome", "pick_applications", "ASSISTED_SITES",
           "apply_external_linkedin", "apply_external_linkedin_user", "pick_external_linkedin_applications",
           "Path"]
