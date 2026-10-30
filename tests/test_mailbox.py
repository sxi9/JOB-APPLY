"""Mailbox settings: provider presets, saving into the Vault, the connection test (fake IMAP/SMTP), the
Settings card and the onboarding step (CSRF, per-user), and the `jobapply mail` CLI."""

import re
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from typer.testing import CliRunner

from jobapply.config import get_settings
from jobapply.mail import mailbox as mailbox_mod
from jobapply.mail.mailbox import (
    PROVIDERS,
    MailboxSettings,
    clear_mailbox,
    mailbox_status,
    provider_for,
    save_mailbox,
    status_chip,
)
from jobapply.mail.mailbox import test_mailbox as run_test  # not `test_*`: pytest would collect it
from jobapply.models import Credential, User, utcnow
from jobapply.seed import seed
from jobapply.users import create_user
from jobapply.vault import Vault, generate_key
from jobapply.web.app import create_app

VAULT_KEY = "Jl0tE_KZf7aYyq3W_e7u6bSJ5l1W9mY2y5V7q9cN6uM="
APP_PW = "abcd efgh ijkl mnop"

# ----------------------------------------------------------------- fakes


class FakeImap:
    instances: list = []

    def __init__(self, cfg, messages=None, fail=None):
        self.cfg = cfg
        self.messages = {str(i + 1): m for i, m in enumerate(messages or [])}
        self.fail = fail
        self.fetched = []
        self.logged_out = False
        FakeImap.instances.append(self)
        if fail == "login":
            import imaplib

            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    def select(self, folder):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        return "OK", [" ".join(self.messages).encode()]

    def fetch(self, uid, what):
        self.fetched.append(what)
        return "OK", [(f"{uid} ({what} {{{len(self.messages[uid])}}}".encode(), self.messages[uid]), b")"]

    def store(self, uid, op, flag):
        raise AssertionError("the connection test must never change flags")

    def logout(self):
        self.logged_out = True
        return "BYE", []


class FakeSmtp:
    instances: list = []

    def __init__(self, cfg, fail=False):
        self.cfg = cfg
        self.quit_called = False
        FakeSmtp.instances.append(self)
        if fail:
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        raise AssertionError("the connection test must never send")

    def quit(self):
        self.quit_called = True


