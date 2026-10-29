"""APScheduler wiring. Each task is a plain function so it can also run from the CLI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobapply.config import get_settings
from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.switches import collect_interval

log = get_logger(__name__)


def task_collect() -> None:
    from jobapply.pipeline.collect import run_all

    log.info("scheduled collect", **run_all())


def task_collect_hot() -> None:
    """Hot-polling tier: only the fast exact-timestamp sources (hot ATS companies + LinkedIn guest),
    for minute-level recency. Idempotent — dedupe handles jobs already seen by the normal collect task."""
    from jobapply.collectors.registry import hot_collectors
    from jobapply.pipeline.collect import run_all, user_queries

    collectors = hot_collectors(extra_queries=user_queries())
    if not collectors:
        log.info("scheduled collect_hot skipped", reason="no hot sources configured")
        return
    log.info("scheduled collect_hot", **run_all(collectors=collectors))


def task_score() -> None:
    from jobapply.pipeline.score import score_all

    log.info("scheduled score", results=score_all())


def task_apply() -> None:
    from jobapply.pipeline.apply import apply_all

    # Real submits happen only when the global dry-run switch is off AND the user's site has auto-submit on.
    log.info("scheduled apply", users=len(apply_all(limit=5, dry_run=False)))


def task_inbox() -> None:
    from jobapply.pipeline.mail import sync_all

    log.info("scheduled inbox", results=sync_all())


def task_follow_ups() -> None:
    from jobapply.pipeline.mail import follow_ups_all

    log.info("scheduled follow-ups", results=follow_ups_all())


def task_daily_summary() -> None:
    from jobapply.pipeline.summary import send_summaries

    log.info("scheduled summary", results=send_summaries())


@dataclass
class Task:
    name: str
    func: Callable[[], None]
    trigger: Callable[[], object]
    group: str  # "collector" or "worker"; systemd runs each group as its own service


def _every(minutes: int) -> Callable[[], IntervalTrigger]:
    return lambda: IntervalTrigger(minutes=minutes, jitter=60)


def _collect_trigger() -> IntervalTrigger:
    with session_scope() as session:
        return IntervalTrigger(minutes=collect_interval(session), jitter=120)


def _hot_trigger() -> IntervalTrigger:
    return IntervalTrigger(minutes=get_settings().hot_interval_minutes, jitter=30)


TASKS: list[Task] = [
    Task("collect", task_collect, _collect_trigger, "collector"),
    Task("collect_hot", task_collect_hot, _hot_trigger, "collector"),
    Task("score", task_score, _every(15), "worker"),
    Task("apply", task_apply, _every(30), "worker"),
    Task("inbox", task_inbox, _every(10), "worker"),
    Task("follow_ups", task_follow_ups, lambda: CronTrigger(hour=9, minute=30), "worker"),
    Task("daily_summary", task_daily_summary, lambda: CronTrigger(hour=15, minute=0), "worker"),
]


def build_scheduler(groups: list[str] | None = None, scheduler=None):
    scheduler = scheduler or BlockingScheduler(timezone="UTC")
    for t in TASKS:
        if groups and t.group not in groups:
            continue
        scheduler.add_job(_safe(t), t.trigger(), id=t.name, name=t.name, max_instances=1, coalesce=True,
                          misfire_grace_time=600)
    return scheduler


def _safe(task: Task) -> Callable[[], None]:
    def run() -> None:
        try:
            task.func()
        except Exception:
            log.exception("scheduled task failed", task=task.name)

    run.__name__ = f"task_{task.name}"
    return run


__all__ = ["TASKS", "Task", "build_scheduler", "CronTrigger"]
