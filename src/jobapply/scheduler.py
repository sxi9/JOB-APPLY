"""APScheduler wiring. Each task is a plain function so it can also run from the CLI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobapply.db import session_scope
from jobapply.logs import get_logger
from jobapply.switches import collect_interval

log = get_logger(__name__)


def task_collect() -> None:
    from jobapply.pipeline.collect import run_all

    log.info("scheduled collect", **run_all())


def task_score() -> None:
    from jobapply.pipeline.score import score_all

    log.info("scheduled score", results=score_all())


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


TASKS: list[Task] = [
    Task("collect", task_collect, _collect_trigger, "collector"),
    Task("score", task_score, _every(15), "worker"),
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
