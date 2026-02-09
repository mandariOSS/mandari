"""
PostgreSQL Storage for OParl Data

High-performance async database operations with proper upsert support.
Uses PostgreSQL ON CONFLICT for efficient insert-or-update operations.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from rich.console import Console

from src.config import settings
from src.storage.models import (
    Base,
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
from mandari_oparl import (
    OParlType,
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

console = Console()


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
            stmt = stmt.on_conflict_do_update(
                index_elements=["url"],
                set_={
                    "name": stmt.excluded.name,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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
                now = datetime.now(timezone.utc)
                source.last_sync = now
                if full_sync:
                    source.last_full_sync = now
                await session.commit()

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "name": stmt.excluded.name,
                    "short_name": stmt.excluded.short_name,
                    "website": stmt.excluded.website,
                    "license": stmt.excluded.license,
                    "classification": stmt.excluded.classification,
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
                    "updated_at": func.now(),
                },
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
                body.last_sync = datetime.now(timezone.utc)
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

    async def delete_entity(
        self,
        entity_type: str,
        external_id: str,
    ) -> bool:
        """
        Delete an entity by external ID.

        Used when OParl servers return items with deleted=true
        (Bonn, Aachen, Köln, ITK Rheinland support this).

        Args:
            entity_type: Type of entity
            external_id: The OParl external ID

        Returns:
            True if entity was deleted, False if not found
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
            return False

        async with self.get_session() as session:
            stmt = delete(model).where(model.external_id == external_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
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
                    "updated_at": func.now(),
                },
            ).returning(OParlMeeting.id)

            result = await session.execute(stmt)
            meeting_id = result.scalar_one()
            await session.commit()

            self._meeting_uuid_cache[meeting.external_id] = meeting_id

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "name": stmt.excluded.name,
                    "reference": stmt.excluded.reference,
                    "paper_type": stmt.excluded.paper_type,
                    "date": stmt.excluded.date,
                    "oparl_created": stmt.excluded.oparl_created,
                    "oparl_modified": stmt.excluded.oparl_modified,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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

            return paper_id

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
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
                    "updated_at": func.now(),
                },
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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
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
                    "updated_at": func.now(),
                },
            ).returning(OParlOrganization.id)

            result = await session.execute(stmt)
            org_id = result.scalar_one()
            await session.commit()

            self._organization_uuid_cache[org.external_id] = org_id
            return org_id

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
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
                    "updated_at": func.now(),
                },
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
                "updated_at": func.now(),
            }

            # Only update paper_id if provided (don't overwrite existing link)
            if paper_id is not None:
                update_set["paper_id"] = paper_id
            # Only update meeting_id if provided
            if meeting_id is not None:
                update_set["meeting_id"] = meeting_id

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "description": stmt.excluded.description,
                    "street_address": stmt.excluded.street_address,
                    "room": stmt.excluded.room,
                    "postal_code": stmt.excluded.postal_code,
                    "locality": stmt.excluded.locality,
                    "geojson": stmt.excluded.geojson,
                    "oparl_created": stmt.excluded.oparl_created,
                    "oparl_modified": stmt.excluded.oparl_modified,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "paper_id": paper_id,
                    "paper_external_id": stmt.excluded.paper_external_id,
                    "meeting_external_id": stmt.excluded.meeting_external_id,
                    "agenda_item_external_id": stmt.excluded.agenda_item_external_id,
                    "role": stmt.excluded.role,
                    "authoritative": stmt.excluded.authoritative,
                    "oparl_created": stmt.excluded.oparl_created,
                    "oparl_modified": stmt.excluded.oparl_modified,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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
        # Resolve person and organization UUIDs (both required by Django schema)
        person_id = None
        organization_id = None

        if membership.person_external_id:
            if membership.person_external_id in self._person_uuid_cache:
                person_id = self._person_uuid_cache[membership.person_external_id]

        if membership.organization_external_id:
            if membership.organization_external_id in self._organization_uuid_cache:
                organization_id = self._organization_uuid_cache[membership.organization_external_id]

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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "person_id": person_id,
                    "organization_id": organization_id,
                    "role": stmt.excluded.role,
                    "voting_right": stmt.excluded.voting_right,
                    "start_date": stmt.excluded.start_date,
                    "end_date": stmt.excluded.end_date,
                    "oparl_created": stmt.excluded.oparl_created,
                    "oparl_modified": stmt.excluded.oparl_modified,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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
            stmt = stmt.on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "name": stmt.excluded.name,
                    "start_date": stmt.excluded.start_date,
                    "end_date": stmt.excluded.end_date,
                    "oparl_created": stmt.excluded.oparl_created,
                    "oparl_modified": stmt.excluded.oparl_modified,
                    "raw_json": stmt.excluded.raw_json,
                    "updated_at": func.now(),
                },
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

    async def get_pending_files(
        self,
        body_id: UUID,
        batch_size: int = 100,
        max_size_bytes: int | None = None,
    ) -> list[OParlFile]:
        """
        Get files pending text extraction.

        Args:
            body_id: Body to query files for
            batch_size: Maximum number of files to return
            max_size_bytes: Skip files larger than this (optional)
        """
        async with self.get_session() as session:
            stmt = (
                select(OParlFile)
                .where(
                    OParlFile.body_id == body_id,
                    OParlFile.text_extraction_status == "pending",
                    or_(
                        OParlFile.download_url.isnot(None),
                        OParlFile.access_url.isnot(None),
                    ),
                )
            )

            if max_size_bytes is not None:
                stmt = stmt.where(
                    or_(
                        OParlFile.size.is_(None),
                        OParlFile.size <= max_size_bytes,
                    )
                )

            stmt = stmt.order_by(OParlFile.created_at).limit(batch_size)
            result = await session.execute(stmt)
            return list(result.scalars().all())

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
        from datetime import datetime, timezone as tz

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
                values["text_extracted_at"] = datetime.now(tz.utc)

            stmt = update(OParlFile).where(OParlFile.id == file_id).values(**values)
            await session.execute(stmt)
            await session.commit()

    # ========== Meilisearch Query Helpers ==========

    async def get_all_for_body(
        self,
        body_id: UUID,
        model_class: type,
        limit: int = 10000,
    ) -> list:
        """Generic query: all entities of a type for a body."""
        async with self.get_session() as session:
            stmt = (
                select(model_class)
                .where(model_class.body_id == body_id)
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
                    OParlFile.text_content.isnot(None),
                    OParlFile.text_extraction_status == "completed",
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
