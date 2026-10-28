"""Demo data so the dashboard can be reviewed without any API key or collector run.

Creates admin/admin1234 and demo/demo1234, a few dozen jobs across sources, applications in
every status for the demo user, a couple of mails and collector runs.
"""

from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobapply.collectors.base import NormalizedJob
from jobapply.forms.answers import question_hash
from jobapply.models import (
    AnswerBank,
    Application,
    ApplicationEvent,
    AppStatus,
    CollectorRun,
    Job,
    JobSource,
    MailCategory,
    MailMessage,
    Role,
    User,
    utcnow,
)
from jobapply.pipeline.dedupe import dedupe_key, norm_company
from jobapply.users import create_user

DEMO_ADMIN = ("admin", "admin1234")
DEMO_USER = ("demo", "demo1234")

COMPANIES = [
    ("Stripe", "greenhouse", "Dublin"),
    ("Ramp", "ashby", "New York, NY"),
    ("Palantir", "lever", "London, UK"),
    ("Hugging Face", "workable", "Paris, France"),
    ("Experian", "smartrecruiters", "Heredia, Costa Rica"),
    ("Razorpay", "lever", "Bengaluru, India"),
    ("Zepto", "greenhouse", "Mumbai, India"),
    ("Postman", "greenhouse", "Bengaluru, India"),
    ("Cred", "lever", "Bengaluru, India"),
    ("Remotive Co", "remotive", "Remote"),
    ("Jobicy Partner", "jobicy", "Remote (EMEA)"),
    ("Acme Analytics", "indeed", "Pune, India"),
    ("FUTO", "hn", "Austin, TX / Remote"),
]
TITLES = [
    "Senior Backend Engineer",
    "Backend Engineer",
    "Python Developer",
    "Platform Engineer",
    "Data Engineer",
    "Staff Software Engineer",
    "Site Reliability Engineer",
    "Engineering Manager",
    "Frontend Engineer",
    "Machine Learning Engineer",
    "Full-Stack Developer",
    "DevOps Engineer",
]
DESC = (
    "About the role\n\nYou will design and run the services behind our product. We use Python, FastAPI, Postgres "
    "and Kubernetes on AWS. You own features end to end, from design review to on-call.\n\nWhat we look for\n\n"
    "- 4+ years building backend systems\n- Strong Python and SQL\n- Experience with distributed systems\n"
    "- Clear written communication\n\nNice to have: Go, Kafka, Terraform."
)
DEMO_PROFILE = {
    "first_name": "Demo",
    "last_name": "User",
    "email": "demo.applies@example.com",
    "phone": "+91 98765 43210",
    "location": "Bengaluru, India",
    "linkedin": "https://www.linkedin.com/in/demo-user",
    "github": "https://github.com/demo",
    "work_authorization": "Indian citizen",
    "needs_sponsorship": "no",
    "notice_period": "30 days",
    "current_ctc": "28 LPA",
    "expected_ctc": "40 LPA",
    "years_experience": "6",
    "willing_to_relocate": "yes",
    "current_company": "Example Systems",
    "current_title": "Senior Software Engineer",
    "education": "B.Tech CSE",
    "summary": "Backend engineer with six years on Python services, Postgres and AWS. Led a payments platform rewrite.",
    "skills": {"Python": 6, "FastAPI": 3, "Postgres": 5, "AWS": 4, "Kubernetes": 2, "Go": 1},
    "resume_json": {
        "basics": {
            "name": "Demo User",
            "label": "Senior Software Engineer",
            "email": "demo.applies@example.com",
            "phone": "+91 98765 43210",
            "summary": "Backend engineer with six years on Python services.",
            "location": {"city": "Bengaluru", "countryCode": "IN"},
            "profiles": [{"network": "GitHub", "url": "https://github.com/demo"}],
        },
        "work": [
            {
                "name": "Example Systems",
                "position": "Senior Software Engineer",
                "startDate": "2022-01",
                "highlights": ["Led payments platform rewrite, cut p99 latency 40%", "Ran on-call for 12 services"],
            },
            {
                "name": "Startup Labs",
                "position": "Software Engineer",
                "startDate": "2019-06",
                "endDate": "2021-12",
                "highlights": ["Built the data pipeline on Airflow and Postgres"],
            },
        ],
        "education": [
            {
                "institution": "Example Institute of Technology",
                "area": "Computer Science",
                "studyType": "B.Tech",
                "endDate": "2019",
            }
        ],
        "skills": [{"name": "Backend", "keywords": ["Python", "FastAPI", "Postgres", "AWS", "Kubernetes"]}],
    },
}
DEMO_FILTERS = {
    "roles": ["backend engineer", "python developer", "platform engineer", "software engineer"],
    "keywords_exclude": ["manager", "frontend"],
    "locations": ["bengaluru", "remote"],
    "seniority": ["mid", "senior", "lead", "principal"],
    "max_age_hours": 72,
}
DEMO_PREFS = {
    "score_threshold": 70,
    "cover_letter_tone": "professional",
    "daily_cap": 20,
    "alert_types": ["interview", "assessment", "paused"],
}


