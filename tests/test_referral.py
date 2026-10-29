"""Referral outreach: contact parsing, message drafting, draft storage/dedupe, the gated send path, and the
migration. No test opens a browser or touches the network (the live discovery and send are behind hooks)."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from jobapply.ai.base import LLMError
from jobapply.ai.fake import FakeLLM
from jobapply.ai.heuristics import CONNECTION_NOTE_CAP, referral_message_template
from jobapply.ai.service import referral_message
from jobapply.config import get_settings
from jobapply.db import make_engine, upgrade
from jobapply.models import Job, ReferralDraft, User
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.linkedin import referral as ref
from jobapply.sites.linkedin.referral import (
    Contact,
    find_contacts,
    generate_referral_drafts,
    parse_hiring_team,
    referrals_sent_today,
    send_referral,
)
from jobapply.switches import set_value

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _job(session, title="Backend Engineer", company="Stripe", key="rf1"):
    job = Job(dedupe_key=key, title=title, company=company,
              description="Python, Postgres and AWS backend role.", location="Remote", remote=True,
              url="https://www.linkedin.com/jobs/view/4480000001")
    session.add(job)
    session.commit()
    return job


# ------------------------------------------------------------------ parse_hiring_team (pure)


def test_parse_hiring_team_card_two_contacts():
    contacts = parse_hiring_team(read("hiring_team_card.html"))
    by_url = {c.profile_url: c for c in contacts}
    assert set(by_url) == {
        "https://www.linkedin.com/in/jane-recruiter-9a8b",
        "https://www.linkedin.com/in/raj-kumar-eng",
    }
    jane = by_url["https://www.linkedin.com/in/jane-recruiter-9a8b"]
    raj = by_url["https://www.linkedin.com/in/raj-kumar-eng"]
    assert jane.name == "Jane Recruiter"
    assert "Technical Recruiter" in jane.headline and jane.is_recruiter is True
    assert jane.connect_url is None  # connected: a Message button, no Connect action
    assert raj.name == "Raj Kumar"
    assert "Engineering Manager" in raj.headline and raj.is_recruiter is False
    assert raj.connect_url == raj.profile_url  # a Connect action is available


def test_parse_hiring_team_poster_only():
    contacts = parse_hiring_team(read("hiring_team_poster_only.html"))
    assert len(contacts) == 1
    c = contacts[0]
    assert c.profile_url == "https://www.linkedin.com/in/sara-poster"
    assert c.name == "Sara Poster"
    assert "Talent Acquisition" in c.headline and c.is_recruiter is True


def test_parse_hiring_team_none():
    assert parse_hiring_team(read("hiring_team_none.html")) == []
    assert parse_hiring_team("") == []


def test_parse_ignores_people_also_viewed_rail():
    # The recommended-jobs rail in the card fixture carries its own /in/ link; it must not become a contact.
    urls = {c.profile_url for c in parse_hiring_team(read("hiring_team_card.html"))}
    assert "https://www.linkedin.com/in/some-recommended-person-should-be-ignored" not in urls


# ------------------------------------------------------------------ find_contacts (fake page, no browser)


class FakePage:
    def __init__(self, html):
        self.html = html
        self.visited = []

    def goto(self, url, **kw):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.html


def test_find_contacts_uses_the_parser_on_a_loaded_page():
    page = FakePage(read("hiring_team_card.html"))
    contacts = find_contacts(page, "https://www.linkedin.com/jobs/view/4480000001")
    assert page.visited == ["https://www.linkedin.com/jobs/view/4480000001"]
    assert len(contacts) == 2


# ------------------------------------------------------------------ referral_message


def _contact(connected=False):
    return Contact(name="Jane Recruiter", headline="Technical Recruiter at Stripe",
                   profile_url="https://www.linkedin.com/in/jane-recruiter-9a8b",
                   connect_url=None if connected else "https://www.linkedin.com/in/jane-recruiter-9a8b",
                   is_recruiter=True)


def test_referral_message_with_fakellm(session, demo):
    job = _job(session)
    llm = FakeLLM()
    msg = referral_message(session, llm, demo, job, _contact(), channel="connection_note")
    assert msg and llm.calls[0].task == "referral_message"
    assert len(msg) <= CONNECTION_NOTE_CAP


def test_referral_message_template_fallback_without_llm(session, demo):
    job = _job(session)
    msg = referral_message(session, None, demo, job, _contact(), channel="connection_note")
    assert msg == referral_message_template(demo, job, _contact(), "connection_note")
    assert job.title in msg and job.company in msg
    assert "Dear" not in msg  # a note, not a cover letter


def test_referral_message_connection_note_respects_length_cap(session, demo):
    job = _job(session, title="Senior Distributed Systems and Platform Reliability Engineer, Core Infrastructure",
               company="A Company With A Very Long Name Incorporated Worldwide", key="rf_long")
    long_contact = Contact(name="Somebody With A Rather Long Display Name", headline="Principal Recruiter",
                           profile_url="https://www.linkedin.com/in/long")
    # Even if the LLM returns something over the limit, referral_message caps it.
    llm = FakeLLM()
    llm.queue("referral_message", "x" * 900)
    msg = referral_message(session, llm, demo, job, long_contact, channel="connection_note")
    assert len(msg) <= CONNECTION_NOTE_CAP
    # the template path is also within the cap
    tmpl = referral_message_template(demo, job, long_contact, "connection_note")
    assert len(tmpl) <= CONNECTION_NOTE_CAP


def test_referral_message_falls_back_when_llm_raises(session, demo):
    job = _job(session)

    class Raising(FakeLLM):
        def complete(self, request):
            raise LLMError("boom")

    msg = referral_message(session, Raising(), demo, job, _contact(), channel="message")
    assert msg == referral_message_template(demo, job, _contact(), "message")


# ------------------------------------------------------------------ generate_referral_drafts (dedupe)


def test_generate_referral_drafts_creates_one_per_contact_and_channel(session, demo):
    job = _job(session)
    contacts = parse_hiring_team(read("hiring_team_card.html"))
    drafts = generate_referral_drafts(session, demo, job, contacts, llm=None)
    session.commit()
    assert len(drafts) == 2
    channels = {d.contact_profile_url: d.channel for d in drafts}
    assert channels["https://www.linkedin.com/in/jane-recruiter-9a8b"] == "message"  # connected
    assert channels["https://www.linkedin.com/in/raj-kumar-eng"] == "connection_note"  # not connected
    assert all(d.status == "draft" and d.body for d in drafts)


def test_generate_referral_drafts_dedupes_same_contact_same_job(session, demo):
    job = _job(session)
    contacts = parse_hiring_team(read("hiring_team_card.html"))
    first = generate_referral_drafts(session, demo, job, contacts, llm=None)
    session.commit()
    # Running again (or a contact list with duplicates) must not create a second draft for the same contact.
    again = generate_referral_drafts(session, demo, job, contacts + [contacts[0]], llm=None)
    session.commit()
    assert len(first) == 2 and again == []
    rows = session.exec(select(ReferralDraft).where(ReferralDraft.job_id == job.id)).all()
    assert len(rows) == 2


# ------------------------------------------------------------------ gated send


def _approved_draft(session, demo, job, status="approved"):
    draft = ReferralDraft(user_id=demo.id, job_id=job.id, contact_name="Jane Recruiter",
                          contact_headline="Technical Recruiter at Stripe",
                          contact_profile_url="https://www.linkedin.com/in/jane-recruiter-9a8b",
                          channel="connection_note", body="Hi Jane, ...", status=status)
    session.add(draft)
    session.commit()
    return draft


def test_send_refuses_when_switch_off(session, demo, monkeypatch):
    job = _job(session)
    draft = _approved_draft(session, demo, job)
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    # switch is off by default
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "disabled" in reason
    assert called["n"] == 0  # the browser hook was never reached
    assert draft.status == "approved" and draft.sent_at is None  # draft untouched


def test_send_refuses_when_not_approved(session, demo, monkeypatch):
    job = _job(session)
    draft = _approved_draft(session, demo, job, status="draft")
    set_value(session, "referral_send", "on")
    session.commit()
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "not approved" in reason
    assert called["n"] == 0
    assert draft.status == "draft" and draft.sent_at is None


def test_send_refuses_when_cap_reached(session, demo, monkeypatch):
    job = _job(session)
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    cap = get_settings().referral_daily_cap
    # Record `cap` referrals already sent today.
    from jobapply.models import utcnow

    for i in range(cap):
        session.add(ReferralDraft(user_id=demo.id, job_id=job.id, contact_profile_url=f"https://x/{i}",
                                  status="sent", sent_at=utcnow(), channel="connection_note"))
    session.commit()
    assert referrals_sent_today(session, demo.id) == cap
    draft = _approved_draft(session, demo, job)
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "cap" in reason
    assert called["n"] == 0
    assert draft.status == "approved" and draft.sent_at is None


def test_send_succeeds_when_all_gates_pass(session, demo, monkeypatch):
    job = _job(session)
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: True)
    draft = _approved_draft(session, demo, job)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is True and reason == "sent"
    assert draft.status == "sent" and draft.sent_at is not None
    assert referrals_sent_today(session, demo.id) == 1


def test_send_records_failure_when_hook_returns_false(session, demo, monkeypatch):
    job = _job(session)
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: False)
    draft = _approved_draft(session, demo, job)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and draft.status == "failed" and draft.error


# --- regression tests for the send gates (each gate blocks on its own and leaves the draft untouched) ---


def test_send_refuses_for_draft_of_another_user(session, demo, monkeypatch):
    """A draft that belongs to a different user is never sent, and is not charged to the caller's cap,
    even if every other gate is open. Guards against an unscoped caller passing a foreign draft."""
    from jobapply.users import create_user

    job = _job(session)
    other = create_user(session, "other", "other1234")
    session.commit()
    draft = _approved_draft(session, demo, job)  # owned by demo
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    ok, reason = send_referral(session, other, draft, page=object())
    assert ok is False and "another user" in reason
    assert called["n"] == 0
    assert draft.status == "approved" and draft.sent_at is None


def test_send_refuses_when_global_dry_run_on(session, demo, monkeypatch):
    """The global dry-run master switch (on by default) blocks the send even with referral_send on."""
    job = _job(session)
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    # global_dry_run is on by default (settings.dry_run defaults True) — do not patch it.
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    draft = _approved_draft(session, demo, job)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "dry-run" in reason
    assert called["n"] == 0
    assert draft.status == "approved" and draft.sent_at is None


def test_send_refuses_when_kill_switch_on(session, demo, monkeypatch):
    """The kill switch blocks the send and leaves the draft untouched."""
    job = _job(session)
    set_value(session, "referral_send", "on")
    set_value(session, "kill_switch", "on")
    session.commit()
    monkeypatch.setattr(ref, "_within_active_hours", lambda: True)
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    draft = _approved_draft(session, demo, job)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "kill switch" in reason
    assert called["n"] == 0
    assert draft.status == "approved" and draft.sent_at is None


def test_send_refuses_when_outside_active_hours(session, demo, monkeypatch):
    """Being outside the LinkedIn active hours blocks the send and leaves the draft untouched."""
    job = _job(session)
    set_value(session, "referral_send", "on")
    session.commit()
    monkeypatch.setattr(ref, "global_dry_run", lambda s: False)
    monkeypatch.setattr(ref, "_within_active_hours", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(ref, "_perform_send", lambda page, d: called.__setitem__("n", called["n"] + 1) or True)
    draft = _approved_draft(session, demo, job)
    ok, reason = send_referral(session, demo, draft, page=object())
    assert ok is False and "active hours" in reason
    assert called["n"] == 0
    assert draft.status == "approved" and draft.sent_at is None


# ------------------------------------------------------------------ migration + round-trip


def test_referral_migration_applies_and_round_trips(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    upgrade(url=url)  # exactly `jobapply db upgrade`: all revisions incl. 0004 on a fresh db
    engine = make_engine(url)
    with Session(engine, expire_on_commit=False) as s:
        user = User(username="mig", password_hash="x", profile=DEMO_PROFILE)
        s.add(user)
        s.commit()
        job = Job(dedupe_key="mkref", title="Eng", company="Acme")
        s.add(job)
        s.commit()
        draft = ReferralDraft(user_id=user.id, job_id=job.id, contact_name="Jane",
                              contact_profile_url="https://www.linkedin.com/in/jane",
                              channel="connection_note", body="Hi Jane", status="draft")
        s.add(draft)
        s.commit()
        draft_id = draft.id
    with Session(engine, expire_on_commit=False) as s:
        got = s.get(ReferralDraft, draft_id)
        assert got.contact_name == "Jane" and got.channel == "connection_note" and got.status == "draft"
    get_settings.cache_clear()
