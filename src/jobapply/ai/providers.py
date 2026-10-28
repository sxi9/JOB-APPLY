"""Provider catalog and failover chain.

`FailoverLLM` walks an ordered list of providers. When one is rate limited, out of credits, down or
rejecting the key, it is put in cooldown and the next one answers; the request only fails when every
provider has. Cooldowns are per process and keyed by provider + key, so one user's exhausted key does not
block another user's key for the same provider.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

import httpx

from jobapply.ai.base import LLM, LLMError, LLMRefused, LLMRequest, LLMResult
from jobapply.config import Settings, get_settings
from jobapply.logs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str  # anthropic | openai | gemini
    label: str
    base_url: str | None = None
    model: str = ""
    needs_key: bool = True
    docs: str = ""


# Failover order defaults to this order; JOBAPPLY_LLM_PROVIDERS and each user's settings can change it.
CATALOG: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        "claude", "anthropic", "Anthropic Claude", model="claude-opus-5-5", docs="https://console.anthropic.com/"
    ),
    "openai": ProviderSpec(
        "openai", "openai", "OpenAI", "https://api.openai.com/v1", "gpt-5", docs="https://platform.openai.com/"
    ),
    "gemini": ProviderSpec(
        "gemini", "gemini", "Google Gemini", model="gemini-2.5-flash", docs="https://aistudio.google.com/"
    ),
    "openrouter": ProviderSpec(
        "openrouter",
        "openai",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "meta-llama/llama-3.3-70b-instruct",
        docs="https://openrouter.ai/",
    ),
    "groq": ProviderSpec(
        "groq",
        "openai",
        "Groq",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        docs="https://console.groq.com/",
    ),
    "together": ProviderSpec(
        "together",
        "openai",
        "Together AI",
        "https://api.together.xyz/v1",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        docs="https://api.together.ai/",
    ),
    "mistral": ProviderSpec(
        "mistral",
        "openai",
        "Mistral",
        "https://api.mistral.ai/v1",
        "mistral-large-latest",
        docs="https://console.mistral.ai/",
    ),
    "deepseek": ProviderSpec(
        "deepseek",
        "openai",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        docs="https://platform.deepseek.com/",
    ),
    "xai": ProviderSpec("xai", "openai", "xAI Grok", "https://api.x.ai/v1", "grok-4", docs="https://console.x.ai/"),
    "ollama": ProviderSpec(
        "ollama",
        "openai",
        "Ollama (local)",
        "http://127.0.0.1:11434/v1",
        "llama3.1",
        needs_key=False,
        docs="https://ollama.com/",
    ),
}


@dataclass
class ProviderConfig:
    """A resolved provider: catalog spec plus the key, model and base URL in effect for one user."""

    spec: ProviderSpec
    api_key: str | None
    model: str
    base_url: str | None
    source: str = "server"  # server | user

    @property
    def usable(self) -> bool:
        return bool(self.api_key) or not self.spec.needs_key


def parse_order(text: str | None) -> list[str]:
    seen: list[str] = []
    for part in (text or "").replace("\n", ",").split(","):
        name = part.strip().lower()
        if name in CATALOG and name not in seen:
            seen.append(name)
    return seen


def server_config(name: str, settings: Settings | None = None) -> ProviderConfig:
    s = settings or get_settings()
    spec = CATALOG[name]
    if name == "claude":
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None
        return ProviderConfig(spec, key, s.claude_model, None)
    secret = getattr(s, f"{name}_api_key", None)
    key = secret.get_secret_value() if secret else None
    model = getattr(s, f"{name}_model", None) or spec.model
    base_url = getattr(s, f"{name}_base_url", None) or spec.base_url
    return ProviderConfig(spec, key, model, base_url)


def build_provider(cfg: ProviderConfig, client: httpx.Client | None = None) -> LLM:
    spec = cfg.spec
    if spec.kind == "anthropic":
        from jobapply.ai.claude import ClaudeLLM

        return ClaudeLLM(api_key=cfg.api_key, model=cfg.model)
    if spec.kind == "gemini":
        from jobapply.ai.gemini import GeminiLLM

        return GeminiLLM(api_key=cfg.api_key or "", model=cfg.model, client=client)
    from jobapply.ai.openai_compat import OpenAICompatLLM

    headers = (
        {"HTTP-Referer": "https://github.com/sxi9/JOB-APPLY", "X-Title": "JOB-APPLY"}
        if spec.name == "openrouter"
        else None
    )
    return OpenAICompatLLM(
        spec.name, cfg.base_url or spec.base_url or "", cfg.api_key, cfg.model, client=client, extra_headers=headers
    )


# ---------------------------------------------------------------- cooldowns


@dataclass
class _Health:
    until: float = 0.0
    last_error: str = ""
    failures: int = 0
    successes: int = 0
    last_used: float = 0.0


_health: dict[str, _Health] = {}
_lock = threading.Lock()


def health_key(cfg: ProviderConfig) -> str:
    digest = hashlib.sha1((cfg.api_key or "").encode()).hexdigest()[:8]
    return f"{cfg.spec.name}:{digest}"


def provider_status() -> list[dict]:
    now = time.time()
    with _lock:
        return [
            {
                "provider": k.split(":")[0],
                "key": k,
                "cooling": h.until > now,
                "cooldown_left_s": max(0, int(h.until - now)),
                "last_error": h.last_error,
                "failures": h.failures,
                "successes": h.successes,
                "last_used": h.last_used,
            }
            for k, h in sorted(_health.items())
        ]


def reset_health() -> None:
    with _lock:
        _health.clear()


# ---------------------------------------------------------------- chain


@dataclass
class _Entry:
    cfg: ProviderConfig
    llm: LLM


@dataclass
class FailoverLLM(LLM):
    name = "failover"
    entries: list[_Entry] = field(default_factory=list)
    cooldown_s: float = 600.0

    @classmethod
    def from_configs(
        cls, configs: list[ProviderConfig], cooldown_minutes: float | None = None, client: httpx.Client | None = None
    ) -> FailoverLLM:
        minutes = cooldown_minutes if cooldown_minutes is not None else get_settings().llm_cooldown_minutes
        chain = cls(cooldown_s=minutes * 60)
        for cfg in configs:
            if cfg.usable:
                chain.entries.append(_Entry(cfg, build_provider(cfg, client=client)))
        return chain

    @property
    def providers(self) -> list[str]:
        return [e.cfg.spec.name for e in self.entries]

    def _cooldown(self, entry: _Entry, exc: LLMError) -> None:
        msg = str(exc).lower()
        long = any(w in msg for w in ("credit", "quota", "billing", "authentication", "api key"))
        key = health_key(entry.cfg)
        with _lock:
            h = _health.setdefault(key, _Health())
            h.failures += 1
            h.last_error = str(exc)[:300]
            h.until = time.time() + self.cooldown_s * (6 if long else 1)
        log.warning(
            "provider in cooldown",
            provider=entry.cfg.spec.name,
            minutes=int(self.cooldown_s / 60 * (6 if long else 1)),
            error=str(exc)[:200],
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        if not self.entries:
            raise LLMError("no LLM provider configured: set an API key in .env or on the Settings page")
        errors: list[str] = []
        now = time.time()
        skipped_cooling = 0
        for entry in self.entries:
            key = health_key(entry.cfg)
            with _lock:
                h = _health.setdefault(key, _Health())
                if h.until > now:
                    skipped_cooling += 1
                    continue
            try:
                result = entry.llm.complete(request)
            except LLMRefused as exc:
                errors.append(f"{entry.cfg.spec.name}: {exc}")
                continue  # a different provider may accept the same benign request
            except LLMError as exc:
                errors.append(f"{entry.cfg.spec.name}: {exc}")
                if exc.retryable:
                    self._cooldown(entry, exc)
                continue
            with _lock:
                h.successes += 1
                h.last_used = time.time()
            return result
        if skipped_cooling and not errors:
            raise LLMError("every configured provider is cooling down after recent failures", retryable=True)
        raise LLMError("all providers failed: " + " | ".join(errors), retryable=True)
