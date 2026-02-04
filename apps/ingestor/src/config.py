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

    # Database (will be converted to asyncpg in __init__)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/mandari"

    def model_post_init(self, __context: object) -> None:
        """Convert database URL to use asyncpg driver for async operations."""
        # Ensure we use asyncpg for async SQLAlchemy
        if self.database_url.startswith("postgresql://"):
            object.__setattr__(
                self,
                "database_url",
                self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            )
        elif self.database_url.startswith("postgres://"):
            object.__setattr__(
                self,
                "database_url",
                self.database_url.replace("postgres://", "postgresql+asyncpg://", 1)
            )

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

    # Event Emission Settings
    events_enabled: bool = True  # Enable Redis event emission
    events_batch_size: int = 50  # Batch size for entity events

    # Metrics Settings
    metrics_enabled: bool = True  # Enable Prometheus metrics
    metrics_port: int = 9090  # Port for metrics HTTP server

    # Circuit Breaker Settings
    circuit_breaker_enabled: bool = True  # Enable circuit breakers
    circuit_breaker_failure_threshold: int = 5  # Failures before opening
    circuit_breaker_recovery_timeout: float = 60.0  # Seconds to wait
    circuit_breaker_success_threshold: int = 2  # Successes to close


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
