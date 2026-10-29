"""Database models.

Shared tables: jobs, job_sources, form_profiles, collector_runs, app_settings.
Per-user tables (always queried with user_id): users, user_sites, applications,
application_events, answers_bank, credentials, mail_messages, llm_usage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC now. SQLModel stores datetimes as UTC and returns them aware."""
    return datetime.now(UTC)


def _json(default: Any = None) -> Any:
    factory = (lambda: {}) if default is None else default
    return Field(default_factory=factory, sa_column=Column(JSON, nullable=False))


class Role(StrEnum):
    admin = "admin"
    user = "user"


class AppStatus(StrEnum):
    new = "new"  # passed filters, not scored yet
    scored = "scored"  # scored below the auto threshold
    queued = "queued"  # waiting for approval or for the apply run
    approved = "approved"  # approved by the user, apply on next run
    dry_run = "dry_run"  # filled and screenshotted, not submitted
    applied = "applied"
    failed = "failed"
    paused = "paused"  # captcha or low confidence, needs a human
    skipped = "skipped"
    replied = "replied"
    assessment = "assessment"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"


class MailCategory(StrEnum):
    interview = "interview"
    assessment = "assessment"
    rejection = "rejection"
    recruiter_question = "recruiter_question"
    verification = "verification"
    spam = "spam"
    other = "other"


# ---------------------------------------------------------------- users


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str = ""
    password_hash: str
    role: Role = Role.user
    is_active: bool = True
    # Profile vault: contact, links, work authorization, notice period, CTC, skills with years,
    # EEO answers, education. Free-form JSON so onboarding needs no migrations.
    profile: dict = _json()
    # Filters: keywords_include, keywords_exclude, roles, locations, remote_only, salary_floor,
    # seniority, company_blocklist, max_age_hours.
    filters: dict = _json()
    # Preferences: score_threshold, cover_letter_tone, whatsapp_number, alert_types, daily_cap.
    preferences: dict = _json()
    created_at: datetime = Field(default_factory=utcnow)


class UserSite(SQLModel, table=True):
    __tablename__ = "user_sites"
    __table_args__ = (UniqueConstraint("user_id", "site"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    site: str
    enabled: bool = True
    auto_submit: bool = False
    daily_cap: int = 20
    login_status: str = "unknown"  # unknown | ok | expired | n/a
    last_login_at: datetime | None = None


class Credential(SQLModel, table=True):
    """Encrypted secret per user per site. Only the ciphertext is stored."""

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "site", "kind"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    site: str  # e.g. linkedin, naukri, mailbox, anthropic
    kind: str = "login"  # login | imap | smtp | api_key
    username: str = ""
    secret_blob: bytes = b""  # Fernet ciphertext of a JSON object
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- jobs (shared)


class Job(SQLModel, table=True):
    """Canonical job. Several source rows can point here after dedupe."""

    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    dedupe_key: str = Field(index=True, unique=True)
    title: str
    company: str = Field(index=True)
    company_norm: str = Field(default="", index=True)
    location: str = ""
    remote: bool = False
    posted_at: datetime | None = Field(default=None, index=True)
    url: str = ""
    apply_url: str = ""
    description: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    ats_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    source: str = ""  # source of the canonical row
    first_seen_at: datetime = Field(default_factory=utcnow, index=True)
    last_seen_at: datetime = Field(default_factory=utcnow)


class JobSource(SQLModel, table=True):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    source: str
    source_id: str
    url: str = ""
    raw: dict = _json()
    fetched_at: datetime = Field(default_factory=utcnow)


class CollectorRun(SQLModel, table=True):
    __tablename__ = "collector_runs"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ok: bool = False
    fetched: int = 0
    new: int = 0
    error: str | None = None


# ---------------------------------------------------------------- applying (per user)


class Application(SQLModel, table=True):
    """One row per user per job, from the moment the job passes that user's filters."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "job_id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    status: AppStatus = Field(default=AppStatus.new, index=True)
    score: int | None = None
    score_reason: str = ""
    missing_skills: list = _json(list)
    resume_variant: str | None = None  # path to the tailored PDF
    cover_letter: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))  # per-application note
    answers: dict = _json()  # field label -> value actually filled
    screenshot_path: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    submitted_at: datetime | None = None
    follow_up_at: datetime | None = None


class ApplicationEvent(SQLModel, table=True):
    __tablename__ = "application_events"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="applications.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    kind: str  # status change, dry_run, submitted, reply, follow_up, note
    detail: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AnswerBank(SQLModel, table=True):
    __tablename__ = "answers_bank"
    __table_args__ = (UniqueConstraint("user_id", "question_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    question_hash: str = Field(index=True)
    question: str
    answer: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    source: str = "ai"  # ai | user
    approved: bool = False
    updated_at: datetime = Field(default_factory=utcnow)


class FormProfile(SQLModel, table=True):
    """Field-to-vault-key mapping for a form. No personal data, shared across users."""

    __tablename__ = "form_profiles"
    __table_args__ = (UniqueConstraint("domain", "fingerprint"),)

    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    fingerprint: str
    ats_type: str | None = None
    fields: dict = _json()  # selector -> vault key
    submit: str = ""
    origin: str = "learned"  # builtin | learned
    success_count: int = 0
    fail_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- inbox (per user)


class MailMessage(SQLModel, table=True):
    __tablename__ = "mail_messages"
    __table_args__ = (UniqueConstraint("user_id", "message_id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    application_id: int | None = Field(default=None, foreign_key="applications.id")
    message_id: str
    sender: str = ""
    subject: str = ""
    body: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    received_at: datetime = Field(default_factory=utcnow, index=True)
    category: MailCategory = MailCategory.other
    draft_reply: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    forwarded: bool = False


class LlmUsage(SQLModel, table=True):
    __tablename__ = "llm_usage"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    task: str  # score | resume | cover_letter | answer | form_map | classify
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    """Global runtime switches editable from the admin page (kill switch, schedule)."""

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str = ""
