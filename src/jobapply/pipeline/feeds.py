"""Admin-configured WhatsApp group feeds: new jobs that pass a feed's filters are posted to a group.

A feed needs no user account on the readers' side: the messages carry the apply link only (no dashboard
link). Feeds are rows in `app_settings["group_feeds"]` (a JSON list, `load_feeds` / `save_feeds`), each
with the same filter vocabulary as a user's own filters so `pipeline.filters.match` decides for both:

    {"id", "name", "group_jid", "enabled", "max_per_run", "created_at",
     "filters": {"role_selection", "roles", "keywords_include", "keywords_exclude", "locations",
                 "remote_only", "max_age_hours", "sources", "min_salary"}}

`load_feeds` adds the run stats to every row: `posted_count` is the number of `feed_posts` rows of the feed
and `last_run_at` comes from `app_settings["group_feed_runs"]` (`{feed_id: iso}`), the only setting a run
writes. A run never rewrites `group_feeds` itself, so an admin edit (disable, remove, new filters) made
while a run is posting is never overwritten by the run's stale copy.

`run_feeds(session, new_job_ids)` runs at the end of every collect (after the user alerts) and from the
CLI / admin page. Per enabled feed it evaluates the new jobs (or, without ids, the jobs first seen inside
the feed's `max_age_hours`), skips what the feed already posted (`feed_posts` table, one row per feed and
job), posts batches of `BATCH_SIZE` jobs up to `max_per_run` jobs, and records the `FeedPost` rows only
after a successful send, so a failed send is retried next run and a job never reaches a group twice. It
never raises: a feed's trouble is logged and the next feed still runs.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlmodel import Session, select

from jobapply.logs import get_logger
from jobapply.models import FeedPost, Job, utcnow
from jobapply.notify import mask_target, send_to
from jobapply.pipeline.alerts import BATCH_SIZE, IST, _aware, _posted_key, clean_title, job_block
from jobapply.pipeline.filters import UserFilters, match
from jobapply.switches import get_value, set_value

log = get_logger(__name__)

FEEDS_KEY = "group_feeds"
FEED_RUNS_KEY = "group_feed_runs"  # {feed_id: iso last run}; written by run_feeds only
STATS_KEYS = ("last_run_at", "posted_count")  # derived on load, never stored in the feed definitions
DEFAULT_MAX_PER_RUN = 10
MAX_PER_RUN_LIMIT = 50  # hard ceiling so one feed can never flood a group
DEFAULT_MAX_AGE_HOURS = 24
MAX_AGE_HOURS_LIMIT = 24 * 30
NAME_MAX = 60
MEGAPHONE = "\U0001f4e2"  # 📢
LIST_FILTER_KEYS = ("role_selection", "roles", "keywords_include", "keywords_exclude", "locations", "sources")


# ----------------------------------------------------------------- feed rows (app_settings JSON)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[\n,]", value)]
    else:
        parts = [str(v).strip() for v in value]
    return list(dict.fromkeys(p for p in parts if p))


def _as_int(value, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def new_feed_id() -> str:
    return secrets.token_hex(4)


def normalize_filters(raw: dict | None) -> dict:
    """The feed filter dict with every key present and typed (unknown keys dropped)."""
    raw = raw if isinstance(raw, dict) else {}
    out = {key: _as_list(raw.get(key)) for key in LIST_FILTER_KEYS}
    out["remote_only"] = bool(raw.get("remote_only"))
    # 0 means no age limit
    out["max_age_hours"] = _as_int(raw.get("max_age_hours"), DEFAULT_MAX_AGE_HOURS, 0, MAX_AGE_HOURS_LIMIT)
    out["min_salary"] = _as_float(raw.get("min_salary"))
    return out


def normalize_feed(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "id": str(raw.get("id") or new_feed_id()),
        "name": clean_title(str(raw.get("name") or "Feed"), NAME_MAX),
        "group_jid": str(raw.get("group_jid") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "filters": normalize_filters(raw.get("filters")),
        "max_per_run": _as_int(raw.get("max_per_run"), DEFAULT_MAX_PER_RUN, 1, MAX_PER_RUN_LIMIT),
        "last_run_at": str(raw["last_run_at"]) if raw.get("last_run_at") else None,
        "posted_count": _as_int(raw.get("posted_count"), 0, 0),
        "created_at": str(raw.get("created_at") or utcnow().isoformat()),
    }


def _load_runs(session: Session) -> dict[str, str]:
    raw = get_value(session, FEED_RUNS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}


def feed_stats(session: Session) -> dict[str, dict]:
    """`{feed_id: {"last_run_at", "posted_count"}}` from `feed_posts` and the runs setting."""
    counts = dict(session.exec(select(FeedPost.feed_id, func.count()).group_by(FeedPost.feed_id)).all())
    runs = _load_runs(session)
    return {fid: {"last_run_at": runs.get(fid), "posted_count": int(counts.get(fid, 0))}
            for fid in set(counts) | set(runs)}


def load_feeds(session: Session) -> list[dict]:
    """Every feed, normalised and with its run stats; [] (logged) when the stored value is missing or not a
    JSON list."""
    raw = get_value(session, FEEDS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("group_feeds setting is not valid JSON, ignoring it", error=str(exc)[:200])
        return []
    if not isinstance(data, list):
        log.warning("group_feeds setting is not a list, ignoring it")
        return []
    stats = feed_stats(session)
    feeds = []
    for f in data:
        if isinstance(f, dict):
            feed = normalize_feed(f)
            feed.update(stats.get(feed["id"], {"last_run_at": None, "posted_count": 0}))
            feeds.append(feed)
    return feeds


def save_feeds(session: Session, feeds: Iterable[dict]) -> list[dict]:
    """Store the feed definitions (without the derived run stats)."""
    rows = [normalize_feed(f) for f in feeds]
    stored = [{k: v for k, v in row.items() if k not in STATS_KEYS} for row in rows]
    set_value(session, FEEDS_KEY, json.dumps(stored, ensure_ascii=False))
    return rows


def record_feed_runs(session: Session, feed_ids: Iterable[str], now: datetime) -> None:
    """`last_run_at` for the given feeds (committed). Its own setting, so the feed definitions are left alone."""
    ids = list(feed_ids)
    if not ids:
        return
    runs = _load_runs(session)
    for fid in ids:
        runs[fid] = now.isoformat()
    set_value(session, FEED_RUNS_KEY, json.dumps(runs))
    session.commit()


def get_feed(session: Session, feed_id: str) -> dict | None:
    return next((f for f in load_feeds(session) if f["id"] == feed_id), None)


def add_feed(session: Session, *, name: str, group_jid: str, filters: dict | None = None,
             max_per_run: int = DEFAULT_MAX_PER_RUN, enabled: bool = True) -> dict:
    """Append a feed (not committed). ValueError when the name or the group jid is missing."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    group_jid = str(group_jid or "").strip()
    if not name:
        raise ValueError("a feed needs a name")
    if not group_jid:
        raise ValueError("a feed needs a WhatsApp group jid")
    feeds = load_feeds(session)
    feed = normalize_feed({"id": new_feed_id(), "name": name, "group_jid": group_jid, "filters": filters,
                           "max_per_run": max_per_run, "enabled": enabled})
    feeds.append(feed)
    save_feeds(session, feeds)
    return feed


