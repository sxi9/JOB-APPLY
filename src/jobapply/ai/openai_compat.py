"""OpenAI-compatible chat completions over httpx. Covers OpenAI, OpenRouter, Groq, Together, Mistral,
DeepSeek, xAI, Ollama and LM Studio: same endpoint shape, different base URL."""

from __future__ import annotations

from typing import Any

import httpx

from jobapply.ai.base import LLM, LLMError, LLMRefused, LLMRequest, LLMResult
from jobapply.ai.schema_prompt import json_instructions


class OpenAICompatLLM(LLM):
    def __init__(self, name: str, base_url: str, api_key: str | None, model: str,
                 client: httpx.Client | None = None, extra_headers: dict[str, str] | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), headers=headers)
        self._headers = headers
        self._json_schema_ok = True  # flipped off when the provider rejects response_format=json_schema

    def _body(self, request: LLMRequest, mode: str) -> dict[str, Any]:
        system = request.system
        body: dict[str, Any] = {"model": self.model, "max_tokens": request.max_tokens}
        if request.schema is not None:
            system = system + "\n\n" + json_instructions(request.schema)
            if mode == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": request.schema.__name__, "schema": request.schema.model_json_schema()}}
            elif mode == "json_object":
                body["response_format"] = {"type": "json_object"}
        body["messages"] = [{"role": "system", "content": system}, {"role": "user", "content": request.prompt}]
        return body

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return self.client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: network error: {exc}", retryable=True) from exc

    def complete(self, request: LLMRequest) -> LLMResult:
        mode = "json_schema" if self._json_schema_ok else "json_object"
        resp = self._post(self._body(request, mode))
        if resp.status_code == 400 and request.schema is not None and mode == "json_schema":
            self._json_schema_ok = False
            resp = self._post(self._body(request, "json_object"))
        if resp.status_code == 400 and request.schema is not None and "response_format" in resp.text:
            resp = self._post(self._body(request, "none"))
        if resp.status_code >= 400:
            raise_http(self.name, resp)
        data = resp.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name}: unexpected response shape") from exc
        finish = choice.get("finish_reason")
        if finish == "length":
            raise LLMError(f"{self.name}: output truncated at {request.max_tokens} tokens")
        if finish == "content_filter":
            raise LLMRefused("content_filter")
        parsed = None
        if request.schema is not None:
            parsed = parse_json(self.name, request.schema, text)
        usage = data.get("usage") or {}
        return LLMResult(text=text.strip(), parsed=parsed, model=f"{self.name}:{data.get('model', self.model)}",
                         input_tokens=int(usage.get("prompt_tokens") or 0),
                         output_tokens=int(usage.get("completion_tokens") or 0))


def raise_http(name: str, resp: httpx.Response) -> None:
    snippet = resp.text[:300]
    low = snippet.lower()
    if resp.status_code in (401, 403):
        raise LLMError(f"{name}: authentication failed ({resp.status_code})", retryable=True)
    if resp.status_code in (402, 429) or "quota" in low or "credit" in low or "billing" in low:
        raise LLMError(f"{name}: rate limited or out of credits ({resp.status_code}): {snippet}", retryable=True)
    if resp.status_code >= 500:
        raise LLMError(f"{name}: server error {resp.status_code}", retryable=True)
    raise LLMError(f"{name}: HTTP {resp.status_code}: {snippet}")


def parse_json(name: str, schema, text: str):
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0] if "```" in raw else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        return schema.model_validate_json(raw)
    except ValueError as exc:
        raise LLMError(f"{name}: response did not match {schema.__name__}: {exc}"[:400]) from exc
