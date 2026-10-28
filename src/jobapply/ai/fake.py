"""Deterministic LLM for tests and for running the whole pipeline without an API key.

Scores by skill overlap, writes template letters, tailors the master resume by reordering,
answers from the profile. Tests can also queue canned responses per task.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel

from jobapply.ai.base import LLM, AnswerResult, LLMRequest, LLMResult, ResumeVariant, ScoreResult

# Tech words the fake treats as "requirements" when they show up in a posting.
TECH = {
    "python", "go", "golang", "java", "kotlin", "rust", "typescript", "javascript", "react", "node", "django",
    "fastapi", "flask", "spring", "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "aws", "gcp", "azure", "kubernetes", "docker", "terraform", "airflow", "spark", "sql", "graphql", "grpc",
    "linux", "ci/cd", "pytorch", "tensorflow", "llm", "ml", "machine learning",
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9+./#-]*", (text or "").lower()))


class FakeLLM(LLM):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.canned: dict[str, deque[Any]] = defaultdict(deque)

    def queue(self, task: str, response: BaseModel | str) -> None:
        self.canned[task].append(response)

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        if self.canned[request.task]:
            out = self.canned[request.task].popleft()
            parsed = out if isinstance(out, BaseModel) else None
            text = out if isinstance(out, str) else out.model_dump_json()
        else:
            handler = getattr(self, f"_{request.task}", None)
            if handler is None:
                raise NotImplementedError(f"FakeLLM has no handler for task {request.task!r}")
            out = handler(request.context)
            parsed = out if isinstance(out, BaseModel) else None
            text = out if isinstance(out, str) else out.model_dump_json()
        if request.schema and not isinstance(parsed, request.schema):
            parsed = request.schema.model_validate_json(text)
        tokens = max(1, len(request.system + request.prompt) // 4)
        return LLMResult(text=text, parsed=parsed, model="fake", input_tokens=tokens, output_tokens=len(text) // 4)

    # ------------------------------------------------------------ handlers

    @staticmethod
    def _skills(ctx: dict) -> set[str]:
        return {s.lower() for s in (ctx.get("profile", {}).get("skills") or {})}

    def _score(self, ctx: dict) -> ScoreResult:
        job = ctx["job"]
        text = f"{job.get('title', '')} {job.get('description', '')}".lower()
        posting_tech = {t for t in TECH if re.search(rf"(?<![\w+]){re.escape(t)}(?![\w+])", text)}
        skills = self._skills(ctx)
        matched = sorted(posting_tech & skills)
        missing = sorted(posting_tech - skills)
        score = 40 + 12 * len(matched) - 5 * len(missing)
        title = job.get("title", "").lower()
        roles = [r.lower() for r in ctx.get("profile", {}).get("target_roles", [])]
        if any(r in title for r in roles):
            score += 10
        score = max(5, min(96, score))
        reason = (f"Matches {', '.join(matched)}" if matched else "Little overlap with the profile") + (
            f"; posting also wants {', '.join(missing)}." if missing else ".")
        return ScoreResult(score=score, reason=reason, matched_skills=matched, missing_skills=missing)

    def _cover_letter(self, ctx: dict) -> str:
        job, p = ctx["job"], ctx.get("profile", {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or "Candidate"
        skills = ", ".join(list(p.get("skills") or {})[:3]) or "my experience"
        return (
            f"Dear {job.get('company', '')} team,\n\n"
            f"I am applying for the {job.get('title', '')} role. {p.get('summary', '')}\n\n"
            f"My work with {skills} maps directly onto what the posting asks for, and I am used to owning "
            f"services end to end.\n\nI am available with a notice period of {p.get('notice_period', 'one month')} "
            f"and happy to talk whenever suits you.\n\nBest regards,\n{name}"
        )

    def _resume(self, ctx: dict) -> ResumeVariant:
        master, job = ctx["resume"], ctx["job"]
        posting = _words(job.get("description", "") + " " + job.get("title", ""))

        def relevance(text: str) -> int:
            return len(_words(text) & posting)

        skills = []
        for group in master.get("skills", []):
            kws = sorted(group.get("keywords", []), key=lambda k: -relevance(k))
            skills.append({"name": group.get("name", "Skills"), "keywords": kws})
        work = []
        for w in master.get("work", []):
            hl = sorted(w.get("highlights", []), key=lambda h: -relevance(h))[:4]
            work.append({"name": w.get("name", ""), "position": w.get("position", ""),
                         "startDate": w.get("startDate", ""), "endDate": w.get("endDate", ""),
                         "summary": w.get("summary", ""), "highlights": hl})
        edu = [{"institution": e.get("institution", ""), "area": e.get("area", ""), "studyType": e.get("studyType", ""),
                "endDate": e.get("endDate", "")} for e in master.get("education", [])]
        basics = master.get("basics", {})
        summary = basics.get("summary") or ctx.get("profile", {}).get("summary", "")
        return ResumeVariant(label=job.get("title", basics.get("label", "")), summary=summary, skills=skills,
                             work=work, education=edu)

    def _answer(self, ctx: dict) -> AnswerResult:
        q, p = ctx["question"].lower(), ctx.get("profile", {})
        skills = {k.lower(): v for k, v in (p.get("skills") or {}).items()}
        for skill, years in skills.items():
            if "years" in q and skill in q:
                return AnswerResult(answer=f"{years:g}", confidence=95)
        rules = [
            ("notice", p.get("notice_period")), ("current ctc", p.get("current_ctc")),
            ("current salary", p.get("current_ctc")), ("expected", p.get("expected_ctc")),
            ("authori", p.get("work_authorization")), ("relocat", p.get("willing_to_relocate")),
            ("sponsor", p.get("needs_sponsorship")), ("linkedin", p.get("linkedin")), ("github", p.get("github")),
            ("years of experience", p.get("years_experience")), ("phone", p.get("phone")),
        ]
        for needle, value in rules:
            if needle in q and value:
                return AnswerResult(answer=str(value), confidence=90)
        return AnswerResult(answer="I would be glad to discuss this in a conversation.", confidence=30)

    def _classify(self, ctx: dict) -> str:
        text = f"{ctx.get('subject', '')} {ctx.get('body', '')}".lower()
        for cat, needles in [("interview", ["interview", "call with", "schedule a"]),
                             ("assessment", ["assessment", "take-home", "coding test", "hackerrank"]),
                             ("rejection", ["not moving forward", "other candidates", "unfortunately", "regret"]),
                             ("verification", ["verify", "verification", "confirm your email", "otp", "one-time"]),
                             ("recruiter_question", ["could you", "please share", "what is your", "?"])]:
            if any(n in text for n in needles):
                return cat
        return "other"
