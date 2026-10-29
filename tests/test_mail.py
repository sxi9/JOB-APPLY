from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
from sqlmodel import select

from jobapply.ai.base import LLMError
from jobapply.ai.fake import FakeLLM
from jobapply.config import get_settings
from jobapply.mail import inbox as inbox_mod
from jobapply.mail.classify import classify_mail, link_application, rule_category, should_update_status
from jobapply.mail.imap import ImapReader, Mail, MailboxConfig, parse_message, sender_matches
from jobapply.mail.inbox import due_follow_ups, mailbox_configs, send_follow_ups, send_reply, sync_inbox
from jobapply.mail.otp import extract_code, extract_links, wait_for_code, wait_for_link
from jobapply.mail.smtp import SmtpConfig, SmtpSender
from jobapply.models import Application, ApplicationEvent, AppStatus, Job, MailCategory, MailMessage, User, utcnow
from jobapply.seed import DEMO_PROFILE
from jobapply.vault import Vault, generate_key

# ----------------------------------------------------------------- fake IMAP / SMTP servers


class FakeImap:
    """The imaplib subset ImapReader uses, over an in-memory mailbox."""

    def __init__(self, messages: list[bytes]):
        self.messages = {str(i + 1): m for i, m in enumerate(messages)}
        self.flags: dict[str, set[str]] = {}
        self.logged_out = False

    def login(self, user, password):
        return "OK", [b"Logged in"]

    def select(self, folder):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        uids = list(self.messages)
        if "UNSEEN" in criteria:
            uids = [u for u in uids if "\\Seen" not in self.flags.get(u, set())]
        return "OK", [" ".join(uids).encode()]

    def fetch(self, uid, what):
        if uid not in self.messages:
            return "NO", [None]
        return "OK", [(f"{uid} (RFC822 {{{len(self.messages[uid])}}}".encode(), self.messages[uid]), b")"]

    def store(self, uid, op, flag):
        self.flags.setdefault(uid, set()).add(flag)
        return "OK", []

    def logout(self):
        self.logged_out = True
        return "BYE", []


class FakeSmtp:
    sent: list[EmailMessage] = []

    def __init__(self, cfg):
        self.cfg = cfg

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)

    def quit(self):
        pass


