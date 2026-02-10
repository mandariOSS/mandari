"""
Django Signals für automatische Meilisearch-Indexierung.

Aktualisiert die Suchindizes automatisch bei Änderungen an OParl-Modellen.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from .services.search_documents import (
    file_to_doc as _file_to_doc,
    meeting_to_doc as _meeting_to_doc,
    organization_to_doc as _organization_to_doc,
    paper_to_doc as _paper_to_doc,
    person_to_doc as _person_to_doc,
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
    Aktualisiert auch das Parent-Paper (file_contents_preview).
    """
    # Nur indexieren wenn Text vorhanden
    if not instance.text_content:
        return

    doc = _file_to_doc(instance)
    _index_document("files", str(instance.id), doc)

    # Re-index parent paper so file_contents_preview stays current
    if instance.paper_id:
        try:
            paper = OParlPaper.objects.get(id=instance.paper_id)
            files = paper.files.filter(
                text_content__isnull=False,
                text_extraction_status="completed",
            )
            paper_doc = _paper_to_doc(paper, files=files)
            _index_document("papers", str(paper.id), paper_doc)
        except OParlPaper.DoesNotExist:
            pass


@receiver(post_delete, sender=OParlFile)
def delete_file(sender, instance, **kwargs):
    """Löscht eine Datei aus dem Index."""
    _delete_document("files", str(instance.id))
