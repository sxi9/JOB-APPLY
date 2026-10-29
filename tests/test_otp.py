"""One-time code extraction, sender/subject/after filters, the used-code registry and the wait helpers.
No network: the IMAP client is an in-memory fake."""

import imaplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from jobapply.mail import otp as otp_mod
from jobapply.mail.imap import ImapReader, Mail, MailboxConfig
from jobapply.mail.otp import (
    SENDER_PRESETS,
    CodeRegistry,
    code_from_mail,
    extract_code,
    mail_filter,
    mask_digits,
    sender_pattern,
    wait_for_code,
    wait_for_link,
)
from jobapply.models import AppSetting, User, utcnow
from jobapply.switches import get_value

# ----------------------------------------------------------------- fakes


class FakeImap:
    def __init__(self, messages):
        self.messages = {str(i + 1): m for i, m in enumerate(messages)}
        self.calls = []

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, folder):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        self.calls.append(criteria)
        return "OK", [" ".join(self.messages).encode()]

    def fetch(self, uid, what):
        if uid not in self.messages:
            return "NO", [None]
        return "OK", [(f"{uid} ({what[1:-1]} {{{len(self.messages[uid])}}}".encode(), self.messages[uid]), b")"]

    def store(self, uid, op, flag):
        return "OK", []

    def logout(self):
        return "BYE", []


def raw(sender, subject, body, html=None, when=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "demo.applies@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when or utcnow())
    msg["Message-ID"] = f"<{abs(hash((sender, subject, body, str(when))))}@example.com>"
    if body or not html:
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")
    return msg.as_bytes()


CFG = MailboxConfig(host="imap.example.com", username="demo.applies@example.com", password="pw")


def reader(messages):
    fake = FakeImap(messages)
    return ImapReader(CFG, client_factory=lambda cfg: fake), fake


# ----------------------------------------------------------------- extraction table


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your verification code is 482913. It expires in 10 minutes.", "482913"),
        ("Use OTP 7731 to sign in", "7731"),
        ("771234 is your Naukri login code", "771234"),
        ("Your security code: 123 456", "123456"),
        ("Your verification code is 48 29 13", "482913"),
        ("Your Workday one-time passcode is 12-34-56", "123456"),
        ("Enter the code: 1234-5678", "12345678"),
        ("Your Google verification code is G-123456", "123456"),
        ("Verify your email\nPlease enter 304915 to continue", "304915"),
        ("Hi, your login code is 5566 (valid 15 minutes). Job ID 778899.", "5566"),
        ("Thanks for applying in 2026. Call 1800 123 4567 for help. Your code is 2048.", "2048"),
        ("Your code is 482913.\n\n2026 Acme Inc", "482913"),
        ("© 2026 Workday, Inc. Your code is 991122", "991122"),
        ("<html><body><p>Your code is</p><p><b>5 5 4 3 2 1</b></p><p>Call +91 98765 43210</p></body></html>", "554321"),
        ("<div>Security code<br><span style='font-size:2em'>88 22 11</span></div>", "882211"),
        # no code word before the digits: the spaced form is as good as the contiguous one
        ("Use 123 456 to confirm", "123456"),
        ("Enter 123 456 to verify your email", "123456"),
        ("Enter 12-34-56 to verify your email", "123456"),
        # not codes
        ("Shipment of 12 34 56 78 units left the warehouse", None),
        ("Order #12345678 shipped on 2026-10-28", None),
        ("Ref: 445566 - your application was received", None),
        ("Meeting at 10:30 on 28/10/2026, ticket #998877", None),
        ("Posted 10.28.26 and 1-5-2026", None),
        ("Call us on +1 (555) 010-0100 or 020 7946 0958", None),
        ("Candidate ID 123456 has been updated", None),
        ("No numbers at all", None),
        ("", None),
    ],
)
def test_extract_code_table(text, expected):
    assert extract_code(text) == expected


def test_extract_code_respects_lengths():
    assert extract_code("Your PIN is 123", min_len=4) is None
    assert extract_code("Your PIN is 123", min_len=3) is None  # 3-digit runs are not scanned
    assert extract_code("Your code is 12345678", max_len=6) is None
    assert extract_code("Your code is 123456", min_len=6, max_len=6) == "123456"


def test_code_from_mail_uses_html_when_body_is_empty():
    m = Mail(uid="1", message_id="x", sender="noreply@workday.com", sender_name="", subject="Your code",
             body="", html="<table><tr><td>Your verification code is</td></tr><tr><td><b>667788</b></td></tr></table>")
    assert code_from_mail(m) == "667788"
    # the subject alone is enough
    m2 = Mail(uid="2", message_id="y", sender="a@b.com", sender_name="", subject="334455 is your login code", body="")
    assert code_from_mail(m2) == "334455"


# ----------------------------------------------------------------- filters


