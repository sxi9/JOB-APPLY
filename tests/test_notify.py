import json

import httpx
from sqlmodel import select

from jobapply import notify as notify_mod
from jobapply.config import Settings
from jobapply.models import Application, AppStatus, Job, User
from jobapply.notify import LogNotifier, MultiNotifier, WhatsAppNotifier, build_notifier, notify, set_notifier
from jobapply.pipeline.summary import send_summaries, summary_for


def _user(number="+919876543210", alerts=None):
    prefs = {"whatsapp_number": number}
    if alerts is not None:
        prefs["alert_types"] = alerts
    return User(username="demo", password_hash="x", preferences=prefs)


def test_whatsapp_posts_to_bridge():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    n = WhatsAppNotifier(
        "https://wa.example/api/", token="secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert n.send(_user(), "interview", "Interview at Stripe") is True
    req = seen[0]
    assert str(req.url) == "https://wa.example/api/send" and req.headers["authorization"] == "Bearer secret"
    assert json.loads(req.content) == {"to": "+919876543210", "message": "[JOB-APPLY] Interview at Stripe"}
    assert n.send(_user(number=""), "interview", "x") is False and len(seen) == 1


def test_whatsapp_failures_return_false():
    n = WhatsAppNotifier(
        "https://wa.example",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))),
    )
    assert n.send(_user(), "paused", "x") is False

    def raising(req):
        raise httpx.ConnectError("down")

    n = WhatsAppNotifier("https://wa.example", client=httpx.Client(transport=httpx.MockTransport(raising)))
    assert n.send(_user(), "paused", "x") is False


def test_notify_respects_alert_types(session, monkeypatch):
    calls = []

    class Spy:
        def send(self, user, kind, message):
            calls.append(kind)
            return True

    set_notifier(Spy())
    try:
        assert notify(session, _user(alerts=["interview"]), "interview", "m") is True
        assert notify(session, _user(alerts=["interview"]), "paused", "m") is False
        assert notify(session, _user(), "paused", "m") is True  # no preference: everything
        assert calls == ["interview", "paused"]
    finally:
        set_notifier(None)


def test_build_notifier_from_settings():
    assert isinstance(build_notifier(Settings(_env_file=None)), LogNotifier)
    n = build_notifier(Settings(_env_file=None, whatsapp_base_url="https://wa.example", whatsapp_token="t"))
    assert isinstance(n, MultiNotifier) and isinstance(n.backends[1], WhatsAppNotifier)
    assert n.backends[1]._headers == {"Authorization": "Bearer t"}
    assert MultiNotifier(LogNotifier()).send(_user(), "x", "y") is True


def test_daily_summary(session, engine, monkeypatch):
    user = _user()
    session.add(user)
    session.commit()
    job = Job(dedupe_key="k", title="Engineer", company="Acme")
    session.add(job)
    session.flush()
    session.add(Application(user_id=user.id, job_id=job.id, status=AppStatus.queued, score=80))
    session.commit()
    text = summary_for(session, user)
    assert "1 new matches" in text and "1 waiting in the queue" in text
    sent = []
    monkeypatch.setattr(
        notify_mod, "_notifier", type("N", (), {"send": lambda self, u, k, m: sent.append((k, m)) or True})()
    )
    assert send_summaries(engine=engine) == {"demo": True}
    assert sent[0][0] == "daily_summary"
    assert session.exec(select(User)).one().username == "demo"
