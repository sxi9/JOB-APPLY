import json

import httpx
import pytest

from jobapply.ai import providers
from jobapply.ai.base import LLMError, LLMRefused, LLMRequest, ScoreResult
from jobapply.ai.gemini import GeminiLLM
from jobapply.ai.openai_compat import OpenAICompatLLM
from jobapply.ai.providers import CATALOG, FailoverLLM, ProviderConfig, parse_order, server_config
from jobapply.ai.service import get_llm, user_provider_configs
from jobapply.config import Settings, get_settings
from jobapply.models import User
from jobapply.vault import Vault, generate_key

SCORE_JSON = '{"score": 77, "reason": "fits", "matched_skills": ["python"], "missing_skills": []}'


@pytest.fixture(autouse=True)
def _fresh_health():
    providers.reset_health()
    yield
    providers.reset_health()


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def openai_ok(text=SCORE_JSON, finish="stop", model="gpt-x"):
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 12},
        },
    )


# ----------------------------------------------------------------- OpenAI-compatible


def test_openai_structured_request_and_parse():
    seen = []

    def handler(req):
        seen.append(req)
        return openai_ok()

    llm = OpenAICompatLLM("openai", "https://api.openai.com/v1", "sk-1", "gpt-x", client=mock_client(handler))
    out = llm.complete(LLMRequest(task="score", system="SYS", prompt="P", schema=ScoreResult, max_tokens=512))
    assert out.parsed.score == 77 and out.model == "openai:gpt-x" and out.input_tokens == 50
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-1"
    body = json.loads(req.content)
    assert body["model"] == "gpt-x" and body["max_tokens"] == 512
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["role"] == "system" and "JSON schema" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "P"}


def test_openai_falls_back_to_json_object_then_plain():
    modes = []

    def handler(req):
        body = json.loads(req.content)
        fmt = (body.get("response_format") or {}).get("type")
        modes.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported"}})
        return openai_ok("```json\n" + SCORE_JSON + "\n```")

    llm = OpenAICompatLLM("groq", "https://api.groq.com/openai/v1", "k", "m", client=mock_client(handler))
    out = llm.complete(LLMRequest(task="score", system="S", prompt="P", schema=ScoreResult))
    assert out.parsed.score == 77 and modes == ["json_schema", "json_object"]
    llm.complete(LLMRequest(task="score", system="S", prompt="P", schema=ScoreResult))
    assert modes[-1] == "json_object"  # remembers that json_schema is unsupported


@pytest.mark.parametrize(
    "status,body,retryable,needle",
    [
        (429, {"error": {"message": "Rate limit"}}, True, "rate limited"),
        (402, {"error": {"message": "insufficient credits"}}, True, "credits"),
        (401, {"error": {"message": "bad key"}}, True, "authentication"),
        (503, "down", True, "server error"),
        (404, {"error": {"message": "model not found"}}, False, "HTTP 404"),
    ],
)
def test_openai_http_errors(status, body, retryable, needle):
    def handler(req):
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    llm = OpenAICompatLLM("openai", "https://x/v1", "k", "m", client=mock_client(handler))
    with pytest.raises(LLMError) as exc:
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    assert exc.value.retryable is retryable and needle in str(exc.value)


def test_openai_finish_reasons_and_bad_json():
    llm = OpenAICompatLLM("openai", "https://x/v1", "k", "m", client=mock_client(lambda r: openai_ok(finish="length")))
    with pytest.raises(LLMError, match="truncated"):
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    llm = OpenAICompatLLM(
        "openai", "https://x/v1", "k", "m", client=mock_client(lambda r: openai_ok(finish="content_filter"))
    )
    with pytest.raises(LLMRefused):
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    llm = OpenAICompatLLM("openai", "https://x/v1", "k", "m", client=mock_client(lambda r: openai_ok("nope")))
    with pytest.raises(LLMError, match="did not match"):
        llm.complete(LLMRequest(task="t", system="S", prompt="P", schema=ScoreResult))
    llm = OpenAICompatLLM(
        "ollama", "http://127.0.0.1:11434/v1", None, "llama3.1", client=mock_client(lambda r: openai_ok("hello"))
    )
    assert llm.complete(LLMRequest(task="t", system="S", prompt="P")).text == "hello"


