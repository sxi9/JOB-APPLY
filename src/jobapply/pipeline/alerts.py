"""WhatsApp alerts for new jobs and apply outcomes, and the per-user apply mode.

Mode (`preferences.apply_mode`): `notify` (the default) means the pipeline only collects, filters and scores
for the user and posts the matches; nothing is ever filled or submitted. `auto` is the historical behaviour
(every gate intact: kill switch, global dry run, site enabled / auto-submit, caps, active hours) plus an
outcome alert per application. Every apply picker / run function asks `notify_only_skip` first.

New-job alerts (`notify_new_jobs`): per target (the number, the group), the user's applications that have no
`notified` event for that target yet, newest posted first, at most 8 jobs per message and 5 messages per
target and run. A batch is *claimed* with its `notified` events (detail `whatsapp:number` / `whatsapp:group`,
unique per application and target) before the send and released again when the send fails, so a job is never
posted twice - not even by two overlapping runs - and a failed send is retried next run for that target only.
Only applications created in the last `NEW_JOB_WINDOW` are considered, so switching the alerts on does not
replay a whole history. Times are shown in IST (Asia/Kolkata).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.logs import get_logger
from jobapply.models import Application, ApplicationEvent, AppStatus, Job, User, utcnow
from jobapply.notify import alert_target_map, alert_targets, send_to

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
APPLY_MODES = ("notify", "auto")
DEFAULT_APPLY_MODE = "notify"
NOTIFIED_KIND = "notified"
OUTCOME_KIND = "outcome"
BATCH_SIZE = 8
MAX_MESSAGES_PER_RUN = 5
TITLE_MAX = 90
NEW_JOB_WINDOW = timedelta(hours=24)
SKIP_LOG_INTERVAL = 600.0  # seconds between "notify-only, skipping" log lines per user and path

NEW = "\U0001f195"  # 🆕
CLOCK = "\U0001f552"  # 🕒
CLIPBOARD = "\U0001f4cb"  # 📋
OUTCOME_ICONS = {
    AppStatus.applied: "✅ applied",  # ✅
    AppStatus.dry_run: "\U0001f4dd queued for your approval",  # 📝
    AppStatus.queued: "\U0001f4dd queued for your approval",
    AppStatus.paused: "⏸ paused",  # ⏸
    AppStatus.failed: "❌ failed",  # ❌
}


# ----------------------------------------------------------------- mode


def apply_mode(user: User) -> str:
    mode = str((user.preferences or {}).get("apply_mode") or DEFAULT_APPLY_MODE)
    return mode if mode in APPLY_MODES else DEFAULT_APPLY_MODE


def notify_only(user: User) -> bool:
    return apply_mode(user) != "auto"


_skip_logged: dict[tuple[int | None, str], float] = {}


def notify_only_skip(user: User, where: str) -> bool:
    """True when this user is in notify-only mode (so the caller applies nothing); logged once per user and
    path every `SKIP_LOG_INTERVAL` seconds, not for every picker call of a run."""
    if not notify_only(user):
        return False
    key = (user.id, where)
    now = time.monotonic()
    if now - _skip_logged.get(key, -SKIP_LOG_INTERVAL) >= SKIP_LOG_INTERVAL:
        _skip_logged[key] = now
        log.info("notify-only mode: nothing is applied for this user", user=user.username, where=where,
                 hint="switch to automatic in Settings -> Notifications")
    return True


# ----------------------------------------------------------------- formatting


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def fmt_ist(value: datetime) -> str:
    """`14:05 IST, 05 Nov`."""
    return _aware(value).astimezone(IST).strftime("%H:%M IST, %d %b")


def relative_age(when: datetime | None, now: datetime | None = None) -> str:
    """`just now`, `12 min ago`, `3 h ago`, `2 d ago` (UTC in, locale-free out)."""
    if when is None:
        return "unknown age"
    now = _aware(now) or utcnow()
    seconds = int((now - _aware(when)).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def clean_title(title: str, limit: int = TITLE_MAX) -> str:
    """One line, no WhatsApp markup characters, at most `limit` characters (ellipsis included)."""
    text = re.sub(r"\s+", " ", str(title or "")).replace("*", "").replace("_", " ").replace("~", "").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "(untitled)"


def job_link(job: Job) -> str:
    return f"{get_settings().public_url.rstrip('/')}/jobs/{job.id}"


def job_block(job: Job, now: datetime | None = None, *, dashboard_link: bool = True) -> str:
    """The lines for one job: bold title / company / location, age + IST time + source, the apply URL alone
    on its line (WhatsApp makes a bare URL clickable), the dashboard link (`dashboard_link=False` leaves it
    out: group feeds, whose readers may have no account)."""
    when = _aware(job.posted_at) or _aware(job.first_seen_at)
    location = (job.location or "").strip() or ("Remote" if job.remote else "location n/a")
    company = re.sub(r"\s+", " ", job.company or "").strip() or "unknown company"
    lines = [f"*{clean_title(job.title)}* — {company} · {location}"]
    stamp = f"posted {relative_age(when, now)}" + (f" ({fmt_ist(when)})" if when else "")
    lines.append(f"{CLOCK} {stamp} · via {job.source or job.ats_type or 'unknown'}")
    url = (job.apply_url or job.url or "").strip()
    if url and not url.lower().startswith("mailto:"):
        lines.append(url)
    elif url:
        lines.append(f"apply by email: {url[len('mailto:'):].split('?')[0]}")
    if dashboard_link:
        lines.append(f"{CLIPBOARD} {job_link(job)}")
    return "\n".join(lines)


def header(count: int, now: datetime) -> str:
    noun = "new job" if count == 1 else "new jobs"
    return f"{NEW} {count} {noun} — {fmt_ist(now)}"


def build_messages(jobs: list[Job], now: datetime | None = None, *, batch_size: int = BATCH_SIZE) -> list[str]:
    """Batches of at most `batch_size` jobs in the given order, each with its own header."""
    now = _aware(now) or utcnow()
    out: list[str] = []
    for i in range(0, len(jobs), batch_size):
        chunk = jobs[i:i + batch_size]
        out.append(header(len(chunk), now) + "\n\n" + "\n\n".join(job_block(j, now) for j in chunk))
    return out


# ----------------------------------------------------------------- new-job alerts


def _posted_key(job: Job) -> datetime:
    return _aware(job.posted_at) or _aware(job.first_seen_at) or datetime.min.replace(tzinfo=UTC)


def event_detail(target: str) -> str:
    """The `notified` event detail for a target kind: `whatsapp:number` / `whatsapp:group` (never the address)."""
    return f"whatsapp:{target}"


def pending_new_jobs(session: Session, user: User, now: datetime | None = None,
                     target: str | None = None) -> list[tuple[Application, Job]]:
    """This user's applications (any status) created inside `NEW_JOB_WINDOW` that carry no `notified` event
    for `target` ("number" / "group"; None: for any target) and reach the user's `new_jobs_min_score`,
    newest posted first. Per target, a job delivered to the number but not to the group is still pending
    for the group."""
    now = _aware(now) or utcnow()
    prefs = user.preferences or {}
    try:
        min_score = int(prefs.get("new_jobs_min_score") or 0)
    except (TypeError, ValueError):
        min_score = 0
    notified = select(ApplicationEvent.application_id).where(ApplicationEvent.user_id == user.id,
                                                             ApplicationEvent.kind == NOTIFIED_KIND)
    if target is not None:
        notified = notified.where(ApplicationEvent.detail == event_detail(target))
    stmt = (
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.user_id == user.id, Application.created_at >= now - NEW_JOB_WINDOW,
               Application.id.notin_(notified))
    )
    rows = list(session.exec(stmt).all())
    if min_score > 0:
        rows = [(a, j) for a, j in rows if a.score is not None and a.score >= min_score]
    rows.sort(key=lambda r: _posted_key(r[1]), reverse=True)
    return rows


def new_jobs_enabled(user: User) -> bool:
    return bool((user.preferences or {}).get("new_jobs_enabled"))


def _batches(rows: list[tuple[Application, Job]], user: User) -> list[list[tuple[Application, Job]]]:
    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    if len(batches) > MAX_MESSAGES_PER_RUN:
        dropped = sum(len(b) for b in batches[MAX_MESSAGES_PER_RUN:])
        log.warning("new job alerts capped for this run; the rest go out next run", user=user.username,
                    cap=MAX_MESSAGES_PER_RUN, dropped_jobs=dropped)
        batches = batches[:MAX_MESSAGES_PER_RUN]
    return batches


def _claim(session: Session, user: User, batch: list[tuple[Application, Job]], target: str) -> bool:
    """Write (and commit) the batch's `notified` events for this target BEFORE the send. The unique index
    on (application_id, detail) makes a concurrent run (collect and collect_hot share one process; a CLI
    run is another) fail here instead of posting the same jobs again; False means it lost the race."""
    detail = event_detail(target)
    for app, _job in batch:
        session.add(ApplicationEvent(application_id=app.id, user_id=user.id, kind=NOTIFIED_KIND, detail=detail))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        log.info("new job batch already claimed by another run, skipping it", user=user.username,
                 jobs=len(batch), target=target)
        return False
    return True


def _release(session: Session, user: User, batch: list[tuple[Application, Job]], target: str) -> None:
    """Drop the claim again after a failed send so the batch is retried next run."""
    ids = [app.id for app, _job in batch]
    session.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(ids),
                                                   ApplicationEvent.user_id == user.id,
                                                   ApplicationEvent.kind == NOTIFIED_KIND,
                                                   ApplicationEvent.detail == event_detail(target)))
    session.commit()


def notify_new_jobs(session: Session, user: User, *, now: datetime | None = None, dry_run: bool = False) -> list[str]:
    """Post this user's not-yet-notified matches to their WhatsApp target(s), each target getting the jobs it
    has not received yet (with `both`, a job the number got but the group did not goes to the group only).
    Returns the distinct messages (all of them with `dry_run`, otherwise the delivered ones). A batch is
    claimed with its `notified` events before the send (`_claim`) and released when the send fails, so it
    is retried next run for that target and never posted twice."""
    if not new_jobs_enabled(user):
        log.debug("new job alerts off", user=user.username)
        return []
    now = _aware(now) or utcnow()
    targets = alert_target_map(user)
    if not targets:
        if dry_run:  # a preview still works without a target
            batches = _batches(pending_new_jobs(session, user, now), user)
            return [build_messages([j for _a, j in b], now)[0] for b in batches]
        if pending_new_jobs(session, user, now):
            log.warning("new job alerts on but no WhatsApp target set", user=user.username)
        return []
    out: list[str] = []
    for target, address in targets:
        rows = pending_new_jobs(session, user, now, target)
        for batch in _batches(rows, user):
            text = build_messages([j for _a, j in batch], now)[0]
            if dry_run:
                if text not in out:
                    out.append(text)
                continue
            if not _claim(session, user, batch, target):
                continue
            if not send_to(address, text):
                _release(session, user, batch, target)
                log.warning("new job alert not delivered, will retry next run", user=user.username,
                            jobs=len(batch), target=target)
                continue
            if text not in out:
                out.append(text)
            log.info("new job alert sent", user=user.username, jobs=len(batch), target=target)
    return out


def notify_new_jobs_all(session: Session, now: datetime | None = None) -> dict[str, int]:
    """`notify_new_jobs` for every active user; one user's notifier trouble never reaches the caller."""
    out: dict[str, int] = {}
    for user in session.exec(select(User).where(User.is_active)).all():
        try:
            out[user.username] = len(notify_new_jobs(session, user, now=now))
        except Exception as exc:  # noqa: BLE001 - alerts are best effort, collect must finish
            log.warning("new job alerts failed", user=user.username, error=f"{type(exc).__name__}: {exc}"[:300])
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            out[user.username] = 0
    return out