def seed(engine: Engine, jobs: int = 40, rnd: random.Random | None = None) -> dict[str, str]:
    rnd = rnd or random.Random(7)
    with Session(engine, expire_on_commit=False) as s:
        if s.exec(select(User).where(User.username == DEMO_ADMIN[0])).first():
            return {"status": "already seeded"}
        create_user(
            s,
            *DEMO_ADMIN,
            email="admin@example.com",
            role=Role.admin,
            filters={"roles": ["engineering manager"], "max_age_hours": 48},
        )
        demo = create_user(
            s,
            *DEMO_USER,
            email=DEMO_PROFILE["email"],
            profile=DEMO_PROFILE,
            filters=DEMO_FILTERS,
            preferences=DEMO_PREFS,
        )
        now = utcnow()
        created: list[Job] = []
        for i in range(jobs):
            company, source, location = COMPANIES[i % len(COMPANIES)]
            title = TITLES[(i * 5) % len(TITLES)]
            ats = source if source in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters") else None
            remote = "Remote" in location or rnd.random() < 0.3
            nj = NormalizedJob(
                title=title,
                company=company,
                location=location,
                remote=remote,
                source=source,
                source_id=f"demo-{i}",
                url=f"https://example.com/jobs/{i}",
                apply_url=f"https://jobs.example.com/{company.lower().replace(' ', '-')}/{i}/apply",
                description=DESC,
                ats_type=ats,
                posted_at=now - timedelta(hours=rnd.uniform(0.5, 96)),
                salary_min=rnd.choice([None, 1800000, 2500000, 120000]),
                salary_max=None,
                currency=None,
            )
            if nj.salary_min:
                nj.salary_max, nj.currency = nj.salary_min * 1.4, ("USD" if nj.salary_min < 500000 else "INR")
            job = Job(
                dedupe_key=dedupe_key(nj) + str(i),
                company_norm=norm_company(company),
                **nj.model_dump(exclude={"raw", "source_id"}),
            )
            job.first_seen_at = job.posted_at + timedelta(minutes=30)
            s.add(job)
            s.flush()
            s.add(JobSource(job_id=job.id, source=source, source_id=nj.source_id, url=nj.url, raw={"demo": True}))
            created.append(job)

        statuses = [
            AppStatus.new,
            AppStatus.new,
            AppStatus.scored,
            AppStatus.queued,
            AppStatus.queued,
            AppStatus.approved,
            AppStatus.dry_run,
            AppStatus.applied,
            AppStatus.applied,
            AppStatus.applied,
            AppStatus.failed,
            AppStatus.paused,
            AppStatus.replied,
            AppStatus.assessment,
            AppStatus.interview,
            AppStatus.rejected,
            AppStatus.skipped,
        ]
        for i, job in enumerate(created[: len(statuses) + 8]):
            status = statuses[i % len(statuses)]
            score = None if status == AppStatus.new else rnd.randint(35, 96)
            app = Application(
                user_id=demo.id,
                job_id=job.id,
                status=status,
                score=score,
                score_reason=""
                if score is None
                else f"Strong overlap on Python and Postgres; {'missing Go' if score < 70 else 'matches seniority'}.",
                missing_skills=[] if score is None or score >= 80 else ["Go", "Kafka"][: (i % 2) + 1],
                created_at=job.first_seen_at,
                updated_at=now - timedelta(hours=rnd.uniform(0, 48)),
            )
            if status in (
                AppStatus.queued,
                AppStatus.dry_run,
                AppStatus.applied,
                AppStatus.replied,
                AppStatus.interview,
                AppStatus.assessment,
                AppStatus.rejected,
                AppStatus.failed,
                AppStatus.paused,
            ):
                app.cover_letter = (
                    f"Dear {job.company} team,\n\nI am applying for the {job.title} role. Over six years I "
                    "have built Python services on Postgres and AWS, most recently leading a payments "
                    "platform rewrite.\n\nBest regards,\nDemo User"
                )
                app.answers = {
                    "First name": "Demo",
                    "Last name": "User",
                    "Email": DEMO_PROFILE["email"],
                    "Phone": DEMO_PROFILE["phone"],
                    "Notice period": "30 days",
                    "Are you legally authorized to work in India?": "Yes",
                }
                app.resume_variant = f"data/resumes/{demo.id}/variants/{job.company.lower().replace(' ', '-')}.pdf"
            if status in (
                AppStatus.applied,
                AppStatus.replied,
                AppStatus.interview,
                AppStatus.assessment,
                AppStatus.rejected,
            ):
                app.submitted_at = app.updated_at - timedelta(hours=rnd.uniform(1, 72))
                if rnd.random() < 0.5:
                    app.submitted_at = now - timedelta(hours=rnd.uniform(0.2, 6))
                app.follow_up_at = app.submitted_at + timedelta(days=7)
            if status == AppStatus.failed:
                app.error = "Field match rate 55% below threshold; fell back to AI mapper, CAPTCHA detected."
            if status == AppStatus.paused:
                app.error = "CAPTCHA on the final step. Open the posting and finish by hand, or reject."
            s.add(app)
            s.flush()
            s.add(
                ApplicationEvent(
                    application_id=app.id,
                    user_id=demo.id,
                    kind="new",
                    detail="passed filters",
                    created_at=app.created_at,
                )
            )
            if status != AppStatus.new:
                s.add(
                    ApplicationEvent(
                        application_id=app.id,
                        user_id=demo.id,
                        kind=status.value,
                        detail={
                            "applied": "submitted via greenhouse filler",
                            "dry_run": "filled, screenshot saved",
                            "interview": "recruiter mail: interview Thursday",
                        }.get(status.value, ""),
                        created_at=app.updated_at,
                    )
                )
            if status in (AppStatus.replied, AppStatus.interview, AppStatus.assessment, AppStatus.rejected):
                cat = {
                    AppStatus.replied: MailCategory.recruiter_question,
                    AppStatus.interview: MailCategory.interview,
                    AppStatus.assessment: MailCategory.assessment,
                    AppStatus.rejected: MailCategory.rejection,
                }[status]
                s.add(
                    MailMessage(
                        user_id=demo.id,
                        application_id=app.id,
                        message_id=f"<demo-{app.id}@example.com>",
                        sender=f"recruiting@{job.company.lower().replace(' ', '')}.example.com",
                        subject=f"Re: {job.title} application",
                        category=cat,
                        body=f"Hi Demo,\n\nThanks for applying to {job.company}. "
                        + {
                            MailCategory.interview: "Could you do a 45 minute call on Thursday at 3pm IST?",
                            MailCategory.assessment: "Please complete the attached take-home within 5 days.",
                            MailCategory.rejection: "We have decided to move forward with other candidates.",
                            MailCategory.recruiter_question: "What is your notice period and expected CTC?",
                        }[cat]
                        + "\n\nRegards,\nRecruiting",
                        received_at=now - timedelta(hours=rnd.uniform(1, 72)),
                        draft_reply=""
                        if cat == MailCategory.rejection
                        else "Hi,\n\nThanks for getting back to me. Thursday 3pm IST works. "
                        "My notice period is 30 days.\n\nBest,\nDemo",
                    )
                )
        s.add(
            MailMessage(
                user_id=demo.id,
                message_id="<spam-1@example.com>",
                sender="offers@spammy.example",
                subject="Earn from home!!!",
                category=MailCategory.spam,
                body="...",
                received_at=now - timedelta(hours=5),
            )
        )
        for q, a, approved in [
            (
                "Why do you want to work here?",
                "I want to work on payments infrastructure at scale with a team that ships carefully.",
                True,
            ),
            ("Years of experience with Python", "6", True),
            ("Are you willing to relocate?", "Yes, within India or remote.", False),
        ]:
            s.add(
                AnswerBank(
                    user_id=demo.id,
                    question_hash=question_hash(q),
                    question=q,
                    answer=a,
                    source="user" if approved else "ai",
                    approved=approved,
                )
            )
        for src, ok, fetched, new, age in [
            ("greenhouse:stripe", True, 721, 3, 0.4),
            ("lever:razorpay", True, 42, 1, 0.5),
            ("ashby:ramp", True, 168, 0, 0.6),
            ("remotive", True, 11, 2, 0.7),
            ("hn", True, 240, 5, 1.1),
            ("workable:huggingface", False, 0, 0, 0.8),
            ("jobicy", True, 50, 4, 9.0),
        ]:
            s.add(
                CollectorRun(
                    source=src,
                    ok=ok,
                    fetched=fetched,
                    new=new,
                    started_at=now - timedelta(hours=age),
                    finished_at=now - timedelta(hours=age) + timedelta(seconds=12),
                    error=None if ok else "CollectorError: HTTP 429 from https://apply.workable.com/...",
                )
            )
        s.commit()
    return {"admin": f"{DEMO_ADMIN[0]} / {DEMO_ADMIN[1]}", "user": f"{DEMO_USER[0]} / {DEMO_USER[1]}"}