def raw(sender, subject, body):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "demo.applies@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(utcnow())
    msg["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    FakeImap.instances, FakeSmtp.instances = [], []
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", VAULT_KEY)
    get_settings.cache_clear()
    monkeypatch.setattr(
        mailbox_mod, "default_imap_factory",
        lambda cfg: FakeImap(cfg, [raw("no-reply@workday.com", "Your Workday code", "Your code is 123456 (body)")]),
    )
    monkeypatch.setattr(mailbox_mod, "default_smtp_factory", lambda cfg: FakeSmtp(cfg))
    yield
    get_settings.cache_clear()


@pytest.fixture
def user(session):
    u = create_user(session, "maily", "longpassword1", profile={"first_name": "Mai", "last_name": "Ly"})
    session.commit()
    return u


# ----------------------------------------------------------------- settings parsing


def test_provider_presets_and_detection():
    assert PROVIDERS["gmail"]["imap_host"] == "imap.gmail.com" and PROVIDERS["gmail"]["imap_port"] == 993
    assert PROVIDERS["gmail"]["smtp_host"] == "smtp.gmail.com" and PROVIDERS["gmail"]["smtp_port"] == 587
    assert provider_for("me@gmail.com") == "gmail" and provider_for("me@outlook.com") == "outlook"
    assert provider_for("me@yahoo.co.in") == "custom" and provider_for("me@yahoo.com") == "yahoo"
    assert provider_for("me@corp.example") == "custom"
    s = MailboxSettings.from_form({"email": "me@gmail.com", "password": APP_PW})
    assert (s.provider, s.imap_host, s.imap_port, s.smtp_host, s.smtp_port) == (
        "gmail", "imap.gmail.com", 993, "smtp.gmail.com", 587)
    s = MailboxSettings.from_form({"provider": "custom", "email": "me@corp.example", "password": "x",
                                   "imap_host": "MAIL.corp.example", "imap_port": "143",
                                   "smtp_host": "mail.corp.example", "smtp_port": "465"})
    assert s.imap_host == "mail.corp.example" and s.imap_port == 143 and s.smtp_port == 465
    s.validate()
    with pytest.raises(ValueError, match="IMAP host"):
        MailboxSettings.from_form({"provider": "custom", "email": "me@corp.example", "password": "x"}).validate()
    with pytest.raises(ValueError, match="address"):
        MailboxSettings.from_form({"email": "nope", "password": "x"}).validate()
    with pytest.raises(ValueError, match="must be a number"):
        MailboxSettings.from_form({"email": "me@gmail.com", "imap_port": "abc"})
    assert status_chip("ok") == "green" and status_chip("imap_failed") == "red" and status_chip("missing") == "gray"


# ----------------------------------------------------------------- save / status / clear


def test_save_mailbox_stores_imap_and_smtp_and_no_password_on_profile(session, user):
    assert mailbox_status(session, user)["status"] == "missing"
    info = save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    assert "password" not in info and info["status"] == "untested"
    imap = Vault().load(session, user.id, "mailbox", "imap")
    smtp = Vault().load(session, user.id, "mailbox", "smtp")
    assert imap == {"username": "mai@gmail.com", "password": APP_PW, "host": "imap.gmail.com", "port": "993"}
    assert smtp == {"username": "mai@gmail.com", "password": APP_PW, "host": "smtp.gmail.com", "port": "587"}
    for cred in session.exec(select(Credential).where(Credential.user_id == user.id)).all():
        assert APP_PW.encode() not in cred.secret_blob
    assert APP_PW not in str(user.profile)
    st = mailbox_status(session, user)
    assert st["configured"] and st["email"] == "mai@gmail.com" and st["provider"] == "gmail"
    assert st["status"] == "untested" and st["smtp_host"] == "smtp.gmail.com"
    # an empty password keeps the saved one; a changed host resets the test status
    user.profile = {**user.profile, "mailbox": {**user.profile["mailbox"], "status": "ok"}}
    session.add(user)
    session.commit()
    save_mailbox(session, user, MailboxSettings.from_form({"provider": "custom", "email": "mai@gmail.com",
                                                           "imap_host": "imap.other.example",
                                                           "smtp_host": "smtp.other.example"}))
    assert Vault().load(session, user.id, "mailbox", "imap")["password"] == APP_PW
    assert Vault().load(session, user.id, "mailbox", "imap")["host"] == "imap.other.example"
    assert user.profile["mailbox"]["status"] == "untested"
    # nothing saved at all and no password -> error
    fresh = create_user(session, "nopw", "longpassword1")
    session.commit()
    with pytest.raises(ValueError, match="App password"):
        save_mailbox(session, fresh, MailboxSettings.from_form({"email": "nopw@gmail.com"}))
    assert clear_mailbox(session, user) == 2
    assert mailbox_status(session, user)["status"] == "missing" and "mailbox" not in user.profile


# ----------------------------------------------------------------- connection test


def test_test_mailbox_ok_reads_only_the_newest_subject(session, user):
    save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    result = run_test(session, user)
    assert result.ok and result.imap_ok and result.smtp_ok and result.status == "ok"
    assert result.newest_subject == "Your Workday code" and result.newest_from == "no-reply@workday.com"
    assert "Mailbox works" in result.summary() and "Your Workday code" in result.summary()
    fake = FakeImap.instances[-1]
    assert fake.fetched == ["(BODY.PEEK[HEADER])"] and fake.logged_out  # headers only, connection closed
    assert fake.cfg.host == "imap.gmail.com" and fake.cfg.username == "mai@gmail.com" and fake.cfg.timeout == 30.0
    smtp = FakeSmtp.instances[-1]
    assert smtp.cfg.host == "smtp.gmail.com" and smtp.cfg.starttls and smtp.quit_called
    info = user.profile["mailbox"]
    assert info["status"] == "ok" and info["last_test_at"] and info["newest_subject"] == "Your Workday code"
    assert "(body)" not in str(info) and APP_PW not in str(info)
    assert mailbox_status(session, user)["status"] == "ok"


def test_test_mailbox_reports_imap_and_smtp_failures(session, user, monkeypatch):
    assert run_test(session, user).status == "missing"
    save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    monkeypatch.setattr(mailbox_mod, "default_imap_factory", lambda cfg: FakeImap(cfg, fail="login"))
    result = run_test(session, user)
    assert result.status == "imap_failed" and not result.imap_ok and "app password" in result.error
    assert APP_PW not in result.error and APP_PW not in result.summary()
    assert user.profile["mailbox"]["status"] == "imap_failed" and user.profile["mailbox"]["error"] == result.error
    assert not FakeSmtp.instances  # SMTP is not tried when IMAP fails
    monkeypatch.setattr(mailbox_mod, "default_imap_factory", lambda cfg: FakeImap(cfg, []))
    monkeypatch.setattr(mailbox_mod, "default_smtp_factory", lambda cfg: FakeSmtp(cfg, fail=True))
    result = run_test(session, user)
    assert result.status == "smtp_failed" and result.imap_ok and not result.smtp_ok
    assert "wrong address or app password" in result.error and result.newest_subject == ""
    assert "SMTP login failed" in result.summary()
    # empty inbox is still ok
    monkeypatch.setattr(mailbox_mod, "default_smtp_factory", lambda cfg: FakeSmtp(cfg))
    result = run_test(session, user)
    assert result.ok and "inbox is empty" in result.summary()
    # factories can be injected explicitly, and record=False leaves the profile alone
    before = dict(user.profile["mailbox"])
    out = run_test(session, user, imap_factory=lambda cfg: FakeImap(cfg, fail="login"), record=False)
    assert out.status == "imap_failed" and user.profile["mailbox"] == before


def test_mailbox_status_survives_a_rotated_vault_key(session, user, monkeypatch):
    save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())  # rotated after the mailbox was saved
    get_settings.cache_clear()
    st = mailbox_status(session, user)  # must not raise: the Settings / onboarding pages render from it
    assert st["configured"] and st["status"] == "unreadable" and "different vault key" in st["error"]
    assert st["email"] == "mai@gmail.com" and status_chip(st["status"]) == "red"
    assert clear_mailbox(session, user) == 2 and mailbox_status(session, user)["status"] == "missing"


