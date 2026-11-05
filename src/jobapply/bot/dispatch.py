"""Posting new jobs to the WhatsApp subscribers (bot/whatsapp.py onboarding, `subscribers` table).

`notify_subscribers(session, new_job_ids=None, *, now=None, dry_run=False)` runs at the end of every collect
(after the user alerts and the group feeds, inside its own try/except) and from `jobapply bot dispatch` / the
admin card. Per active subscriber whose onboarding is complete (`state == "active"`): the new jobs (or, without
ids, the jobs first seen in the last `NEW_WINDOW`) that pass the subscriber's filters - the same
`pipeline.filters.match` the users and the feeds use, built by `subscriber_filters`: the chosen fields as
`role_selection` (`field:*`), seniority `intern` for the internships pseudo-field, the location (plus `remote`
when remote is fine), no remote-only, `MAX_AGE_HOURS` - minus what `subscriber_posts` already holds, newest
first, batches of `BATCH_SIZE` (8) jobs and at most `MAX_MESSAGES_PER_RUN` (3) messages per subscriber and run.

Claim before send: a batch's `SubscriberPost` rows are written and committed BEFORE the DM goes out (the unique
(subscriber_id, job_id) constraint makes an overlapping run lose and skip) and deleted again when the send
fails, so a job never reaches a number twice and a failed send is retried next run. The messages are the
feed-style job blocks (title / company / location, posted time in IST, the apply URL on its own line, no
dashboard link: subscribers have no account).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from jobapply.config import get_settings
from jobapply.logs import get_logger
from jobapply.models import INTERN_FIELD, Job, Subscriber, SubscriberPost, utcnow
from jobapply.notify import mask_target, send_to
from jobapply.pipeline.alerts import BATCH_SIZE, NEW, _aware, _posted_key, fmt_ist, job_block
from jobapply.pipeline.filters import UserFilters, detect_seniority, match
from jobapply.roles import field as taxonomy_field

log = get_logger(__name__)

MAX_MESSAGES_PER_RUN = 3
MAX_AGE_HOURS = 48
NEW_WINDOW = timedelta(hours=24)
FOOTER = "Reply STOP to pause these alerts."


# ----------------------------------------------------------------- matching


def subscriber_filters(sub: Subscriber) -> UserFilters:
    """The subscriber's choices as the users' `UserFilters` (see the module docstring)."""
    keys = [str(k) for k in (sub.fields or [])]
    roles = [f"{k}:*" for k in keys if k != INTERN_FIELD and taxonomy_field(k) is not None]
    locations = [sub.location] if sub.location else []
    if sub.remote_ok:
        locations.append("remote")
    return UserFilters(role_selection=roles, seniority=["intern"] if INTERN_FIELD in keys else [],
                       locations=locations, remote_only=False, max_age_hours=MAX_AGE_HOURS)


def intern_only(sub: Subscriber) -> bool:
    """Internships chosen and no career field: only titles that read as an internship qualify."""
    keys = [str(k) for k in (sub.fields or [])]
    return INTERN_FIELD in keys and not any(k != INTERN_FIELD and taxonomy_field(k) is not None for k in keys)


def pool_jobs(session: Session, filters: UserFilters, now: datetime, *, new_job_ids: Iterable[int] | None = None,
              hours: int | None = None, require_intern: bool = False, exclude: set[int] | None = None,
              limit: int | None = None) -> list[Job]:
    """Jobs of the shared pool that pass `filters`, newest posted first: the given ids, or the jobs first seen
    inside `hours` (None: the filters' own age window; 0: any age). `require_intern` keeps only titles whose
    seniority reads as intern; `exclude` drops ids; `limit` caps the result."""
    stmt = select(Job)
    if new_job_ids is not None:
        ids = list(new_job_ids)
        if not ids:
            return []
        stmt = stmt.where(Job.id.in_(ids))
    else:
        window = filters.max_age_hours if hours is None else hours
        if window:
            stmt = stmt.where(Job.first_seen_at >= now - timedelta(hours=window))
    out: list[Job] = []
    for job in session.exec(stmt).all():
        if exclude and job.id in exclude:
            continue
        if require_intern and detect_seniority(job.title) != "intern":
            continue
        ok, _reason = match(job, filters, now)
        if ok:
            out.append(job)
    out.sort(key=_posted_key, reverse=True)
    return out[:limit] if limit else out


def posted_job_ids(session: Session, sub: Subscriber) -> set[int]:
    return set(session.exec(select(SubscriberPost.job_id).where(SubscriberPost.subscriber_id == sub.id)).all())


def candidate_jobs(session: Session, sub: Subscriber, new_job_ids: Iterable[int] | None = None,
                   now: datetime | None = None) -> list[Job]:
    """What this subscriber would get now: the new jobs (or the last `NEW_WINDOW`) that pass their filters and
    were not posted to them yet, newest first."""
    now = _aware(now) or utcnow()
    hours = None if new_job_ids is not None else int(NEW_WINDOW.total_seconds() // 3600)
    return pool_jobs(session, subscriber_filters(sub), now, new_job_ids=new_job_ids, hours=hours,
                     require_intern=intern_only(sub), exclude=posted_job_ids(session, sub))


# ----------------------------------------------------------------- messages


def subscriber_header(count: int, now: datetime) -> str:
    noun = "new job" if count == 1 else "new jobs"
    return f"{NEW} {count} {noun} for you — {fmt_ist(now)}"


def build_subscriber_messages(jobs: list[Job], now: datetime | None = None, *,
                              batch_size: int = BATCH_SIZE) -> list[str]:
    """Batches of at most `batch_size` jobs in the given order: header, the feed-style job blocks (no
    dashboard link), the STOP footer."""
    now = _aware(now) or utcnow()
    out: list[str] = []
    for i in range(0, len(jobs), batch_size):
        chunk = jobs[i:i + batch_size]
        body = "\n\n".join(job_block(j, now, dashboard_link=False) for j in chunk)
        out.append(subscriber_header(len(chunk), now) + "\n\n" + body + "\n\n" + FOOTER)
    return out


# ----------------------------------------------------------------- claim / release


def _claim(session: Session, sub: Subscriber, batch: list[Job], now: datetime) -> bool:
    """Write (and commit) the batch's `subscriber_posts` rows BEFORE the send; False when another run got
    there first (unique constraint), in which case nothing of this batch is sent."""
    for job in batch:
        session.add(SubscriberPost(subscriber_id=sub.id, job_id=job.id, posted_at=now))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        log.info("subscriber batch already claimed by another run, skipping it", to=mask_target(sub.phone_jid),
                 jobs=len(batch))
        return False
    return True


def _release(session: Session, sub: Subscriber, batch: list[Job]) -> None:
    """Drop the claim again after a failed send so the batch is retried next run."""
    session.execute(delete(SubscriberPost).where(SubscriberPost.subscriber_id == sub.id,
                                                 SubscriberPost.job_id.in_([j.id for j in batch])))
    session.commit()


# ----------------------------------------------------------------- running


def _result(sub: Subscriber) -> dict:
    return {"id": sub.id, "target": mask_target(sub.phone_jid), "name": sub.name, "fields": list(sub.fields or []),
            "skipped": "", "candidates": 0, "messages": [], "sent": 0, "posted": 0, "error": ""}


def notify_subscriber(session: Session, sub: Subscriber, new_job_ids: Iterable[int] | None = None, *,
                      now: datetime | None = None, dry_run: bool = False) -> dict:
    """One subscriber: `{id, target, name, fields, skipped, candidates, messages, sent, posted, error}`. With
    `dry_run` the messages are built and nothing is sent or recorded."""
    now = _aware(now) or utcnow()
    result = _result(sub)
    if not sub.active:
        result["skipped"] = "paused"
        return result
    if sub.state != "active":
        result["skipped"] = f"onboarding ({sub.state})"
        return result
    jobs = candidate_jobs(session, sub, new_job_ids, now)
    result["candidates"] = len(jobs)
    batches = [jobs[i:i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]
    if len(batches) > MAX_MESSAGES_PER_RUN:
        log.info("subscriber posts capped for this run; the rest goes out next run", to=mask_target(sub.phone_jid),
                 cap=MAX_MESSAGES_PER_RUN, dropped=sum(len(b) for b in batches[MAX_MESSAGES_PER_RUN:]))
        batches = batches[:MAX_MESSAGES_PER_RUN]
    result["messages"] = [build_subscriber_messages(b, now)[0] for b in batches]
    if dry_run or not batches:
        return result
    for batch, text in zip(batches, result["messages"], strict=True):
        if not _claim(session, sub, batch, now):
            continue
        if not send_to(sub.phone_jid, text):
            _release(session, sub, batch)
            result["error"] = "send failed"
            log.warning("subscriber post not delivered, will retry next run", to=mask_target(sub.phone_jid),
                        jobs=len(batch))
            break
        result["sent"] += 1
        result["posted"] += len(batch)
        log.info("subscriber post sent", to=mask_target(sub.phone_jid), jobs=len(batch))
    return result


def notify_subscribers(session: Session, new_job_ids: Iterable[int] | None = None, *, now: datetime | None = None,
                       dry_run: bool = False) -> dict:
    """Every subscriber (see the module docstring): `{"subscribers": [notify_subscriber result, ...],
    "posted": n, "messages": n, "disabled": bool}`. Never raises: one subscriber's trouble is logged with
    `error` set and the next one still runs. `JOBAPPLY_WHATSAPP_BOT_ENABLED=false` sends nothing (a dry run
    still shows what would go out)."""
    now = _aware(now) or utcnow()
    ids = list(new_job_ids) if new_job_ids is not None else None
    out: dict = {"subscribers": [], "posted": 0, "messages": 0, "disabled": False}
    if not get_settings().whatsapp_bot_enabled and not dry_run:
        out["disabled"] = True
        return out
    if ids is not None and not ids:
        return out
    try:
        subs = list(session.exec(select(Subscriber).order_by(Subscriber.id)).all())
    except Exception as exc:  # noqa: BLE001 - a missing table (migration pending) must not break collect
        log.warning("subscribers unavailable", error=f"{type(exc).__name__}: {exc}"[:300])
        return out
    for sub in subs:
        try:
            result = notify_subscriber(session, sub, ids, now=now, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("subscriber post failed", to=mask_target(sub.phone_jid),
                        error=f"{type(exc).__name__}: {exc}"[:300])
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            result = _result(sub)
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["subscribers"].append(result)
        out["posted"] += result["posted"]
        out["messages"] += result["sent"]
    return out


__all__ = ["MAX_MESSAGES_PER_RUN", "MAX_AGE_HOURS", "NEW_WINDOW", "FOOTER", "subscriber_filters", "intern_only",
           "pool_jobs", "posted_job_ids", "candidate_jobs", "subscriber_header", "build_subscriber_messages",
           "notify_subscriber", "notify_subscribers"]
