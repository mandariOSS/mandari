"""
PostgreSQL Storage for OParl Data

High-performance async database operations with proper upsert support.
Uses PostgreSQL ON CONFLICT for efficient insert-or-update operations.
"""

from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any
from uuid import UUID

from mandari_oparl import (
    ProcessedAgendaItem,
    ProcessedBody,
    ProcessedConsultation,
    ProcessedEntity,
    ProcessedFile,
    ProcessedLegislativeTerm,
    ProcessedLocation,
    ProcessedMeeting,
    ProcessedMembership,
    ProcessedOrganization,
    ProcessedPaper,
    ProcessedPerson,
)
from rich.console import Console
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.storage.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

console = Console()


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
ENRICHMENT_FIELDS: frozenset[str] = frozenset({
    # OParlFile: text extraction / OCR pipeline
    "text_content",
    "text_extraction_status",
    "text_extraction_method",
    "text_extraction_error",
    "text_extracted_at",
    "page_count",
    "sha256_hash",
    "local_path",
    # OParlPaper: AI enrichment + georeferencing (Django-managed)
    "summary",
    "locations",
    "georef_status",
    # OParlBody: Django-managed presentation + geo fields
    "display_name",
    "logo",
    "slug",
    "latitude",
    "longitude",
    "bbox_north",
    "bbox_south",
    "bbox_east",
    "bbox_west",
    "osm_relation_id",
    "ags",
    # OParlBody: person photo scraping configuration (Django-managed)
    "person_photo_url_template",
    "person_photo_id_pattern",
})


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

    def __init__(self, database_url: str | None = None) -> None:
        """
        Initialize database storage.

        Args:
            database_url: Database connection URL. Defaults to settings.
        """
        self.database_url = database_url or settings.database_url
        self._engine = create_async_engine(
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

        # Cache for body UUIDs (external_id -> UUID)
        self._body_uuid_cache: dict[str, UUID] = {}
        self._meeting_uuid_cache: dict[str, UUID] = {}
        self._paper_uuid_cache: dict[str, UUID] = {}
        self._person_uuid_cache: dict[str, UUID] = {}
        self._organization_uuid_cache: dict[str, UUID] = {}

    async def initialize(self) -> None:
        """
        Verify database schema exists.

        Django owns the schema via migrations. The ingestor must NOT create tables.
        If tables are missing, raise an error pointing to Django migrate.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_name = 'oparl_bodies'"
                ")"
            ))
            exists = result.scalar()
            if not exists:
                raise RuntimeError(
                    "Database schema not found! "
                    "Django owns the schema. Please run: "
                    "cd mandari && python manage.py migrate"
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

        async with self.get_session() as session:
            async with session.begin():
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
    ) -> UUID:
        """
        Insert or update an OParl source.

        Returns the source UUID.
        """
        async with self.get_session() as session:
            stmt = pg_insert(OParlSource).values(
                url=url,
                name=name,
                raw_json=raw_json or {},
                is_active=True,
                created_at=func.now(),
                updated_at=func.now(),
            )
            update_set = {
                "name": stmt.excluded.name,
                "raw_json": stmt.excluded.raw_json,
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
                stmt = stmt.where(OParlSource.is_active == True)
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
            result = await session.execute(
                select(OParlSource).where(OParlSource.url == source_url)
            )
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

    # Schlüssel in OParlSource.sync_config für den persistierten
    # Scraper-Zustand (Listen-Snapshots, Missing-Counter, letzte Läufe).
    SYNC_CONFIG_SCRAPER_STATE_KEY = "scraper_state"

    async def update_scraper_state(self, source_url: str, state: dict[str, Any]) -> None:
        """
        Persistiert den Scraper-Zustand einer Quelle additiv in sync_config
        (Schlüssel "scraper_state"); andere Schlüssel bleiben unberührt.
        """
        async with self.get_session() as session:
            result = await session.execute(
                select(OParlSource).where(OParlSource.url == source_url)
            )
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
            hashes: dict[str, str | None] = {eid: None for eid in external_ids}
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

    # ========== Body Operations ==========

    async def upsert_body(
        self,
        body: ProcessedBody,
        source_id: UUID,
    ) -> UUID:
        """
        Insert or update a body.

        Returns the body UUID.
        """
        async with self.get_session() as session:
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
                "classification": func.coalesce(
                    stmt.excluded.classification, OParlBody.classification
                ),
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlBody.id)

            result = await session.execute(stmt)
            body_id = result.scalar_one()
            await session.commit()

            # Cache the UUID
            self._body_uuid_cache[body.external_id] = body_id

            # Process nested legislative terms
            for nested in body.nested_entities:
                if isinstance(nested, ProcessedLegislativeTerm):
                    await self.upsert_legislative_term(nested, body_id)

            return body_id

    async def get_body_by_external_id(self, external_id: str) -> OParlBody | None:
        """Get a body by external ID."""
        async with self.get_session() as session:
            stmt = select(OParlBody).where(OParlBody.external_id == external_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_body_uuid(self, external_id: str) -> UUID | None:
        """Get a body's UUID by external ID (cached)."""
        if external_id in self._body_uuid_cache:
            return self._body_uuid_cache[external_id]

        body = await self.get_body_by_external_id(external_id)
        if body:
            self._body_uuid_cache[external_id] = body.id
            return body.id
        return None

    async def update_body_sync_time(self, body_id: UUID) -> None:
        """Update the last sync timestamp for a body."""
        async with self.get_session() as session:
            body = await session.get(OParlBody, body_id)
            if body:
                body.last_sync = datetime.now(UTC)
                await session.commit()

    # ========== Entity Existence Check ==========

    async def get_entity_modified_date(
        self,
        entity_type: str,
        external_id: str,
    ) -> datetime | None:
        """
        Check if an entity exists and return its oparl_modified date.

        Args:
            entity_type: Type of entity (meeting, paper, person, organization, membership)
            external_id: The OParl external ID

        Returns:
            The oparl_modified datetime if exists, None if not found
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

        async with self.get_session() as session:
            stmt = select(model.oparl_modified).where(model.external_id == external_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

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
            stmt = select(model.external_id, model.oparl_modified).where(
                model.external_id.in_(external_ids)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # Create dict with all IDs defaulting to None
            result_dict: dict[str, datetime | None] = {eid: None for eid in external_ids}
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
        async with self.get_session() as session:
            stmt = (
                update(model)
                .where(model.external_id == external_id, model.deleted == False)  # noqa: E712
                .values(
                    deleted=True,
                    deleted_at=now,
                    oparl_modified=modified or now,
                    updated_at=func.now(),
                )
                .returning(model.id)
            )
            result = await session.execute(stmt)
            entity_id = result.scalar_one_or_none()
            await session.commit()
            return entity_id

    # ========== Meeting Operations ==========

    async def upsert_meeting(
        self,
        meeting: ProcessedMeeting,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a meeting."""
        async with self.get_session() as session:
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
            )
            update_set = {
                "name": stmt.excluded.name,
                "meeting_state": stmt.excluded.meeting_state,
                "cancelled": stmt.excluded.cancelled,
                "start": stmt.excluded.start,
                "end": stmt.excluded.end,
                "location_name": stmt.excluded.location_name,
                "location_address": stmt.excluded.location_address,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlMeeting.id)

            result = await session.execute(stmt)
            meeting_id = result.scalar_one()
            await session.commit()

            self._meeting_uuid_cache[meeting.external_id] = meeting_id

            # Link M2M organizations from raw_json
            org_urls = (meeting.raw_json or {}).get("organization", [])
            if isinstance(org_urls, str):
                org_urls = [org_urls]
            if org_urls:
                # Batched lookup (cache-first, DB fallback) — previously this
                # relied on a globally pre-loaded organization cache.
                org_map = await self.get_organization_ids_by_external_ids(org_urls)
                org_ids = [org_map[url] for url in org_urls if url in org_map]
                if org_ids:
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
                    await session.commit()

            # Process nested entities
            for nested in meeting.nested_entities:
                if isinstance(nested, ProcessedAgendaItem):
                    await self.upsert_agenda_item(nested, meeting_id)
                elif isinstance(nested, ProcessedFile):
                    await self.upsert_file(nested, body_id, meeting_id=meeting_id)
                elif isinstance(nested, ProcessedLocation):
                    await self.upsert_location(nested, body_id)

            return meeting_id

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
        """Insert or update a paper."""
        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlPaper.id)

            result = await session.execute(stmt)
            paper_id = result.scalar_one()
            await session.commit()

            self._paper_uuid_cache[paper.external_id] = paper_id

            # Process nested entities
            for nested in paper.nested_entities:
                if isinstance(nested, ProcessedFile):
                    await self.upsert_file(nested, body_id, paper_id=paper_id)
                elif isinstance(nested, ProcessedConsultation):
                    await self.upsert_consultation(nested, body_id, paper_id)
                elif isinstance(nested, ProcessedLocation):
                    await self.upsert_location(nested, body_id)

            # Link official OParl locations (M2M table managed by Django)
            if paper.location_external_ids:
                await self._link_paper_locations(paper_id, paper.location_external_ids)

            return paper_id

    async def _link_paper_locations(
        self,
        paper_id: UUID,
        location_external_ids: list[str],
    ) -> None:
        """
        Link a paper to its official OParl locations via oparl_papers_locations.

        Only links locations that already exist in the database (embedded
        objects are upserted beforehand; unresolved string refs are skipped
        and picked up on a later sync once the location list is fetched).
        Defensive: if the M2M table does not exist yet (Django migration
        not applied), the link step is skipped with a warning.
        """
        async with self.get_session() as session:
            stmt = select(OParlLocation.id, OParlLocation.external_id).where(
                OParlLocation.external_id.in_(location_external_ids)
            )
            result = await session.execute(stmt)
            location_ids = [row[0] for row in result.fetchall()]
            if not location_ids:
                return

            try:
                for loc_id in location_ids:
                    await session.execute(
                        text(
                            "INSERT INTO oparl_papers_locations (oparlpaper_id, oparllocation_id) "
                            "VALUES (:pid, :lid) ON CONFLICT DO NOTHING"
                        ),
                        {"pid": paper_id, "lid": loc_id},
                    )
                await session.commit()
            except Exception as e:  # noqa: BLE001 - table may not exist yet
                await session.rollback()
                console.print(
                    f"[yellow]Paper-Location-Link übersprungen (Tabelle fehlt?): {e}[/yellow]"
                )

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
        """Insert or update a person."""
        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlPerson.id)

            result = await session.execute(stmt)
            person_id = result.scalar_one()
            await session.commit()

            self._person_uuid_cache[person.external_id] = person_id
            return person_id

    # ========== Organization Operations ==========

    async def upsert_organization(
        self,
        org: ProcessedOrganization,
        body_id: UUID,
    ) -> UUID:
        """Insert or update an organization."""
        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlOrganization.id)

            result = await session.execute(stmt)
            org_id = result.scalar_one()
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
                stmt = select(OParlPerson.external_id, OParlPerson.id).where(
                    OParlPerson.external_id.in_(missing)
                )
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
                stmt = select(
                    OParlOrganization.external_id, OParlOrganization.id
                ).where(OParlOrganization.external_id.in_(missing))
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
            )
            update_set = {
                "meeting_id": meeting_id,
                "number": stmt.excluded.number,
                "order": stmt.excluded.order,
                "name": stmt.excluded.name,
                "public": stmt.excluded.public,
                "result": stmt.excluded.result,
                "resolution_text": stmt.excluded.resolution_text,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlAgendaItem.id)

            result = await session.execute(stmt)
            item_id = result.scalar_one()
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
                "size": stmt.excluded.size,
                "access_url": stmt.excluded.access_url,
                "download_url": stmt.excluded.download_url,
                "file_date": stmt.excluded.file_date,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
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
            await session.commit()
            return file_id

    # ========== Location Operations ==========

    async def upsert_location(
        self,
        location: ProcessedLocation,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a location."""
        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlLocation.id)

            result = await session.execute(stmt)
            location_id = result.scalar_one()
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
            update_set = {
                "paper_id": paper_id,
                "paper_external_id": stmt.excluded.paper_external_id,
                "meeting_external_id": stmt.excluded.meeting_external_id,
                "agenda_item_external_id": stmt.excluded.agenda_item_external_id,
                "role": stmt.excluded.role,
                "authoritative": stmt.excluded.authoritative,
                "oparl_created": stmt.excluded.oparl_created,
                "oparl_modified": stmt.excluded.oparl_modified,
                "raw_json": stmt.excluded.raw_json,
                # Quelle liefert das Objekt wieder regulaer -> Tombstone aufheben
                "deleted": False,
                "deleted_at": None,
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlConsultation.id)

            result = await session.execute(stmt)
            consultation_id = result.scalar_one()
            await session.commit()
            return consultation_id

    # ========== Membership Operations ==========

    async def upsert_membership(
        self,
        membership: ProcessedMembership,
        body_id: UUID,
    ) -> UUID | None:
        """Insert or update a membership. Returns None if FKs can't be resolved."""
        # Resolve person and organization UUIDs (both required by Django schema).
        # Cache-first with targeted DB fallback (no global cache pre-load).
        person_id = None
        organization_id = None

        if membership.person_external_id:
            person_map = await self.get_person_ids_by_external_ids(
                [membership.person_external_id]
            )
            person_id = person_map.get(membership.person_external_id)

        if membership.organization_external_id:
            org_map = await self.get_organization_ids_by_external_ids(
                [membership.organization_external_id]
            )
            organization_id = org_map.get(membership.organization_external_id)

        # Both FKs are NOT NULL in Django schema - skip if unresolved
        if not person_id or not organization_id:
            return None

        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlMembership.id)

            result = await session.execute(stmt)
            membership_id = result.scalar_one()
            await session.commit()
            return membership_id

    # ========== Legislative Term Operations ==========

    async def upsert_legislative_term(
        self,
        term: ProcessedLegislativeTerm,
        body_id: UUID,
    ) -> UUID:
        """Insert or update a legislative term."""
        async with self.get_session() as session:
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
                "updated_at": func.now(),
            }
            _assert_no_enrichment_overwrite(update_set)
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_=update_set,
            ).returning(OParlLegislativeTerm.id)

            result = await session.execute(stmt)
            term_id = result.scalar_one()
            await session.commit()
            return term_id

    # ========== Generic Entity Dispatcher ==========

    async def upsert_entity(
        self,
        entity: ProcessedEntity,
        body_id: UUID,
    ) -> UUID | None:
        """
        Generic upsert that dispatches to the correct handler.

        Args:
            entity: The processed entity to upsert
            body_id: The body UUID this entity belongs to

        Returns:
            The entity UUID or None if type not supported
        """
        if isinstance(entity, ProcessedMeeting):
            return await self.upsert_meeting(entity, body_id)
        elif isinstance(entity, ProcessedPaper):
            return await self.upsert_paper(entity, body_id)
        elif isinstance(entity, ProcessedPerson):
            return await self.upsert_person(entity, body_id)
        elif isinstance(entity, ProcessedOrganization):
            return await self.upsert_organization(entity, body_id)
        elif isinstance(entity, ProcessedFile):
            return await self.upsert_file(entity, body_id)
        elif isinstance(entity, ProcessedLocation):
            return await self.upsert_location(entity, body_id)
        elif isinstance(entity, ProcessedMembership):
            return await self.upsert_membership(entity, body_id)
        elif isinstance(entity, ProcessedLegislativeTerm):
            return await self.upsert_legislative_term(entity, body_id)

        return None

    # ========== Batch Operations ==========

    async def upsert_entities_batch(
        self,
        entities: list[ProcessedEntity],
        body_id: UUID,
    ) -> int:
        """
        Upsert multiple entities in a batch.

        Returns the number of entities processed.
        """
        count = 0
        for entity in entities:
            result = await self.upsert_entity(entity, body_id)
            if result:
                count += 1
        return count

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

    async def get_bodies_for_source(self, source_id: UUID) -> list[OParlBody]:
        """Get all bodies for a source."""
        async with self.get_session() as session:
            stmt = (
                select(OParlBody)
                .where(OParlBody.source_id == source_id)
                .order_by(OParlBody.name)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ========== Text Extraction Queries ==========

    # Files stuck in 'processing' longer than this are considered abandoned
    # (worker crash) and become claimable again.
    PROCESSING_STALE_AFTER = timedelta(hours=2)

    async def get_pending_files(
        self,
        body_id: UUID,
        batch_size: int = 100,
        max_size_bytes: int | None = None,
    ) -> list[OParlFile]:
        """
        Atomically CLAIM files pending text extraction (multi-worker safe).

        Candidate rows are selected with FOR UPDATE SKIP LOCKED and their
        text_extraction_status is set to 'processing' in one atomic
        UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING
        statement, so two concurrent extraction workers (e.g. server + local
        PC) never claim the same file twice.

        The extractor later overwrites the transient 'processing' status with
        completed/failed/skipped via update_file_text. Crash recovery: rows
        stuck in 'processing' with updated_at older than PROCESSING_STALE_AFTER
        are treated as claimable again.

        Args:
            body_id: Body to query files for
            batch_size: Maximum number of files to claim
            max_size_bytes: Skip files larger than this (optional)
        """
        stale_cutoff = datetime.now(UTC) - self.PROCESSING_STALE_AFTER

        async with self.get_session() as session:
            candidates = (
                select(OParlFile.id)
                .where(
                    OParlFile.body_id == body_id,
                    # Keine Textextraktion fuer von der Quelle geloeschte Dateien
                    OParlFile.deleted == False,  # noqa: E712
                    or_(
                        OParlFile.text_extraction_status == "pending",
                        and_(
                            OParlFile.text_extraction_status == "processing",
                            OParlFile.updated_at < stale_cutoff,
                        ),
                    ),
                    or_(
                        OParlFile.download_url.isnot(None),
                        OParlFile.access_url.isnot(None),
                    ),
                )
            )

            if max_size_bytes is not None:
                candidates = candidates.where(
                    or_(
                        OParlFile.size.is_(None),
                        OParlFile.size <= max_size_bytes,
                    )
                )

            candidates = (
                candidates.order_by(OParlFile.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )

            claim_stmt = (
                update(OParlFile)
                .where(OParlFile.id.in_(candidates.scalar_subquery()))
                .values(
                    text_extraction_status="processing",
                    updated_at=func.now(),
                )
                .returning(OParlFile)
            )

            result = await session.execute(claim_stmt)
            files = list(result.scalars().all())
            await session.commit()
            return files

    async def update_file_text(
        self,
        file_id: UUID,
        text_content: str | None = None,
        method: str | None = None,
        status: str = "completed",
        error: str | None = None,
        page_count: int | None = None,
        sha256_hash: str | None = None,
    ) -> None:
        """Update a file with text extraction results."""
        from datetime import datetime

        async with self.get_session() as session:
            values: dict = {
                "text_extraction_status": status,
                "updated_at": func.now(),
            }
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

            stmt = update(OParlFile).where(OParlFile.id == file_id).values(**values)
            await session.execute(stmt)
            await session.commit()

    # ========== Search Indexing Query Helpers ==========

    async def get_all_for_body(
        self,
        body_id: UUID,
        model_class: type,
        limit: int = 10000,
    ) -> list:
        """Generic query: all non-deleted entities of a type for a body."""
        async with self.get_session() as session:
            stmt = (
                select(model_class)
                .where(
                    model_class.body_id == body_id,
                    model_class.deleted == False,  # noqa: E712
                )
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_files_with_text(self, body_id: UUID) -> list[OParlFile]:
        """Get files that have extracted text content."""
        async with self.get_session() as session:
            stmt = (
                select(OParlFile)
                .where(
                    OParlFile.body_id == body_id,
                    OParlFile.deleted == False,  # noqa: E712
                    OParlFile.text_content.isnot(None),
                    OParlFile.text_extraction_status == "completed",
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
