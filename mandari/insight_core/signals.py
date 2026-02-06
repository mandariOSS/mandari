"""
Django Signals für automatische Meilisearch-Indexierung.

Aktualisiert die Suchindizes automatisch bei Änderungen an OParl-Modellen.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import (
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

logger = logging.getLogger(__name__)


def is_auto_indexing_enabled() -> bool:
    """Prüft ob automatische Indexierung aktiviert ist."""
    return getattr(settings, "MEILISEARCH_AUTO_INDEX", True)


def _get_meilisearch_client():
    """Gibt den Meilisearch-Client zurück."""
    try:
        import meilisearch
        url = getattr(settings, "MEILISEARCH_URL", "http://localhost:7700")
        key = getattr(settings, "MEILISEARCH_KEY", "")
        return meilisearch.Client(url, key)
    except ImportError:
        logger.warning("meilisearch nicht installiert")
        return None
    except Exception as e:
        logger.warning(f"Meilisearch-Client Fehler: {e}")
        return None


def _paper_to_doc(paper: OParlPaper) -> dict[str, Any]:
    """Konvertiert ein Paper zu einem Meilisearch-Dokument."""
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
    }


def _meeting_to_doc(meeting: OParlMeeting) -> dict[str, Any]:
    """Konvertiert ein Meeting zu einem Meilisearch-Dokument."""
    org_names = [org.name for org in meeting.organizations.all() if org.name]
    return {
        "id": str(meeting.id),
        "type": "meeting",
        "body_id": str(meeting.body_id) if meeting.body_id else None,
        "name": meeting.name or meeting.get_display_name(),
        "organization_names": org_names,
        "location_name": meeting.location_name or "",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "cancelled": meeting.cancelled,
        "oparl_modified": meeting.oparl_modified.isoformat() if meeting.oparl_modified else None,
    }


def _person_to_doc(person: OParlPerson) -> dict[str, Any]:
    """Konvertiert eine Person zu einem Meilisearch-Dokument."""
    return {
        "id": str(person.id),
        "type": "person",
        "body_id": str(person.body_id) if person.body_id else None,
        "name": person.name or person.display_name,
        "given_name": person.given_name or "",
        "family_name": person.family_name or "",
        "title": person.title or "",
        "oparl_modified": person.oparl_modified.isoformat() if person.oparl_modified else None,
    }


def _organization_to_doc(org: OParlOrganization) -> dict[str, Any]:
    """Konvertiert eine Organization zu einem Meilisearch-Dokument."""
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


def _file_to_doc(file: OParlFile) -> dict[str, Any]:
    """Konvertiert eine File zu einem Meilisearch-Dokument."""
    # Text für Vorschau kürzen
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
        "paper_name": file.paper.name if file.paper else None,
        "paper_reference": file.paper.reference if file.paper else None,
        "meeting_id": str(file.meeting_id) if file.meeting_id else None,
        "oparl_modified": file.oparl_modified.isoformat() if file.oparl_modified else None,
    }


def _index_document(index_name: str, doc_id: str, document: dict[str, Any]):
    """Indexiert ein Dokument in Meilisearch."""
    if not is_auto_indexing_enabled():
        return

    client = _get_meilisearch_client()
    if not client:
        return

    try:
        index = client.index(index_name)
        index.add_documents([document], primary_key="id")
        logger.debug(f"Dokument indexiert: {index_name}/{doc_id}")
    except Exception as e:
        logger.warning(f"Indexierung fehlgeschlagen für {index_name}/{doc_id}: {e}")


def _delete_document(index_name: str, doc_id: str):
    """Löscht ein Dokument aus Meilisearch."""
    if not is_auto_indexing_enabled():
        return

    client = _get_meilisearch_client()
    if not client:
        return

    try:
        index = client.index(index_name)
        index.delete_document(doc_id)
        logger.debug(f"Dokument gelöscht: {index_name}/{doc_id}")
    except Exception as e:
        logger.warning(f"Löschung fehlgeschlagen für {index_name}/{doc_id}: {e}")


# =============================================================================
# Signal Handlers
# =============================================================================

@receiver(post_save, sender=OParlPaper)
def index_paper(sender, instance, **kwargs):
    """Indexiert einen Vorgang nach dem Speichern."""
    doc = _paper_to_doc(instance)
    _index_document("papers", str(instance.id), doc)


@receiver(post_delete, sender=OParlPaper)
def delete_paper(sender, instance, **kwargs):
    """Löscht einen Vorgang aus dem Index."""
    _delete_document("papers", str(instance.id))


@receiver(post_save, sender=OParlMeeting)
def index_meeting(sender, instance, **kwargs):
    """Indexiert eine Sitzung nach dem Speichern."""
    doc = _meeting_to_doc(instance)
    _index_document("meetings", str(instance.id), doc)


@receiver(post_delete, sender=OParlMeeting)
def delete_meeting(sender, instance, **kwargs):
    """Löscht eine Sitzung aus dem Index."""
    _delete_document("meetings", str(instance.id))


@receiver(post_save, sender=OParlPerson)
def index_person(sender, instance, **kwargs):
    """Indexiert eine Person nach dem Speichern."""
    doc = _person_to_doc(instance)
    _index_document("persons", str(instance.id), doc)


@receiver(post_delete, sender=OParlPerson)
def delete_person(sender, instance, **kwargs):
    """Löscht eine Person aus dem Index."""
    _delete_document("persons", str(instance.id))


@receiver(post_save, sender=OParlOrganization)
def index_organization(sender, instance, **kwargs):
    """Indexiert ein Gremium nach dem Speichern."""
    doc = _organization_to_doc(instance)
    _index_document("organizations", str(instance.id), doc)


@receiver(post_delete, sender=OParlOrganization)
def delete_organization(sender, instance, **kwargs):
    """Löscht ein Gremium aus dem Index."""
    _delete_document("organizations", str(instance.id))


@receiver(post_save, sender=OParlFile)
def index_file(sender, instance, **kwargs):
    """
    Indexiert eine Datei nach dem Speichern.

    Nur wenn text_content vorhanden ist.
    """
    # Nur indexieren wenn Text vorhanden
    if not instance.text_content:
        return

    doc = _file_to_doc(instance)
    _index_document("files", str(instance.id), doc)


@receiver(post_delete, sender=OParlFile)
def delete_file(sender, instance, **kwargs):
    """Löscht eine Datei aus dem Index."""
    _delete_document("files", str(instance.id))