def raw(sender, subject, body, html=None, when=None, message_id=None, in_reply_to=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "demo.applies@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when or utcnow())
    msg["Message-ID"] = message_id or f"<{abs(hash(subject + body))}@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


CFG = MailboxConfig(host="imap.example.com", username="demo.applies@example.com", password="pw")


def reader(messages):
    fake = FakeImap(messages)
    return ImapReader(CFG, client_factory=lambda cfg: fake), fake


@pytest.fixture(autouse=True)
def _clear_smtp():
    FakeSmtp.sent = []


@pytest.fixture
def demo(session):
    user = User(
        username="demo",
        password_hash="x",
        profile=DEMO_PROFILE,
        preferences={"forward_email": "me@real.example", "alert_types": ["interview", "assessment", "paused"]},
    )
    session.add(user)
    session.commit()
    return user


def _applied(session, user, company="Stripe", title="Backend Engineer", status=AppStatus.applied, days_ago=10):
    job = Job(dedupe_key=f"{company}-{title}", title=title, company=company, apply_url="https://x/apply")
    session.add(job)
    session.flush()
    app = Application(
        user_id=user.id,
        job_id=job.id,
        status=status,
        submitted_at=utcnow() - timedelta(days=days_ago),
        follow_up_at=utcnow() - timedelta(days=days_ago - 7),
    )
    session.add(app)
    session.commit()
    return app, job


# ----------------------------------------------------------------- parsing and OTP


def test_parse_message_text_and_html():
    m = parse_message(
        "7", raw("Ada Recruiter <ada@stripe.com>", "Re: Backend Engineer", "Plain body", html="<p>Rich <b>body</b></p>")
    )
    assert m.uid == "7" and m.sender == "ada@stripe.com" and m.sender_name == "Ada Recruiter"
    assert m.body == "Plain body" and "<b>" in m.html and m.sender_domain == "stripe.com"
    only_html = parse_message("8", raw("x@y.com", "s", "", html="<div>Only <i>html</i> here</div>"))
    assert "<" not in only_html.body and "Only html here" in only_html.body.replace("\n", " ")


def test_extract_code_and_links():
    assert extract_code("Your verification code is 482913. It expires in 10 minutes.") == "482913"
    assert extract_code("Use OTP 7731 to sign in") == "7731"
    assert extract_code("771234 is your Naukri login code") == "771234"
    assert (
        extract_code("Order #12345678 shipped on 2026-10-28") is None
    )  # 8 digits but no code context, year is 4 digits
    assert extract_code("No numbers at all") is None
    links = extract_links(
        "Click https://acme.example/verify?token=abc123 or https://acme.example/unsubscribe/1",
        html='<a href="https://acme.example/confirm-email/xyz">Confirm</a>',
    )
    assert links == ["https://acme.example/confirm-email/xyz", "https://acme.example/verify?token=abc123"]


def test_wait_for_code_and_link():
    r, _ = reader(
        [
            raw("no-reply@workday.com", "Your code", "Your verification code is 998877"),
            raw("noreply@linkedin.com", "Verify", "Please confirm: https://www.linkedin.com/verify?t=1"),
        ]
    )
    assert wait_for_code(r, sender="workday.com", timeout=1, poll=0.1) == "998877"
    assert wait_for_code(r, sender="greenhouse.io", timeout=0.2, poll=0.1) is None
    assert wait_for_link(r, sender="linkedin.com", timeout=1, poll=0.1) == "https://www.linkedin.com/verify?t=1"


def test_reader_recent_mark_seen_and_sender_matches():
    r, fake = reader([raw("a@b.com", "one", "x"), raw("c@d.com", "two", "y")])
    mails = r.recent()
    assert [m.subject for m in mails] == ["one", "two"]
    r.mark_seen([mails[0].uid])
    assert [m.subject for m in r.recent(unseen_only=True)] == ["two"]
    r.close()
    assert fake.logged_out
    m = Mail(uid="1", message_id="x", sender="ada@mail.stripe.com", sender_name="Ada", subject="", body="")
    assert (
        sender_matches(m, "stripe.com")
        and sender_matches(m, "ada@mail.stripe.com")
        and sender_matches(m, r"stripe|lever")
    )
    assert not sender_matches(m, "lever.co")


# ----------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Verify your email", "Your verification code is 123456", MailCategory.verification),
        ("Interview with Stripe", "We'd like to schedule a call with you next week.", MailCategory.interview),
        ("Next steps", "Please complete this take-home assessment within 5 days.", MailCategory.assessment),
        (
            "Your application",
            "Unfortunately we have decided to move forward with other candidates.",
            MailCategory.rejection,
        ),
        ("Jobs you may like", "Click to unsubscribe from these job alerts", MailCategory.spam),
        (
            "Application received",
            "Thank you for applying to Stripe. We have received your application.",
            MailCategory.other,
        ),
        ("Quick question", "Could you share your notice period?", None),
    ],
)
def test_rule_category(subject, body, expected):
    m = Mail(uid="1", message_id="x", sender="a@b.com", sender_name="", subject=subject, body=body)
    assert rule_category(m) == expected


def test_classify_with_fake_llm_drafts_reply(session, demo):
    m = Mail(
        uid="1",
        message_id="x",
        sender="ada@stripe.com",
        sender_name="Ada",
        subject="Quick question",
        body="Could you share your notice period and expected compensation?",
    )
    out = classify_mail(session, FakeLLM(), demo, m)
    assert out.category == "recruiter_question" and out.needs_reply and "30 days" in out.draft_reply
    spam = Mail(
        uid="2", message_id="y", sender="x@y.com", sender_name="", subject="Jobs you may like", body="unsubscribe"
    )
    assert classify_mail(session, FakeLLM(), demo, spam).category == "spam"
    assert classify_mail(session, None, demo, m).category == "other"  # no LLM: rules only


