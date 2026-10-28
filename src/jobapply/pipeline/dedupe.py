"""Store collected jobs, merging the same posting seen on several sources.

Exact match on a hash of normalized company + title + location, then a fuzzy pass over
recent jobs from the same company (title and description similarity). The canonical row
keeps the direct ATS link and exact posting time when any source has them.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from rapidfuzz import fuzz
from sqlmodel import Session, select

from jobapply.collectors.base import NormalizedJob
from jobapply.models import Job, JobSource, utcnow

ATS_SOURCES = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday"}

_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|bv|plc|corp|corporation|co|pvt|private|"
    r"technologies|technology|labs|software|group|holdings)\b\.?", re.I)
_TITLE_NOISE = re.compile(r"\((?:m/w/d|f/m/d|m/f/d|w/m/d|all genders?|remote|hybrid)[^)]*\)|\bremote\b", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")

TITLE_THRESHOLD = 92
DESCRIPTION_THRESHOLD = 85
FUZZY_WINDOW_DAYS = 30


def _squash(text: str) -> str:
    return _SPACE.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def norm_company(name: str) -> str:
    return _squash(_COMPANY_SUFFIX.sub(" ", name or ""))


def norm_title(title: str) -> str:
    stripped = _TITLE_NOISE.sub(" ", title or "")
    return _squash(stripped) or _squash(title or "")


def norm_location(location: str, remote: bool = False) -> str:
    loc = _squash((location or "").split(",")[0].split(";")[0])
    if not loc or loc in {"remote", "anywhere", "worldwide"}:
        return "remote" if remote or loc else ""
    return loc


def dedupe_key(job: NormalizedJob) -> str:
    basis = "|".join((norm_company(job.company), norm_title(job.title), norm_location(job.location, job.remote)))
    return hashlib.sha1(basis.encode()).hexdigest()[:24]


def _is_ats(source: str) -> bool:
    return source in ATS_SOURCES


def _fuzzy_match(session: Session, nj: NormalizedJob, company_norm: str) -> Job | None:
    if not company_norm:
        return None
    since = utcnow() - timedelta(days=FUZZY_WINDOW_DAYS)
    candidates = session.exec(
        select(Job).where(Job.company_norm == company_norm, Job.last_seen_at >= since)
    ).all()
    title = norm_title(nj.title)
    for cand in candidates:
        if fuzz.ratio(title, norm_title(cand.title)) < TITLE_THRESHOLD:
            continue
        if nj.description and cand.description:
            if fuzz.token_set_ratio(nj.description[:3000], cand.description[:3000]) < DESCRIPTION_THRESHOLD:
                continue
        loc_a, loc_b = norm_location(nj.location, nj.remote), norm_location(cand.location, cand.remote)
        if loc_a and loc_b and loc_a != loc_b and fuzz.partial_ratio(loc_a, loc_b) < 80:
            continue
        return cand
    return None


def _merge(job: Job, nj: NormalizedJob) -> None:
    """Fold a new source row into the canonical job, preferring ATS data."""
    now = utcnow()
    job.last_seen_at = now
    if _is_ats(nj.source) and not _is_ats(job.source):
        job.source = nj.source
        job.ats_type = nj.ats_type
        job.apply_url = nj.apply_url or job.apply_url
        job.url = nj.url or job.url
        if nj.posted_at:
            job.posted_at = nj.posted_at
    elif nj.ats_type and not job.ats_type:
        job.ats_type = nj.ats_type
        job.apply_url = nj.apply_url or job.apply_url
    if nj.posted_at and (job.posted_at is None or (not _is_ats(job.source) and nj.posted_at < job.posted_at)):
        job.posted_at = nj.posted_at
    if len(nj.description) > len(job.description):
        job.description = nj.description
    for attr in ("salary_min", "salary_max", "currency"):
        if getattr(job, attr) is None and getattr(nj, attr) is not None:
            setattr(job, attr, getattr(nj, attr))
    job.remote = job.remote or nj.remote


def _new_job(nj: NormalizedJob, key: str, company_norm: str) -> Job:
    return Job(
        dedupe_key=key, company_norm=company_norm,
        **nj.model_dump(exclude={"raw", "source_id"}),
    )


def store_jobs(session: Session, jobs: list[NormalizedJob]) -> list[int]:
    """Upsert jobs and their source rows. Returns ids of canonical jobs created in this call."""
    new_ids: list[int] = []
    for nj in jobs:
        existing_src = session.exec(
            select(JobSource).where(JobSource.source == nj.source, JobSource.source_id == nj.source_id)
        ).first()
        if existing_src:
            existing_src.fetched_at = utcnow()
            job = session.get(Job, existing_src.job_id)
            if job:
                _merge(job, nj)
            continue

        key = dedupe_key(nj)
        company_norm = norm_company(nj.company)
        job = session.exec(select(Job).where(Job.dedupe_key == key)).first() or _fuzzy_match(session, nj, company_norm)
        if job is None:
            job = _new_job(nj, key, company_norm)
            session.add(job)
            session.flush()
            new_ids.append(job.id)
        else:
            _merge(job, nj)
        session.add(JobSource(job_id=job.id, source=nj.source, source_id=nj.source_id, url=nj.url,
                              raw=nj.raw))
        session.flush()
    return new_ids
