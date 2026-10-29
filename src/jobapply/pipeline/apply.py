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
from jobapply.models import Application, AppStatus, Job, User, UserSite, utcnow
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

    app.answers = {**(app.answers or {}), **result.answers}
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
    runner = FormRunner(session, vault, answers=AnswerSource(session, llm, user, job), llm=llm, user=user)
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


__all__ = ["apply_all", "apply_user", "run_one", "record_outcome", "pick_applications", "ASSISTED_SITES", "Path"]
