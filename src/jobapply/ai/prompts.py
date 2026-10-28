"""Prompt builders. System prompts carry the stable, per-user part (profile) so prompt caching
works across the jobs scored in one run; the job goes in the user message."""

from __future__ import annotations

import json
from typing import Any

from jobapply.models import Job, User

SCORE_SYSTEM = """You assess how well a candidate matches a job posting for an automated job application
assistant. Judge on skills, seniority, domain, location and work authorization. Be strict: 85+ means a
clear fit the candidate should apply to today, 70-84 a good fit, 50-69 a stretch, below 50 a poor fit.
Keep `reason` to one sentence. `missing_skills` lists requirements from the posting the candidate does not
show; `matched_skills` lists the strongest overlaps. Use the candidate profile below as the only source of
truth about the candidate.

Candidate profile:
{profile}
"""

RESUME_SYSTEM = """You tailor a resume to a job posting for an automated job application assistant.
You receive the candidate's master resume (JSON Resume format, contact details removed) and a posting.
Return a variant that reorders and rephrases what is already in the master to match the posting:
pick the most relevant highlights first, mirror the posting's terminology where the master supports it,
write a two-sentence summary aimed at this role, and set `label` to a headline that fits the role.
Never invent employers, dates, titles, degrees, tools or achievements that are not in the master.
Keep every work entry and education entry from the master; only reorder highlights within them and drop
highlights that are irrelevant when an entry has more than four.

Master resume:
{resume}
"""

COVER_LETTER_SYSTEM = """You write short cover letters for an automated job application assistant.
Tone: {tone}. Length: 120 to 180 words, three short paragraphs, no headers, no placeholders, no bullet
points. Open with the role and why this company specifically, show two concrete matches between the
candidate and the posting, close with availability. Address it "Dear {company} team,". Sign off with the
candidate's name. Only use facts from the candidate profile below.

Candidate profile:
{profile}
"""

ANSWER_SYSTEM = """You answer application form questions on behalf of a candidate, using only the
candidate profile and answer bank below. Answer in the first person, concise, factual, in the form the
question expects (a number for "how many years", yes/no for yes/no questions, a short paragraph for
open questions). If the profile does not contain the fact, give the most reasonable answer and set
`confidence` below 50 so a human reviews it. Never claim credentials, visas or experience the profile does
not show.

Candidate profile:
{profile}

Answer bank (question -> approved answer):
{answers}
"""


def profile_text(user: User) -> str:
    p = dict(user.profile or {})
    p.pop("resume_json", None)
    p.pop("resumes", None)
    return json.dumps(p, indent=1, ensure_ascii=False, sort_keys=True)


def job_text(job: Job, max_chars: int = 12000) -> str:
    head = {
        "title": job.title, "company": job.company, "location": job.location, "remote": job.remote,
        "salary": f"{job.currency or ''} {job.salary_min or ''}-{job.salary_max or ''}".strip(" -"),
    }
    return json.dumps(head, ensure_ascii=False) + "\n\nPosting:\n" + (job.description or "")[:max_chars]


def master_resume(user: User) -> dict[str, Any]:
    """The master JSON Resume, or one built from the profile when the user has not pasted one."""
    p = user.profile or {}
    resume = dict(p.get("resume_json") or {})
    if not resume.get("basics"):
        resume["basics"] = {
            "name": f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or user.username,
            "label": p.get("current_title", ""), "email": p.get("email", ""), "phone": p.get("phone", ""),
            "summary": p.get("summary", ""),
            "location": {"city": p.get("location", "")},
            "profiles": [{"network": k.title(), "url": p[k]} for k in ("linkedin", "github", "portfolio") if p.get(k)],
        }
    if not resume.get("skills") and p.get("skills"):
        resume["skills"] = [{"name": "Skills", "keywords": list(p["skills"])}]
    if not resume.get("work") and p.get("current_company"):
        resume["work"] = [{"name": p["current_company"], "position": p.get("current_title", ""), "highlights": []}]
    if not resume.get("education") and p.get("education"):
        resume["education"] = [{"institution": p["education"]}]
    return resume


def resume_without_contact(resume: dict[str, Any]) -> dict[str, Any]:
    basics = resume.get("basics") or {}
    return {**resume, "basics": {k: basics.get(k, "") for k in ("label", "summary")}}