def test_link_application_by_domain_subject_and_thread(session, demo):
    app, job = _applied(session, demo, "Stripe", "Backend Engineer")
    other, _ = _applied(session, demo, "Ramp", "Platform Engineer")
    m = Mail(
        uid="1", message_id="x", sender="recruiting@stripe.com", sender_name="", subject="Your application", body=""
    )
    assert link_application(session, demo, m).id == app.id
    m2 = Mail(
        uid="2",
        message_id="y",
        sender="hr@agency.example",
        sender_name="",
        subject="Platform Engineer at Ramp",
        body="",
    )
    assert link_application(session, demo, m2).id == other.id
    m3 = Mail(uid="3", message_id="z", sender="someone@nowhere.example", sender_name="", subject="hello", body="")
    assert link_application(session, demo, m3) is None
    app.answers = {"_last_message_id": "<mine@local>"}
    session.add(app)
    session.commit()
    m4 = Mail(
        uid="4",
        message_id="w",
        sender="someone@nowhere.example",
        sender_name="",
        subject="re",
        body="",
        in_reply_to="<mine@local>",
    )
    assert link_application(session, demo, m4).id == app.id


def test_status_never_moves_backwards():
    assert should_update_status(AppStatus.applied, AppStatus.interview)
    assert not should_update_status(AppStatus.interview, AppStatus.replied)
    assert should_update_status(AppStatus.interview, AppStatus.rejected)
    assert not should_update_status(AppStatus.offer, AppStatus.rejected)


# ----------------------------------------------------------------- inbox sync


def test_sync_inbox_end_to_end(session, demo):
    app, job = _applied(session, demo, "Stripe", "Backend Engineer")
    messages = [
        raw(
            "Ada <ada@stripe.com>",
            "Interview for Backend Engineer",
            "We'd like to schedule a call with you on Thursday.",
        ),
        raw("noreply@greenhouse.io", "Verify your email", "Your verification code is 445566"),
        raw("alerts@jobs.example", "Jobs you may like", "unsubscribe here"),
        raw("Ada <ada@stripe.com>", "Re: Interview", "Does Thursday 3pm work?", message_id="<dup@x>"),
    ]
    r, _ = reader(messages)
    sender = SmtpSender(
        SmtpConfig(host="smtp.example.com", username="demo.applies@example.com", password="pw"), client_factory=FakeSmtp
    )
    counts = sync_inbox(session, demo, r, FakeLLM(), sender)
    assert counts["seen"] == 4 and counts["new"] == 4 and counts["linked"] == 2 and counts["status_changes"] == 1
    session.refresh(app)
    assert app.status == AppStatus.interview and app.follow_up_at is None
    rows = {m.subject: m for m in session.exec(select(MailMessage).where(MailMessage.user_id == demo.id)).all()}
    assert rows["Interview for Backend Engineer"].category == MailCategory.interview
    assert rows["Interview for Backend Engineer"].application_id == app.id
    assert rows["Interview for Backend Engineer"].forwarded and rows["Interview for Backend Engineer"].draft_reply
    assert (
        rows["Verify your email"].category == MailCategory.verification
        and rows["Verify your email"].application_id is None
    )
    assert rows["Jobs you may like"].category == MailCategory.spam
    # both interview mails are forwarded; only the first changes the status
    assert len(FakeSmtp.sent) == 2 and FakeSmtp.sent[0]["To"] == "me@real.example"
    assert FakeSmtp.sent[0]["Subject"].startswith("Fwd: Interview")
    # second sync: nothing new, nothing duplicated
    counts2 = sync_inbox(session, demo, reader(messages)[0], FakeLLM(), sender)
    assert counts2["new"] == 0
    assert session.exec(select(MailMessage)).all().__len__() == 4


def test_rejection_updates_status_and_is_not_forwarded(session, demo):
    app, _ = _applied(session, demo, "Stripe", "Backend Engineer")
    r, _ = reader(
        [
            raw(
                "ada@stripe.com",
                "Your application to Stripe",
                "Unfortunately we have decided to move forward with other candidates.",
            )
        ]
    )
    sync_inbox(session, demo, r, FakeLLM(), None)
    session.refresh(app)
    assert app.status == AppStatus.rejected
    assert not session.exec(select(MailMessage)).one().forwarded
    events = session.exec(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)).all()
    assert any(e.kind == "rejected" for e in events)


