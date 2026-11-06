"""Process settings. Secrets come from .env / environment; paths derive from the repo root."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PACKAGE_ROOT / "config"
MIGRATIONS_DIR = REPO_ROOT / "migrations"

USER_AGENT = "argusvue/0.1 (+https://github.com/nachobeesknees/cybertruck-weather; research pilot)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://argusvue:argusvue@localhost:5432/argusvue"
    test_database_url: str | None = None
    anthropic_api_key: str | None = None
    brave_api_key: str | None = None
    parallel_api_key: str | None = None
    exa_api_key: str | None = None
    daily_spend_cap_usd: float = 20.0
    timezone: str = "America/Phoenix"
    # "api": call the Claude API with ANTHROPIC_API_KEY. "session": write each model request to
    # out/llm/inbox/<id>.json and read the answer from out/llm/outbox/<id>.json (answered by the Claude Code
    # session and its subagents). Validation, logging and citations are identical in both modes.
    llm_provider: str = "api"
    # Structured sources fetch explicit publisher-linked URLs at the publisher's cadence; the web
    # collectors enforce robots.txt on discovered URLs. Set true to also disable the structured sources
    # whose host robots.txt disallows crawlers (flagged robots_disallowed in sources.yaml). See DECISIONS.
    strict_robots: bool = False

    data_dir: Path = Field(default=REPO_ROOT / "data")
    out_dir: Path = Field(default=REPO_ROOT / "out")

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"


@lru_cache
def get_settings() -> Settings:
    return Settings()