def test_openai_network_error_is_retryable():
    def handler(req):
        raise httpx.ConnectError("refused")

    llm = OpenAICompatLLM("openai", "https://x/v1", "k", "m", client=mock_client(handler))
    with pytest.raises(LLMError) as exc:
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    assert exc.value.retryable


# ----------------------------------------------------------------- Gemini


def test_gemini_request_and_parse():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-2.5-flash-001",
                "candidates": [{"content": {"parts": [{"text": SCORE_JSON}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 20},
            },
        )

    llm = GeminiLLM("g-key", "gemini-2.5-flash", client=mock_client(handler))
    out = llm.complete(LLMRequest(task="score", system="SYS", prompt="P", schema=ScoreResult, max_tokens=300))
    assert out.parsed.score == 77 and out.model == "gemini:gemini-2.5-flash-001" and out.input_tokens == 80
    req = seen[0]
    assert "models/gemini-2.5-flash:generateContent" in str(req.url) and req.headers["x-goog-api-key"] == "g-key"
    body = json.loads(req.content)
    assert body["generationConfig"] == {"maxOutputTokens": 300, "responseMimeType": "application/json"}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "P"}]}]
    assert "SYS" in body["system_instruction"]["parts"][0]["text"]


def test_gemini_blocks_and_errors():
    llm = GeminiLLM(
        "k", "m", client=mock_client(lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    )
    with pytest.raises(LLMRefused):
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    llm = GeminiLLM("k", "m", client=mock_client(lambda r: httpx.Response(429, json={"error": {"message": "quota"}})))
    with pytest.raises(LLMError) as exc:
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))
    assert exc.value.retryable
    llm = GeminiLLM(
        "k",
        "m",
        client=mock_client(
            lambda r: httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]}
            )
        ),
    )
    with pytest.raises(LLMError, match="truncated"):
        llm.complete(LLMRequest(task="t", system="S", prompt="P"))


# ----------------------------------------------------------------- catalog and failover


def test_parse_order_and_server_config():
    assert parse_order("Groq, nope, claude,groq\nopenai") == ["groq", "claude", "openai"]
    s = Settings(_env_file=None, openai_api_key="k", openai_model="gpt-custom", groq_base_url=None)
    cfg = server_config("openai", s)
    assert cfg.usable and cfg.model == "gpt-custom" and cfg.base_url == "https://api.openai.com/v1"
    assert not server_config("groq", s).usable
    assert server_config("ollama", s).usable  # no key needed
    assert server_config("claude", s).model == "claude-opus-5-5" and not server_config("claude", s).usable


def _chain(handlers: dict[str, object], order=("openai", "groq", "gemini"), minutes=10):
    """A chain where each provider answers through its own mock handler (a Response or an exception)."""

    def handler(req):
        host = req.url.host
        name = {"api.openai.com": "openai", "api.groq.com": "groq", "generativelanguage.googleapis.com": "gemini"}[host]
        out = handlers[name]
        if isinstance(out, Exception):
            raise out
        return out() if callable(out) else out

    client = mock_client(handler)
    configs = [ProviderConfig(CATALOG[n], "key", CATALOG[n].model, CATALOG[n].base_url) for n in order]
    return FailoverLLM.from_configs(configs, cooldown_minutes=minutes, client=client)


def test_failover_moves_to_next_provider_and_cools_down():
    calls = {"openai": 0, "groq": 0}

    def openai_down():
        calls["openai"] += 1
        return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})

    def groq_ok():
        calls["groq"] += 1
        return openai_ok("fine", model="llama")

    chain = _chain({"openai": openai_down, "groq": groq_ok, "gemini": httpx.Response(500)})
    assert chain.providers == ["openai", "groq", "gemini"]
    req = LLMRequest(task="t", system="S", prompt="P")
    assert chain.complete(req).model == "groq:llama"
    assert chain.complete(req).model == "groq:llama"
    assert calls == {"openai": 1, "groq": 2}  # openai is in cooldown, not retried
    status = {h["provider"]: h for h in providers.provider_status()}
    assert status["openai"]["cooling"] and "Rate limit" in status["openai"]["last_error"]
    assert status["groq"]["successes"] == 2 and not status["groq"]["cooling"]


