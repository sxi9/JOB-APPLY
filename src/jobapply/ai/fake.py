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
from jobapply.ai.heuristics import TECH, cover_letter_template, heuristic_answer, score_job_heuristic  # noqa: F401


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
        profile = ctx.get("profile", {})
        return score_job_heuristic(profile, {"roles": profile.get("target_roles", [])}, ctx["job"])

    def _cover_letter(self, ctx: dict) -> str:
        return cover_letter_template(ctx.get("profile", {}), ctx["job"], ctx.get("tone", "professional"))

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
            work.append(
                {
                    "name": w.get("name", ""),
                    "position": w.get("position", ""),
                    "startDate": w.get("startDate", ""),
                    "endDate": w.get("endDate", ""),
                    "summary": w.get("summary", ""),
                    "highlights": hl,
                }
            )
        edu = [
            {
                "institution": e.get("institution", ""),
                "area": e.get("area", ""),
                "studyType": e.get("studyType", ""),
                "endDate": e.get("endDate", ""),
            }
            for e in master.get("education", [])
        ]
        basics = master.get("basics", {})
        summary = basics.get("summary") or ctx.get("profile", {}).get("summary") or ""
        label = job.get("title") or basics.get("label") or "Application"
        return ResumeVariant(
            label=label, summary=summary, skills=skills, work=work, education=edu
        )

    def _answer(self, ctx: dict) -> AnswerResult:
        options = ctx.get("options") or []
        answer, confidence = heuristic_answer(ctx["question"], options, ctx.get("profile", {}))
        if answer is not None:
            return AnswerResult(answer=answer, confidence=confidence)
        if options:
            return AnswerResult(answer=options[0], confidence=25)
        return AnswerResult(answer="I would be glad to discuss this in a conversation.", confidence=30)

    def _form_map(self, ctx: dict):
        from jobapply.forms.mapper import FieldMapping, FormMapping

        rules = [
            (("first name", "given name"), "profile.first_name"),
            (("last name", "family name", "surname"), "profile.last_name"),
            (("full name", "legal name", "your name"), "derived.full_name"),
            (("email",), "profile.email"),
            (("phone",), "profile.phone"),
            (("linkedin",), "profile.linkedin"),
            (("github",), "profile.github"),
            (("portfolio", "website"), "profile.portfolio"),
            (("current company", "employer"), "profile.current_company"),
            (("location", "city", "country of residence"), "profile.location"),
            (("sponsorship", "visa"), "derived.needs_sponsorship"),
            (("relocat",), "derived.willing_to_relocate"),
            (("notice period",), "profile.notice_period"),
            (("current ctc", "current salary"), "profile.current_ctc"),
            (("expected ctc", "expected salary"), "profile.expected_ctc"),
            (("authorized to work", "right to work", "work authorization"), "profile.work_authorization"),
            (("gender",), "profile.gender"),
            (("veteran",), "profile.veteran_status"),
            (("disability",), "profile.disability_status"),
            (("cover letter",), "derived.cover_letter"),
            (("privacy", "consent to the processing", "terms", "i agree", "certify"), "const:Yes"),
        ]
        out = []
        for f in ctx.get("fields", []):
            text = f"{f.get('group', '')} {f.get('label', '')} {f.get('name', '')}".lower()
            ftype = f.get("type", "text")
            if "recaptcha" in text or "captcha" in text or ftype in ("search", "password"):
                out.append(FieldMapping(idx=f["idx"], key="skip", confidence=95))
                continue
            if any(w in text for w in ("secret", "puzzle", "decode", "video", "not a bot")):
                out.append(
                    FieldMapping(
                        idx=f["idx"], key="skip", confidence=90, needs_human=True, note="anti-bot or manual task"
                    )
                )
                continue
            if ftype == "file":
                key = "skip" if "cover" in text else "derived.resume_path"
                out.append(FieldMapping(idx=f["idx"], key=key, confidence=90))
                continue
            if any(w in text for w in ("sms", "whatsapp", "newsletter", "marketing", "hispanic")):
                out.append(FieldMapping(idx=f["idx"], key="skip", confidence=85))
                continue
            for needles, key in rules:
                if any(n in text for n in needles):
                    out.append(FieldMapping(idx=f["idx"], key=key, confidence=88))
                    break
            else:
                q = (f.get("group") or f.get("label") or "").strip()
                if q and ftype in ("text", "textarea", "select", "combobox", "radio", "checkbox", "number"):
                    out.append(FieldMapping(idx=f["idx"], key=f"answers.{q[:80]}", confidence=70))
                else:
                    out.append(FieldMapping(idx=f["idx"], key="skip", confidence=50))
        return FormMapping(mappings=out)

    def _classify(self, ctx: dict):
        from jobapply.mail.classify import MailClassification

        text = f"{ctx.get('subject', '')} {ctx.get('body', '')}".lower()
        category = ctx.get("hint") or "other"
        for cat, needles in [
            ("interview", ["interview", "call with", "schedule a", "book a time"]),
            ("assessment", ["assessment", "take-home", "coding test", "hackerrank"]),
            ("rejection", ["not moving forward", "other candidates", "unfortunately", "regret"]),
            ("recruiter_question", ["could you", "please share", "what is your", "let us know"]),
        ]:
            if any(n in text for n in needles):
                category = cat
                break
        needs_reply = category in ("interview", "assessment", "recruiter_question")
        p = ctx.get("profile") or {}
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or "Candidate"
        draft = ""
        if needs_reply:
            draft = (
                "Hi,\n\nThank you for getting back to me. "
                + {
                    "interview": "The proposed time works for me and I look forward to the conversation.",
                    "assessment": "I will complete the assessment within the given window.",
                    "recruiter_question": f"My notice period is {p.get('notice_period', '30 days')} and my expected "
                    f"compensation is {p.get('expected_ctc', 'open to discussion')}.",
                }[category]
                + f"\n\nBest regards,\n{name}"
            )
        return MailClassification(
            category=category, needs_reply=needs_reply, draft_reply=draft, summary=ctx.get("subject", "")[:80]
        )
