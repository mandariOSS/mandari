"""
Search Indexer

Indexes data from the database into Meilisearch.
Run this after ingesting OParl data to make it searchable.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings
from src.oparl.models import OParlFile, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson
from src.search.service import (
    INDEX_FILES,
    INDEX_MEETINGS,
    INDEX_ORGANIZATIONS,
    INDEX_PAPERS,
    INDEX_PERSONS,
    get_search_service,
)

logger = logging.getLogger(__name__)


def meeting_to_doc(meeting: OParlMeeting) -> dict[str, Any]:
    """Convert meeting to search document."""
    return {
        "id": str(meeting.id),
        "name": meeting.name or "",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "cancelled": meeting.cancelled or False,
        "location_name": meeting.location_name,
        "location_address": meeting.location_address,
        "body_id": str(meeting.body_id) if meeting.body_id else None,
        "type": "meeting",
    }


def paper_to_doc(paper: OParlPaper) -> dict[str, Any]:
    """Convert paper to search document."""
    return {
        "id": str(paper.id),
        "name": paper.name or "",
        "reference": paper.reference,
        "paper_type": paper.paper_type,
        "date": paper.date.isoformat() if paper.date else None,
        "summary": paper.summary,
        "body_id": str(paper.body_id) if paper.body_id else None,
        "type": "paper",
    }


def person_to_doc(person: OParlPerson) -> dict[str, Any]:
    """Convert person to search document."""
    # Handle email which can be string or list
    email = person.email
    if isinstance(email, list):
        email = email[0] if email else None

    return {
        "id": str(person.id),
        "name": person.name or "",
        "family_name": person.family_name,
        "given_name": person.given_name,
        "title": person.title[0] if isinstance(person.title, list) and person.title else person.title,
        "email": email,
        "gender": person.gender,
        "body_id": str(person.body_id) if person.body_id else None,
        "type": "person",
    }


def organization_to_doc(org: OParlOrganization) -> dict[str, Any]:
    """Convert organization to search document."""
    return {
        "id": str(org.id),
        "name": org.name or "",
        "short_name": org.short_name,
        "organization_type": org.organization_type,
        "classification": org.classification,
        "start_date": org.start_date.isoformat() if org.start_date else None,
        "end_date": org.end_date.isoformat() if org.end_date else None,
        "body_id": str(org.body_id) if org.body_id else None,
        "type": "organization",
    }


def file_to_doc(file: OParlFile, paper: OParlPaper | None = None) -> dict[str, Any]:
    """Convert file to search document."""
    # Create text preview (first 500 chars)
    text_preview = ""
    if file.text_content:
        text_preview = file.text_content[:500].strip()
        if len(file.text_content) > 500:
            text_preview += "..."

    return {
        "id": str(file.id),
        "name": file.name or "",
        "file_name": file.file_name,
        "mime_type": file.mime_type,
        "size": file.size,
        "text_content": file.text_content or "",
        "text_preview": text_preview,
        "paper_id": str(file.paper_id) if file.paper_id else None,
        "paper_name": paper.name if paper else None,
        "paper_reference": paper.reference if paper else None,
        "body_id": str(paper.body_id) if paper and paper.body_id else None,
        "created": file.oparl_created.isoformat() if file.oparl_created else None,
        "type": "file",
    }


async def index_all(batch_size: int = 500) -> dict[str, int]:
    """
    Index all data from the database into Meilisearch.

    Args:
        batch_size: Number of documents to index at once

    Returns:
        Dict with counts of indexed documents per type
    """
    search_service = get_search_service()

    # Initialize indexes
    logger.info("Initializing Meilisearch indexes...")
    search_service.initialize_indexes()

    # Create database session
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    counts = {
        "meetings": 0,
        "papers": 0,
        "persons": 0,
        "organizations": 0,
        "files": 0,
    }

    async with async_session() as session:
        # Index meetings
        logger.info("Indexing meetings...")
        result = await session.execute(select(OParlMeeting))
        meetings = result.scalars().all()
        if meetings:
            docs = [meeting_to_doc(m) for m in meetings]
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                search_service.index_documents(INDEX_MEETINGS, batch)
            counts["meetings"] = len(meetings)
            logger.info(f"Indexed {len(meetings)} meetings")

        # Index papers
        logger.info("Indexing papers...")
        result = await session.execute(select(OParlPaper))
        papers = result.scalars().all()
        if papers:
            docs = [paper_to_doc(p) for p in papers]
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                search_service.index_documents(INDEX_PAPERS, batch)
            counts["papers"] = len(papers)
            logger.info(f"Indexed {len(papers)} papers")

        # Index persons
        logger.info("Indexing persons...")
        result = await session.execute(select(OParlPerson))
        persons = result.scalars().all()
        if persons:
            docs = [person_to_doc(p) for p in persons]
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                search_service.index_documents(INDEX_PERSONS, batch)
            counts["persons"] = len(persons)
            logger.info(f"Indexed {len(persons)} persons")

        # Index organizations
        logger.info("Indexing organizations...")
        result = await session.execute(select(OParlOrganization))
        orgs = result.scalars().all()
        if orgs:
            docs = [organization_to_doc(o) for o in orgs]
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                search_service.index_documents(INDEX_ORGANIZATIONS, batch)
            counts["organizations"] = len(orgs)
            logger.info(f"Indexed {len(orgs)} organizations")

        # Index files (only those with text_content)
        logger.info("Indexing files with extracted text...")
        result = await session.execute(
            select(OParlFile)
            .where(OParlFile.text_content.isnot(None))
            .where(OParlFile.text_content != "")
        )
        files = result.scalars().all()
        if files:
            # Build paper lookup for efficiency
            paper_ids = {f.paper_id for f in files if f.paper_id}
            papers_result = await session.execute(
                select(OParlPaper).where(OParlPaper.id.in_(paper_ids))
            )
            papers_lookup = {p.id: p for p in papers_result.scalars().all()}

            docs = [file_to_doc(f, papers_lookup.get(f.paper_id)) for f in files]
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                search_service.index_documents(INDEX_FILES, batch)
            counts["files"] = len(files)
            logger.info(f"Indexed {len(files)} files with text content")

    await engine.dispose()

    logger.info(f"Indexing complete: {counts}")
    return counts


def main() -> None:
    """CLI entry point for indexing."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    print("Starting Meilisearch indexing...")
    counts = asyncio.run(index_all())
    print(f"\nIndexing complete!")
    print(f"  Meetings: {counts['meetings']}")
    print(f"  Papers: {counts['papers']}")
    print(f"  Persons: {counts['persons']}")
    print(f"  Organizations: {counts['organizations']}")
    print(f"  Files: {counts['files']}")


if __name__ == "__main__":
    main()
