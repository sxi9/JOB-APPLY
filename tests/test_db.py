import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from jobapply.db import make_engine, upgrade
from jobapply.models import Application, AppStatus, Job, User
from jobapply.repo import UserScope


def _user(session, name):
    u = User(username=name, password_hash="x")
    session.add(u)
    session.commit()
    return u


def _job(session, key="k1"):
    j = Job(dedupe_key=key, title="Engineer", company="Acme")
    session.add(j)
    session.commit()
    return j


def test_migrations_create_all_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    tables = set(inspect(make_engine(url)).get_table_names())
    for t in ("users", "jobs", "job_sources", "applications", "answers_bank", "form_profiles",
              "credentials", "collector_runs", "user_sites", "mail_messages", "llm_usage"):
        assert t in tables


def test_user_scope_isolation(session):
    alice, bob = _user(session, "alice"), _user(session, "bob")
    job = _job(session)
    app = Application(user_id=alice.id, job_id=job.id)
    session.add(app)
    session.commit()

    assert UserScope(session, alice).application_for(job.id) is not None
    assert UserScope(session, bob).application_for(job.id) is None
    assert UserScope(session, bob).get(Application, app.id) is None
    with pytest.raises(PermissionError):
        UserScope(session, bob).set_status(app, AppStatus.skipped)


def test_set_status_records_event(session):
    alice = _user(session, "alice")
    job = _job(session)
    app = Application(user_id=alice.id, job_id=job.id)
    session.add(app)
    session.commit()
    scope = UserScope(session, alice)
    scope.set_status(app, AppStatus.queued, "score 88")
    session.commit()
    assert [e.kind for e in scope.events(app)] == ["queued"]
    assert app.updated_at.tzinfo is not None


def test_one_application_per_user_and_job(session):
    alice = _user(session, "alice")
    job = _job(session)
    session.add(Application(user_id=alice.id, job_id=job.id))
    session.commit()
    session.add(Application(user_id=alice.id, job_id=job.id))
    with pytest.raises(IntegrityError):
        session.commit()
