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

    # Credential vault: Fernet key (urlsafe base64, 32 bytes). Generate with `jobapply vault-key`.
    vault_key: SecretStr | None = None

    # AI
    llm_backend: str = Field("fake", description="fake | claude")
    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-sonnet-5-5"
    score_threshold: int = 70

    # Job APIs
    adzuna_app_id: str | None = None
    adzuna_app_key: SecretStr | None = None
    adzuna_country: str = "in"
    rapidapi_key: SecretStr | None = None

    # Collectors
    http_timeout: float = 20.0
    user_agent: str = "jobapply/0.1 (+https://github.com/sxi9/JOB-APPLY)"
    collect_interval_minutes: int = 45

    # Notifier
    whatsapp_base_url: str | None = None
    whatsapp_token: SecretStr | None = None

    # Browser. Leave empty to use Playwright's bundled Chromium.
    chromium_executable: str | None = None

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"


@lru_cache
def get_settings() -> Settings:
    return Settings()
