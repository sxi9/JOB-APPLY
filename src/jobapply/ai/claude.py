"""Claude backend over the official SDK. One request per task, structured outputs for schemas,
prompt caching on the stable system prompt, server-side fallbacks on safety refusals."""

from __future__ import annotations

from typing import Any

import anthropic

from jobapply.ai.base import LLM, LLMError, LLMRefused, LLMRequest, LLMResult
from jobapply.config import get_settings
from jobapply.logs import get_logger

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeLLM(LLM):
    name = "claude"

    def __init__(self, api_key: str | None = None, model: str | None = None, effort: str | None = None,
                 client: Any = None):
        s = get_settings()
        self.model = model or s.claude_model
        self.effort = effort or s.claude_effort
        if client is None:
            key = api_key or (s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None)
            if not key:
                raise LLMError("no Anthropic API key: set JOBAPPLY_ANTHROPIC_API_KEY or a per-user key")
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    def _kwargs(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": request.prompt}],
            "output_config": {"effort": self.effort},
            # Safety classifiers can decline a request; "default" re-runs it on Anthropic's recommended model.
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

    def complete(self, request: LLMRequest) -> LLMResult:
        kwargs = self._kwargs(request)
        try:
            if request.schema is not None:
                response = self.client.beta.messages.parse(output_format=request.schema, **kwargs)
            else:
                response = self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc.message}", retryable=True) from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("invalid Anthropic API key") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"bad request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"network error: {exc}", retryable=True) from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefused(getattr(details, "category", None), getattr(details, "explanation", None))
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{request.task}: output truncated at {request.max_tokens} tokens")
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        parsed = getattr(response, "parsed_output", None) if request.schema is not None else None
        if request.schema is not None and parsed is None:
            try:
                parsed = request.schema.model_validate_json(text)
            except ValueError as exc:
                raise LLMError(f"{request.task}: response did not match {request.schema.__name__}") from exc
        usage = response.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        log.debug("claude call", task=request.task, model=response.model, input=usage.input_tokens,
                  cached=cached, output=usage.output_tokens)
        return LLMResult(text=text, parsed=parsed, model=f"claude:{response.model}",
                         input_tokens=usage.input_tokens + cached, output_tokens=usage.output_tokens)
