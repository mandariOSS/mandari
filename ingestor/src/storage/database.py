"""
PostgreSQL Storage for OParl Data

High-performance async database operations with proper upsert support.
Uses PostgreSQL ON CONFLICT for efficient insert-or-update operations.
"""

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from mandari_oparl import (
    IdBases,
    ProcessedAgendaItem,
    ProcessedBody,
    ProcessedConsultation,
    ProcessedFile,
    ProcessedLegislativeTerm,
    ProcessedLocation,
    ProcessedMeeting,
    ProcessedMembership,
    ProcessedOrganization,
    ProcessedPaper,
    ProcessedPerson,
)
from mandari_oparl.extensions import AGENDA_ITEM_COLUMNS, MEETING_COLUMNS
from sqlalchemy import and_, bindparam, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.metrics import metrics
from src.redaction import MaskingConsole
from src.storage import events, ris_events
from src.storage.engine import engine_erzeugen
from src.storage.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlFileBlob,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

console = MaskingConsole()
logger = logging.getLogger(__name__)


# Entity-Typ-Name -> SQLAlchemy-Modell (für generische Lookups, u. a.
# Content-Hash-Diffing und Verschwinde-Erkennung der Scraper-Quellen).
_ENTITY_MODEL_MAP: dict[str, type] = {
    "meeting": OParlMeeting,
    "paper": OParlPaper,
    "person": OParlPerson,
    "organization": OParlOrganization,
    "membership": OParlMembership,
    "location": OParlLocation,
    "agendaitem": OParlAgendaItem,
    "consultation": OParlConsultation,
    "file": OParlFile,
    "legislativeterm": OParlLegislativeTerm,
}


# Columns that are populated by enrichment workers AFTER the initial sync
# (text extraction pipeline, OCR workers, Django georeferencing / AI
# summaries, photo scraping, ...). They must NEVER appear in the
# ON CONFLICT update-set of an OParl upsert, otherwise a routine re-sync
# would silently wipe the workers' results.
#
# Some of these columns only exist in Django's schema (not in the
# ingestor's SQLAlchemy models) — they are listed anyway so the guard
# also catches future model additions.
#: Grund für Dateien, deren Bearbeitung wiederholt abbrach (Worker beendet, meist Speichermangel, Issue #817);
#: Django zählt sie über Status ``failed`` mit Abbruchzähler > 0, nicht über diesen Text.
STALE_GIVE_UP_REASON: Final = "Speichergrenze"

ENRICHMENT_FIELDS: frozenset[str] = frozenset(
    {
        # OParlFile: text extraction / OCR pipeline
        "text_content",
        "text_extraction_status",
        "text_extraction_method",
        "text_extraction_error",
        "text_extracted_at",
        "page_count",
        "text_extraction_attempts",
        "text_extraction_started_at",
        "sha256_hash",
        "local_path",
        "local_status",
        "local_cached_at",
        "local_error",
        "local_size",
        "blob_id",
        # OParlPaper: AI enrichment + georeferencing (Django-managed)
        "summary",
        "locations",
        "georef_status",
        # OParlBody: Django-managed presentation + geo fields
        "display_name",
        "logo",
        "slug",
        # OParlBody: festgeschriebenes Verzeichnis im Dokument-Cache (Django-managed, Issue #373)
        "file_cache_dir",
        "latitude",
        "longitude",
        "bbox_north",
        "bbox_south",
        "bbox_east",
        "bbox_west",
        "osm_relation_id",
        "ags",
        # OParlBody: automatische Geo-Zuordnung (Django-managed, resolve_body_geodata, Issue #351)
        "rgs",
        "is_non_territorial",
        "territory_parent_id",
        "territory_set_manually",
        # OParlBody: person photo scraping configuration (Django-managed)
        "person_photo_url_template",
        "person_photo_id_pattern",
        # OParlPerson: lokal gecachte Fotos (Django-managed, fetch_person_photos)
        "photo",
        "photo_status",
        "photo_fetched_at",
        "photo_error",
    }
)


#: Schlüsselraum der Sperre für neu erkannte Objekte (``pg_advisory_xact_lock`` mit zwei Schlüsseln).
#: Die Form mit zwei Schlüsseln überschneidet sich nicht mit der Form mit einem Schlüssel, die der
#: Scheduler für seine Einzelinstanz-Sperre nimmt (src/scheduler/singleton.py).
_NEW_OBJECT_LOCK_CLASS: Final = 513

#: Schlüssel in ``OParlSource.sync_config``: ``false`` nimmt die Quelle von den Ereignissen aus.
SYNC_CONFIG_EVENTS_KEY: Final = "events_enabled"


@dataclass(frozen=True)
class _Origin:
    """Herkunft der Ereignisse einer Kommune."""

    #: Quelle der Kommune (Mandant der Hülle, ``source:<uuid>``)
    source_id: UUID | None
    #: Name der Kommune, Label ``source`` der Metrik (wie bei ``mandari_ingestor_entities_synced_total``)
    label: str
    #: ``False``: Die Quelle ist über ``sync_config`` von den Ereignissen ausgenommen.
    events: bool


def _normalized_urls(value: Any) -> list[str]:
    """Verweise als Liste von URLs: eine URL, eine Liste von URLs oder eingebettete Objekte mit ``id``."""
    items = value if isinstance(value, list) else [value]
    urls: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("id")
        if isinstance(item, str) and item and item not in urls:
            urls.append(item)
    return urls