def test_send_reply_threads_and_records(session, demo):
    app, _ = _applied(session, demo)
    row = MailMessage(
        user_id=demo.id,
        application_id=app.id,
        message_id="<q1@stripe.com>",
        sender="Ada <ada@stripe.com>",
        subject="Quick question",
        body="?",
    )
    session.add(row)
    session.commit()
    sender = SmtpSender(
        SmtpConfig(host="smtp.example.com", username="demo.applies@example.com", password="pw", from_name="Demo User"),
        client_factory=FakeSmtp,
    )
    mid = send_reply(session, demo, row, "Thanks, 30 days.", sender)
    session.commit()
    sent = FakeSmtp.sent[0]
    assert sent["To"] == "ada@stripe.com" and sent["Subject"] == "Re: Quick question"
    assert sent["In-Reply-To"] == "<q1@stripe.com>" and sent["From"] == "Demo User <demo.applies@example.com>"
    session.refresh(app)
    assert app.answers["_last_message_id"] == mid
    assert any(e.kind == "reply_sent" for e in session.exec(select(ApplicationEvent)).all())


def test_follow_ups_send_or_remind(session, demo, monkeypatch):
    reminded = []
    monkeypatch.setattr(inbox_mod, "notify", lambda s, u, kind, msg: reminded.append(msg))
    with_contact, _ = _applied(session, demo, "Stripe", "Backend Engineer", days_ago=10)
    session.add(
        MailMessage(
            user_id=demo.id,
            application_id=with_contact.id,
            message_id="<m@stripe.com>",
            sender="ada@stripe.com",
            subject="Application received",
            body="thanks",
        )
    )
    without_contact, _ = _applied(session, demo, "Ramp", "Platform Engineer", days_ago=10)
    not_due, _ = _applied(session, demo, "Cred", "Data Engineer", days_ago=2)
    session.commit()
    assert {a.id for a, _, _ in due_follow_ups(session, demo)} == {with_contact.id, without_contact.id}
    sender = SmtpSender(
        SmtpConfig(host="smtp.example.com", username="demo.applies@example.com", password="pw"), client_factory=FakeSmtp
    )
    counts = send_follow_ups(session, demo, sender)
    assert counts == {"sent": 1, "reminded": 1, "skipped": 0}
    assert FakeSmtp.sent[0]["To"] == "ada@stripe.com" and "Backend Engineer" in FakeSmtp.sent[0].get_content()
    assert reminded and "Ramp" in reminded[0]
    for a in (with_contact, without_contact):
        session.refresh(a)
        assert a.follow_up_at > utcnow() and a.answers["_follow_ups"] == 1
    # after MAX_FOLLOW_UPS the chaser stops
    with_contact.answers = {**with_contact.answers, "_follow_ups": 2}
    with_contact.follow_up_at = utcnow() - timedelta(hours=1)
    session.add(with_contact)
    session.commit()
    assert send_follow_ups(session, demo, sender)["skipped"] == 1
    session.refresh(with_contact)
    assert with_contact.follow_up_at is None


