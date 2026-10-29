"""LLM interface. Every AI task goes through `LLM.complete`, so tests run on `FakeLLM`
and production on `ClaudeLLM` without the callers knowing which."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Call failed. `retryable` says whether a later attempt may work (rate limit, network)."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMRefused(LLMError):
    def __init__(self, category: str | None, explanation: str | None = None):
        super().__init__(f"model declined the request ({category or 'unspecified'}): {explanation or ''}".strip())
        self.category = category


@dataclass
class LLMRequest:
    task: str  # score | resume | cover_letter | answer | classify | form_map
    system: str
    prompt: str
    schema: type[BaseModel] | None = None
    max_tokens: int = 4096
    # Structured facts behind the prompt. Fakes read these instead of parsing prose.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    text: str
    parsed: BaseModel | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    def as_(self, schema: type[T]) -> T:
        if not isinstance(self.parsed, schema):
            raise LLMError(f"expected {schema.__name__}, got {type(self.parsed).__name__}")
        return self.parsed


class LLM(ABC):
    name: str

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResult: ...


# ---------------------------------------------------------------- schemas


class ScoreResult(BaseModel):
    score: int
    reason: str
    matched_skills: list[str] = []
    missing_skills: list[str] = []


class SkillGroup(BaseModel):
    name: str
    keywords: list[str] = []


class WorkEntry(BaseModel):
    name: str
    position: str
    startDate: str = ""  # noqa: N815 (JSON Resume field names)
    endDate: str = ""  # noqa: N815
    summary: str = ""
    highlights: list[str] = []


class EducationEntry(BaseModel):
    institution: str
    area: str = ""
    studyType: str = ""  # noqa: N815
    endDate: str = ""  # noqa: N815


class ResumeVariant(BaseModel):
    """Tailored content. Contact details stay on the master and are never sent for rewriting."""

    label: str
    summary: str
    skills: list[SkillGroup] = []
    work: list[WorkEntry] = []
    education: list[EducationEntry] = []


class AnswerResult(BaseModel):
    answer: str
    confidence: int  # 0-100, how well the vault supports this answer
