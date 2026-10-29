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
from dataclasses import dataclass, field, replace

import httpx

from jobapply.ai.base import LLM, LLMError, LLMRefused, LLMRequest, LLMResult
from jobapply.config import Settings, get_settings
from jobapply.logs import get_logger
from jobapply.vault import mask

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
    index: int = 1  # 1-based position of this key among the provider's keys from the same source

    @property
    def usable(self) -> bool:
        return bool(self.api_key) or not self.spec.needs_key

    @property
    def label(self) -> str:
        """Short chain label: `gemini#2` for the server's second key, `gemini#u1` for a user's first."""
        return f"{self.spec.name}#{'u' if self.source == 'user' else ''}{self.index}"

    @property
    def key_hint(self) -> str:
        return key_hint(self.api_key)


def key_hint(key: str | None) -> str:
    """Last four characters of a key for labels; never the key itself."""
    if not key:
        return "no key"
    return "…" + key[-4:] if len(key) >= 12 else mask(key)


def parse_order(text: str | None) -> list[str]:
    seen: list[str] = []
    for part in (text or "").replace("\n", ",").split(","):
        name = part.strip().lower()
        if name in CATALOG and name not in seen:
            seen.append(name)
    return seen


def parse_keys(*texts: str | None) -> list[str]:
    """Keys from comma- or newline-separated text (several sources allowed), deduped, order kept."""
    seen: list[str] = []
    for text in texts:
        for part in (text or "").replace("\n", ",").split(","):
            key = part.strip()
            if key and key not in seen:
                seen.append(key)
    return seen


def server_keys(name: str, settings: Settings | None = None) -> list[str]:
    """Every server key for a provider: JOBAPPLY_<PROVIDER>_API_KEY (list allowed) plus _API_KEYS."""
    s = settings or get_settings()
    prefix = "anthropic" if name == "claude" else name
    values = []
    for attr in (f"{prefix}_api_key", f"{prefix}_api_keys"):
        secret = getattr(s, attr, None)
        values.append(secret.get_secret_value() if secret else None)
    return parse_keys(*values)


def server_configs(name: str, settings: Settings | None = None) -> list[ProviderConfig]:
    """One ProviderConfig per server key (same model and base URL); a keyless placeholder when none is set."""
    s = settings or get_settings()
    spec = CATALOG[name]
    if name == "claude":
        model, base_url = s.claude_model, None
    else:
        model = getattr(s, f"{name}_model", None) or spec.model
        base_url = getattr(s, f"{name}_base_url", None) or spec.base_url
    keys = server_keys(name, s)
    if not keys:
        return [ProviderConfig(spec, None, model, base_url)]
    return [ProviderConfig(spec, key, model, base_url, index=i) for i, key in enumerate(keys, start=1)]


def server_config(name: str, settings: Settings | None = None) -> ProviderConfig:
    """The provider's first server key (kept for callers that only need model/base URL or "is a key set")."""
    return server_configs(name, settings)[0]


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
    label: str = ""  # gemini#2 / gemini#u1, from the last chain that registered this key
    hint: str = ""  # …last4, never the key


_health: dict[str, _Health] = {}
_rr: dict[str, int] = {}  # round-robin counter per provider name and key source (`gemini:user`, `gemini:server`)
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
                "label": h.label or k.split(":")[0],
                "hint": h.hint,
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
        _rr.clear()


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
                with _lock:
                    h = _health.setdefault(health_key(cfg), _Health())
                    h.label, h.hint = cfg.label, cfg.key_hint
        return chain

    @property
    def providers(self) -> list[str]:
        return [e.cfg.spec.name for e in self.entries]

    @property
    def labels(self) -> list[str]:
        return [e.cfg.label for e in self.entries]

    @staticmethod
    def _group_key(entry: _Entry) -> str:
        return f"{entry.cfg.spec.name}:{entry.cfg.source}"

    def _groups(self) -> list[list[_Entry]]:
        """Entries grouped by provider and key source, in chain order (a provider's keys are contiguous in the
        chain, the user's ahead of the server's). Keeping the sources apart is what makes a user's own keys
        answer before the server's keys are charged."""
        groups: list[list[_Entry]] = []
        for entry in self.entries:
            if groups and self._group_key(groups[-1][0]) == self._group_key(entry):
                groups[-1].append(entry)
            else:
                groups.append([entry])
        return groups

    def _rotated(self, group: list[_Entry]) -> list[_Entry]:
        """Round-robin start within one provider's keys of one source so load spreads over them; cooling keys
        are skipped later."""
        if len(group) < 2:
            return group
        name = self._group_key(group[0])
        with _lock:
            start = _rr.get(name, 0) % len(group)
            _rr[name] = start + 1
        return group[start:] + group[:start]

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
            "provider key in cooldown",
            provider=entry.cfg.spec.name,
            key=entry.cfg.label,
            minutes=int(self.cooldown_s / 60 * (6 if long else 1)),
            error=str(exc)[:200],
        )

    def complete(self, request: LLMRequest) -> LLMResult:
        if not self.entries:
            raise LLMError("no LLM provider configured: set an API key in .env or on the Settings page")
        errors: list[str] = []
        now = time.time()
        skipped_cooling = 0
        any_retryable = False
        for group in self._groups():
            for entry in self._rotated(group):
                key = health_key(entry.cfg)
                with _lock:
                    h = _health.setdefault(key, _Health())
                    if h.until > now:
                        skipped_cooling += 1
                        continue
                try:
                    result = entry.llm.complete(request)
                except LLMRefused as exc:
                    errors.append(f"{entry.cfg.label}: {exc}")
                    continue  # a different provider may accept the same benign request
                except LLMError as exc:
                    errors.append(f"{entry.cfg.label}: {exc}")
                    if exc.retryable:
                        any_retryable = True
                        self._cooldown(entry, exc)
                    continue
                with _lock:
                    h.successes += 1
                    h.last_used = time.time()
                return _labelled(result, entry.cfg)
        if skipped_cooling and not errors:
            raise LLMError("every configured provider is cooling down after recent failures", retryable=True)
        # The combined error is retryable only when some key is (or was) worth retrying later: a rate limit,
        # credits, auth or network problem, or a key sitting in cooldown. When every key answered with a
        # per-request problem (refusal, bad request, schema mismatch) a later attempt with the same request
        # would fail the same way, so callers must not treat the chain as exhausted.
        raise LLMError("all providers failed: " + " | ".join(errors), retryable=any_retryable or skipped_cooling > 0)


def _labelled(result: LLMResult, cfg: ProviderConfig) -> LLMResult:
    """`gemini:gemini-2.5-flash` -> `gemini#2:gemini-2.5-flash`, so llm_usage records which key answered."""
    name = cfg.spec.name
    model = result.model
    if model.startswith(name + ":"):
        model = model[len(name) + 1 :]
    return replace(result, model=f"{cfg.label}:{model}")
