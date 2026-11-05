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
    body = json.loads(req.content)
    # whatsapp-headless /api/send requires the field to be named "text" (400 "to and text are required" otherwise)
    assert body == {"to": "+919876543210", "text": "[JOB-APPLY] Interview at Stripe"}
    assert "message" not in body
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


def test_build_notifier_from_settings(monkeypatch):
    # the box's .env exports the bridge URL/token; Settings(_env_file=None) still reads os.environ
    monkeypatch.delenv("JOBAPPLY_WHATSAPP_BASE_URL", raising=False)
    monkeypatch.delenv("JOBAPPLY_WHATSAPP_TOKEN", raising=False)
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


# ----------------------------------------------------------------- direct targets, groups, health


def _bridge(routes, log=None):
    """A WhatsAppNotifier whose bridge is a MockTransport: `routes` maps a path suffix to (status, json)."""

    def handler(req):
        if log is not None:
            log.append(req)
        for suffix, (status, body) in routes.items():
            if str(req.url).endswith(suffix):
                if isinstance(body, Exception):
                    raise body
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "no route"})

    return WhatsAppNotifier("https://wa.example/api", token="t",
                            client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_send_text_posts_to_a_direct_target_without_prefix():
    seen = []
    n = _bridge({"/send": (200, {"ok": True, "id": "x"})}, seen)
    assert n.send_text("120363012345678901@g.us", "*hello*\nhttps://a/b") is True
    assert json.loads(seen[0].content) == {"to": "120363012345678901@g.us", "text": "*hello*\nhttps://a/b"}
    assert seen[0].headers["authorization"] == "Bearer t"
    assert n.send_text("", "x") is False and n.send_text("+911", "") is False and len(seen) == 1
    bad = _bridge({"/send": (400, {"error": "to and text are required"})})
    assert bad.send_text("+919876543210", "x") is False
    down = _bridge({"/send": (0, httpx.ConnectError("down"))})
    assert down.send_text("+919876543210", "x") is False


def test_send_to_and_alert_targets():
    from jobapply.notify import alert_targets, send_to

    seen = []
    set_notifier(MultiNotifier(LogNotifier(), _bridge({"/send": (200, {"ok": True})}, seen)))
    try:
        assert send_to("+919876543210", "hi") is True and len(seen) == 1
        set_notifier(MultiNotifier(LogNotifier(), _bridge({"/send": (500, {"error": "x"})})))
        assert send_to("+919876543210", "hi") is False  # the log backend alone does not count as delivered
        set_notifier(LogNotifier())
        assert send_to("+919876543210", "hi") is True  # no transport configured: logged only
    finally:
        set_notifier(None)
    u = _user()
    assert alert_targets(u) == ["+919876543210"]
    u.preferences = {"whatsapp_number": "+911", "whatsapp_group_jid": "1@g.us", "alerts_target": "both"}
    assert alert_targets(u) == ["+911", "1@g.us"]
    u.preferences = {"whatsapp_number": "+911", "whatsapp_group_jid": "1@g.us", "alerts_target": "group"}
    assert alert_targets(u) == ["1@g.us"]
    u.preferences = {"whatsapp_number": "", "whatsapp_group_jid": "1@g.us", "alerts_target": "bogus"}
    assert alert_targets(u) == []  # unknown mode falls back to the number, which is empty


def test_list_groups_cache_shapes_and_failure():
    from jobapply.notify import list_groups

    seen = []
    groups = [{"name": "Zeta hiring", "jid": "2@g.us", "size": 3}, {"subject": "alpha", "id": "1@g.us"}]
    set_notifier(MultiNotifier(LogNotifier(), _bridge({"/groups": (200, groups)}, seen)))
    try:
        first = list_groups()
        assert first == [{"name": "alpha", "jid": "1@g.us"}, {"name": "Zeta hiring", "jid": "2@g.us"}]
        assert list_groups() == first and len(seen) == 1  # cached for 10 minutes
        assert list_groups(refresh=True) == first and len(seen) == 2
        assert seen[-1].method == "GET" and str(seen[-1].url) == "https://wa.example/api/groups"
        # a wrapped answer works too; an error answers [] without raising; no bridge at all answers []
        set_notifier(MultiNotifier(LogNotifier(), _bridge({"/groups": (200, {"groups": groups[:1]})})))
        assert list_groups() == [{"name": "Zeta hiring", "jid": "2@g.us"}]
        set_notifier(_bridge({"/groups": (500, {"error": "boom"})}))
        assert list_groups() == []
        set_notifier(_bridge({"/groups": (0, httpx.ReadTimeout("slow"))}))
        assert list_groups() == []
        set_notifier(LogNotifier())
        assert list_groups() == []
    finally:
        set_notifier(None)


def test_bridge_health_masks_the_number():
    from jobapply.notify import bridge_health, mask_target

    set_notifier(MultiNotifier(LogNotifier(), _bridge({"/health": (200, {"status": "open",
                                                                          "me": "919876543210@s.whatsapp.net"})})))
    try:
        h = bridge_health()
        assert h["ok"] is True and h["status"] == "open" and h["me_masked"] == "••••3210"
        assert "919876543210" not in str(h)
        set_notifier(_bridge({"/health": (200, {"status": "connecting"})}))
        assert bridge_health()["ok"] is False and bridge_health()["status"] == "connecting"
        set_notifier(_bridge({"/health": (0, httpx.ConnectError("down"))}))
        assert bridge_health() == {"ok": False, "status": "unreachable", "me_masked": "", "error": "down"}
        set_notifier(LogNotifier())
        assert bridge_health() == {"ok": False, "status": "not configured", "me_masked": ""}
    finally:
        set_notifier(None)
    assert mask_target("+919876543210") == "••••3210"
    assert mask_target("120363012345678901@g.us") == "••••8901@g.us"
    assert mask_target("") == ""