def test_newest_subject_is_stored_with_digit_runs_masked(session, user, monkeypatch):
    save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    monkeypatch.setattr(mailbox_mod, "default_imap_factory",
                        lambda cfg: FakeImap(cfg, [raw("no-reply@workday.com", "482913 is your Workday code", "x")]))
    result = run_test(session, user)
    assert result.ok and result.newest_subject == "**** is your Workday code"
    assert "482913" not in result.summary() and "482913" not in str(user.profile)


def test_test_mailbox_without_vault_key(session, user, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", "")  # an env override beats any .env on the dev box
    get_settings.cache_clear()
    st = mailbox_status(session, user)
    assert st["vault_ready"] is False and st["status"] == "missing" and not st["configured"]
    assert run_test(session, user).status == "missing"


# ----------------------------------------------------------------- web: settings card and onboarding step


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(engine.url.database) + "-data")
    get_settings.cache_clear()
    seed(engine)
    app = create_app(engine=engine, secret_key="test-secret")
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def _csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def login(client, username, password):
    csrf = _csrf(client.get("/login").text)
    client.post("/login", data={"username": username, "password": password, "csrf": csrf, "next": "/"})
    return csrf


def signup(client, username, password):
    csrf = _csrf(client.get("/signup").text)
    client.post("/signup", data={"username": username, "password": password, "confirm": password, "csrf": csrf})
    return csrf


def _user(engine, username):
    with Session(engine) as s:
        return s.exec(select(User).where(User.username == username)).one()


