"""
Ingestor Configuration

Settings for the OParl synchronization service.
"""

from functools import lru_cache
from importlib import metadata

from mandari_oparl.crawler import user_agent as crawler_user_agent
from mandari_oparl.pacing import DEFAULT_INTERVAL
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _ingestor_version() -> str:
    """Paketversion für den User-Agent; ohne installiertes Paket (z. B. Quellbaum) ein fester Wert."""
    try:
        return metadata.version("mandari-ingestor")
    except metadata.PackageNotFoundError:
        return "0.1.0"


# Transparenter User-Agent (Produkt-Token/Version, Infoseite, Kontaktadresse), damit
# Betreiber uns identifizieren, auf der Infoseite unsere Regeln finden und uns gezielt
# drosseln, ausschließen oder ansprechen können (mandari_oparl.crawler). Dasselbe
# Produkt-Token wertet die robots.txt-Prüfung aus.
# Mindestens ein RIS filtert den Begriff „crawler“ im User-Agent und antwortet mit 403,
# obwohl derselbe Abruf mit neutralem Client durchgeht (Issue #123). Für solche Quellen
# gilt der User-Agent je Quelle (OParlSource.user_agent), z. B. ohne Infoseiten-Pfad;
# die Sperre meldet der Monitor als „User-Agent gesperrt“.
DEFAULT_USER_AGENT = crawler_user_agent(_ingestor_version())


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
                self, "database_url", self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            )
        elif self.database_url.startswith("postgres://"):
            object.__setattr__(
                self, "database_url", self.database_url.replace("postgres://", "postgresql+asyncpg://", 1)
            )

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Search
    elasticsearch_url: str = "http://localhost:9200"

    # OParl Sync Settings
    oparl_request_timeout: int = 60  # Sekunden pro HTTP-Request (zuvor 300)
    oparl_max_retries: int = 3  # Wiederholungsversuche bei Fehlern (zuvor 5)
    oparl_retry_backoff: float = 2.0
    # Wartezeit je Abrufplatz; gilt nur noch mit abgeschalteter Drossel (INGESTOR_REQUEST_INTERVAL=0)
    oparl_wait_time: float = 0.05
    # Drossel je Host über alle Quellen und Prozesse (src/client/host_pacing.py): Mindestabstand in Sekunden
    # zwischen zwei Anfragen an denselben Host. Standard eine Anfrage je Sekunde; je Quelle abweichend über
    # sync_config["request_interval"]. 0 schaltet die Drossel ab (nur für Tests und Notfälle).
    request_interval: float = Field(
        default=DEFAULT_INTERVAL,
        validation_alias=AliasChoices("INGESTOR_REQUEST_INTERVAL", "request_interval"),
    )
    # Höchstens so viele laufende Anfragen je Host in einem Prozess (Reservierung des Takts und Anfrage
    # zusammen). Hält den reservierten Takt kurz: Ohne Grenze reservierte jeder Abrufplatz einen eigenen
    # Zeitpunkt, der Horizont je Host lag bei 20 Plätzen 20 s voraus, und die Vorschau fand keinen freien
    # Zeitpunkt mehr. 0 = keine Grenze.
    host_max_concurrent: int = Field(
        default=2,
        validation_alias=AliasChoices("INGESTOR_HOST_MAX_CONCURRENT", "host_max_concurrent"),
    )
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

    # Dokumentablage (Issue #788): Dateien, die der Ingestor für den Text ohnehin lädt, legt er gleich in
    # der Ablage nach SHA-256 der Anwendung ab (gleiches Volume, OPARL_FILES_ROOT). Leer = nicht ablegen.
    # Nur gelistete Kommunen, nur mit FILE_STORE_LAYOUT=sha256, nie unter FILE_CACHE_MIN_FREE_GB freiem Platz.
    oparl_files_root: str = ""
    file_store_layout: str = "sha256"
    file_cache_min_free_gb: int = 15

    # Mistral OCR (optional): wenn ein API-Key gesetzt ist, laeuft OCR fuer
    # Scan-PDFs ueber die Mistral-API statt lokal per Tesseract (deutlich
    # schneller bei grossen Backlogs). Tesseract bleibt Fallback.
    mistral_api_key: str = ""
    mistral_ocr_model: str = "pixtral-12b-2409"

    # Elasticsearch Indexing
    elasticsearch_indexing_enabled: bool = True
    elasticsearch_batch_size: int = 500

    # Ereignistechnik (docs/adr/20260929-ereignistechnik-postgres.md): Der Ingestor meldet Änderungen am
    # RIS-Bestand als ris.*-Ereignisse im Journal (events_event), in derselben Transaktion wie die
    # Datenänderung. Standard aus; der Betrieb schaltet das Schreiben gezielt ein, sobald die
    # Django-Migration der Ereignistechnik eingespielt ist und der Sequenzierer läuft
    # (Env INGESTOR_EVENTS_ENABLED). Eingeschaltet kostet jeder Upsert eine zusätzliche Abfrage
    # (bisheriger Stand, mit Zeilensperre) und bei echter Änderung ein INSERT ins Journal.
    # Der Schalter gilt für alle Quellen; eine einzelne nimmt sync_config["events_enabled"] = false
    # aus (src/storage/database.py, SYNC_CONFIG_EVENTS_KEY).
    events_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("INGESTOR_EVENTS_ENABLED", "events_enabled"),
    )

    # User-Agent für OParl-Client und Scraper (siehe DEFAULT_USER_AGENT).
    # Env INGESTOR_USER_AGENT; SCRAPER_USER_AGENT bleibt als älterer Name gültig.
    user_agent: str = Field(
        default=DEFAULT_USER_AGENT,
        validation_alias=AliasChoices("INGESTOR_USER_AGENT", "SCRAPER_USER_AGENT", "user_agent"),
    )

    # Sperr- und Störungserkennung je Host (Issue #123): Auf HTTP 403 folgt genau
    # eine Vergleichsanfrage mit neutralem Client-Header — nur zur Diagnose, der
    # Regelbetrieb läuft weiter mit unserem User-Agent. Ab N aufeinanderfolgenden
    # 5xx-Antworten je Host gilt die Quelle als gestört (Fehlerklasse
    # server_error_series) und wird geschont.
    oparl_ua_probe_enabled: bool = True
    oparl_server_error_series_threshold: int = 5

    # Scraper (Nicht-OParl-Quellen, siehe src/scrapers/ und docs/SCRAPER_SOURCES.md).
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
