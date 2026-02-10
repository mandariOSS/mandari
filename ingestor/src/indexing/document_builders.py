"""
Entity -> Meilisearch Document Converters.

Mirrors the logic in mandari/insight_core/signals.py but works with
SQLAlchemy model instances from the ingestor's storage layer.
"""

from __future__ import annotations

from typing import Any


def paper_to_doc(paper, files=None) -> dict[str, Any]:
    """Convert a Paper SQLAlchemy row to a Meilisearch document.

    Args:
        paper: Paper SQLAlchemy row.
        files: Optional list of File rows with text_content.
               If None, file_contents_preview will be empty (no ORM auto-fetch in ingestor).
    """
    file_names: list[str] = []
    file_texts: list[str] = []
    total_len = 0
    max_per_file = 5000
    max_total = 25000

    for f in (files or []):
        if f.file_name:
            file_names.append(f.file_name)
        if f.text_content and total_len < max_total:
            chunk = f.text_content[:max_per_file].strip()
            file_texts.append(chunk)
            total_len += len(chunk)

    file_contents_preview = "\n\n".join(file_texts)
    if len(file_contents_preview) > max_total:
        file_contents_preview = file_contents_preview[:max_total]

    return {
        "id": str(paper.id),
        "type": "paper",
        "body_id": str(paper.body_id) if paper.body_id else None,
        "name": paper.name or "",
        "reference": paper.reference or "",
        "paper_type": paper.paper_type or "",
        "date": paper.date.isoformat() if paper.date else None,
        "oparl_created": paper.oparl_created.isoformat() if paper.oparl_created else None,
        "oparl_modified": paper.oparl_modified.isoformat() if paper.oparl_modified else None,
        "file_contents_preview": file_contents_preview,
        "file_names": file_names,
    }


def meeting_to_doc(meeting) -> dict[str, Any]:
    """Convert a Meeting SQLAlchemy row to a Meilisearch document.

    Note: organization_names is not available here (M2M only in Django).
    """
    return {
        "id": str(meeting.id),
        "type": "meeting",
        "body_id": str(meeting.body_id) if meeting.body_id else None,
        "name": meeting.name or "",
        "location_name": meeting.location_name or "",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "cancelled": meeting.cancelled,
        "oparl_modified": meeting.oparl_modified.isoformat() if meeting.oparl_modified else None,
    }


def person_to_doc(person) -> dict[str, Any]:
    """Convert a Person SQLAlchemy row to a Meilisearch document."""
    return {
        "id": str(person.id),
        "type": "person",
        "body_id": str(person.body_id) if person.body_id else None,
        "name": person.name or "",
        "given_name": person.given_name or "",
        "family_name": person.family_name or "",
        "title": person.title or "",
        "oparl_modified": person.oparl_modified.isoformat() if person.oparl_modified else None,
    }


def organization_to_doc(org) -> dict[str, Any]:
    """Convert an Organization SQLAlchemy row to a Meilisearch document."""
    return {
        "id": str(org.id),
        "type": "organization",
        "body_id": str(org.body_id) if org.body_id else None,
        "name": org.name or "",
        "short_name": org.short_name or "",
        "organization_type": org.organization_type or "",
        "classification": org.classification or "",
        "oparl_modified": org.oparl_modified.isoformat() if org.oparl_modified else None,
    }


def file_to_doc(file) -> dict[str, Any]:
    """Convert a File SQLAlchemy row to a Meilisearch document.

    Note: paper_name/paper_reference not available here (needs Django JOIN).
    The Django reindex_meilisearch command fills those fields.
    """
    text_preview = ""
    if file.text_content:
        text_preview = file.text_content[:500].strip()
        if len(file.text_content) > 500:
            text_preview += "..."

    return {
        "id": str(file.id),
        "type": "file",
        "body_id": str(file.body_id) if file.body_id else None,
        "name": file.name or "",
        "file_name": file.file_name or "",
        "mime_type": file.mime_type or "",
        "text_content": file.text_content or "",
        "text_preview": text_preview,
        "paper_id": str(file.paper_id) if file.paper_id else None,
        "meeting_id": str(file.meeting_id) if file.meeting_id else None,
        "oparl_modified": file.oparl_modified.isoformat() if file.oparl_modified else None,
    }