def test_mailbox_configs_from_vault(session, demo, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    get_settings.cache_clear()
    try:
        assert mailbox_configs(session, demo) == (None, None)
        Vault().store(
            session,
            demo.id,
            "mailbox",
            "imap",
            "demo.applies@example.com",
            {"password": "app-pw", "host": "imap.gmail.com", "port": "993"},
        )
        session.commit()
        imap, smtp = mailbox_configs(session, demo)
        assert imap.host == "imap.gmail.com" and imap.port == 993 and imap.username == "demo.applies@example.com"
        assert smtp.host == "smtp.gmail.com" and smtp.port == 587 and smtp.from_name == "Demo User"
        Vault().store(
            session,
            demo.id,
            "mailbox",
            "smtp",
            "demo.applies@example.com",
            {"password": "app-pw", "host": "smtp.fastmail.com", "port": "465"},
        )
        session.commit()
        assert mailbox_configs(session, demo)[1].host == "smtp.fastmail.com"
    finally:
        get_settings.cache_clear()


def test_smtp_build_attachment(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF-1.4")
    sender = SmtpSender(SmtpConfig(host="h", username="me@x.io", password="p"), client_factory=FakeSmtp)
    sender.send("hr@acme.example", "Application", "Hello", attachments=[f], cc="cc@x.io")
    msg = FakeSmtp.sent[0]
    assert msg["Cc"] == "cc@x.io" and any(p.get_filename() == "resume.pdf" for p in msg.iter_attachments())


# ----------------------------------------------------------------- rules only, no LLM


def test_rules_alone_classify_and_draft_without_llm(session, demo):
    m = Mail(
        uid="1",
        message_id="x",
        sender="ada@stripe.com",
        sender_name="Ada",
        subject="Interview for Backend Engineer",
        body="We'd like to schedule a call with you on Thursday.",
    )
    out = classify_mail(session, None, demo, m)
    assert out.category == "interview" and out.needs_reply and "Demo User" in out.draft_reply
    assert out.summary == m.subject
    rej = Mail(uid="2", message_id="y", sender="a@b.com", sender_name="", subject="Update",
               body="Unfortunately we will not be moving forward.")
    out = classify_mail(session, None, demo, rej)
    assert out.category == "rejection" and not out.needs_reply and not out.draft_reply
    # With an LLM, a rule hit that wants a reply keeps the rule's category but the LLM enriches it (company,
    # title, tailored draft); rejections never reach the LLM.
    llm = FakeLLM()
    out = classify_mail(session, llm, demo, m)
    assert out.category == "interview" and out.needs_reply and "Demo User" in out.draft_reply
    assert len(llm.calls) == 1 and llm.calls[0].context["hint"] == "interview"
    assert classify_mail(session, llm, demo, rej).category == "rejection"
    assert len(llm.calls) == 1
    # Mail the rules could not place goes to the LLM.
    q = Mail(uid="3", message_id="z", sender="ada@stripe.com", sender_name="Ada", subject="Quick question",
             body="Could you share your notice period?")
    assert classify_mail(session, llm, demo, q).category == "recruiter_question" and len(llm.calls) == 2
    assert classify_mail(session, None, demo, q).category == "other"


def test_interview_mail_with_llm_keeps_rule_category_and_gets_llm_extraction(session, demo):
    """Regression: a rule hit used to short-circuit before the LLM, so an interview mail from a recruiting
    agency never got the company extracted and could not be linked to the application."""
    from jobapply.mail.classify import MailClassification

    m = Mail(uid="1", message_id="x", sender="jo@talent-agency.io", sender_name="Jo", subject="Interview request",
             body="Stripe would like to interview you for Backend Engineer on Thursday 3pm.")
    llm = FakeLLM()
    llm.queue("classify", MailClassification(category="other", company="Stripe", job_title="Backend Engineer",
                                             needs_reply=False, draft_reply="Thursday 3pm works for me.", summary="s"))
    out = classify_mail(session, llm, demo, m)
    assert out.category == "interview" and out.company == "Stripe" and out.job_title == "Backend Engineer"
    assert out.needs_reply and out.draft_reply == "Thursday 3pm works for me."
    # An empty LLM draft falls back to the template; an LLM failure falls back to the rules entirely.
    llm.queue("classify", MailClassification(category="interview", company="Stripe"))
    out = classify_mail(session, llm, demo, m)
    assert out.company == "Stripe" and "Demo User" in out.draft_reply

    class Down(FakeLLM):
        def complete(self, request):
            raise LLMError("429", retryable=True)

    out = classify_mail(session, Down(), demo, m)
    assert out.category == "interview" and out.company == "" and "Demo User" in out.draft_reply


def test_sync_inbox_without_llm_never_raises(session, demo):
    app, _ = _applied(session, demo, "Stripe", "Backend Engineer")
    messages = [
        raw("Ada <ada@stripe.com>", "Interview for Backend Engineer", "Let's schedule a call on Thursday."),
        raw("noreply@greenhouse.io", "Verify your email", "Your verification code is 445566"),
        raw("alerts@jobs.example", "Jobs you may like", "unsubscribe here"),
        raw("someone@elsewhere.example", "Hello", "Could you tell me more about yourself?"),
    ]
    r, _ = reader(messages)
    sender = SmtpSender(
        SmtpConfig(host="smtp.example.com", username="demo.applies@example.com", password="pw"), client_factory=FakeSmtp
    )
    counts = sync_inbox(session, demo, r, None, sender)
    assert counts["seen"] == 4 and counts["new"] == 4 and counts["status_changes"] == 1
    session.refresh(app)
    assert app.status == AppStatus.interview
    rows = {m.subject: m for m in session.exec(select(MailMessage).where(MailMessage.user_id == demo.id)).all()}
    assert rows["Interview for Backend Engineer"].draft_reply and rows["Interview for Backend Engineer"].forwarded
    assert rows["Hello"].category == MailCategory.other and not rows["Hello"].draft_reply
    assert len(FakeSmtp.sent) == 1