def _extension_columns(values: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    """
    Spalten der Beschlussfassung bzw. der Genehmigung (``mandari_oparl.extensions``, Issue #525): immer alle,
    fehlende als ``None``. Liefert die Quelle eine Erweiterung nicht mehr, wird die Spalte geleert.
    """
    return {name: values.get(name) for name in columns}


def _assert_no_enrichment_overwrite(update_set: dict) -> None:
    """
    Guard rail for upsert update-sets.

    Raises immediately (in every build) if an ON CONFLICT update-set would
    overwrite worker-populated enrichment columns. Call this before every
    ``on_conflict_do_update(set_=...)`` so a future edit cannot silently
    add a protected column.
    """
    overlap = ENRICHMENT_FIELDS.intersection(update_set)
    if overlap:
        raise AssertionError(
            "Upsert update-set would overwrite worker-populated enrichment "
            f"columns: {sorted(overlap)}. See ENRICHMENT_FIELDS in "
            "src/storage/database.py — these columns must never be part of "
            "an ON CONFLICT update-set."
        )


class DatabaseStorage:
    """
    High-performance async PostgreSQL storage for OParl data.

    Features:
    - Async SQLAlchemy with asyncpg driver
    - Efficient upsert using PostgreSQL ON CONFLICT
    - Batch operations for performance
    - Automatic relationship handling
    """

    def __init__(self, database_url: str | None = None, *, events_enabled: bool | None = None) -> None:
        """
        Initialize database storage.

        Args:
            database_url: Database connection URL. Defaults to settings.
            events_enabled: RIS-Ereignisse ins Journal schreiben. Defaults to settings
                (``INGESTOR_EVENTS_ENABLED``, Standard aus).
        """
        self.database_url = database_url or settings.database_url
        # Ereignistechnik: Änderungen am RIS-Bestand als ris.*-Ereignisse melden, in derselben
        # Transaktion wie die Datenänderung (src/storage/events.py, src/storage/ris_events.py).
        self.events_enabled = settings.events_enabled if events_enabled is None else events_enabled
        # Über engine_erzeugen(), damit die SQLAlchemy-Instrumentierung auch hier Spans je Anweisung liefert
        self._engine = engine_erzeugen(
            self.database_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Festgeschriebene Basen der Kennungen umgezogener Quellen (Issue #733): Verweise in Ereignissen
        # tragen dieselben Kennungen wie die Objekte. Der Orchestrator trägt sie je Quelle ein und teilt sie
        # mit dem Prozessor.
        self.id_bases = IdBases()

        # Letztes Auflösen abgebrochener Textextraktionen (Uhr des Extraktors, Issue #817): gilt für alle
        # Extraktoren an diesem Speicher, auch wenn Sync und Scraper je Kommune einen eigenen anlegen
        self.stale_extractions_released_at: float | None = None

        # Cache for body UUIDs (external_id -> UUID)
        self._body_uuid_cache: dict[str, UUID] = {}
        self._meeting_uuid_cache: dict[str, UUID] = {}
        self._paper_uuid_cache: dict[str, UUID] = {}
        self._person_uuid_cache: dict[str, UUID] = {}
        self._organization_uuid_cache: dict[str, UUID] = {}
        # Herkunft der Ereignisse: Kommune -> Quelle (tenant_ref, Ausnahme) und Sitzung -> Kommune
        self._origin_cache: dict[UUID, _Origin] = {}
        self._meeting_body_cache: dict[UUID, UUID | None] = {}
        # Gibt es die Zwischentabelle oparl_papers_locations? (None: in diesem Zyklus noch nicht geprüft)
        self._paper_locations_table: bool | None = None

    async def initialize(self) -> None:
        """
        Verify database schema exists.

        Django owns the schema via migrations. The ingestor must NOT create tables.
        If tables are missing, raise an error pointing to Django migrate.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("SELECT EXISTS (  SELECT 1 FROM information_schema.tables   WHERE table_name = 'oparl_bodies')")
            )
            exists = result.scalar()
            if not exists:
                raise RuntimeError(
                    "Database schema not found! "
                    "Django owns the schema. Please run: "
                    "cd mandari && python manage.py migrate"
                )
            if self.events_enabled:
                # Eingeschaltet, aber ohne Journal scheiterte jeder Upsert an seinem Ereignis und
                # nähme die Datenänderung mit zurück. Lieber beim Start abbrechen.
                journal = await conn.execute(text("SELECT to_regclass('events_event') IS NOT NULL"))
                if not journal.scalar():
                    raise RuntimeError(
                        "INGESTOR_EVENTS_ENABLED ist gesetzt, aber die Tabelle events_event fehlt. "
                        "Zuerst die Django-Migrationen einspielen (cd mandari && python manage.py migrate) "
                        "oder den Schalter ausschalten."
                    )

    async def close(self) -> None:
        """Close the database connection."""
        await self._engine.dispose()

    async def __aenter__(self) -> "DatabaseStorage":
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def get_session(self) -> AsyncSession:
        """Get a new database session."""
        return self._session_factory()

    async def write_sync_log(
        self,
        *,
        source_id: UUID | None,
        sync_type: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        duration_seconds: float,
        entities_synced: int,
        errors: list[str],
        details: dict,
        triggered_by: str = "daemon",
    ) -> None:
        """Write a sync log entry to insight_sync_synclog (Django's SyncLog table)."""
        import json

        errors_json = json.dumps(errors or [])
        details_json = json.dumps(details or {})

        async with self.get_session() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO insight_sync_synclog"
                    " (sync_type, status, started_at, finished_at, duration_seconds,"
                    "  entities_synced, errors, details, triggered_by, source_id)"
                    " VALUES"
                    " (:sync_type, :status, :started_at, :finished_at, :duration_seconds,"
                    "  :entities_synced, cast(:errors as jsonb), cast(:details as jsonb),"
                    "  :triggered_by, :source_id)"
                ),
                {
                    "sync_type": sync_type,
                    "status": status,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": duration_seconds,
                    "entities_synced": entities_synced,
                    "errors": errors_json,
                    "details": details_json,
                    "triggered_by": triggered_by,
                    "source_id": source_id,
                },
            )

    # ========== Source Operations ==========

    async def upsert_source(
        self,
        url: str,
        name: str,
        raw_json: dict[str, Any] | None = None,
        oparl_version: str | None = None,
    ) -> UUID:
        """
        Insert or update an OParl source.

        ``oparl_version`` ("1.0"/"1.1") stammt aus der Autodiscovery und wird
        nur überschrieben, wenn ein Wert erkannt wurde (Issue #122).

        Returns the source UUID.
        """
        async with self.get_session() as session:
            stmt = pg_insert(OParlSource).values(
                url=url,
                name=name,
                raw_json=raw_json or {},
                oparl_version=oparl_version,
                is_active=True,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "raw_json": stmt.excluded.raw_json,
                "oparl_version": func.coalesce(stmt.excluded.oparl_version, OParlSource.oparl_version),
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["url"],
                set_=update_set,
            ).returning(OParlSource.id)

            result = await session.execute(stmt)
            source_id = result.scalar_one()
            await session.commit()

            return source_id

    async def get_download_headers_for_body(self, body_id: UUID) -> dict[str, str]:
        """
        Download-Header der Quelle eines Bodies (``sync_config["download_headers"]``, Issue #116).
        Nur String-Werte; leer, wenn nichts konfiguriert ist.
        """
        async with self.get_session() as session:
            result = await session.execute(
                select(OParlSource.sync_config)
                .join(OParlBody, OParlBody.source_id == OParlSource.id)
                .where(OParlBody.id == body_id)
            )
            sync_config = result.scalar_one_or_none() or {}
        headers = sync_config.get("download_headers") if isinstance(sync_config, dict) else None
        if not isinstance(headers, dict):
            return {}
        return {str(k): str(v) for k, v in headers.items() if isinstance(v, str | int | float) and str(k).strip()}

    async def file_downloads_enabled_for_body(self, body_id: UUID) -> bool:
        """
        Ob Dateien der Quelle eines Bodies automatisch abgerufen werden dürfen
        (``sync_config["file_downloads"]``, ``src/client/source_options.py``). Ohne Quelle: ja.
        """
        async with self.get_session() as session:
            result = await session.execute(
                select(OParlSource.sync_config)
                .join(OParlBody, OParlBody.source_id == OParlSource.id)
                .where(OParlBody.id == body_id)
            )
            sync_config = result.scalar_one_or_none()
        return SourceFetchOptions.from_sync_config(sync_config).file_downloads

    async def get_fetch_options_for_body(self, body_id: UUID) -> SourceFetchOptions:
        """Abrufoptionen der Quelle eines Bodies (Abstand, robots-Ausnahme mit Vermerk; ``sync_config``)."""
        async with self.get_session() as session:
            result = await session.execute(
                select(OParlSource.sync_config)
                .join(OParlBody, OParlBody.source_id == OParlSource.id)
                .where(OParlBody.id == body_id)
            )
            sync_config = result.scalar_one_or_none()
        return SourceFetchOptions.from_sync_config(sync_config)

    async def get_source_by_url(self, url: str) -> OParlSource | None:
        """Get a source by URL."""
        async with self.get_session() as session:
            stmt = select(OParlSource).where(OParlSource.url == url)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_all_sources(self, active_only: bool = True) -> list[OParlSource]:
        """
        Get all registered sources.

        Args:
            active_only: If True (default), only return sources with is_active=True.
                        Set to False to get ALL sources including inactive ones.
        """
        async with self.get_session() as session:
            stmt = select(OParlSource)
            if active_only:
                stmt = stmt.where(OParlSource.is_active.is_(True))
            stmt = stmt.order_by(OParlSource.name)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_source_sync_time(
        self,
        source_id: UUID,
        full_sync: bool = False,
    ) -> None:
        """Update the last sync timestamp for a source."""
        async with self.get_session() as session:
            source = await session.get(OParlSource, source_id)
            if source:
                now = datetime.now(UTC)
                source.last_sync = now
                if full_sync:
                    source.last_full_sync = now
                # Erfolg setzt den Fehlerstatus des Betriebsmonitors zurück
                source.last_error = None
                source.last_error_at = None
                source.last_error_kind = None
                source.consecutive_failures = 0
                await session.commit()

    async def update_source_sync_config(self, source_id: UUID, sync_config: dict[str, Any]) -> None:
        """sync_config einer Quelle ersetzen (z. B. Zensus-Ergebnis unter ``probe``, Issue #114)."""
        async with self.get_session() as session:
            source = await session.get(OParlSource, source_id)
            if source:
                source.sync_config = sync_config
                await session.commit()

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        """
        Fehlgeschlagenen Sync-Versuch an der Quelle festhalten (Betriebsmonitor
        im Django-Admin). Die Quelle wird über ihre URL gefunden, weil bei einem
        Verbindungsfehler noch keine Source-ID vorliegt. ``error_kind`` ordnet
        den Fehler einer Sperre oder Störung zu (Issue #123) und steuert die
        Quellen-Schonung im nächsten Zyklus.
        """
        async with self.get_session() as session:
            result = await session.execute(select(OParlSource).where(OParlSource.url == url))
            source = result.scalar_one_or_none()
            if source is None:
                return
            source.last_error = (error or "Unbekannter Fehler")[:2000]
            source.last_error_at = datetime.now(UTC)
            source.last_error_kind = error_kind
            source.consecutive_failures = (source.consecutive_failures or 0) + 1
            await session.commit()

    # Schlüssel in OParlSource.sync_config für den persistierten
    # Capability-Cache (Hosts, die modified_since ablehnen, Issue #22).
    SYNC_CONFIG_MODIFIED_SINCE_KEY = "modified_since_unsupported_hosts"

    async def get_modified_since_unsupported_hosts(self) -> set[str]:
        """Union der persistierten Hosts ohne modified_since-Support (alle Quellen)."""
        async with self.get_session() as session:
            result = await session.execute(select(OParlSource.sync_config))
            hosts: set[str] = set()
            for (sync_config,) in result.all():
                if isinstance(sync_config, dict):
                    stored = sync_config.get(self.SYNC_CONFIG_MODIFIED_SINCE_KEY) or []
                    hosts.update(h for h in stored if isinstance(h, str) and h)
            return hosts

    async def add_modified_since_unsupported_hosts(
        self,
        source_url: str,
        hosts: set[str],
    ) -> None:
        """
        Persistiert Hosts ohne modified_since-Support in der sync_config
        der Quelle, damit der Fallback-Befund Daemon-Neustarts überlebt.
        """
        if not hosts:
            return
        async with self.get_session() as session:
            result = await session.execute(select(OParlSource).where(OParlSource.url == source_url))
            source = result.scalar_one_or_none()
            if source is None:
                return
            sync_config = dict(source.sync_config or {})
            stored = set(sync_config.get(self.SYNC_CONFIG_MODIFIED_SINCE_KEY) or [])
            merged = stored | {h for h in hosts if h}
            if merged == stored:
                return
            sync_config[self.SYNC_CONFIG_MODIFIED_SINCE_KEY] = sorted(merged)
            source.sync_config = sync_config
            await session.commit()

    # Ergebnis der letzten Prüfung von modified_since (nach jedem Vollabgleich)
    SYNC_CONFIG_MODIFIED_SINCE_CHECK_KEY = "modified_since_check"

    async def apply_modified_since_check(self, source_url: str, host: str, verdict: str, supported: bool) -> None:
        """
        Ergebnis der modified_since-Prüfung festhalten: an der Quelle vermerken und den Host im
        persistierten Capability-Cache aller Quellen eintragen (filtert nicht) oder austragen (filtert).
        """
        if not host:
            return
        async with self.get_session() as session:
            result = await session.execute(select(OParlSource).with_for_update())
            for source in result.scalars().all():
                sync_config = dict(source.sync_config or {})
                stored = {h for h in sync_config.get(self.SYNC_CONFIG_MODIFIED_SINCE_KEY) or [] if isinstance(h, str)}
                changed = False
                if supported and host in stored:
                    stored.discard(host)
                    changed = True
                if source.url == source_url:
                    if not supported and host not in stored:
                        stored.add(host)
                    sync_config[self.SYNC_CONFIG_MODIFIED_SINCE_CHECK_KEY] = {
                        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "host": host,
                        "result": verdict,
                    }
                    changed = True
                if not changed:
                    continue
                if stored:
                    sync_config[self.SYNC_CONFIG_MODIFIED_SINCE_KEY] = sorted(stored)
                else:
                    sync_config.pop(self.SYNC_CONFIG_MODIFIED_SINCE_KEY, None)
                source.sync_config = sync_config
            await session.commit()

    # Schlüssel in OParlSource.sync_config für den persistierten
    # Scraper-Zustand (Listen-Snapshots, Missing-Counter, letzte Läufe).
    SYNC_CONFIG_SCRAPER_STATE_KEY = "scraper_state"

    async def update_scraper_state(self, source_url: str, state: dict[str, Any]) -> None:
        """
        Persistiert den Scraper-Zustand einer Quelle additiv in sync_config
        (Schlüssel "scraper_state"); andere Schlüssel bleiben unberührt.
        """
        async with self.get_session() as session:
            result = await session.execute(select(OParlSource).where(OParlSource.url == source_url))
            source = result.scalar_one_or_none()
            if source is None:
                return
            sync_config = dict(source.sync_config or {})
            sync_config[self.SYNC_CONFIG_SCRAPER_STATE_KEY] = state
            source.sync_config = sync_config
            await session.commit()

    async def get_entity_content_hashes(
        self,
        entity_type: str,
        external_ids: list[str],
    ) -> dict[str, str | None]:
        """
        Liest die gespeicherten Content-Hashes ("mandari:contentHash" im
        raw_json) für Feld-Diffing von Scraper-Quellen. Unbekannte IDs und
        Objekte ohne Hash liefern None (=> Upsert).
        """
        model = _ENTITY_MODEL_MAP.get(entity_type)
        if not model or not external_ids:
            return {}

        async with self.get_session() as session:
            stmt = select(
                model.external_id,
                model.raw_json["mandari:contentHash"].astext,
            ).where(model.external_id.in_(external_ids))
            result = await session.execute(stmt)
            hashes: dict[str, str | None] = dict.fromkeys(external_ids)
            for external_id, stored_hash in result.all():
                hashes[external_id] = stored_hash
            return hashes

    async def get_active_external_ids_for_body(
        self,
        entity_type: str,
        body_id: UUID,
    ) -> set[str]:
        """
        Alle nicht-tombstoneden external_ids eines Entity-Typs eines Bodies
        (für die Verschwinde-Erkennung von Scraper-Quellen). Nur für Typen
        mit body_id-Spalte.
        """
        model = _ENTITY_MODEL_MAP.get(entity_type)
        if not model or not hasattr(model, "body_id"):
            return set()
        async with self.get_session() as session:
            stmt = select(model.external_id).where(
                model.body_id == body_id,
                model.deleted == False,  # noqa: E712
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}

    async def get_active_meeting_ids_in_window(
        self,
        body_id: UUID,
        window_start: "date_type",
        window_end: "date_type",
    ) -> set[str]:
        """
        Nicht-tombstonede Sitzungen eines Bodies mit Start im Crawl-Fenster
        (Kandidatenmenge der Verschwinde-Erkennung — nur Objekte, die ein
        Full-Crawl des Fensters sicher gesehen haben muss).
        """
        async with self.get_session() as session:
            stmt = select(OParlMeeting.external_id).where(
                OParlMeeting.body_id == body_id,
                OParlMeeting.deleted == False,  # noqa: E712
                OParlMeeting.start.isnot(None),
                func.date(OParlMeeting.start) >= window_start,
                func.date(OParlMeeting.start) <= window_end,
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}

    def clear_uuid_caches(self) -> None:
        """
        Leert die FK-UUID-Caches (external_id -> UUID).

        Die Caches beschleunigen FK-Lookups innerhalb eines Sync-Zyklus,
        wachsen im Daemon aber sonst monoton über Zyklen hinweg (Issue #22).
        Der Orchestrator ruft dies am Ende jedes Zyklus auf; Cache-Misses
        danach fallen auf die DB-Lookups zurück.
        """
        self._body_uuid_cache.clear()
        self._meeting_uuid_cache.clear()
        self._paper_uuid_cache.clear()
        self._person_uuid_cache.clear()
        self._organization_uuid_cache.clear()
        self._origin_cache.clear()
        self._meeting_body_cache.clear()
        self._paper_locations_table = None

    # ========== Ereignisse (Journal der Ereignistechnik) ==========

    async def _events_for(self, session: AsyncSession, body_id: UUID | None) -> bool:
        """
        Ob Änderungen an Objekten dieser Kommune Ereignisse schreiben.

        Der Schalter ``INGESTOR_EVENTS_ENABLED`` gilt für alle Quellen. Eine einzelne Quelle nimmt
        ``sync_config["events_enabled"] = false`` aus; ihre Upserts laufen dann wie ausgeschaltet,
        ohne Abfrage des bisherigen Stands. Ohne Quelle gibt es keinen Mandanten für die Hülle.
        """
        if not self.events_enabled or body_id is None:
            return False
        origin = await self._origin(session, body_id)
        return origin.source_id is not None and origin.events

    async def _source_emits(self, session: AsyncSession, source_id: UUID) -> bool:
        """
        Ob Änderungen aus dieser Quelle Ereignisse schreiben (Schalter und Ausnahme in ``sync_config``).

        Für die Kommune selbst: Vor ihrem ersten Upsert gibt es keine Zeile, über die ``_events_for``
        die Quelle fände.
        """
        if not self.events_enabled:
            return False
        found = await session.execute(select(OParlSource.sync_config).where(OParlSource.id == source_id))
        sync_config = found.scalar_one_or_none()
        return not (isinstance(sync_config, dict) and sync_config.get(SYNC_CONFIG_EVENTS_KEY) is False)

    async def _origin(self, session: AsyncSession, body_id: UUID) -> _Origin:
        """Herkunft der Ereignisse einer Kommune (zwischengespeichert bis zum Ende des Zyklus)."""
        origin = self._origin_cache.get(body_id)
        if origin is None:
            result = await session.execute(
                select(OParlBody.source_id, OParlBody.name, OParlSource.sync_config)
                .outerjoin(OParlSource, OParlSource.id == OParlBody.source_id)
                .where(OParlBody.id == body_id)
            )
            row = result.first()
            sync_config = row[2] if row is not None and isinstance(row[2], dict) else {}
            origin = _Origin(
                source_id=row[0] if row is not None else None,
                label=(row[1] if row is not None else None) or "unknown",
                events=sync_config.get(SYNC_CONFIG_EVENTS_KEY) is not False,
            )
            self._origin_cache[body_id] = origin
            # Einmal je Zyklus und Kommune, nicht je Objekt
            if origin.source_id is None:
                logger.warning("Kommune %s ohne Quelle: Ihre Änderungen schreiben keine Ereignisse", body_id)
            elif not origin.events:
                logger.info("Quelle %s ist von den Ereignissen ausgenommen (sync_config)", origin.source_id)
        return origin

    async def _prior(self, session: AsyncSession, model: type, external_id: str) -> ris_events.Prior | None:
        """
        Stand einer Zeile vor dem Upsert, für den Vergleich, ob sich etwas geändert hat.

        Nur mit eingeschalteten Ereignissen aufrufen. Die Zeile bleibt bis zum Commit gesperrt: Der
        Vergleich gilt damit für genau den Stand, den der folgende Upsert überschreibt, auch wenn ein
        zweiter Abgleich dasselbe Objekt gleichzeitig schreibt. ``None``: Das Objekt ist neu.

        - Gesperrt wird mit ``FOR NO KEY UPDATE``, so stark wie der folgende Upsert selbst (er ändert
          keine Schlüsselspalte). ``FOR UPDATE`` wartete zusätzlich auf jede offene Transaktion, die
          gerade eine Zeile mit Fremdschlüssel auf dieses Objekt einfügt, und hielte solche
          Einfügungen auf.
        - Ein neues Objekt hat noch keine Zeile, die sich sperren ließe. Für diesen Fall nimmt der
          Abgleich eine Sperre auf die Kennung, die bis zum Ende der Transaktion gilt, und liest
          danach noch einmal. Schreiben zwei Abgleiche dasselbe neue Objekt gleichzeitig (etwa eine
          Datei, die in einer Sitzung und in einer Vorlage eingebettet ist), sieht der zweite die
          Zeile des ersten und meldet das Objekt nicht ein zweites Mal als neu.
        """
        columns = [model.raw_json, model.deleted]
        if model is OParlAgendaItem:
            columns += [OParlAgendaItem.meeting_id, OParlAgendaItem.public]
        elif model is OParlFile:
            columns += [OParlFile.meeting_id, OParlFile.paper_id]
        stmt = select(*columns).where(model.external_id == external_id).with_for_update(key_share=True)
        row = (await session.execute(stmt)).first()
        if row is None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:space AS integer), hashtext(CAST(:external_id AS text)))"),
                {"space": _NEW_OBJECT_LOCK_CLASS, "external_id": external_id},
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return None
        if model is OParlAgendaItem:
            return ris_events.Prior(
                raw_json=row[0] or {}, deleted=bool(row[1]), meeting_id=row[2], public=row[3] is not False
            )
        if model is OParlFile:
            return ris_events.Prior(raw_json=row[0] or {}, deleted=bool(row[1]), meeting_id=row[2], paper_id=row[3])
        return ris_events.Prior(raw_json=row[0] or {}, deleted=bool(row[1]))

    async def _emit(
        self,
        session: AsyncSession,
        body_id: UUID | None,
        drafts: list[ris_events.Draft],
        modified: datetime | None = None,
    ) -> None:
        """
        Schreibt Ereignisse in die laufende Transaktion der Sitzung, also vor deren Commit.

        Nur aufrufen, wenn ``_events_for`` die Kommune freigibt. Mandant ist die Quelle der Kommune
        (``source:<uuid>``). ``modified`` ist der Änderungszeitpunkt laut Quelle; liegt er in der
        Zukunft oder fehlt die Zeitzone, gilt der Zeitpunkt des Abgleichs.
        """
        if not drafts or body_id is None:
            return
        origin = await self._origin(session, body_id)
        source_id = origin.source_id
        if source_id is None:
            return
        now = datetime.now(UTC)
        occurred_at = modified if modified is not None and modified.utcoffset() is not None and modified <= now else now
        await events.publish_many(
            session,
            [
                events.NewEvent(
                    type=draft.type,
                    version=draft.version,
                    aggregate_type=draft.aggregate_type,
                    aggregate_id=draft.aggregate_id,
                    tenant_ref=f"source:{source_id}",
                    body_id=body_id,
                    visibility=draft.visibility,
                    operation=draft.operation,
                    occurred_at=occurred_at,
                    payload=draft.payload,
                )
                for draft in drafts
            ],
        )
        for draft in drafts:
            metrics.record_event_published(draft.type, origin.label)

    async def _body_of_meeting(self, session: AsyncSession, meeting_id: UUID | None) -> UUID | None:
        """Kommune einer Sitzung (zwischengespeichert bis zum Ende des Zyklus)."""
        if meeting_id is None:
            return None
        if meeting_id not in self._meeting_body_cache:
            result = await session.execute(select(OParlMeeting.body_id).where(OParlMeeting.id == meeting_id))
            self._meeting_body_cache[meeting_id] = result.scalar_one_or_none()
        return self._meeting_body_cache[meeting_id]

    # ========== Body Operations ==========

    async def upsert_body(
        self,
        body: ProcessedBody,
        source_id: UUID,
    ) -> UUID:
        """
        Insert or update a body.

        Returns the body UUID. Zeile und Ereignis (``ris.body.changed``) entstehen in einer Transaktion.
        Die Herkunft der Ereignisse ergibt sich erst nach dem Upsert (eine neue Kommune hat vorher keine
        Zeile); ob die Quelle ausgenommen ist, steht vorher an der Quelle selbst.
        """
        async with self.get_session() as session:
            emit = await self._source_emits(session, source_id)
            prior = await self._prior(session, OParlBody, body.external_id) if emit else None
            stmt = pg_insert(OParlBody).values(
                id=body.id,
                external_id=body.external_id,
                source_id=source_id,
                name=body.name,
                short_name=body.short_name,
                website=body.website,
                license=body.license,
                classification=body.classification,
                organization_list_url=body.organization_list_url,
                person_list_url=body.person_list_url,
                meeting_list_url=body.meeting_list_url,
                paper_list_url=body.paper_list_url,
                membership_list_url=body.membership_list_url,
                agenda_item_list_url=body.agenda_item_list_url,
                file_list_url=body.file_list_url,
                oparl_created=body.oparl_created,
                oparl_modified=body.oparl_modified,
                raw_json=body.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "short_name": stmt.excluded.short_name,
                "website": stmt.excluded.website,
                "license": stmt.excluded.license,
                # Manuell gepflegte Klassifikation (z. B. "Kreisfreie Stadt")
                # nicht mit NULL ueberschreiben, wenn die Quelle keine liefert
                "classification": func.coalesce(stmt.excluded.classification, OParlBody.classification),
                "organization_list_url": stmt.excluded.organization_list_url,
                "person_list_url": stmt.excluded.person_list_url,
                "meeting_list_url": stmt.excluded.meeting_list_url,
                "paper_list_url": stmt.excluded.paper_list_url,
                "membership_list_url": stmt.excluded.membership_list_url,
                "agenda_item_list_url": stmt.excluded.agenda_item_list_url,
                "file_list_url": stmt.excluded.file_list_url,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlBody.id)

            result = await session.execute(stmt)
            body_id = result.scalar_one()
            if emit and await self._events_for(session, body_id):
                drafts = ris_events.body_events(body_id, body.raw_json or {}, prior)
                await self._emit(session, body_id, drafts, body.oparl_modified)
            await session.commit()

            # Cache the UUID
            self._body_uuid_cache[body.external_id] = body_id

            # Process nested legislative terms
            for nested in body.nested_entities:
                if isinstance(nested, ProcessedLegislativeTerm):
                    await self.upsert_legislative_term(nested, body_id)

            return body_id

    async def update_body_sync_time(self, body_id: UUID) -> None:
        """Update the last sync timestamp for a body."""
        async with self.get_session() as session:
            body = await session.get(OParlBody, body_id)
            if body:
                body.last_sync = datetime.now(UTC)
                await session.commit()

    # ========== Entity Existence Check ==========

    async def batch_check_entities_exist(
        self,
        entity_type: str,
        external_ids: list[str],
    ) -> dict[str, datetime | None]:
        """
        Batch check which entities exist and get their modified dates.

        More efficient than individual checks for a whole page.

        Args:
            entity_type: Type of entity
            external_ids: List of external IDs to check

        Returns:
            Dict mapping external_id -> oparl_modified (or None if not found)
        """
        model_map = {
            "meeting": OParlMeeting,
            "paper": OParlPaper,
            "person": OParlPerson,
            "organization": OParlOrganization,
            "membership": OParlMembership,
            "location": OParlLocation,
            "agendaitem": OParlAgendaItem,
            "consultation": OParlConsultation,
            "file": OParlFile,
            "legislativeterm": OParlLegislativeTerm,
        }

        model = model_map.get(entity_type)
        if not model:
            return {}

        async with self.get_session() as session:
            stmt = select(model.external_id, model.oparl_modified).where(model.external_id.in_(external_ids))
            result = await session.execute(stmt)
            rows = result.all()

            # Create dict with all IDs defaulting to None
            result_dict: dict[str, datetime | None] = dict.fromkeys(external_ids)
            # Update with found entries
            for external_id, modified in rows:
                result_dict[external_id] = modified

            return result_dict

    async def mark_entity_deleted(
        self,
        entity_type: str,
        external_id: str,
        modified: datetime | None = None,
    ) -> UUID | None:
        """
        Mark an entity as deleted by the source (OParl tombstone).

        Used when OParl servers return items with deleted=true
        (Bonn, Aachen, Köln, ITK Rheinland support this).

        We NEVER physically delete synced objects — they are only flagged
        (deleted=true, deleted_at=now) so the public portals can hide them
        and our own OParl API can serve spec-compliant tombstones. Physical
        deletion happens exclusively via Django's ``purge_deleted`` command
        after an explicit request from the municipality.

        ``oparl_modified`` is advanced to the tombstone's ``modified`` (or
        the detection time) so incremental clients of our OParl API pick up
        the deletion via ``modified_since``.

        Args:
            entity_type: Type of entity
            external_id: The OParl external ID
            modified: ``modified`` timestamp of the source tombstone, if any

        Returns:
            The internal UUID if the entity was newly marked, else None
            (not found or already marked).
        """
        model_map = {
            "meeting": OParlMeeting,
            "paper": OParlPaper,
            "person": OParlPerson,
            "organization": OParlOrganization,
            "membership": OParlMembership,
            "location": OParlLocation,
            "agendaitem": OParlAgendaItem,
            "consultation": OParlConsultation,
            "file": OParlFile,
            "legislativeterm": OParlLegislativeTerm,
        }

        model = model_map.get(entity_type)
        if not model:
            return None

        now = datetime.now(UTC)
        # Spalte, über die sich die Kommune des Objekts ergibt (für die Herkunft des Ereignisses)
        if entity_type == "agendaitem":
            parent = OParlAgendaItem.meeting_id
        elif entity_type == "membership":
            parent = OParlMembership.organization_id
        else:
            parent = model.body_id
        returning = [model.id, parent]
        if entity_type == "agendaitem":
            # Ein nichtöffentlicher Punkt meldet seine Löschung nicht öffentlich.
            returning.append(OParlAgendaItem.public)
        async with self.get_session() as session:
            stmt = (
                update(model)
                .where(model.external_id == external_id, model.deleted == False)  # noqa: E712
                .values(
                    deleted=True,
                    deleted_at=now,
                    deletion_reason=ris_events.REASON_DELETED_AT_SOURCE,
                    oparl_modified=modified or now,
                    updated_at=func.now(),
                )
                .returning(*returning)
            )
            result = await session.execute(stmt)
            row = result.first()
            entity_id = row[0] if row is not None else None
            if row is not None and self.events_enabled:
                # Rücknahme melden, in derselben Transaktion wie die Markierung
                public, meeting_id = True, None
                if entity_type == "agendaitem":
                    body_id = await self._body_of_meeting(session, row[1])
                    public, meeting_id = row[2] is not False, row[1]
                elif entity_type == "membership":
                    found = await session.execute(
                        select(OParlOrganization.body_id).where(OParlOrganization.id == row[1])
                    )
                    body_id = found.scalar_one_or_none()
                else:
                    body_id = row[1]
                if await self._events_for(session, body_id):
                    drafts = ris_events.depublished_events(entity_type, row[0], public=public, meeting_id=meeting_id)
                    await self._emit(session, body_id, drafts, modified)
            await session.commit()
            return entity_id

    # ========== Meeting Operations ==========

    async def upsert_meeting(
        self,
        meeting: ProcessedMeeting,
        body_id: UUID,
    ) -> UUID:
        """
        Insert or update a meeting.

        Zeile, Zuordnung der Gremien und Ereignis entstehen in einer Transaktion: Wer auf das
        Ereignis hin beim Bestand nachliest, sieht die Sitzung mit ihren Gremien.
        """
        # Gremien vorab auflösen (eigene Lesesitzung), damit die Zuordnung in die Transaktion des
        # Upserts passt. Batched lookup (cache-first, DB fallback).
        org_ids: list[UUID] = []
        org_urls = _normalized_urls((meeting.raw_json or {}).get("organization"))
        if org_urls:
            org_map = await self.get_organization_ids_by_external_ids(org_urls)
            org_ids = [org_map[url] for url in org_urls if url in org_map]

        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlMeeting, meeting.external_id) if emit else None
            stmt = pg_insert(OParlMeeting).values(
                id=meeting.id,
                external_id=meeting.external_id,
                body_id=body_id,
                name=meeting.name,
                meeting_state=meeting.meeting_state,
                cancelled=meeting.cancelled,
                start=meeting.start,
                end=meeting.end,
                location_name=meeting.location_name,
                location_address=meeting.location_address,
                oparl_created=meeting.oparl_created,
                oparl_modified=meeting.oparl_modified,
                raw_json=meeting.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
                **_extension_columns(meeting.protocol_approval, MEETING_COLUMNS),
            )
            update_set = {
                "name": stmt.excluded.name,
                "meeting_state": stmt.excluded.meeting_state,
                "cancelled": stmt.excluded.cancelled,
                "start": stmt.excluded.start,
                "end": stmt.excluded.end,
                "location_name": stmt.excluded.location_name,
                "location_address": stmt.excluded.location_address,
                **{name: getattr(stmt.excluded, name) for name in MEETING_COLUMNS},
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlMeeting.id)

            result = await session.execute(stmt)
            meeting_id = result.scalar_one()
            # Link M2M organizations from raw_json, vor dem Commit
            organizations_changed = await self._link_meeting_organizations(
                session, meeting_id, org_ids, compare=prior is not None
            )
            if emit:
                drafts = ris_events.meeting_events(
                    meeting_id,
                    meeting.raw_json or {},
                    prior,
                    organizations_changed=organizations_changed,
                    ids=self.id_bases,
                )
                await self._emit(session, body_id, drafts, meeting.oparl_modified)
            await session.commit()

            self._meeting_uuid_cache[meeting.external_id] = meeting_id
            self._meeting_body_cache[meeting_id] = body_id

            # Process nested entities
            for nested in meeting.nested_entities:
                if isinstance(nested, ProcessedAgendaItem):
                    await self.upsert_agenda_item(nested, meeting_id)
                elif isinstance(nested, ProcessedFile):
                    await self.upsert_file(nested, body_id, meeting_id=meeting_id)
                elif isinstance(nested, ProcessedLocation):
                    await self.upsert_location(nested, body_id)

            return meeting_id

    async def _link_meeting_organizations(
        self,
        session: AsyncSession,
        meeting_id: UUID,
        org_ids: list[UUID],
        *,
        compare: bool,
    ) -> bool:
        """
        Ordnet einer Sitzung ihre Gremien zu (``oparl_meetings_organizations``), in der Transaktion
        des Upserts und ohne eigenen Commit.

        Nennt die Quelle kein Gremium mehr oder ist keines davon im Bestand, wird die bisherige
        Zuordnung entfernt (Issue #553): Die Zuordnung folgt dem Objekt der Quelle, sonst nennte das
        Ereignis ``organization`` als geändert, der Bestand aber noch das alte Gremium. ``compare``:
        vorher die bisherige Zuordnung lesen. Rückgabe ``True``, wenn sie sich dadurch geändert hat
        (beim Entfernen immer, sonst nur mit ``compare``; für das Ereignis).
        """
        if not org_ids:
            removed = await session.execute(
                text(
                    "DELETE FROM oparl_meetings_organizations WHERE oparlmeeting_id = :mid "
                    "RETURNING oparlorganization_id"
                ),
                {"mid": meeting_id},
            )
            return removed.first() is not None
        changed = False
        if compare:
            before = await session.execute(
                text("SELECT oparlorganization_id FROM oparl_meetings_organizations WHERE oparlmeeting_id = :mid"),
                {"mid": meeting_id},
            )
            changed = {row[0] for row in before} != set(org_ids)
        # Clear existing M2M links
        await session.execute(
            text("DELETE FROM oparl_meetings_organizations WHERE oparlmeeting_id = :mid"),
            {"mid": meeting_id},
        )
        # Insert new M2M links
        for oid in org_ids:
            await session.execute(
                text(
                    "INSERT INTO oparl_meetings_organizations (oparlmeeting_id, oparlorganization_id) "
                    "VALUES (:mid, :oid) ON CONFLICT DO NOTHING"
                ),
                {"mid": meeting_id, "oid": oid},
            )
        return changed

    async def get_meeting_uuid(self, external_id: str) -> UUID | None:
        """Get a meeting's UUID by external ID (cached)."""
        if external_id in self._meeting_uuid_cache:
            return self._meeting_uuid_cache[external_id]

        async with self.get_session() as session:
            stmt = select(OParlMeeting.id).where(OParlMeeting.external_id == external_id)
            result = await session.execute(stmt)
            uuid = result.scalar_one_or_none()
            if uuid:
                self._meeting_uuid_cache[external_id] = uuid
            return uuid

    # ========== Paper Operations ==========

    async def upsert_paper(
        self,
        paper: ProcessedPaper,
        body_id: UUID,
    ) -> UUID:
        """
        Insert or update a paper.

        Zeile, Zuordnung der Orte und Ereignis entstehen in einer Transaktion: Wer auf das Ereignis
        hin beim Bestand nachliest, sieht die Vorlage mit ihren Orten.
        """
        # Eingebettete Orte zuerst (eigene Transaktion je Ort, kein Bezug auf die Vorlage): Die
        # Verknüpfung unten findet sie dann im Bestand.
        for nested in paper.nested_entities:
            if isinstance(nested, ProcessedLocation):
                await self.upsert_location(nested, body_id)

        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlPaper, paper.external_id) if emit else None
            stmt = pg_insert(OParlPaper).values(
                id=paper.id,
                external_id=paper.external_id,
                body_id=body_id,
                name=paper.name,
                reference=paper.reference,
                paper_type=paper.paper_type,
                date=paper.date,
                oparl_created=paper.oparl_created,
                oparl_modified=paper.oparl_modified,
                raw_json=paper.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "reference": stmt.excluded.reference,
                "paper_type": stmt.excluded.paper_type,
                "date": stmt.excluded.date,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlPaper.id)

            result = await session.execute(stmt)
            paper_id = result.scalar_one()
            # Link official OParl locations (M2M table managed by Django), vor dem Commit. Auch ohne Orte:
            # Nennt die Quelle keinen mehr, wird die bisherige Zuordnung entfernt (Issue #553).
            locations_changed = await self._link_paper_locations(session, paper_id, paper.location_external_ids)
            if emit:
                drafts = ris_events.paper_events(
                    paper_id, paper.raw_json or {}, prior, locations_changed=locations_changed
                )
                await self._emit(session, body_id, drafts, paper.oparl_modified)
            await session.commit()

            self._paper_uuid_cache[paper.external_id] = paper_id

            # Process nested entities (Orte stehen schon im Bestand, siehe oben)
            for nested in paper.nested_entities:
                if isinstance(nested, ProcessedFile):
                    await self.upsert_file(nested, body_id, paper_id=paper_id)
                elif isinstance(nested, ProcessedConsultation):
                    await self.upsert_consultation(nested, body_id, paper_id)

            return paper_id

    async def _link_paper_locations(
        self,
        session: AsyncSession,
        paper_id: UUID,
        location_external_ids: list[str],
    ) -> bool:
        """
        Link a paper to its official OParl locations via oparl_papers_locations, in der Transaktion
        des Upserts und ohne eigenen Commit.

        Only links locations that already exist in the database (embedded
        objects are upserted beforehand; unresolved string refs are skipped
        and picked up on a later sync once the location list is fetched).
        Defensive: if the M2M table does not exist yet (Django migration
        not applied), the link step is skipped with a warning. Das wird vorab geprüft (einmal je
        Zyklus), nicht am Fehler erkannt: Ein Fehler machte die Transaktion des Upserts ungültig.
        Jeder Fehler beim Zuordnen bricht deshalb den Upsert ab; Vorlage und Ereignis werden nie
        ohne die Zuordnung festgeschrieben.

        Die Zuordnung folgt dem Objekt der Quelle (Issue #553): Orte, die die Quelle nicht mehr nennt
        oder die nicht im Bestand sind, werden entfernt, auch wenn sie keinen Ort mehr nennt. Die
        übernommenen Koordinaten (``OParlPaper.locations``, Herkunft ``oparl``) räumt Django danach
        auf (``apply_oparl_locations`` im Georef-Lauf); der Ingestor schreibt dieses Feld nie.

        Rückgabe ``True``, wenn ein Ort neu zugeordnet oder entfernt wurde (für das Ereignis).
        """
        if self._paper_locations_table is None:
            found = await session.execute(text("SELECT to_regclass('oparl_papers_locations') IS NOT NULL"))
            self._paper_locations_table = bool(found.scalar())
            if not self._paper_locations_table:
                console.print("[yellow]Paper-Location-Link übersprungen (Tabelle fehlt)[/yellow]")
        if not self._paper_locations_table:
            return False

        location_ids: list[UUID] = []
        if location_external_ids:
            stmt = select(OParlLocation.id).where(OParlLocation.external_id.in_(location_external_ids))
            location_ids = [row[0] for row in (await session.execute(stmt)).fetchall()]
        # Entfernen, was die Quelle nicht mehr nennt (Index über oparlpaper_id, oparllocation_id)
        if location_ids:
            remove = text(
                "DELETE FROM oparl_papers_locations WHERE oparlpaper_id = :pid "
                "AND oparllocation_id NOT IN :lids RETURNING oparllocation_id"
            ).bindparams(bindparam("lids", expanding=True))
            removed = await session.execute(remove, {"pid": paper_id, "lids": location_ids})
        else:
            removed = await session.execute(
                text("DELETE FROM oparl_papers_locations WHERE oparlpaper_id = :pid RETURNING oparllocation_id"),
                {"pid": paper_id},
            )
        changed = removed.first() is not None
        for loc_id in location_ids:
            inserted = await session.execute(
                text(
                    "INSERT INTO oparl_papers_locations (oparlpaper_id, oparllocation_id) "
                    "VALUES (:pid, :lid) ON CONFLICT DO NOTHING RETURNING oparllocation_id"
                ),
                {"pid": paper_id, "lid": loc_id},
            )
            changed = inserted.first() is not None or changed
        return changed

    async def get_paper_uuid(self, external_id: str) -> UUID | None:
        """Get a paper's UUID by external ID (cached)."""
        if external_id in self._paper_uuid_cache:
            return self._paper_uuid_cache[external_id]

        async with self.get_session() as session:
            stmt = select(OParlPaper.id).where(OParlPaper.external_id == external_id)
            result = await session.execute(stmt)
            uuid = result.scalar_one_or_none()
            if uuid:
                self._paper_uuid_cache[external_id] = uuid
            return uuid

    # ========== Person Operations ==========

    async def upsert_person(
        self,
        person: ProcessedPerson,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a person; Zeile und Ereignis (``ris.person.changed``) in einer Transaktion."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlPerson, person.external_id) if emit else None
            stmt = pg_insert(OParlPerson).values(
                id=person.id,
                external_id=person.external_id,
                body_id=body_id,
                name=person.name,
                family_name=person.family_name,
                given_name=person.given_name,
                title=person.title,
                gender=person.gender,
                email=person.email,
                phone=person.phone,
                oparl_created=person.oparl_created,
                oparl_modified=person.oparl_modified,
                raw_json=person.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "family_name": stmt.excluded.family_name,
                "given_name": stmt.excluded.given_name,
                "title": stmt.excluded.title,
                "gender": stmt.excluded.gender,
                "email": stmt.excluded.email,
                "phone": stmt.excluded.phone,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlPerson.id)

            result = await session.execute(stmt)
            person_id = result.scalar_one()
            if emit:
                drafts = ris_events.person_events(person_id, person.raw_json or {}, prior)
                await self._emit(session, body_id, drafts, person.oparl_modified)
            await session.commit()

            self._person_uuid_cache[person.external_id] = person_id
            return person_id

    # ========== Organization Operations ==========

    async def upsert_organization(
        self,
        org: ProcessedOrganization,
        body_id: UUID,
    ) -> UUID:
        """Insert or update an organization; Zeile und Ereignis (``ris.organization.changed``) in einer Transaktion."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlOrganization, org.external_id) if emit else None
            stmt = pg_insert(OParlOrganization).values(
                id=org.id,
                external_id=org.external_id,
                body_id=body_id,
                name=org.name,
                short_name=org.short_name,
                organization_type=org.organization_type,
                classification=org.classification,
                start_date=org.start_date,
                end_date=org.end_date,
                website=org.website,
                oparl_created=org.oparl_created,
                oparl_modified=org.oparl_modified,
                raw_json=org.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "short_name": stmt.excluded.short_name,
                "organization_type": stmt.excluded.organization_type,
                "classification": stmt.excluded.classification,
                "start_date": stmt.excluded.start_date,
                "end_date": stmt.excluded.end_date,
                "website": stmt.excluded.website,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlOrganization.id)

            result = await session.execute(stmt)
            org_id = result.scalar_one()
            if emit:
                drafts = ris_events.organization_events(org_id, org.raw_json or {}, prior)
                await self._emit(session, body_id, drafts, org.oparl_modified)
            await session.commit()

            self._organization_uuid_cache[org.external_id] = org_id
            return org_id

    async def get_person_ids_by_external_ids(
        self,
        external_ids: list[str],
    ) -> dict[str, UUID]:
        """
        Resolve person external_ids to UUIDs with one batched SELECT.

        Replaces the former global FK cache pre-load (which pulled ALL
        persons into memory). Resolved IDs are added to the instance cache
        so subsequent lookups (e.g. upsert_membership) are free.

        Returns:
            Dict mapping external_id -> UUID. External IDs not found in the
            database are absent from the result.
        """
        result_map: dict[str, UUID] = {}
        missing: list[str] = []
        for ext_id in external_ids:
            cached = self._person_uuid_cache.get(ext_id)
            if cached is not None:
                result_map[ext_id] = cached
            else:
                missing.append(ext_id)

        if missing:
            async with self.get_session() as session:
                stmt = select(OParlPerson.external_id, OParlPerson.id).where(OParlPerson.external_id.in_(missing))
                result = await session.execute(stmt)
                for ext_id, uuid in result.all():
                    self._person_uuid_cache[ext_id] = uuid
                    result_map[ext_id] = uuid

        return result_map

    async def get_organization_ids_by_external_ids(
        self,
        external_ids: list[str],
    ) -> dict[str, UUID]:
        """
        Resolve organization external_ids to UUIDs with one batched SELECT.

        Same contract as get_person_ids_by_external_ids: cached-first,
        missing IDs resolved in a single WHERE external_id IN (...) query,
        results cached; not-found IDs are absent from the result dict.
        """
        result_map: dict[str, UUID] = {}
        missing: list[str] = []
        for ext_id in external_ids:
            cached = self._organization_uuid_cache.get(ext_id)
            if cached is not None:
                result_map[ext_id] = cached
            else:
                missing.append(ext_id)

        if missing:
            async with self.get_session() as session:
                stmt = select(OParlOrganization.external_id, OParlOrganization.id).where(
                    OParlOrganization.external_id.in_(missing)
                )
                result = await session.execute(stmt)
                for ext_id, uuid in result.all():
                    self._organization_uuid_cache[ext_id] = uuid
                    result_map[ext_id] = uuid

        return result_map

    # ========== Agenda Item Operations ==========

    async def upsert_agenda_item(
        self,
        item: ProcessedAgendaItem,
        meeting_id: UUID,
    ) -> UUID:
        """Insert or update an agenda item."""
        async with self.get_session() as session:
            body_id = await self._body_of_meeting(session, meeting_id) if self.events_enabled else None
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlAgendaItem, item.external_id) if emit else None
            stmt = pg_insert(OParlAgendaItem).values(
                id=item.id,
                external_id=item.external_id,
                meeting_id=meeting_id,
                number=item.number,
                order=item.order,
                name=item.name,
                public=item.public,
                result=item.result,
                resolution_text=item.resolution_text,
                oparl_created=item.oparl_created,
                oparl_modified=item.oparl_modified,
                raw_json=item.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
                **_extension_columns(item.decision, AGENDA_ITEM_COLUMNS),
            )
            update_set = {
                "meeting_id": meeting_id,
                "number": stmt.excluded.number,
                "order": stmt.excluded.order,
                "name": stmt.excluded.name,
                "public": stmt.excluded.public,
                "result": stmt.excluded.result,
                "resolution_text": stmt.excluded.resolution_text,
                **{name: getattr(stmt.excluded, name) for name in AGENDA_ITEM_COLUMNS},
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlAgendaItem.id)

            result = await session.execute(stmt)
            item_id = result.scalar_one()
            if emit:
                drafts = ris_events.agenda_item_events(
                    item_id, item.raw_json or {}, prior, meeting_id=meeting_id, public=item.public is not False
                )
                await self._emit(session, body_id, drafts, item.oparl_modified)
            await session.commit()
            return item_id

    # ========== File Operations ==========

    async def upsert_file(
        self,
        file: ProcessedFile,
        body_id: UUID,
        paper_id: UUID | None = None,
        meeting_id: UUID | None = None,
    ) -> UUID:
        """Insert or update a file."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlFile, file.external_id) if emit else None
            stmt = pg_insert(OParlFile).values(
                id=file.id,
                external_id=file.external_id,
                body_id=body_id,
                paper_id=paper_id,
                meeting_id=meeting_id,
                name=file.name,
                file_name=file.file_name,
                mime_type=file.mime_type,
                size=file.size,
                access_url=file.access_url,
                download_url=file.download_url,
                file_date=file.date,
                oparl_created=file.oparl_created,
                oparl_modified=file.oparl_modified,
                raw_json=file.raw_json,
                text_extraction_status="pending",
                created_at=func.now(),
                updated_at=func.now(),
            )

            # Build update set - only update paper_id/meeting_id if we have values
            # This prevents overwriting existing links when syncing standalone files
            update_set = {
                "name": stmt.excluded.name,
                "file_name": stmt.excluded.file_name,
                "mime_type": stmt.excluded.mime_type,
                # Ohne Größenangabe der Quelle bleibt eine vorhandene Größe stehen (Issue #786)
                "size": func.coalesce(stmt.excluded.size, OParlFile.size),
                "access_url": stmt.excluded.access_url,
                "download_url": stmt.excluded.download_url,
                "file_date": stmt.excluded.file_date,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }

            # Only update paper_id if provided (don't overwrite existing link)
            if paper_id is not None:
                update_set["paper_id"] = paper_id
            # Only update meeting_id if provided
            if meeting_id is not None:
                update_set["meeting_id"] = meeting_id

            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlFile.id)

            result = await session.execute(stmt)
            file_id = result.scalar_one()
            if emit:
                drafts = ris_events.file_events(
                    file_id, file.raw_json or {}, prior, paper_id=paper_id, meeting_id=meeting_id, ids=self.id_bases
                )
                await self._emit(session, body_id, drafts, file.oparl_modified)
            await session.commit()
            return file_id

    # ========== Location Operations ==========

    async def upsert_location(
        self,
        location: ProcessedLocation,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a location; Zeile und Ereignis (``ris.location.changed``) in einer Transaktion."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlLocation, location.external_id) if emit else None
            stmt = pg_insert(OParlLocation).values(
                id=location.id,
                external_id=location.external_id,
                body_id=body_id,
                description=location.description,
                street_address=location.street_address,
                room=location.room,
                postal_code=location.postal_code,
                locality=location.locality,
                geojson=location.geojson,
                oparl_created=location.oparl_created,
                oparl_modified=location.oparl_modified,
                raw_json=location.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "description": stmt.excluded.description,
                "street_address": stmt.excluded.street_address,
                "room": stmt.excluded.room,
                "postal_code": stmt.excluded.postal_code,
                "locality": stmt.excluded.locality,
                "geojson": stmt.excluded.geojson,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlLocation.id)

            result = await session.execute(stmt)
            location_id = result.scalar_one()
            if emit:
                drafts = ris_events.location_events(location_id, location.raw_json or {}, prior)
                await self._emit(session, body_id, drafts, location.oparl_modified)
            await session.commit()
            return location_id

    # ========== Consultation Operations ==========

    async def upsert_consultation(
        self,
        consultation: ProcessedConsultation,
        body_id: UUID,
        paper_id: UUID | None = None,
    ) -> UUID:
        """Insert or update a consultation."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlConsultation, consultation.external_id) if emit else None
            stmt = pg_insert(OParlConsultation).values(
                id=consultation.id,
                external_id=consultation.external_id,
                body_id=body_id,
                paper_id=paper_id,
                paper_external_id=consultation.paper_external_id,
                meeting_external_id=consultation.meeting_external_id,
                agenda_item_external_id=consultation.agenda_item_external_id,
                role=consultation.role,
                authoritative=consultation.authoritative,
                oparl_created=consultation.oparl_created,
                oparl_modified=consultation.oparl_modified,
                raw_json=consultation.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            # Bezüge nur ersetzen, nicht leeren: In Vorlagen eingebettete Beratungen nennen Sitzung und
            # Tagesordnungspunkt oft nicht (ALLRIS); ohne COALESCE löschte jeder Vorlagen-Abgleich die
            # Verknüpfung, die die Beratungsliste derselben Quelle liefert.
            update_set = {
                "paper_id": func.coalesce(stmt.excluded.paper_id, OParlConsultation.paper_id),
                "paper_external_id": func.coalesce(
                    stmt.excluded.paper_external_id, OParlConsultation.paper_external_id
                ),
                "meeting_external_id": func.coalesce(
                    stmt.excluded.meeting_external_id, OParlConsultation.meeting_external_id
                ),
                "agenda_item_external_id": func.coalesce(
                    stmt.excluded.agenda_item_external_id, OParlConsultation.agenda_item_external_id
                ),
                "role": stmt.excluded.role,
                "authoritative": stmt.excluded.authoritative,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlConsultation.id)

            result = await session.execute(stmt)
            consultation_id = result.scalar_one()
            if emit:
                drafts = ris_events.consultation_events(
                    consultation_id,
                    consultation.raw_json or {},
                    prior,
                    paper_id=paper_id,
                    paper_external_id=consultation.paper_external_id,
                    ids=self.id_bases,
                )
                await self._emit(session, body_id, drafts, consultation.oparl_modified)
            await session.commit()
            return consultation_id

    # ========== Membership Operations ==========

    async def upsert_membership(
        self,
        membership: ProcessedMembership,
        body_id: UUID,
    ) -> UUID | None:
        """
        Insert or update a membership. Returns None if FKs can't be resolved.

        Zeile und Ereignis (``ris.membership.changed``) entstehen in einer Transaktion; die Kommune der
        Ereignisse ist ``body_id`` (die Tabelle selbst hat keine).
        """
        # Resolve person and organization UUIDs (both required by Django schema).
        # Cache-first with targeted DB fallback (no global cache pre-load).
        person_id = None
        organization_id = None

        if membership.person_external_id:
            person_map = await self.get_person_ids_by_external_ids([membership.person_external_id])
            person_id = person_map.get(membership.person_external_id)

        if membership.organization_external_id:
            org_map = await self.get_organization_ids_by_external_ids([membership.organization_external_id])
            organization_id = org_map.get(membership.organization_external_id)

        # Both FKs are NOT NULL in Django schema - skip if unresolved
        if not person_id or not organization_id:
            return None

        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlMembership, membership.external_id) if emit else None
            stmt = pg_insert(OParlMembership).values(
                id=membership.id,
                external_id=membership.external_id,
                person_id=person_id,
                organization_id=organization_id,
                role=membership.role,
                voting_right=membership.voting_right,
                start_date=membership.start_date,
                end_date=membership.end_date,
                oparl_created=membership.oparl_created,
                oparl_modified=membership.oparl_modified,
                raw_json=membership.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "person_id": person_id,
                "organization_id": organization_id,
                "role": stmt.excluded.role,
                "voting_right": stmt.excluded.voting_right,
                "start_date": stmt.excluded.start_date,
                "end_date": stmt.excluded.end_date,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlMembership.id)

            result = await session.execute(stmt)
            membership_id = result.scalar_one()
            if emit:
                drafts = ris_events.membership_events(
                    membership_id,
                    membership.raw_json or {},
                    prior,
                    person_id=person_id,
                    organization_id=organization_id,
                )
                await self._emit(session, body_id, drafts, membership.oparl_modified)
            await session.commit()
            return membership_id

    # ========== Legislative Term Operations ==========

    async def upsert_legislative_term(
        self,
        term: ProcessedLegislativeTerm,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a legislative term; Zeile und Ereignis (``ris.legislativeterm.changed``) zusammen."""
        async with self.get_session() as session:
            emit = await self._events_for(session, body_id)
            prior = await self._prior(session, OParlLegislativeTerm, term.external_id) if emit else None
            stmt = pg_insert(OParlLegislativeTerm).values(
                id=term.id,
                external_id=term.external_id,
                body_id=body_id,
                name=term.name,
                start_date=term.start_date,
                end_date=term.end_date,
                oparl_created=term.oparl_created,
                oparl_modified=term.oparl_modified,
                raw_json=term.raw_json,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "start_date": stmt.excluded.start_date,
                "end_date": stmt.excluded.end_date,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "deletion_reason": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlLegislativeTerm.id)

            result = await session.execute(stmt)
            term_id = result.scalar_one()
            if emit:
                drafts = ris_events.legislative_term_events(term_id, term.raw_json or {}, prior)
                await self._emit(session, body_id, drafts, term.oparl_modified)
            await session.commit()
            return term_id

    # ========== Statistics ==========

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        async with self.get_session() as session:
            stats = {}

            tables = [
                ("sources", OParlSource),
                ("bodies", OParlBody),
                ("meetings", OParlMeeting),
                ("papers", OParlPaper),
                ("persons", OParlPerson),
                ("organizations", OParlOrganization),
                ("agenda_items", OParlAgendaItem),
                ("files", OParlFile),
                ("locations", OParlLocation),
                ("consultations", OParlConsultation),
                ("memberships", OParlMembership),
                ("legislative_terms", OParlLegislativeTerm),
            ]

            for name, model in tables:
                stmt = select(func.count()).select_from(model)
                result = await session.execute(stmt)
                stats[name] = result.scalar_one()

            return stats

    async def get_all_bodies(self) -> list[OParlBody]:
        """Get all bodies from the database."""
        async with self.get_session() as session:
            stmt = select(OParlBody).order_by(OParlBody.name)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========== Text Extraction Queries ==========

    # Dateien in "processing", deren Bearbeitung länger zurückliegt, gelten als abgebrochen (Worker beendet).
    # Gilt bei Aufruf ohne eigene Grenze; der Extraktor nimmt TEXT_EXTRACTION_STALE_MINUTES (Issue #817).
    PROCESSING_STALE_AFTER = timedelta(hours=1)

    @staticmethod
    def _stale_processing(cutoff: datetime) -> Any:
        """
        Bedingung für abgebrochene Bearbeitungen: Status ``processing`` und Beginn vor ``cutoff``. Ohne
        Beginn (Zeilen aus der Zeit vor Issue #817) zählt die letzte Änderung der Zeile.
        """
        return and_(
            OParlFile.text_extraction_status == "processing",
            or_(
                OParlFile.text_extraction_started_at < cutoff,
                and_(OParlFile.text_extraction_started_at.is_(None), OParlFile.updated_at < cutoff),
            ),
        )

    async def release_stale_extractions(
        self, stale_after: timedelta | None = None, max_attempts: int = 3
    ) -> tuple[int, int]:
        """
        Abgebrochene Bearbeitungen auflösen (Issue #817), über alle Kommunen.

        Stirbt der Worker mitten in der Arbeit (etwa durch den Speicherwächter des Kernels), bleiben seine
        Dateien in ``processing``. Nach ``stale_after``:

        - Dateien, deren Bearbeitung schon ``max_attempts``-mal begonnen und nie beendet wurde, werden
          ``failed`` mit dem Grund „Speichergrenze“ – keine Endlosschleife über dieselbe Datei. Der Zeitpunkt
          der Aufgabe steht in ``text_extracted_at`` (Prüfung ``texterkennung``: aufgegeben in 24 h), nicht nur
          in ``updated_at``, das jede spätere Änderung der Zeile verschiebt.
        - alle anderen zurück nach ``pending``; der Zähler bleibt, der nächste Versuch läuft allein.

        Rückgabe: (zurückgestellt, aufgegeben).
        """
        cutoff = datetime.now(UTC) - (stale_after or self.PROCESSING_STALE_AFTER)
        stale = self._stale_processing(cutoff)
        async with self.get_session() as session:
            given_up = await session.execute(
                update(OParlFile)
                .where(stale, OParlFile.text_extraction_attempts >= max_attempts)
                .values(
                    text_extraction_status="failed",
                    text_extraction_error=(
                        f"{STALE_GIVE_UP_REASON}: Bearbeitung {max_attempts}-mal abgebrochen (Worker beendet)"
                    ),
                    text_extraction_started_at=None,
                    text_extracted_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(OParlFile.id)
            )
            aufgegeben = len(given_up.all())
            released = await session.execute(
                update(OParlFile)
                .where(stale)
                .values(text_extraction_status="pending", text_extraction_started_at=None, updated_at=func.now())
                .returning(OParlFile.id)
            )
            zurueck = len(released.all())
            await session.commit()
        return zurueck, aufgegeben

    async def get_pending_files(
        self,
        body_id: UUID,
        batch_size: int = 100,
        max_size_bytes: int | None = None,
        retried: bool | None = None,
    ) -> list[OParlFile]:
        """
        Atomically CLAIM files pending text extraction (multi-worker safe).

        Candidate rows are selected with FOR UPDATE SKIP LOCKED and their
        text_extraction_status is set to 'processing' in one atomic
        UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING
        statement, so two concurrent extraction workers (e.g. server + local
        PC) never claim the same file twice.

        The extractor later overwrites the transient 'processing' status with
        completed/failed/skipped via update_file_text. Abgebrochene Bearbeitungen
        stellt ``release_stale_extractions`` zurück (bzw. gibt sie nach mehreren
        Abbrüchen auf); beansprucht wird nur ``pending``.

        Das Beanspruchen setzt den Beginn (``text_extraction_started_at``); ab dann läuft die Zeitgrenze für
        abgebrochene Bearbeitungen. Aufrufer beanspruchen deshalb nur, was sie gleich bearbeiten (kleine
        Portionen, Issue #817), sonst gelten wartende Dateien eines langen Stapels als abgebrochen.

        Args:
            body_id: Body to query files for
            batch_size: Maximum number of files to claim
            max_size_bytes: Skip files larger than this (optional)
            retried: ``False`` nur Dateien ohne Abbruch, ``True`` nur Dateien nach einem Abbruch
                (``text_extraction_attempts`` > 0), ``None`` alle
        """
        async with self.get_session() as session:
            candidates = select(OParlFile.id).where(
                OParlFile.body_id == body_id,
                # Keine Textextraktion fuer von der Quelle geloeschte oder dort fehlende Dateien (#787)
                OParlFile.deleted == False,  # noqa: E712
                OParlFile.source_missing_since.is_(None),
                OParlFile.text_extraction_status == "pending",
                or_(
                    OParlFile.download_url.isnot(None),
                    OParlFile.access_url.isnot(None),
                ),
            )

            if max_size_bytes is not None:
                candidates = candidates.where(
                    or_(
                        OParlFile.size.is_(None),
                        OParlFile.size <= max_size_bytes,
                    )
                )
            if retried is True:
                candidates = candidates.where(OParlFile.text_extraction_attempts > 0)
            elif retried is False:
                candidates = candidates.where(OParlFile.text_extraction_attempts <= 0)

            candidates = candidates.order_by(OParlFile.created_at).limit(batch_size).with_for_update(skip_locked=True)

            claim_stmt = (
                update(OParlFile)
                .where(OParlFile.id.in_(candidates.scalar_subquery()))
                .values(
                    text_extraction_status="processing",
                    text_extraction_started_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(OParlFile)
            )

            result = await session.execute(claim_stmt)
            files = list(result.scalars().all())
            await session.commit()
            return files

    async def mark_extraction_started(self, file_id: UUID) -> int | None:
        """
        Bearbeitung einer beanspruchten Datei beginnt: Versuch zählen, Beginn festhalten (Issue #817).

        Endet die Bearbeitung nie (Worker stirbt), bleibt der Zähler stehen; ``release_stale_extractions``
        gibt die Datei nach ``TEXT_EXTRACTION_MAX_ATTEMPTS`` Abbrüchen auf. ``update_file_text`` setzt ihn
        zurück. Wurde die Datei inzwischen zurückgestellt, nimmt dieser Aufruf sie wieder in Bearbeitung.
        Rückgabe: Zahl der begonnenen Versuche, ``None``, wenn die Datei nicht mehr zu bearbeiten ist.
        """
        async with self.get_session() as session:
            result = await session.execute(
                update(OParlFile)
                .where(
                    OParlFile.id == file_id,
                    OParlFile.text_extraction_status.in_(("processing", "pending")),
                )
                .values(
                    text_extraction_status="processing",
                    text_extraction_attempts=OParlFile.text_extraction_attempts + 1,
                    text_extraction_started_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(OParlFile.text_extraction_attempts)
            )
            attempts = result.scalar()
            await session.commit()
            return None if attempts is None else int(attempts)

    async def update_file_text(
        self,
        file_id: UUID,
        text_content: str | None = None,
        method: str | None = None,
        status: str = "completed",
        error: str | None = None,
        page_count: int | None = None,
        sha256_hash: str | None = None,
        reset_attempts: bool = True,
    ) -> None:
        """
        Update a file with text extraction results.

        ``reset_attempts=False``: Die Datei wird zurückgestellt, bevor ihre Bearbeitung begann (robots.txt
        nicht erreichbar); der Abbruchzähler bleibt, sonst liefe eine Datei, an der der Worker schon starb,
        wieder parallel statt einzeln und zuletzt (Issue #817).

        Liegt ein Text vor (``completed`` mit ``text_content``), meldet ``ris.file.text_extracted`` das in
        derselben Transaktion (Issue #821): Wer auf das Ereignis hin den Bestand liest, sieht den Text.
        """
        from datetime import datetime

        # PostgreSQL lehnt Null-Bytes in Textfeldern ab („invalid byte sequence for encoding UTF8:
        # 0x00“). Manche PDFs enthalten sie im Textstrom; ohne diese Bereinigung scheiterte das
        # Speichern, die Datei blieb in „processing“ und wurde nach PROCESSING_STALE_AFTER immer
        # wieder neu verarbeitet (Sept. 2026: ~14 000 Fehlversuche am Tag).
        if text_content is not None:
            text_content = text_content.replace("\x00", "")
        if error is not None:
            error = error.replace("\x00", "")

        async with self.get_session() as session:
            values: dict = {
                "text_extraction_status": status,
                "text_extraction_started_at": None,
                "updated_at": func.now(),
            }
            if reset_attempts:
                # Bearbeitung beendet (gleich wie): kein Abbruch, Zähler zurücksetzen (Issue #817)
                values["text_extraction_attempts"] = 0
            if text_content is not None:
                values["text_content"] = text_content
            if method is not None:
                values["text_extraction_method"] = method
            if error is not None:
                values["text_extraction_error"] = error
            if page_count is not None:
                values["page_count"] = page_count
            if sha256_hash is not None:
                values["sha256_hash"] = sha256_hash
            if status == "completed":
                values["text_extracted_at"] = datetime.now(UTC)

            stmt = update(OParlFile).where(OParlFile.id == file_id).values(**values).returning(OParlFile.body_id)
            body_id = (await session.execute(stmt)).scalar_one_or_none()
            if status == "completed" and text_content and await self._events_for(session, body_id):
                drafts = ris_events.text_extracted_events(file_id, method, len(text_content))
                await self._emit(session, body_id, drafts)
            await session.commit()

    # ========== Dokumentablage nach SHA-256 (Issue #788) ==========

    async def body_stores_files(self, body_id: UUID) -> bool:
        """Ist die Kommune gelistet (Django-Spalte ``is_listed``)? Im Zweifel nicht ablegen."""
        try:
            async with self.get_session() as session:
                result = await session.execute(
                    text("SELECT is_listed FROM oparl_bodies WHERE id = :id"), {"id": body_id}
                )
                return bool(result.scalar())
        except Exception as e:  # noqa: BLE001 - ohne Spalte (älteres Schema) wird nichts abgelegt
            logger.debug("is_listed für Body %s nicht lesbar: %s", body_id, e)
            return False

    async def attach_file_blob(self, file_id: UUID, sha256: str, size: int, source: Path, target: Path) -> bool:
        """
        Inhalt unter seinem SHA-256 ablegen und die Datei darauf verweisen lassen.

        Gleiches Vorgehen wie ``file_store.attach`` in Django: Zeile des Inhalts sperren (bzw. anlegen),
        Datei verschieben, solange die Sperre gilt, Referenz zählen, eine bisherige Referenz freigeben.
        ``source`` liegt im selben Dateisystem wie ``target`` (``sha256/tmp``). Rückgabe: abgelegt?
        """
        remove_after: list[Path] = []
        async with self.get_session() as session:
            for _ in range(3):
                await session.execute(
                    pg_insert(OParlFileBlob)
                    .values(sha256=sha256, size=size, ref_count=0)
                    .on_conflict_do_nothing(index_elements=["sha256"])
                )
                locked = await session.execute(
                    select(OParlFileBlob.sha256).where(OParlFileBlob.sha256 == sha256).with_for_update()
                )
                if locked.scalar() is not None:
                    break
            else:
                await session.rollback()
                return False
            if target.is_file():
                remove_after.append(source)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Temporäre Dateien entstehen mit 0600; Webserver und Anwendung müssen den Inhalt lesen
                os.chmod(source, 0o644)
                os.replace(source, target)
            current = (
                await session.execute(
                    select(OParlFile.blob_id, OParlFile.local_path).where(OParlFile.id == file_id).with_for_update()
                )
            ).first()
            if current is None:
                await session.rollback()
                source.unlink(missing_ok=True)
                return False
            old_blob, old_path = current
            if old_blob != sha256:
                await session.execute(
                    update(OParlFileBlob)
                    .where(OParlFileBlob.sha256 == sha256)
                    .values(ref_count=OParlFileBlob.ref_count + 1, orphaned_at=None)
                )
                if old_blob:
                    await session.execute(
                        update(OParlFileBlob)
                        .where(OParlFileBlob.sha256 == old_blob)
                        .values(ref_count=OParlFileBlob.ref_count - 1)
                    )
                    await session.execute(
                        update(OParlFileBlob)
                        .where(
                            OParlFileBlob.sha256 == old_blob,
                            OParlFileBlob.ref_count <= 0,
                            OParlFileBlob.orphaned_at.is_(None),
                        )
                        .values(orphaned_at=func.now())
                    )
                elif old_path and Path(old_path) != target and not Path(old_path).is_relative_to(target.parent.parent):
                    # Kopie im bisherigen Layout je Kommune: nach dem Commit löschen. Ein Pfad unter sha256/
                    # ohne Referenz gehört anderen Dateien und bleibt.
                    remove_after.append(Path(old_path))
            await session.execute(
                update(OParlFile)
                .where(OParlFile.id == file_id)
                .values(
                    blob_id=sha256,
                    local_path=str(target),
                    local_size=size,
                    sha256_hash=sha256,
                    local_status="ok",
                    local_error="",
                    local_cached_at=func.now(),
                )
            )
            await session.commit()
        for path in remove_after:
            path.unlink(missing_ok=True)
        return True

    # ========== Search Indexing Query Helpers ==========

    #: Einträge je Seite beim Aufbau des Suchindex (Speicher bleibt je Seite begrenzt)
    INDEX_PAGE_SIZE: Final = 500

    async def get_body_last_sync(self, body_id: UUID) -> datetime | None:
        """Letzter Abgleich eines Bodies (``last_sync``); Grundlage für die Indexierung geänderter Objekte."""
        async with self.get_session() as session:
            result = await session.execute(select(OParlBody.last_sync).where(OParlBody.id == body_id))
            value: datetime | None = result.scalar_one_or_none()
            return value

    async def iter_for_body(
        self,
        body_id: UUID,
        model_class: Any,
        page_size: int | None = None,
        *,
        updated_since: datetime | None = None,
        with_changed_files: bool = False,
    ) -> AsyncIterator[list[Any]]:
        """
        Alle nicht gelöschten Objekte einer Art eines Bodies, seitenweise nach ``id`` (Keyset).

        Ersetzt die frühere Abfrage mit fester Obergrenze (10.000 je Art): Größere Kommunen fehlten
        danach teilweise im Suchindex. Jede Seite kommt aus einer eigenen kurzen Sitzung.

        ``updated_since``: nur Objekte, die seitdem geschrieben wurden (inkrementeller Abgleich). Mit
        ``with_changed_files`` (Vorgänge) zählen auch Vorgänge, deren Dateien seitdem geändert wurden, etwa
        durch eine neue Textextraktion: Deren Text fließt in die Gewichtung des Vorgangs ein.

        Keyset über die UUID mit kurzer Sitzung je Seite: Ein Objekt, das während des Laufs mit kleinerer ID
        hinzukommt, fehlt bis zum nächsten Lauf. Das ist hinnehmbar; der Speicher bleibt je Seite begrenzt.
        """
        size = max(1, page_size or self.INDEX_PAGE_SIZE)
        last_id: UUID | None = None
        while True:
            async with self.get_session() as session:
                stmt = select(model_class).where(
                    model_class.body_id == body_id,
                    model_class.deleted == False,  # noqa: E712
                )
                if updated_since is not None:
                    changed = model_class.updated_at >= updated_since
                    if with_changed_files:
                        changed_files = select(OParlFile.paper_id).where(
                            OParlFile.body_id == body_id,
                            OParlFile.paper_id.isnot(None),
                            OParlFile.updated_at >= updated_since,
                        )
                        changed = or_(changed, model_class.id.in_(changed_files))
                    stmt = stmt.where(changed)
                if last_id is not None:
                    stmt = stmt.where(model_class.id > last_id)
                stmt = stmt.order_by(model_class.id).limit(size)
                rows: list[Any] = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return
            yield rows
            if len(rows) < size:
                return
            last_id = rows[-1].id

    def _files_with_text(self, body_id: UUID) -> Any:
        return select(OParlFile).where(
            OParlFile.body_id == body_id,
            OParlFile.deleted == False,  # noqa: E712
            # In der Quelle nicht mehr abrufbar (Löschabgleich, #787): nicht wieder indexieren
            OParlFile.source_missing_since.is_(None),
            OParlFile.text_content.isnot(None),
            OParlFile.text_extraction_status == "completed",
        )

    async def iter_files_with_text(
        self, body_id: UUID, page_size: int | None = None, *, updated_since: datetime | None = None
    ) -> AsyncIterator[list[OParlFile]]:
        """
        Dateien eines Bodies mit extrahiertem Text, seitenweise nach ``id`` (Volltexte sind groß).
        ``updated_since``: nur seitdem geschriebene Dateien (inkrementeller Abgleich).
        """
        size = max(1, page_size or self.INDEX_PAGE_SIZE)
        last_id: UUID | None = None
        while True:
            async with self.get_session() as session:
                stmt = self._files_with_text(body_id)
                if updated_since is not None:
                    stmt = stmt.where(OParlFile.updated_at >= updated_since)
                if last_id is not None:
                    stmt = stmt.where(OParlFile.id > last_id)
                result = await session.execute(stmt.order_by(OParlFile.id).limit(size))
                rows: list[OParlFile] = list(result.scalars().all())
            if not rows:
                return
            yield rows
            if len(rows) < size:
                return
            last_id = rows[-1].id

    async def get_files_with_text_for_papers(
        self, body_id: UUID, paper_ids: list[UUID], max_chars: int | None = None
    ) -> list[Any]:
        """
        Dateien mit extrahiertem Text zu einer Seite von Vorgängen, für die Gewichtung im Suchindex: nur
        ``paper_id``, ``file_name`` und die ersten ``max_chars`` Zeichen des Textes (mehr nutzt der Vorgang nicht;
        die Volltexte lädt nur die Indexierung der Dateien selbst).
        """
        if not paper_ids:
            return []
        text_content: Any = OParlFile.text_content
        if max_chars is not None:
            text_content = func.substr(OParlFile.text_content, 1, max_chars)
        async with self.get_session() as session:
            stmt = (
                select(OParlFile.paper_id, OParlFile.file_name, text_content.label("text_content"))
                .where(
                    OParlFile.body_id == body_id,
                    OParlFile.deleted == False,  # noqa: E712
                    # In der Quelle nicht mehr abrufbar (Löschabgleich, #787): nicht in den Vorgang übernehmen
                    OParlFile.source_missing_since.is_(None),
                    OParlFile.text_content.isnot(None),
                    OParlFile.text_extraction_status == "completed",
                    OParlFile.paper_id.in_(paper_ids),
                )
                .order_by(OParlFile.id)
            )
            return list((await session.execute(stmt)).all())
