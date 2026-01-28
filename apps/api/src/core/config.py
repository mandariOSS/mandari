"""
Application Configuration

Uses pydantic-settings for environment variable management.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def find_env_file() -> Path | None:
    """Find .env file by searching up the directory tree."""
    current = Path(__file__).resolve().parent
    for _ in range(10):  # Search up to 10 levels
        env_file = current / ".env"
        if env_file.exists():
            return env_file
        if current.parent == current:  # Reached root
            break
        current = current.parent
    return None


def find_project_root() -> Path | None:
    """Find project root by looking for pyproject.toml or .env."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "pyproject.toml").exists() or (current / ".env").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


# Find .env file relative to project root
_env_file = find_env_file()
_project_root = find_project_root()

# Debug: Print env file location during startup
if _env_file:
    print(f"[Config] Loading .env from: {_env_file}")
else:
    print("[Config] WARNING: No .env file found!")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_env_file) if _env_file else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra env vars not defined in Settings
    )

    # Application
    app_name: str = "Mandari API"
    debug: bool = False
    environment: str = "development"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/mandari"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Search
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_key: str = "masterKey"

    # Security
    secret_key: str = "change-me-in-production-with-a-real-secret-key"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # CORS (JSON array in .env, e.g. ["http://localhost:3000","http://localhost:5173"])
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # AI
    groq_api_key: str | None = None
    openai_api_key: str | None = None

    # OParl
    oparl_request_timeout: int = 300
    oparl_max_retries: int = 5


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