def test_sender_presets_and_filter():
    assert sender_pattern("Workday") == SENDER_PRESETS["workday"]
    assert sender_pattern("stripe.com") == "stripe.com"  # not a preset: passthrough
    wd = Mail(uid="1", message_id="x", sender="no-reply@myworkday.com", sender_name="Workday", subject="Code", body="")
    gh = Mail(uid="2", message_id="y", sender="no-reply@greenhouse.io", sender_name="", subject="Verify", body="")
    assert mail_filter("workday")(wd) and not mail_filter("workday")(gh)
    assert mail_filter("Greenhouse")(gh) and not mail_filter("greenhouse")(wd)  # preset without regex metachars
    assert mail_filter("greenhouse.io")(gh) and not mail_filter("greenhouse.io")(wd)  # plain domain
    assert mail_filter("noreply")(wd) and mail_filter("noreply")(gh)
    assert mail_filter("", subject_words=("verify",))(gh) and not mail_filter("", subject_words=("verify",))(wd)
    assert mail_filter("")(wd)  # empty: everything


def test_filter_after_rejects_old_mail_with_skew():
    now = datetime.now(UTC)
    old = Mail(uid="1", message_id="x", sender="a@b.com", sender_name="", subject="", body="",
               received_at=now - timedelta(minutes=10))
    fresh = Mail(uid="2", message_id="y", sender="a@b.com", sender_name="", subject="", body="",
                 received_at=now + timedelta(seconds=5))
    borderline = Mail(uid="3", message_id="z", sender="a@b.com", sender_name="", subject="", body="",
                      received_at=now - timedelta(seconds=60))
    pred = mail_filter("", after=now)
    assert not pred(old) and pred(fresh) and pred(borderline)  # 2 minutes of clock slack
    assert not mail_filter("", after=now, skew=0)(borderline)
    naive = Mail(uid="4", message_id="w", sender="a@b.com", sender_name="", subject="", body="",
                 received_at=(now + timedelta(seconds=1)).replace(tzinfo=None))
    assert mail_filter("", after=now.replace(tzinfo=None))(naive)


# ----------------------------------------------------------------- wait_for_code / wait_for_link


def test_wait_for_code_only_accepts_mail_after_the_request():
    now = datetime.now(UTC)
    r, _ = reader([
        raw("no-reply@workday.com", "Your code", "Your verification code is 111111", when=now - timedelta(minutes=5)),
    ])
    assert wait_for_code(r, sender="workday", timeout=0.2, poll=0.05) == "111111"  # no `after`: recent mail is fine
    assert wait_for_code(r, sender="workday", timeout=0.2, poll=0.05, after=now) is None
    r2, _ = reader([
        raw("no-reply@workday.com", "Your code", "Your verification code is 111111", when=now - timedelta(minutes=5)),
        raw("no-reply@workday.com", "Your code", "Your verification code is 222222", when=now + timedelta(seconds=3)),
    ])
    assert wait_for_code(r2, sender="workday", timeout=1, poll=0.05, after=now) == "222222"


def test_wait_for_code_newest_first_and_subject_words():
    now = datetime.now(UTC)
    r, _ = reader([
        raw("noreply@greenhouse.io", "Verify your email", "Your code is 333333", when=now - timedelta(minutes=2)),
        raw("noreply@greenhouse.io", "Verify your email", "Your code is 444444", when=now - timedelta(minutes=1)),
        raw("jobs@greenhouse.io", "Jobs you may like", "Your lucky number is 555555", when=now),
    ])
    assert wait_for_code(r, sender="greenhouse", subject_words=("verify",), timeout=1, poll=0.05) == "444444"
    assert wait_for_code(r, sender="greenhouse", subject_words=("nothing",), timeout=0.2, poll=0.05) is None


def test_wait_for_link_after():
    now = datetime.now(UTC)
    old, new = "Old: https://www.linkedin.com/verify?t=old", "New: https://www.linkedin.com/verify?t=new"
    r, _ = reader([
        raw("noreply@linkedin.com", "Verify", old, when=now - timedelta(hours=1)),
        raw("noreply@linkedin.com", "Verify", new, when=now + timedelta(seconds=2)),
    ])
    assert wait_for_link(r, sender="linkedin", timeout=1, poll=0.05, after=now) == "https://www.linkedin.com/verify?t=new"
    assert wait_for_link(r, sender="linkedin", timeout=0.2, poll=0.05, after=now + timedelta(minutes=10)) is None


def test_wait_for_tolerates_a_transient_imap_error():
    class Flaky(FakeImap):
        def __init__(self, messages):
            super().__init__(messages)
            self.failed = False

        def search(self, charset, *criteria):
            if not self.failed:
                self.failed = True
                raise OSError("connection reset")
            return super().search(charset, *criteria)

    fake = Flaky([raw("no-reply@workday.com", "Your code", "Your verification code is 777777")])
    r = ImapReader(CFG, client_factory=lambda cfg: fake)
    assert wait_for_code(r, sender="workday", timeout=2, poll=0.05) == "777777"


