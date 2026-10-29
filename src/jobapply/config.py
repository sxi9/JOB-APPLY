"""Application settings, read from environment variables and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBAPPLY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    database_url: str = "sqlite:///jobapply.db"
    data_dir: Path = Path("data")
    companies_file: Path = Path("config/companies.yaml")
    profiles_dir: Path = Path("config/form_profiles")
    log_level: str = "INFO"
    log_json: bool = False

    # Safety. Dry run is on unless explicitly disabled; the kill switch stops all applying.
    dry_run: bool = True
    kill_switch: bool = False

    # Dashboard
    secret_key: SecretStr = SecretStr("change-me")
    host: str = "127.0.0.1"
    port: int = 8000
    # Self-service sign-up. When False, /signup 404s and the login page hides the "Create account" link;
    # the first user is still bootstrapped by the CLI (`jobapply create-user`).
    allow_signup: bool = True

    # Credential vault: Fernet key (urlsafe base64, 32 bytes). Generate with `jobapply vault-key`.
    vault_key: SecretStr | None = None

    # AI. `llm_providers` is the failover order; a provider is used only when its key is set
    # (ollama needs none). Users can override order, keys and models on their Settings page.
    # Every `<provider>_api_key` accepts one key or a comma/newline-separated list; `<provider>_api_keys`
    # is an alias for the list form. Keys of one provider are rotated round-robin and each cools down alone.
    llm_backend: str = Field("fake", description="fake | live")
    llm_providers: str = Field("claude,openai,gemini,openrouter,groq,together,mistral,deepseek,xai,ollama",
                               description="comma-separated failover order")
    anthropic_api_key: SecretStr | None = None
    anthropic_api_keys: SecretStr | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = Field("medium", description="low | medium | high | xhigh | max")
    openai_api_key: SecretStr | None = None
    openai_api_keys: SecretStr | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_api_keys: SecretStr | None = None
    gemini_model: str | None = None
    openrouter_api_key: SecretStr | None = None
    openrouter_api_keys: SecretStr | None = None
    openrouter_model: str | None = None
    groq_api_key: SecretStr | None = None
    groq_api_keys: SecretStr | None = None
    groq_model: str | None = None
    together_api_key: SecretStr | None = None
    together_api_keys: SecretStr | None = None
    together_model: str | None = None
    mistral_api_key: SecretStr | None = None
    mistral_api_keys: SecretStr | None = None
    mistral_model: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_api_keys: SecretStr | None = None
    deepseek_model: str | None = None
    xai_api_key: SecretStr | None = None
    xai_api_keys: SecretStr | None = None
    xai_model: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    llm_cooldown_minutes: int = 10  # after a rate limit / outage; quota and auth failures wait 6x longer
    score_threshold: int = 70
    resume_cache_days: int = 30

    # Job APIs
    adzuna_app_id: str | None = None
    adzuna_app_key: SecretStr | None = None
    adzuna_country: str = "in"
    rapidapi_key: SecretStr | None = None

    # Collectors
    http_timeout: float = 20.0
    user_agent: str = "jobapply/0.1 (+https://github.com/sxi9/JOB-APPLY)"
    # Ingest guard for evergreen/stale reqs: an ATS posting whose createdAt is older than this is still
    # stored (we never fake the date) but flagged in its source raw (`stale_posting`) so downstream can
    # de-prioritise it. The default lines up with the sane default recency windows in the UI and in the
    # per-user filters (UserFilters.max_age_hours=72), which already exclude postings this old.
    max_posting_age_days: int = 365
    collect_interval_minutes: int = 45
    # Hot-polling tier: a second collect task over the fast exact-timestamp sources only
    # (ATS APIs for the hot company list + LinkedIn guest), for minute-level recency.
    hot_interval_minutes: int = 5

    # Notifier
    whatsapp_base_url: str | None = None
    whatsapp_token: SecretStr | None = None

    # Browser. Leave empty to use Playwright's bundled Chromium.
    chromium_executable: str | None = None

    # LinkedIn (assisted site). The persistent profile keeps the user's login; the login flow itself
    # always needs a visible window. Runs outside the active hours are skipped by the scheduler.
    linkedin_headless: bool = False
    linkedin_active_hours: str = "08:00-23:00"

    # Referral outreach (DRAFT-ONLY by default). Generating/queuing drafts never contacts anyone.
    # SENDING a connection request or message is gated behind this switch (off), the admin toggle, a
    # per-user daily cap, the kill switch, the active hours and per-draft approval. LinkedIn bans accounts
    # for automated outreach, so this must stay off until the user explicitly turns it on.
    referral_send: bool = False
    referral_daily_cap: int = 5

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def browser_profiles_dir(self) -> Path:
        """Persistent Playwright profiles, one directory per user per site."""
        return self.data_dir / "browser-profiles"


@lru_cache
def get_settings() -> Settings:
    return Settings()
