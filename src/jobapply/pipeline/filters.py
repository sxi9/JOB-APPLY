"""Per-user job filters and assignment of new jobs to users."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from math import ceil

from pydantic import BaseModel, Field, model_serializer, model_validator
from sqlmodel import Session, select

from jobapply.logs import get_logger
from jobapply.models import Application, ApplicationEvent, Job, User, utcnow
from jobapply.pipeline.dedupe import norm_company
from jobapply.roles import match_title, titles_for

log = get_logger(__name__)

SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "lead", "principal", "manager", "director"]
_SENIORITY_PATTERNS = [
    ("intern", r"\bintern(ship)?\b|\bpraktik|\btrainee\b|\bwerkstudent"),
    ("director", r"\bdirector\b|\bvp\b|\bvice president\b|\bhead of\b|\bchief\b|\bcto\b"),
    ("manager", r"\bmanager\b|\bengineering manager\b"),
    ("principal", r"\bprincipal\b|\bstaff\b|\bdistinguished\b|\barchitect\b"),
    ("lead", r"\blead\b|\btech lead\b"),
    ("senior", r"\bsenior\b|\bsr\.?\b|\biii\b|\bexperienced\b"),
    ("junior", r"\bjunior\b|\bjr\.?\b|\bentry[- ]level\b|\bgraduate\b|\bnew grad\b|\bassociate\b"),
]


def detect_seniority(title: str) -> str | None:
    for level, pattern in _SENIORITY_PATTERNS:
        if re.search(pattern, title, re.I):
            return level
    return None


# Sources whose posted_at is an exact timestamp we can trust to the minute. ATS company APIs and the
# LinkedIn guest search return a precise posting time; aggregators (indeed, feeds, jobspy, adzuna, …)
# often give a coarse "x days ago", so a sub-hour (minutes) filter must not be applied to them precisely.
EXACT_TIMESTAMP_SOURCES = frozenset(
    {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "linkedin"}
)


def has_exact_timestamp(source: str | None) -> bool:
    return (source or "") in EXACT_TIMESTAMP_SOURCES


def age_cutoff(f: UserFilters, now: datetime, exact: bool):
    """Oldest allowed posting time for this filter, or None when no age limit applies.

    `max_age_minutes` wins over `max_age_hours` when set. A minutes window is applied to the minute only
    for exact-timestamp sources; for coarse-timestamp sources it is widened to whole-hour granularity
    (rounding the window up, at least one hour) so a minutes filter never hides a coarse job wrongly.
    """
    if f.max_age_minutes is not None:
        if exact:
            return now - timedelta(minutes=f.max_age_minutes)
        return now - timedelta(hours=max(1, ceil(f.max_age_minutes / 60)))
    if f.max_age_hours:
        return now - timedelta(hours=f.max_age_hours)
    return None


class UserFilters(BaseModel):
    keywords_include: list[str] = Field(default_factory=list)  # any must appear in title or description
    keywords_exclude: list[str] = Field(default_factory=list)  # none may appear in the title
    roles: list[str] = Field(default_factory=list)  # any must appear in the title (all words)
    locations: list[str] = Field(default_factory=list)  # any must appear in location; "remote" matches remote jobs
    remote_only: bool = False
    salary_floor: float | None = None  # rejects only when the posted maximum is below it
    seniority: list[str] = Field(default_factory=list)  # allowed levels; unknown level passes
    company_blocklist: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # empty means all
    max_age_hours: int = 72
    # Minute-level recency. When set it overrides max_age_hours (kept for back-compat). Applied to the
    # minute only for exact-timestamp sources; coarse sources fall back to whole-hour granularity.
    max_age_minutes: int | None = None
    # Role-taxonomy picker: field keys ("cybersecurity"), whole-field keys ("cybersecurity:*") and/or
    # sub-role keys ("cybersecurity:soc"). Expanded via roles.titles_for() to title keywords that gate
    # matching in `match()` and are folded into keywords_include on serialisation so the heuristic scorer
    # (which reads the stored filters dict, not this model) treats them as role/title keywords too.
    role_selection: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _restore_raw_keywords(cls, data):
        # The dumped dict folds the role expansion into `keywords_include` for the scorer but keeps the
        # user's own free-text under `keywords_include_text`; restore that here so round-trips stay stable
        # (the free-text box never fills up with expanded synonyms).
        if isinstance(data, dict) and "keywords_include_text" in data:
            data = dict(data)
            data["keywords_include"] = data.pop("keywords_include_text")
        return data

    @model_serializer(mode="wrap")
    def _fold_role_selection(self, handler):
        data = handler(self)
        if self.role_selection:
            expansion = titles_for(self.role_selection)
            if expansion:
                data["keywords_include_text"] = list(self.keywords_include)
                merged = list(self.keywords_include)
                for keyword in sorted(expansion):
                    if keyword not in merged:
                        merged.append(keyword)
                data["keywords_include"] = merged
        return data


def _contains(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(needle.lower())}(?!\w)", haystack) is not None


def match(job: Job, f: UserFilters, now=None) -> tuple[bool, str]:
    """Return (passes, reason for rejection or 'ok')."""
    now = now or utcnow()
    title = job.title.lower()
    text = f"{title}\n{job.description.lower()}"
    location = (job.location or "").lower()

    when = job.posted_at or job.first_seen_at
    cutoff = age_cutoff(f, now, has_exact_timestamp(job.source))
    if when and cutoff and when < cutoff:
        return False, "too old"
    if f.sources and job.source not in f.sources:
        return False, "source not selected"
    company = norm_company(job.company)
    if any(b and norm_company(b) and norm_company(b) in company for b in f.company_blocklist):
        return False, "company blocklisted"
    if f.roles or f.role_selection:
        free_ok = bool(f.roles) and any(
            all(_contains(title, w) for w in role.lower().split()) for role in f.roles
        )
        tax_ok = bool(f.role_selection) and match_title(job.title, f.role_selection)
        if not (free_ok or tax_ok):
            return False, "role not matched"
    if f.keywords_include and not any(_contains(text, k) for k in f.keywords_include):
        return False, "no include keyword"
    if any(_contains(title, k) for k in f.keywords_exclude):
        return False, "exclude keyword in title"
    if f.remote_only and not job.remote:
        return False, "not remote"
    if f.locations:
        wants_remote = any(loc.lower() == "remote" for loc in f.locations)
        if not ((wants_remote and job.remote) or any(_contains(location, loc) for loc in f.locations
                                                       if loc.lower() != "remote")):
            return False, "location not matched"
    if f.salary_floor and job.salary_max and job.salary_max < f.salary_floor:
        return False, "salary below floor"
    if f.seniority:
        level = detect_seniority(job.title)
        if level and level not in f.seniority:
            return False, f"seniority {level}"
    return True, "ok"


def user_filters(user: User) -> UserFilters:
    return UserFilters.model_validate(user.filters or {})


def assign_jobs(session: Session, job_ids: list[int] | None = None, users: list[User] | None = None) -> dict[str, int]:
    """Create an `Application(status=new)` for each user whose filters a job passes.

    `job_ids=None` re-evaluates every job inside the users' age windows (used after filters change).
    Jobs a user already has a row for are left alone, so nothing is applied to twice.
    """
    users = users if users is not None else list(session.exec(select(User).where(User.is_active)).all())
    counts: dict[str, int] = {}
    for user in users:
        f = user_filters(user)
        stmt = select(Job)
        if job_ids is not None:
            if not job_ids:
                counts[user.username] = 0
                continue
            stmt = stmt.where(Job.id.in_(job_ids))
        elif f.max_age_minutes is not None:
            # Generous candidate window (match() does the precise, per-source check). The widest match
            # window is the coarse whole-hour one; double it so no candidate is missed.
            buffer_hours = max(1, ceil(f.max_age_minutes / 60)) * 2
            stmt = stmt.where(Job.first_seen_at >= utcnow() - timedelta(hours=buffer_hours))
        elif f.max_age_hours:
            stmt = stmt.where(Job.first_seen_at >= utcnow() - timedelta(hours=f.max_age_hours * 2))
        existing = set(session.exec(select(Application.job_id).where(Application.user_id == user.id)).all())
        added = 0
        for job in session.exec(stmt).all():
            if job.id in existing:
                continue
            ok, _reason = match(job, f)
            if ok:
                app = Application(user_id=user.id, job_id=job.id)
                session.add(app)
                session.flush()
                session.add(ApplicationEvent(application_id=app.id, user_id=user.id, kind="new",
                                             detail="passed filters"))
                added += 1
        counts[user.username] = added
        log.info("assigned jobs", user=user.username, added=added)
    return counts
