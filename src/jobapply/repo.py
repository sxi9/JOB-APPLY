"""Per-user data access. Every query on a per-user table goes through `UserScope`,
which always filters on user_id, so one user can never read another's rows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from sqlmodel import Session, SQLModel, select

from jobapply.models import (
    AnswerBank,
    Application,
    ApplicationEvent,
    AppStatus,
    Credential,
    Job,
    MailMessage,
    User,
    UserSite,
    utcnow,
)

T = TypeVar("T", bound=SQLModel)


class UserScope:
    def __init__(self, session: Session, user: User | int):
        self.session = session
        self.user_id = user if isinstance(user, int) else user.id
        if self.user_id is None:
            raise ValueError("user must be saved before scoping")

    # generic helpers --------------------------------------------------------
    def all(self, model: type[T], *where, order_by=None, limit: int | None = None) -> Sequence[T]:
        stmt = select(model).where(model.user_id == self.user_id, *where)  # type: ignore[attr-defined]
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if limit:
            stmt = stmt.limit(limit)
        return self.session.exec(stmt).all()

    def get(self, model: type[T], obj_id: int) -> T | None:
        obj = self.session.get(model, obj_id)
        if obj is None or getattr(obj, "user_id", None) != self.user_id:
            return None
        return obj

    # applications -----------------------------------------------------------
    def application_for(self, job_id: int) -> Application | None:
        return self.session.exec(
            select(Application).where(Application.user_id == self.user_id, Application.job_id == job_id)
        ).first()

    def applications_with_jobs(self, *where, limit: int = 200) -> list[tuple[Application, Job]]:
        stmt = (
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.user_id == self.user_id, *where)
            .order_by(Application.updated_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(stmt).all())

    def set_status(self, app: Application, status: AppStatus, detail: str = "") -> None:
        if app.user_id != self.user_id:
            raise PermissionError("application belongs to another user")
        app.status = status
        app.updated_at = utcnow()
        self.session.add(app)
        self.session.add(
            ApplicationEvent(application_id=app.id, user_id=self.user_id, kind=status.value, detail=detail)
        )

    def events(self, app: Application) -> Sequence[ApplicationEvent]:
        return self.all(ApplicationEvent, ApplicationEvent.application_id == app.id,
                        order_by=ApplicationEvent.created_at)

    # sites, answers, credentials, mail --------------------------------------
    def site(self, site: str) -> UserSite | None:
        return self.session.exec(
            select(UserSite).where(UserSite.user_id == self.user_id, UserSite.site == site)
        ).first()

    def sites(self) -> Sequence[UserSite]:
        return self.all(UserSite, order_by=UserSite.site)

    def answer(self, question_hash: str) -> AnswerBank | None:
        return self.session.exec(
            select(AnswerBank).where(AnswerBank.user_id == self.user_id,
                                     AnswerBank.question_hash == question_hash)
        ).first()

    def credential(self, site: str, kind: str = "login") -> Credential | None:
        return self.session.exec(
            select(Credential).where(Credential.user_id == self.user_id, Credential.site == site,
                                     Credential.kind == kind)
        ).first()

    def mail(self, *where, limit: int = 100) -> Sequence[MailMessage]:
        return self.all(MailMessage, *where, order_by=MailMessage.received_at.desc(), limit=limit)