# ----------------------------------------------------------------- outcome alerts (automatic mode)


def outcome_text(job: Job, status: AppStatus | str, detail: str = "") -> str | None:
    """`✅ applied`, `📝 queued for your approval`, `⏸ paused: <reason>`, `❌ failed: <reason>` with the job
    and the dashboard link; None for statuses that get no alert."""
    try:
        status = AppStatus(getattr(status, "value", status))
    except ValueError:
        return None
    head = OUTCOME_ICONS.get(status)
    if head is None:
        return None
    reason = re.sub(r"\s+", " ", str(detail or "")).strip()
    if status in (AppStatus.paused, AppStatus.failed):
        head = f"{head}: {reason[:200]}" if reason else head
    company = re.sub(r"\s+", " ", job.company or "").strip()
    return f"{head}\n*{clean_title(job.title)}* — {company}\n{CLIPBOARD} {job_link(job)}"


def notify_outcome(session: Session, user: User, app: Application, job: Job, status: AppStatus | str,
                   detail: str = "") -> bool:
    """Automatic mode only: post the outcome to the user's target(s) and record an `outcome` event. Never
    raises; False when nothing was sent (notify mode, no target, unknown status, transport failure)."""
    try:
        if apply_mode(user) != "auto":
            return False
        text = outcome_text(job, status, detail)
        if text is None:
            return False
        targets = alert_targets(user)
        if not targets:
            return False
        delivered = [t for t in targets if send_to(t, text)]
        if not delivered:
            return False
        status_value = str(getattr(status, "value", status))
        if app.id is not None:
            session.add(ApplicationEvent(application_id=app.id, user_id=user.id, kind=OUTCOME_KIND,
                                         detail=f"{status_value}: whatsapp x{len(delivered)}"))
        return True
    except Exception as exc:  # noqa: BLE001 - an alert must never undo or break a recorded outcome
        log.warning("outcome alert failed", user=getattr(user, "username", "?"), error=str(exc)[:200])
        return False


__all__ = ["APPLY_MODES", "DEFAULT_APPLY_MODE", "BATCH_SIZE", "MAX_MESSAGES_PER_RUN", "NEW_JOB_WINDOW", "IST",
           "apply_mode", "notify_only", "notify_only_skip", "fmt_ist", "relative_age", "clean_title", "job_block",
           "build_messages", "event_detail", "pending_new_jobs", "notify_new_jobs", "notify_new_jobs_all",
           "outcome_text",
           "notify_outcome"]
