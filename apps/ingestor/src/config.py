"""
Ingestor Configuration

Settings for the OParl synchronization service.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Ingestor settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra env vars from Django's .env
    )

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/mandari"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Search
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_key: str = "masterKey"

    # OParl Sync Settings
    oparl_request_timeout: int = 300
    oparl_max_retries: int = 5
    oparl_retry_backoff: float = 2.0
    oparl_wait_time: float = 0.05  # Seconds between requests (reduced from 0.2)
    oparl_etag_cache_enabled: bool = True
    oparl_modified_since_enabled: bool = True
    oparl_max_concurrent: int = 20  # Concurrent HTTP requests

    # Parallel Processing
    max_workers: int = 8  # Increased from 4

    # File Storage
    file_storage_path: str = "./data/files"
    download_files: bool = True

    # Scheduler Settings
    sync_interval_minutes: int = 15  # Incremental sync every 15 minutes
    full_sync_interval_hours: int = 24  # Full sync once a day
    sync_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
