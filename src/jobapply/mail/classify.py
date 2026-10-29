"""Classify inbound mail and link it to an application.

Rules first (cheap, deterministic for the obvious cases), the LLM for the rest. The LLM also drafts a
reply for mails that expect one.
"""

from __future__ import annotations

import re

from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlmodel import Session, select

from jobapply.ai.base import LLM, LLMError
from jobapply.ai.prompts import profile_text
from jobapply.ai.service import structured
from jobapply.logs import get_logger
from jobapply.mail.imap import Mail
from jobapply.mail.otp import extract_code, verification_link
from jobapply.models import Application, AppStatus, Job, MailCategory, User
from jobapply.pipeline.dedupe import norm_company

log = get_logger(__name__)

_RULES: list[tuple[MailCategory, re.Pattern[str]]] = [
    (
        MailCategory.verification,
        re.compile(
            r"verif(y|ication) (your )?(email|account)|confirm your (email|account|address)|one[- ]time (code|password)|"  # noqa: E501
            r"\botp\b|security code|verification code|activate your account|magic link",
            re.I,
        ),
    ),
    (
        MailCategory.interview,
        re.compile(
            r"\binterview\b|schedule (a|your) (call|conversation|chat)|book a time|calendly|phone screen|"
            r"meet(ing)? with (the|our) team|next (step|round)s? ",
            re.I,
        ),
    ),
    (
        MailCategory.assessment,
        re.compile(
            r"assessment|take[- ]home|coding (test|challenge|exercise)|hackerrank|codility|codesignal|technical test",
            re.I,
        ),
    ),
    (
        MailCategory.rejection,
        re.compile(
            r"not (be )?moving forward|other candidates|decided to (pursue|proceed with) other|not selected|"
            r"unfortunately|regret to inform|will not be (progressing|proceeding)|position has been filled|"
            r"no longer (considering|under consideration)|unsuccessful",
            re.I,
        ),
    ),
    (
        MailCategory.spam,
        re.compile(
            r"unsubscribe|newsletter|webinar|limited time|earn (money|from home)|\$\$\$|job alert|jobs you may like|"
            r"recommended jobs|trending jobs",
            re.I,
        ),
    ),
]
_ACK = re.compile(
    r"thank you for (applying|your application)|we('ve| have) received your application|"
    r"application (has been )?received|application confirmation",
    re.I,
)


class MailClassification(BaseModel):
    category: str
    company: str = ""
    job_title: str = ""
    needs_reply: bool = False
    draft_reply: str = ""
    summary: str = ""


CLASSIFY_SYSTEM = """You sort a job seeker's inbox. Categories: interview (a call, meeting or next round is
proposed or scheduled), assessment (a test or take-home is requested), rejection, recruiter_question (a recruiter
asks the candidate something), verification (email verification, OTP, login code), spam (newsletters, job
alerts, marketing), other (acknowledgements, receipts, anything else).
Extract the company and job title when the mail names them. Set needs_reply when a human reply is expected
(interview scheduling, questions, assessment confirmations). When needs_reply, write `draft_reply`: a short,
polite first-person reply in the candidate's voice using only facts from the profile, confirming availability
or answering the question, 3 to 6 sentences, no placeholders. One-line `summary`.

Candidate profile:
{profile}
"""


def rule_category(mail: Mail) -> MailCategory | None:
    text = f"{mail.subject}\n{mail.body[:4000]}"
    if _ACK.search(text) and not re.search(r"interview|assessment|unfortunately", text, re.I):
        return MailCategory.other
    for category, rx in _RULES:
        if rx.search(text):
            if category == MailCategory.verification and not (extract_code(text) or verification_link(mail)):
                continue
            return category
    return None


def classify_mail(session: Session, llm: LLM | None, user: User, mail: Mail) -> MailClassification:
    rule = rule_category(mail)
    if rule in (MailCategory.verification, MailCategory.spam) or llm is None:
        return MailClassification(category=(rule or MailCategory.other).value)
    prompt = f"From: {mail.sender_name} <{mail.sender}>\nSubject: {mail.subject}\n\n{mail.body[:6000]}" + (
        f"\n\n(Rule-based hint: {rule.value})" if rule else ""
    )
    try:
        out = structured(
            session,
            llm,
            user,
            task="classify",
            system=CLASSIFY_SYSTEM.format(profile=profile_text(user)),
            prompt=prompt,
            schema=MailClassification,
            max_tokens=4096,
            context={
                "subject": mail.subject,
                "body": mail.body,
                "sender": mail.sender,
                "profile": user.profile or {},
                "hint": rule.value if rule else "",
            },
        )
    except LLMError as exc:
        log.warning("classify failed, using rules", error=str(exc))
        return MailClassification(category=(rule or MailCategory.other).value)
    if out.category not in MailCategory.__members__:
        out.category = (rule or MailCategory.other).value
    return out


def link_application(
    session: Session, user: User, mail: Mail, hint_company: str = "", hint_title: str = ""
) -> Application | None:
    """Best matching application: same company as the sender domain / named company, recent, submitted."""
    rows = session.exec(
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(
            Application.user_id == user.id,
            Application.status.notin_([AppStatus.new, AppStatus.scored, AppStatus.skipped]),
        )
    ).all()
    if not rows:
        return None
    domain = mail.sender_domain.split(".")[0] if mail.sender_domain else ""
    haystack = f"{mail.subject} {mail.body[:1500]}".lower()
    best, best_score = None, 0
    for app, job in rows:
        company = norm_company(job.company)
        score = 0
        if hint_company and fuzz.token_set_ratio(norm_company(hint_company), company) >= 85:
            score += 60
        if domain and company and (domain in company.replace(" ", "") or company.replace(" ", "") in domain):
            score += 50
        if company and company in haystack:
            score += 30
        if hint_title and fuzz.token_set_ratio(hint_title.lower(), job.title.lower()) >= 80:
            score += 20
        elif job.title.lower() in haystack:
            score += 15
        if mail.in_reply_to and app.answers and mail.in_reply_to == app.answers.get("_last_message_id"):
            score += 100
        if score > best_score:
            best, best_score = app, score
    return best if best_score >= 30 else None


STATUS_FOR = {
    MailCategory.interview: AppStatus.interview,
    MailCategory.assessment: AppStatus.assessment,
    MailCategory.rejection: AppStatus.rejected,
    MailCategory.recruiter_question: AppStatus.replied,
}
# Never move an application backwards: an interview mail after an offer stays an offer.
_RANK = {
    s: i
    for i, s in enumerate(
        [AppStatus.applied, AppStatus.replied, AppStatus.assessment, AppStatus.interview, AppStatus.offer]
    )
}


def should_update_status(current: AppStatus, new: AppStatus) -> bool:
    if new == AppStatus.rejected:
        return current not in (AppStatus.offer,)
    return _RANK.get(new, -1) > _RANK.get(current, -1)