def test_wait_for_raises_on_a_refused_login_without_retrying():
    attempts: list[int] = []

    def refused(cfg):
        attempts.append(1)
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    with pytest.raises(imaplib.IMAP4.error):
        wait_for_code(ImapReader(CFG, client_factory=refused), sender="workday", timeout=1, poll=0.05)
    assert len(attempts) == 1  # a wrong app password is not retried on every poll
    # a network hiccup while connecting is still transient: retried, the mail is found on the next poll
    calls: list[int] = []
    fake = FakeImap([raw("no-reply@workday.com", "Your code", "Your verification code is 777777")])

    def flaky(cfg):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("connection reset")
        return fake

    assert wait_for_code(ImapReader(CFG, client_factory=flaky), sender="workday", timeout=2, poll=0.05) == "777777"
    assert len(calls) == 2


def test_polling_never_marks_mail_as_read():
    class Recording(FakeImap):
        def __init__(self, messages):
            super().__init__(messages)
            self.fetched = []

        def fetch(self, uid, what):
            self.fetched.append(what)
            return super().fetch(uid, what)

    fake = Recording([raw("no-reply@workday.com", "Your code", "Your verification code is 313131")])
    r = ImapReader(CFG, client_factory=lambda cfg: fake)
    assert wait_for_code(r, sender="workday", timeout=0.5, poll=0.05) == "313131"
    assert len(r.recent(limit=5)) == 1
    assert fake.fetched and all(what == "(BODY.PEEK[])" for what in fake.fetched)  # never RFC822 (sets \Seen)


def test_otp_log_lines_never_carry_the_code(monkeypatch, session):
    assert mask_digits("482913 is your Workday code") == "**** is your Workday code"
    assert mask_digits("Your code: 48 29 13 (ref 12)") == "Your code: **** (ref 12)"
    assert mask_digits("") == ""
    events: list[tuple[str, dict]] = []

    class Recorder:
        def info(self, msg, **kw):
            events.append((msg, kw))

        debug = warning = info

    monkeypatch.setattr(otp_mod, "log", Recorder())
    u = User(username="otplog", password_hash="x")
    session.add(u)
    session.commit()
    reg = CodeRegistry(session, u.id)
    mails = [raw("no-reply@workday.com", "482913 is your Workday verification code", "Use 482913 to verify")]
    r, _ = reader(mails)
    assert wait_for_code(r, sender="workday", timeout=0.5, poll=0.05, registry=reg) == "482913"
    r, _ = reader(mails)
    assert wait_for_code(r, sender="workday", timeout=0.2, poll=0.05, registry=reg) is None  # "already used" line
    assert {msg for msg, _ in events} >= {"otp received", "otp already used, waiting for a newer one"}
    assert all("482913" not in str(kw) for _, kw in events)


# ----------------------------------------------------------------- used-code registry


@pytest.fixture
def user(session):
    u = User(username="otp", password_hash="x")
    session.add(u)
    session.commit()
    return u


def test_registry_marks_and_expires_without_storing_the_code(session, user):
    reg = CodeRegistry(session, user.id)
    assert not reg.is_used("482913") and len(reg) == 0
    reg.mark_used("482913")
    assert reg.is_used("482913") and not reg.is_used("482914") and len(reg) == 1
    stored = get_value(session, f"otp_used:{user.id}")
    assert "482913" not in stored  # only a salted digest is kept
    # another user's registry does not see it
    assert not CodeRegistry(session, user.id + 1).is_used("482913")
    # an expired entry is dropped on the next read
    row = session.get(AppSetting, reg.key)
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    row.value = row.value.replace(row.value[row.value.index('"at"'):row.value.rindex('"')], f'"at": "{old}')
    session.add(row)
    session.commit()
    assert not reg.is_used("482913")
    # garbage in the setting is treated as empty
    row.value = "not json"
    session.add(row)
    session.commit()
    assert len(reg) == 0 and not reg.is_used("482913")


def test_wait_for_code_never_consumes_the_same_code_twice(session, user):
    now = datetime.now(UTC)
    messages = [raw("no-reply@workday.com", "Your code", "Your verification code is 606060", when=now)]
    reg = CodeRegistry(session, user.id)
    r, _ = reader(messages)
    assert wait_for_code(r, sender="workday", timeout=0.5, poll=0.05, registry=reg) == "606060"
    assert reg.is_used("606060")
    # same inbox again: the stale code is skipped, nothing newer -> None, no loop
    r, _ = reader(messages)
    assert wait_for_code(r, sender="workday", timeout=0.3, poll=0.05, registry=reg) is None
    # a newer mail with a fresh code wins
    r, _ = reader(messages + [
        raw("no-reply@workday.com", "Your code", "Your verification code is 707070", when=now + timedelta(seconds=5)),
    ])
    assert wait_for_code(r, sender="workday", timeout=0.5, poll=0.05, registry=reg) == "707070"
    assert reg.is_used("707070") and len(reg) == 2