def update_feed(session: Session, feed_id: str, **changes) -> dict:
    """Replace the given top-level keys of one feed (`filters` as a whole dict). ValueError when unknown."""
    feeds = load_feeds(session)
    for i, feed in enumerate(feeds):
        if feed["id"] != feed_id:
            continue
        merged = {**feed, **{k: v for k, v in changes.items() if k in feed}}
        if not str(merged.get("name") or "").strip():
            raise ValueError("a feed needs a name")
        if not str(merged.get("group_jid") or "").strip():
            raise ValueError("a feed needs a WhatsApp group jid")
        feeds[i] = normalize_feed(merged)
        save_feeds(session, feeds)
        return feeds[i]
    raise ValueError(f"no feed with id {feed_id!r}")


def set_enabled(session: Session, feed_id: str, enabled: bool) -> dict:
    return update_feed(session, feed_id, enabled=bool(enabled))


def remove_feed(session: Session, feed_id: str) -> bool:
    """Drop the feed and its dedupe rows; False when there is no such feed."""
    feeds = load_feeds(session)
    kept = [f for f in feeds if f["id"] != feed_id]
    if len(kept) == len(feeds):
        return False
    save_feeds(session, kept)
    session.execute(delete(FeedPost).where(FeedPost.feed_id == feed_id))
    runs = _load_runs(session)
    if runs.pop(feed_id, None) is not None:
        set_value(session, FEED_RUNS_KEY, json.dumps(runs))
    return True