def test_settings_mailbox_card_save_test_and_remove(client, engine):
    csrf = login(client, "demo", "demo1234")
    page = client.get("/settings").text
    assert 'id="mailbox"' in page and 'action="/settings/mailbox"' in page and "App passwords" in page
    assert "docs/MAILBOX.md" in page and 'name="provider"' in page and 'value="gmail"' in page
    assert page.index('id="mailbox"') < page.index('id="credentials"')  # above the generic Credentials form
    assert 'id="mailbox-status"' in page and ">missing<" in page
    # CSRF is required
    assert client.post("/settings/mailbox", data={"email": "demo@gmail.com", "password": APP_PW}).status_code == 403
    assert client.post("/settings/mailbox/test", data={}).status_code == 403
    resp = client.post("/settings/mailbox", data={"csrf": csrf, "provider": "gmail", "email": "demo@gmail.com",
                                                  "password": APP_PW})
    assert resp.status_code == 303 and resp.headers["location"] == "/settings#mailbox"
    page = client.get("/settings").text
    assert "Mailbox saved for demo@gmail.com" in page and APP_PW not in page and ">untested<" in page
    assert "saved, never shown" in page and 'value="imap.gmail.com"' in page
    demo = _user(engine, "demo")
    with Session(engine) as s:
        assert Vault().load(s, demo.id, "mailbox", "imap")["host"] == "imap.gmail.com"
        assert Vault().load(s, demo.id, "mailbox", "smtp")["password"] == APP_PW
    assert "password" not in demo.profile["mailbox"]
    # test connection -> status chip + newest subject, never a body
    resp = client.post("/settings/mailbox/test", data={"csrf": csrf})
    assert resp.status_code == 303
    page = client.get("/settings").text
    assert "Mailbox works" in page and ">ok<" in page and "Your Workday code" in page and "(body)" not in page
    assert _user(engine, "demo").profile["mailbox"]["status"] == "ok"
    # save-and-test in one go, blank password keeps the saved one
    resp = client.post("/settings/mailbox", data={"csrf": csrf, "provider": "gmail", "email": "demo@gmail.com",
                                                  "password": "", "action": "test"})
    assert resp.status_code == 303
    page = client.get("/settings").text
    assert "Mailbox saved" in page and "Mailbox works" in page
    # a failing IMAP login shows a reason and a red chip, no password anywhere
    FakeImap.instances.clear()
    import jobapply.mail.mailbox as mm

    original = mm.default_imap_factory
    mm.default_imap_factory = lambda cfg: FakeImap(cfg, fail="login")
    try:
        client.post("/settings/mailbox/test", data={"csrf": csrf})
    finally:
        mm.default_imap_factory = original
    page = client.get("/settings").text
    assert "IMAP login failed" in page and ">imap failed<" in page and APP_PW not in page
    # bad input is flashed, nothing changes
    resp = client.post("/settings/mailbox", data={"csrf": csrf, "provider": "custom", "email": "demo@gmail.com"})
    assert resp.status_code == 303 and "Mailbox not saved: IMAP host" in client.get("/settings").text
    # remove
    resp = client.post("/settings/mailbox/delete", data={"csrf": csrf})
    assert resp.status_code == 303
    page = client.get("/settings").text
    assert "Mailbox removed" in page and ">missing<" in page
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        assert not s.exec(select(Credential).where(Credential.user_id == demo.id, Credential.site == "mailbox")).all()
        assert "mailbox" not in demo.profile


def test_settings_page_renders_with_an_unreadable_mailbox(client, engine, monkeypatch):
    csrf = login(client, "demo", "demo1234")
    client.post("/settings/mailbox", data={"csrf": csrf, "email": "demo@gmail.com", "password": APP_PW})
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    page = client.get("/settings")
    assert page.status_code == 200 and "different vault key" in page.text and ">unreadable<" in page.text
    assert client.get("/onboarding?step=mailbox").status_code == 200
    assert client.post("/settings/mailbox/delete", data={"csrf": csrf}).status_code == 303  # recovery works
    assert ">missing<" in client.get("/settings").text


def test_settings_mailbox_is_per_user(client, engine):
    csrf = login(client, "demo", "demo1234")
    client.post("/settings/mailbox", data={"csrf": csrf, "email": "demo@gmail.com", "password": APP_PW})
    client.post("/logout", data={"csrf": csrf})
    csrf = login(client, "admin", "admin1234")
    page = client.get("/settings").text
    assert "demo@gmail.com" not in page and ">missing<" in page
    client.post("/settings/mailbox/delete", data={"csrf": csrf})  # admin's own (empty) mailbox
    with Session(engine) as s:
        demo = s.exec(select(User).where(User.username == "demo")).one()
        assert Vault().load(s, demo.id, "mailbox", "imap")["username"] == "demo@gmail.com"  # untouched


def test_onboarding_mailbox_step(client, engine):
    csrf = signup(client, "mailwiz", "longpassword1")
    nav = client.get("/onboarding").text
    assert "6. Mailbox" in nav and "7. Linkedin" in nav
    # answers now leads to the mailbox step
    resp = client.post("/onboarding/answers", data={"csrf": csrf, "notice_period": "30 days"})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=mailbox"
    page = client.get("/onboarding?step=mailbox").text
    assert 'action="/onboarding/mailbox"' in page and "App passwords" in page and "docs/MAILBOX.md" in page
    assert "{{" not in page and "Skip for now" in page and 'action="/onboarding/mailbox/skip"' in page
    assert client.post("/onboarding/mailbox", data={"email": "w@gmail.com", "password": APP_PW}).status_code == 403
    resp = client.post("/onboarding/mailbox", data={"csrf": csrf, "provider": "gmail", "email": "w@gmail.com",
                                                    "password": APP_PW, "action": "test"})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=mailbox"
    page = client.get("/onboarding?step=mailbox").text
    assert "Mailbox saved for w@gmail.com" in page and "Mailbox works" in page and ">ok<" in page
    assert APP_PW not in page and "(body)" not in page and "Continue" in page
    u = _user(engine, "mailwiz")
    assert u.profile["mailbox"]["status"] == "ok" and u.profile["mailbox"]["email"] == "w@gmail.com"
    with Session(engine) as s:
        assert Vault().load(s, u.id, "mailbox", "smtp")["host"] == "smtp.gmail.com"
    resp = client.post("/onboarding/mailbox/test", data={"csrf": csrf})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=mailbox"
    resp = client.post("/onboarding/mailbox/skip", data={"csrf": csrf})
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding?step=linkedin"
    assert client.post("/onboarding/mailbox/skip", data={}).status_code == 403
    # another user cannot see it
    client.post("/logout", data={"csrf": csrf})
    signup(client, "other", "longpassword1")
    page = client.get("/onboarding?step=mailbox").text
    assert "w@gmail.com" not in page and ">missing<" in page


