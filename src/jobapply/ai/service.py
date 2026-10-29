"""AI tasks wired to the database: build the LLM for a user, run a task, record usage."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from sqlmodel import Session, select

from jobapply.ai import prompts
from jobapply.ai.base import LLM, AnswerResult, LLMError, LLMRequest, LLMResult, ResumeVariant, ScoreResult
from jobapply.ai.heuristics import cover_letter_template, score_job_heuristic
from jobapply.ai.providers import FailoverLLM, ProviderConfig, parse_keys, parse_order, server_configs
from jobapply.ai.resume import render_resume_pdf
from jobapply.config import get_settings
from jobapply.forms.answers import question_hash
from jobapply.logs import get_logger
from jobapply.models import AnswerBank, Application, Job, LlmUsage, User, utcnow
from jobapply.pipeline.dedupe import norm_company
from jobapply.vault import Vault, VaultError

log = get_logger(__name__)


def user_provider_configs(session: Session, user: User | None, settings=None) -> list[ProviderConfig]:
    """Failover chain for a user: their own order/keys/models first, then the server's.

    Each provider contributes one ProviderConfig per key: the user's vault keys (`gemini#u1`, `gemini#u2`)
    ahead of the server's (`gemini#1`, `gemini#2`), all sharing the model and base URL in effect.
    """
    s = settings or get_settings()
    prefs = (user.preferences or {}) if user else {}
    order = parse_order(prefs.get("llm_providers")) or parse_order(s.llm_providers)
    user_models = prefs.get("llm_models") or {}
    configs: list[ProviderConfig] = []
    vault = _vault_or_none() if user is not None else None
    for name in order:
        servers = server_configs(name, s)
        model = user_models.get(name) or servers[0].model
        base = servers[0]
        chain: list[ProviderConfig] = []
        for i, key in enumerate(user_keys(session, user, name, vault=vault), start=1):
            chain.append(ProviderConfig(base.spec, key, model, base.base_url, source="user", index=i))
        for cfg in servers:
            if cfg.usable or not chain:  # keep one keyless placeholder so callers can show "missing"
                chain.append(ProviderConfig(cfg.spec, cfg.api_key, model, cfg.base_url, cfg.source, cfg.index))
        configs.extend(chain)
    return configs


def _vault_or_none() -> Vault | None:
    try:
        return Vault()
    except VaultError as exc:
        log.warning("vault unavailable, user API keys ignored", error=str(exc))
        return None


def user_keys(session: Session, user: User | None, provider: str, vault: Vault | None = None) -> list[str]:
    """A user's keys for a provider from the vault: `keys` list when present, else the single `password`.

    Pass `vault` when looking several providers up in one request (the Settings page) so the vault key is
    read and checked once. A vault problem is logged, not raised: the caller sees "no user keys".
    """
    if user is None:
        return []
    vault = vault or _vault_or_none()
    if vault is None:
        return []
    try:
        cred = vault.load(session, user.id, provider, "api_key")
    except VaultError as exc:
        log.warning("user API key could not be decrypted", user=user.username, provider=provider, error=str(exc))
        return []
    if not cred:
        return []
    keys = cred.get("keys")
    if isinstance(keys, list) and keys:
        return parse_keys(*[str(k) for k in keys])
    return parse_keys(str(cred.get("password") or ""))


def ai_mode(user: User | None) -> str:
    """User preference `ai_mode`: `auto` (LLM when one is configured, heuristics otherwise) or `off`."""
    mode = str(((user.preferences or {}) if user else {}).get("ai_mode") or "auto").strip().lower()
    return mode if mode in ("auto", "off") else "auto"


def get_llm(session: Session, user: User | None = None, backend: str | None = None) -> LLM:
    """The LLM for this user: the fake, or a failover chain over every provider with a key."""
    if ai_mode(user) == "off":  # the user chose no AI: every caller falls back to jobapply.ai.heuristics
        raise LLMError("ai_mode is off for this user; heuristics only")
    backend = backend or get_settings().llm_backend
    if backend == "fake":
        from jobapply.ai.fake import FakeLLM

        return FakeLLM()
    if backend not in ("live", "claude"):  # "claude" kept for older .env files
        raise ValueError(f"unknown JOBAPPLY_LLM_BACKEND {backend!r}")
    chain = FailoverLLM.from_configs(user_provider_configs(session, user))
    if not chain.entries:
        raise LLMError("no LLM provider has an API key: set one in .env or on the Settings page")
    return chain


def run(session: Session, llm: LLM, user: User | None, request: LLMRequest) -> LLMResult:
    result = llm.complete(request)
    session.add(LlmUsage(user_id=user.id if user else None, task=request.task, model=result.model,
                         input_tokens=result.input_tokens, output_tokens=result.output_tokens))
    return result


def _profile_ctx(user: User) -> dict:
    p = dict(user.profile or {})
    p["target_roles"] = (user.filters or {}).get("roles", [])
    return p


def _job_ctx(job: Job) -> dict:
    return {"title": job.title, "company": job.company, "location": job.location, "description": job.description}


# ------------------------------------------------------------------ tasks


def score_job(session: Session, llm: LLM, user: User, job: Job) -> ScoreResult:
    req = LLMRequest(
        task="score", schema=ScoreResult, max_tokens=4096,
        system=prompts.SCORE_SYSTEM.format(profile=prompts.profile_text(user)),
        prompt="Score this posting for the candidate.\n\n" + prompts.job_text(job),
        context={"profile": _profile_ctx(user), "job": _job_ctx(job)},
    )
    result = run(session, llm, user, req).as_(ScoreResult)
    result.score = max(0, min(100, int(result.score)))
    return result


HEURISTIC_PREFIX = "[heuristic] "


def score_job_fallback(user: User, job: Job) -> ScoreResult:
    """No-LLM score; the reason carries `[heuristic]` so the dashboard shows how it was produced."""
    result = score_job_heuristic(user, user, job)
    result.reason = HEURISTIC_PREFIX + result.reason
    return result


def write_cover_letter(session: Session, llm: LLM, user: User, job: Job) -> str:
    tone = (user.preferences or {}).get("cover_letter_tone", "professional")
    req = LLMRequest(
        task="cover_letter", max_tokens=4096,
        system=prompts.COVER_LETTER_SYSTEM.format(tone=tone, company=job.company,
                                                  profile=prompts.profile_text(user)),
        prompt="Write the cover letter for this posting.\n\n" + prompts.job_text(job, 8000),
        context={"profile": _profile_ctx(user), "job": _job_ctx(job), "tone": tone},
    )
    return run(session, llm, user, req).text.strip()


def tailor_resume(session: Session, llm: LLM, user: User, job: Job) -> ResumeVariant:
    master = prompts.master_resume(user)
    stripped = prompts.resume_without_contact(master)
    req = LLMRequest(
        task="resume", schema=ResumeVariant, max_tokens=16000,
        system=prompts.RESUME_SYSTEM.format(resume=json.dumps(stripped, indent=1, ensure_ascii=False)),
        prompt="Tailor the resume to this posting.\n\n" + prompts.job_text(job, 8000),
        context={"profile": _profile_ctx(user), "job": _job_ctx(job), "resume": stripped},
    )
    return run(session, llm, user, req).as_(ResumeVariant)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "company"


def resume_variant_path(user: User, job: Job) -> Path:
    return get_settings().resumes_dir / str(user.id) / "variants" / f"{_slug(norm_company(job.company))}.pdf"


def ensure_resume_variant(session: Session, llm: LLM, user: User, job: Job, force: bool = False) -> Path:
    """Tailored PDF for this company, reused for other postings at the same company within the cache window."""
    path = resume_variant_path(user, job)
    s = get_settings()
    if path.exists() and not force:
        age = utcnow().timestamp() - path.stat().st_mtime
        if age < s.resume_cache_days * 86400:
            return path
    variant = tailor_resume(session, llm, user, job)
    basics = prompts.master_resume(user).get("basics", {})
    render_resume_pdf(basics, variant, path)
    path.with_suffix(".json").write_text(variant.model_dump_json(indent=1), encoding="utf-8")
    return path


def bank_answer(session: Session, user: User, question: str) -> AnswerResult | None:
    """The stored answer for this question, if any: 100 when the user approved it, 60 for an AI draft."""
    row = session.exec(select(AnswerBank).where(AnswerBank.user_id == user.id,
                                                AnswerBank.question_hash == question_hash(question))).first()
    if row and row.answer:
        return AnswerResult(answer=row.answer, confidence=100 if row.approved else 60)
    return None


def answer_question(session: Session, llm: LLM | None, user: User, question: str, job: Job | None = None,
                    use_bank: bool = True, options: list[str] | None = None) -> AnswerResult:
    """Answer bank first; otherwise ask the LLM and store the draft for the user to approve.

    Without an LLM the result is an empty answer with confidence 0 (callers treat it as "no answer");
    the heuristic pass lives in `jobapply.forms.runner.AnswerSource`, ahead of this call.
    """
    qh = question_hash(question)
    if use_bank:
        found = bank_answer(session, user, question)
        if found is not None:
            return found
    if llm is None:
        return AnswerResult(answer="", confidence=0)
    bank = session.exec(select(AnswerBank).where(AnswerBank.user_id == user.id, AnswerBank.approved)).all()
    bank_text = "\n".join(f"- {b.question} -> {b.answer}" for b in bank[:60]) or "(empty)"
    prompt = f"Question: {question}"
    if options:
        prompt += "\nAnswer with exactly one of these options, verbatim: " + " | ".join(options[:60])
    if job is not None:
        prompt += f"\n\nThe form belongs to this posting:\n{prompts.job_text(job, 3000)}"
    req = LLMRequest(
        task="answer", schema=AnswerResult, max_tokens=2048,
        system=prompts.ANSWER_SYSTEM.format(profile=prompts.profile_text(user), answers=bank_text),
        prompt=prompt,
        context={"profile": _profile_ctx(user), "question": question, "options": options or [],
                 "job": _job_ctx(job) if job else {}},
    )
    result = run(session, llm, user, req).as_(AnswerResult)
    result.confidence = max(0, min(100, int(result.confidence)))
    row = session.exec(select(AnswerBank).where(AnswerBank.user_id == user.id,
                                                AnswerBank.question_hash == qh)).first()
    if row is None:
        session.add(AnswerBank(user_id=user.id, question_hash=qh, question=question, answer=result.answer,
                               source="ai", approved=False))
    return result


def master_resume_pdf(user: User) -> Path:
    """The user's resume with no tailoring: the uploaded PDF when there is one, else the master JSON
    Resume rendered once to `variants/master.pdf` (re-rendered when the profile changes)."""
    uploads = [r for r in (user.profile or {}).get("resumes") or [] if str(r.get("path", "")).lower().endswith(".pdf")]
    for entry in reversed(uploads):
        if Path(entry["path"]).exists():
            return Path(entry["path"])
    master = prompts.master_resume(user)
    variant = ResumeVariant.model_validate({
        "label": (master.get("basics") or {}).get("label") or (user.profile or {}).get("current_title") or "",
        "summary": (master.get("basics") or {}).get("summary") or (user.profile or {}).get("summary") or "",
        "skills": [{"name": g.get("name", "Skills"), "keywords": list(g.get("keywords") or [])}
                   for g in master.get("skills") or []],
        "work": [{k: w.get(k, "" if k != "highlights" else []) for k in
                  ("name", "position", "startDate", "endDate", "summary", "highlights")}
                 for w in master.get("work") or []],
        "education": [{k: e.get(k, "") for k in ("institution", "area", "studyType", "endDate")}
                      for e in master.get("education") or []],
    })
    path = get_settings().resumes_dir / str(user.id) / "variants" / "master.pdf"
    stamp = variant.model_dump_json()
    json_path = path.with_suffix(".json")
    if path.exists() and json_path.exists() and json_path.read_text(encoding="utf-8") == stamp:
        return path
    render_resume_pdf(master.get("basics", {}), variant, path)
    json_path.write_text(stamp, encoding="utf-8")
    return path


def prepare_application(session: Session, llm: LLM | None, user: User, app: Application, job: Job) -> None:
    """Resume variant and cover letter for an application that will be applied to.

    Without an LLM (or when every provider fails) this never raises: the application gets the template
    cover letter and the untailored master resume PDF instead.
    """
    if not app.cover_letter:
        if llm is not None:
            try:
                app.cover_letter = write_cover_letter(session, llm, user, job)
            except LLMError as exc:
                log.warning("cover letter via LLM failed, using template", user=user.username, job=job.id,
                            error=str(exc))
        if not app.cover_letter:
            tone = (user.preferences or {}).get("cover_letter_tone", "professional")
            app.cover_letter = cover_letter_template(user, job, tone)
    if not app.resume_variant or not Path(app.resume_variant).exists():
        resume: Path | None = None
        if llm is not None:
            try:
                resume = ensure_resume_variant(session, llm, user, job)
            except LLMError as exc:
                log.warning("resume tailoring via LLM failed, using master", user=user.username, job=job.id,
                            error=str(exc))
            except Exception as exc:  # noqa: BLE001 - a bad variant must never stop the application queueing
                log.warning("resume tailoring errored, using master", user=user.username, job=job.id,
                            error=f"{type(exc).__name__}: {exc}")
        if resume is None:
            try:
                resume = master_resume_pdf(user)
            except Exception as exc:  # a broken master must not stop the application from queueing
                log.warning("master resume not available", user=user.username, error=f"{type(exc).__name__}: {exc}")
        app.resume_variant = str(resume) if resume else None
    app.updated_at = utcnow()
    session.add(app)


def structured(session: Session, llm: LLM, user: User | None, task: str, system: str, prompt: str,
               schema: type[BaseModel], context: dict | None = None, max_tokens: int = 4096) -> BaseModel:
    """Generic structured call for later modules (inbox classifier, form mapper)."""
    req = LLMRequest(task=task, system=system, prompt=prompt, schema=schema, max_tokens=max_tokens,
                     context=context or {})
    return run(session, llm, user, req).as_(schema)


def recent_usage(session: Session, user: User, days: int = 30) -> dict[str, int]:
    rows = session.exec(select(LlmUsage).where(LlmUsage.user_id == user.id,
                                               LlmUsage.created_at >= utcnow() - timedelta(days=days))).all()
    return {"calls": len(rows), "input_tokens": sum(r.input_tokens for r in rows),
            "output_tokens": sum(r.output_tokens for r in rows)}