# ----------------------------------------------------------------- matching (the users' matcher)


def feed_filters(feed: dict) -> UserFilters:
    """The feed's filter dict as the same `UserFilters` the per-user assignment uses (`min_salary` is the
    users' `salary_floor`)."""
    f = normalize_filters(feed.get("filters"))
    return UserFilters(role_selection=f["role_selection"], roles=f["roles"], keywords_include=f["keywords_include"],
                       keywords_exclude=f["keywords_exclude"], locations=f["locations"],
                       remote_only=f["remote_only"], max_age_hours=f["max_age_hours"], sources=f["sources"],
                       salary_floor=f["min_salary"])


def filters_summary(feed: dict) -> str:
    """One line for the admin table and the CLI: `fields: cybersecurity:* · exclude: intern · remote · 24 h`."""
    f = normalize_filters(feed.get("filters"))
    parts = []
    if f["role_selection"]:
        parts.append("fields: " + ", ".join(f["role_selection"]))
    if f["roles"]:
        parts.append("roles: " + ", ".join(f["roles"]))
    if f["keywords_include"]:
        parts.append("include: " + ", ".join(f["keywords_include"]))
    if f["keywords_exclude"]:
        parts.append("exclude: " + ", ".join(f["keywords_exclude"]))
    if f["locations"]:
        parts.append("in: " + ", ".join(f["locations"]))
    if f["remote_only"]:
        parts.append("remote only")
    if f["sources"]:
        parts.append("sources: " + ", ".join(f["sources"]))
    if f["min_salary"]:
        parts.append(f"salary ≥ {f['min_salary']:g}")
    parts.append(f"{f['max_age_hours']} h" if f["max_age_hours"] else "any age")
    return " · ".join(parts)


def posted_job_ids(session: Session, feed_id: str) -> set[int]:
    return set(session.exec(select(FeedPost.job_id).where(FeedPost.feed_id == feed_id)).all())


def candidate_jobs(session: Session, feed: dict, new_job_ids: Iterable[int] | None = None,
                   now: datetime | None = None) -> list[Job]:
    """Jobs the feed would post now, newest posted first: the given new jobs (or, without ids, the jobs
    first seen inside the feed's age window) that pass the feed's filters and were not posted by it yet."""
    now = _aware(now) or utcnow()
    f = feed_filters(feed)
    stmt = select(Job)
    if new_job_ids is not None:
        ids = list(new_job_ids)
        if not ids:
            return []
        stmt = stmt.where(Job.id.in_(ids))
    elif f.max_age_hours:  # 0 means no age limit (match() applies none either)
        stmt = stmt.where(Job.first_seen_at >= now - timedelta(hours=f.max_age_hours))
    posted = posted_job_ids(session, feed["id"])
    jobs = []
    for job in session.exec(stmt).all():
        if job.id in posted:
            continue
        ok, _reason = match(job, f, now)
        if ok:
            jobs.append(job)
    jobs.sort(key=_posted_key, reverse=True)
    return jobs


# ----------------------------------------------------------------- messages


def feed_header(name: str, count: int, now: datetime) -> str:
    """`📢 <feed name> — 3 new jobs — 14:05 IST`."""
    noun = "new job" if count == 1 else "new jobs"
    stamp = _aware(now).astimezone(IST).strftime("%H:%M IST")
    return f"{MEGAPHONE} {clean_title(name, NAME_MAX)} — {count} {noun} — {stamp}"


def build_feed_messages(feed: dict, jobs: list[Job], now: datetime | None = None, *,
                        batch_size: int = BATCH_SIZE) -> list[str]:
    """Batches of at most `batch_size` jobs in the given order, the user-alert job block without the
    dashboard link (group readers may have no account)."""
    now = _aware(now) or utcnow()
    out: list[str] = []
    for i in range(0, len(jobs), batch_size):
        chunk = jobs[i:i + batch_size]
        body = "\n\n".join(job_block(j, now, dashboard_link=False) for j in chunk)
        out.append(feed_header(feed["name"], len(chunk), now) + "\n\n" + body)
    return out


# ----------------------------------------------------------------- running


