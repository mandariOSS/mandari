"""
Entity -> Elasticsearch Document Converters.

Teildokumente: Die vollständigen Suchdokumente baut Django
(mandari/insight_core/services/search_documents.py). Der Ingestor liefert nur
die Felder, die er aus seinen eigenen Zeilen kennt, und schreibt sie per
partiellem Update (siehe elasticsearch.py). Felder, die allein Django setzt,
dürfen hier nicht auftauchen, sonst überschriebe jeder Sync die Werte des
Portals (Issue #429): siehe ``DJANGO_ONLY_FIELDS``.
"""

from __future__ import annotations

from typing import Any

# Felder, die nur die Portal-Indexierung (Django) setzt. Der Ingestor lässt sie weg;
# das partielle Update erhält sie in vorhandenen Dokumenten.
DJANGO_ONLY_FIELDS = frozenset(
    {
        "organization_names",  # Gremienfilter (Vorlagen über Beratungen, Sitzungen über M2M, Dateien über Kontext)
        "access_url",
        "paper_name",
        "paper_reference",
        "meeting_name",
        "meeting_date",
        "agenda_number",
    }
)


def _with_name(doc: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Name nur setzen, wenn vorhanden: Für namenlose Sitzungen/Personen bildet Django einen Ersatznamen."""
    if name:
        doc["name"] = name
    return doc


def paper_to_doc(paper, files=None) -> dict[str, Any]:
    """Convert a Paper SQLAlchemy row to a partial Elasticsearch document (organization_names kommt von Django).

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

    for f in files or []:
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
    """Convert a Meeting SQLAlchemy row to a partial Elasticsearch document.

    organization_names und der Ersatzname namenloser Sitzungen kommen von Django.
    """
    doc = {
        "id": str(meeting.id),
        "type": "meeting",
        "body_id": str(meeting.body_id) if meeting.body_id else None,
        "location_name": meeting.location_name or "",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "cancelled": meeting.cancelled,
        "oparl_modified": meeting.oparl_modified.isoformat() if meeting.oparl_modified else None,
    }
    return _with_name(doc, meeting.name)


def person_to_doc(person) -> dict[str, Any]:
    """Convert a Person SQLAlchemy row to a partial Elasticsearch document (Ersatzname kommt von Django)."""
    doc = {
        "id": str(person.id),
        "type": "person",
        "body_id": str(person.body_id) if person.body_id else None,
        "given_name": person.given_name or "",
        "family_name": person.family_name or "",
        "title": person.title or "",
        "oparl_modified": person.oparl_modified.isoformat() if person.oparl_modified else None,
    }
    return _with_name(doc, person.name)


def organization_to_doc(org) -> dict[str, Any]:
    """Convert an Organization SQLAlchemy row to a Elasticsearch document."""
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
    """Convert a File SQLAlchemy row to a partial Elasticsearch document.

    access_url, paper_name/paper_reference und der Kontext (Gremien, Sitzung, TOP)
    kommen von Django und bleiben beim partiellen Update erhalten.
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
