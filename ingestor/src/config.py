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
    elasticsearch_url: str = "http://localhost:9200"

    # OParl Sync Settings
    oparl_request_timeout: int = 60  # Sekunden pro HTTP-Request (zuvor 300)
    oparl_max_retries: int = 3  # Wiederholungsversuche bei Fehlern (zuvor 5)
    oparl_retry_backoff: float = 2.0
    oparl_wait_time: float = 0.05  # Seconds between requests (reduced from 0.2)
    oparl_etag_cache_enabled: bool = True
    oparl_modified_since_enabled: bool = True
    oparl_max_concurrent: int = 20  # Concurrent HTTP requests

    # Parallel Processing
    max_workers: int = 8  # Increased from 4
    # Max bodies of one source synced concurrently. Bounds peak memory:
    # each in-flight body sync holds pages, caches and extraction buffers.
    # Env-overridable (SYNC_BODY_CONCURRENCY).
    sync_body_concurrency: int = 2
    # Max sources synced concurrently in sync_all. Together with
    # sync_body_concurrency and sync_entity_concurrency this bounds the
    # peak parallelism (sources x bodies x entity types) and therefore
    # the memory budget of a sync cycle (Issue #22).
    # Env-overridable (SYNC_SOURCE_CONCURRENCY).
    sync_source_concurrency: int = 2
    # Max entity-type syncs running concurrently within one body
    # (organizations/persons, meetings/papers, locations/agenda items/
    # files/consultations). Env-overridable (SYNC_ENTITY_CONCURRENCY).
    sync_entity_concurrency: int = 2

    # File Storage
    file_storage_path: str = "./data/files"
    download_files: bool = True

    # Scheduler Settings
    sync_interval_minutes: int = 10  # Incremental sync every 10 minutes
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

    # Text Extraction
    text_extraction_enabled: bool = True
    text_extraction_max_size_mb: int = 50
    text_extraction_concurrency: int = 4
    text_extraction_timeout: float = 120.0
    text_extraction_batch_size: int = 500

    # Mistral OCR (optional): wenn ein API-Key gesetzt ist, laeuft OCR fuer
    # Scan-PDFs ueber die Mistral-API statt lokal per Tesseract (deutlich
    # schneller bei grossen Backlogs). Tesseract bleibt Fallback.
    mistral_api_key: str = ""
    mistral_ocr_model: str = "pixtral-12b-2409"

    # Elasticsearch Indexing
    elasticsearch_indexing_enabled: bool = True
    elasticsearch_batch_size: int = 500

    # Scraper (Nicht-OParl-Quellen, siehe src/scrapers/ und
    # docs/SCRAPER_SOURCES.md). Transparenter User-Agent mit Kontakt-URL,
    # damit Kommunen uns identifizieren und gezielt steuern können.
    scraper_user_agent: str = "mandari-ingestor (+https://mandari.de/crawler)"
    # Log-Warnung + Fehler-Eintrag, wenn die Parse-Quote eines Laufs
    # (erfolgreich geparste Detailseiten / abgerufene Detailseiten)
    # unter diesen Wert fällt (Parser-Bruch-Erkennung).
    scraper_parse_quota_warn: float = 0.8
    # Objekte werden erst nach N aufeinanderfolgenden Full-Crawls ohne
    # Sichtung als geloescht markiert (Tombstone, nie physisch).
    scraper_tombstone_full_crawls: int = 3


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