def test_failover_quota_cooldown_is_longer_and_everything_failing_raises():
    chain = _chain(
        {
            "openai": httpx.Response(402, json={"error": {"message": "insufficient credits"}}),
            "groq": httpx.Response(503, text="down"),
            "gemini": httpx.Response(401, json={"error": {"message": "API key not valid"}}),
        }
    )
    with pytest.raises(LLMError) as exc:
        chain.complete(LLMRequest(task="t", system="S", prompt="P"))
    assert "openai" in str(exc.value) and "groq" in str(exc.value) and "gemini" in str(exc.value)
    status = {h["provider"]: h for h in providers.provider_status()}
    assert status["openai"]["cooldown_left_s"] > status["groq"]["cooldown_left_s"]  # credits: 6x longer
    with pytest.raises(LLMError, match="cooling down"):
        chain.complete(LLMRequest(task="t", system="S", prompt="P"))


def test_failover_skips_refusal_and_non_retryable_without_cooldown():
    calls = {"openai": 0}

    def openai_refuses():
        calls["openai"] += 1
        return openai_ok(finish="content_filter")

    chain = _chain(
        {
            "openai": openai_refuses,
            "groq": httpx.Response(404, json={"error": {"message": "no model"}}),
            "gemini": httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
            ),
        }
    )
    req = LLMRequest(task="t", system="S", prompt="P")
    assert chain.complete(req).model.startswith("gemini:")
    assert chain.complete(req).model.startswith("gemini:")
    assert calls["openai"] == 2  # refusals do not cool a provider down
    assert not any(h["cooling"] for h in providers.provider_status())


def test_failover_cooldown_is_per_key():
    a = ProviderConfig(CATALOG["openai"], "key-a", "m", "https://api.openai.com/v1")
    b = ProviderConfig(CATALOG["openai"], "key-b", "m", "https://api.openai.com/v1")
    assert providers.health_key(a) != providers.health_key(b)


def test_empty_chain_raises():
    with pytest.raises(LLMError, match="no LLM provider"):
        FailoverLLM.from_configs([], cooldown_minutes=1).complete(LLMRequest(task="t", system="S", prompt="P"))


# ----------------------------------------------------------------- per-user resolution


def test_user_provider_configs_prefers_user_keys_order_and_models(session, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_VAULT_KEY", generate_key())
    monkeypatch.setenv("JOBAPPLY_OPENAI_API_KEY", "server-openai")
    monkeypatch.setenv("JOBAPPLY_LLM_PROVIDERS", "claude,openai,groq")
    get_settings.cache_clear()
    try:
        user = User(
            username="u",
            password_hash="x",
            preferences={"llm_providers": "groq,openai", "llm_models": {"openai": "gpt-mine"}},
        )
        session.add(user)
        session.commit()
        Vault().store(session, user.id, "groq", "api_key", "", {"password": "user-groq"})
        session.commit()
        configs = user_provider_configs(session, user)
        assert [c.spec.name for c in configs] == ["groq", "openai"]
        assert configs[0].api_key == "user-groq" and configs[0].source == "user"
        assert (
            configs[1].api_key == "server-openai" and configs[1].model == "gpt-mine" and configs[1].source == "server"
        )
        # no preference: server order, server keys
        plain = User(username="p", password_hash="x")
        session.add(plain)
        session.commit()
        names = [(c.spec.name, c.usable) for c in user_provider_configs(session, plain)]
        assert names == [("claude", False), ("openai", True), ("groq", False)]
        chain = get_llm(session, plain, backend="live")
        assert isinstance(chain, FailoverLLM) and chain.providers == ["openai"]
    finally:
        get_settings.cache_clear()


def test_get_llm_live_without_any_key_raises(session, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_LLM_PROVIDERS", "claude,openai")
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMError, match="no LLM provider has an API key"):
            get_llm(session, None, backend="live")
    finally:
        get_settings.cache_clear()