# ----------------------------------------------------------------- CLI


def test_cli_mail_test_and_wait_code(engine, monkeypatch):
    from jobapply import db as db_mod
    from jobapply.cli_mail import mail_app

    monkeypatch.setattr(db_mod, "get_engine", lambda: engine)
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    with Session(engine, expire_on_commit=False) as s:
        u = create_user(s, "cli", "longpassword1")
        s.commit()
        uid = u.id
    runner = CliRunner()
    out = runner.invoke(mail_app, ["test", "-u", "cli"])
    assert out.exit_code == 1 and "status=missing" in out.output
    assert runner.invoke(mail_app, ["test", "-u", "nobody"]).exit_code == 2
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, uid)
        save_mailbox(s, u, MailboxSettings.from_form({"email": "cli@gmail.com", "password": APP_PW}))
    out = runner.invoke(mail_app, ["test", "-u", "cli"])
    assert out.exit_code == 0 and "status=ok imap=ok smtp=ok" in out.output and "Your Workday code" in out.output
    assert APP_PW not in out.output
    # wait-code goes through ImapReader's default client: patch it to the fake inbox
    from jobapply.mail import imap as imap_mod

    monkeypatch.setattr(
        imap_mod, "_default_client",
        lambda cfg: FakeImap(cfg, [raw("no-reply@workday.com", "Your code", "Your verification code is 424242")]),
    )
    out = runner.invoke(mail_app, ["wait-code", "-u", "cli", "--sender", "workday", "--timeout", "1",
                                   "--poll", "0.05", "--since-minutes", "10"])
    assert out.exit_code == 0 and "code found: 6 digits" in out.output and "424242" not in out.output
    # consumed once: the registry refuses it the second time
    out = runner.invoke(mail_app, ["wait-code", "-u", "cli", "--sender", "workday", "--timeout", "0.3",
                                   "--poll", "0.05", "--since-minutes", "10"])
    assert out.exit_code == 1 and "no code within" in out.output
    out = runner.invoke(mail_app, ["wait-code", "-u", "cli", "--sender", "workday", "--timeout", "0.3",
                                   "--poll", "0.05", "--since-minutes", "10", "--allow-reuse", "--show"])
    assert out.exit_code == 0 and "424242" in out.output
    # default: only mail newer than the command start, so this (older) mail is ignored
    monkeypatch.setattr(
        imap_mod, "_default_client",
        lambda cfg: FakeImap(cfg, [raw_at("no-reply@workday.com", "Your code", "Your verification code is 515151")]),
    )
    out = runner.invoke(mail_app, ["wait-code", "-u", "cli", "--sender", "workday", "--timeout", "0.3",
                                   "--poll", "0.05"])
    assert out.exit_code == 1
    get_settings.cache_clear()


def raw_at(sender, subject, body):
    from datetime import timedelta

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "x@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(utcnow() - timedelta(minutes=30))
    msg["Message-ID"] = "<old@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


def test_save_mailbox_fills_a_blank_profile_email_but_never_overwrites_one(session, user):
    user.profile = {**(user.profile or {}), "email": ""}
    session.add(user)
    session.commit()
    save_mailbox(session, user, MailboxSettings.from_form({"email": "mai@gmail.com", "password": APP_PW}))
    assert user.profile["email"] == "mai@gmail.com"
    user.profile = {**user.profile, "email": "me@work.example"}
    session.add(user)
    session.commit()
    save_mailbox(session, user, MailboxSettings.from_form({"email": "other@gmail.com", "password": APP_PW}))
    assert user.profile["email"] == "me@work.example"
