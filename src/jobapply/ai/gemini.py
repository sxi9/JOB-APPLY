"""Google Gemini (Generative Language API) over httpx."""

from __future__ import annotations

from typing import Any

import httpx

from jobapply.ai.base import LLM, LLMError, LLMRefused, LLMRequest, LLMResult
from jobapply.ai.openai_compat import parse_json, raise_http
from jobapply.ai.schema_prompt import json_instructions

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiLLM(LLM):
    name = "gemini"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None, base_url: str = DEFAULT_BASE):
        self.model, self.base_url = model, base_url.rstrip("/")
        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def complete(self, request: LLMRequest) -> LLMResult:
        system = request.system
        config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if request.schema is not None:
            system += "\n\n" + json_instructions(request.schema)
            config["responseMimeType"] = "application/json"
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": config,
        }
        try:
            resp = self.client.post(f"{self.base_url}/models/{self.model}:generateContent", json=body,
                                    headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"gemini: network error: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise_http("gemini", resp)
        data = resp.json()
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMRefused(block)
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("gemini: no candidates in response")
        cand = candidates[0]
        finish = cand.get("finishReason", "STOP")
        if finish == "MAX_TOKENS":
            raise LLMError(f"gemini: output truncated at {request.max_tokens} tokens")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise LLMRefused(finish)
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        parsed = parse_json("gemini", request.schema, text) if request.schema is not None else None
        usage = data.get("usageMetadata") or {}
        return LLMResult(text=text.strip(), parsed=parsed, model=f"gemini:{data.get('modelVersion', self.model)}",
                         input_tokens=int(usage.get("promptTokenCount") or 0),
                         output_tokens=int(usage.get("candidatesTokenCount") or 0))