def run_feed(session: Session, feed: dict, new_job_ids: Iterable[int] | None = None, *,
             now: datetime | None = None, dry_run: bool = False, ignore_disabled: bool = False) -> dict:
    """One feed. Returns `{id, name, enabled, skipped, candidates, messages, sent, posted, error}`: with
    `dry_run` the messages are built and nothing is sent or recorded; otherwise every batch goes to the
    group and its `FeedPost` rows are written (and committed) only after that send succeeded. Stops at
    the first failed send so the rest is retried next run."""
    feed = normalize_feed(feed)
    now = _aware(now) or utcnow()
    result = {"id": feed["id"], "name": feed["name"], "enabled": feed["enabled"], "skipped": "",
              "candidates": 0, "messages": [], "sent": 0, "posted": 0, "error": ""}
    if not feed["enabled"] and not ignore_disabled:
        result["skipped"] = "disabled"
        return result
    jobs = candidate_jobs(session, feed, new_job_ids, now)
    result["candidates"] = len(jobs)
    if len(jobs) > feed["max_per_run"]:
        log.info("feed capped for this run; the rest goes out next run", feed=feed["name"],
                 cap=feed["max_per_run"], dropped=len(jobs) - feed["max_per_run"])
        jobs = jobs[:feed["max_per_run"]]
    batches = [jobs[i:i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]
    result["messages"] = [build_feed_messages(feed, b, now)[0] for b in batches]
    if dry_run or not batches:
        return result
    if not feed["group_jid"]:
        result["skipped"] = "no group"
        log.warning("feed has no group jid, nothing posted", feed=feed["name"])
        return result
    for batch, text in zip(batches, result["messages"], strict=True):
        if not send_to(feed["group_jid"], text):
            result["error"] = "send failed"
            log.warning("feed message not delivered, will retry next run", feed=feed["name"],
                        group=mask_target(feed["group_jid"]), jobs=len(batch))
            break
        for job in batch:
            session.add(FeedPost(feed_id=feed["id"], job_id=job.id, posted_at=now))
        session.commit()
        result["sent"] += 1
        result["posted"] += len(batch)
        log.info("feed message sent", feed=feed["name"], group=mask_target(feed["group_jid"]), jobs=len(batch))
    return result


def run_feeds(session: Session, new_job_ids: Iterable[int] | None = None, *, now: datetime | None = None,
              dry_run: bool = False, feed_ids: Iterable[str] | None = None) -> dict:
    """Every enabled feed (or only `feed_ids`). Returns `{"feeds": [run_feed result, ...], "posted": n,
    "messages": n}`. Records each run feed's `last_run_at` (`record_feed_runs`, not with `dry_run`) and
    never touches the feed definitions. Never raises: a failing feed is logged with `error` set and the
    next one still runs."""
    now = _aware(now) or utcnow()
    ids = list(new_job_ids) if new_job_ids is not None else None
    only = set(feed_ids) if feed_ids is not None else None
    out: dict = {"feeds": [], "posted": 0, "messages": 0}
    try:
        feeds = load_feeds(session)
    except Exception as exc:  # noqa: BLE001 - a broken setting must not break collect
        log.warning("group feeds unavailable", error=f"{type(exc).__name__}: {exc}"[:300])
        return out
    ran: list[str] = []
    for feed in feeds:
        if only is not None and feed["id"] not in only:
            continue
        try:
            result = run_feed(session, feed, ids, now=now, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("group feed failed", feed=feed["name"], error=f"{type(exc).__name__}: {exc}"[:300])
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            result = {"id": feed["id"], "name": feed["name"], "enabled": feed["enabled"], "skipped": "",
                      "candidates": 0, "messages": [], "sent": 0, "posted": 0,
                      "error": f"{type(exc).__name__}: {exc}"[:300]}
        out["feeds"].append(result)
        out["posted"] += result["posted"]
        out["messages"] += result["sent"]
        if not dry_run and not result["skipped"]:
            ran.append(feed["id"])
    try:
        record_feed_runs(session, ran, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save feed run stats", error=f"{type(exc).__name__}: {exc}"[:300])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
    return out


__all__ = ["FEEDS_KEY", "FEED_RUNS_KEY", "DEFAULT_MAX_PER_RUN", "DEFAULT_MAX_AGE_HOURS", "MAX_PER_RUN_LIMIT",
           "LIST_FILTER_KEYS", "normalize_filters", "normalize_feed", "load_feeds", "save_feeds", "feed_stats",
           "record_feed_runs", "get_feed", "add_feed",
           "update_feed", "set_enabled", "remove_feed", "feed_filters", "filters_summary", "posted_job_ids",
           "candidate_jobs", "feed_header", "build_feed_messages", "run_feed", "run_feeds"]

