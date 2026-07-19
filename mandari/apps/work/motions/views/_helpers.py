# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging
import uuid

from django.shortcuts import get_object_or_404

logger = logging.getLogger("apps.work.motions")


from ..models import (
    DocumentFolder,
)


def _flatten_folder_tree(organization):
    """
    Alle Ordner der Organisation als Vorordnungs-Liste [(folder, depth)].

    Grundlage für die Ordner-Spalte in der Dokumentliste und die
    "Verschieben nach"-Dropdowns (Einrückung über depth).
    """
    folders = list(DocumentFolder.objects.filter(organization=organization))
    children_map = {}
    for folder in folders:
        children_map.setdefault(folder.parent_id, []).append(folder)
    for siblings in children_map.values():
        siblings.sort(key=lambda f: (f.position, f.name.lower()))

    result = []

    def walk(parent_id, depth):
        for folder in children_map.get(parent_id, []):
            result.append((folder, depth))
            walk(folder.id, depth + 1)

    walk(None, 1)
    return result


def _get_org_folder_or_404(organization, folder_id):
    """Ordner org-gebunden laden; ungültige IDs und fremde Ordner → 404."""
    from django.http import Http404

    try:
        folder_uuid = uuid.UUID(str(folder_id))
    except (ValueError, AttributeError):
        raise Http404("Ordner nicht gefunden")
    return get_object_or_404(DocumentFolder, id=folder_uuid, organization=organization)


def _broadcast_doc_reload(motion, version=None):
    """
    Alle verbundenen Kollaborations-Clients zum Neuladen auffordern.

    Nutzt das bestehende doc.reload-Event der Channel-Layer-Gruppe
    (DocumentCollaborationConsumer.doc_reload) — z. B. nach Revision-Restore
    oder Statuswechsel in einen gesperrten Status (Clients laden neu und
    erhalten dabei ihre ggf. herabgestufte Zugriffsstufe). Best-Effort:
    Fehler beim Broadcast brechen die aufrufende Aktion nicht ab.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f"doc_{motion.id}",
                {"type": "doc.reload", "version": version},
            )
    except Exception as e:
        logger.warning(f"[DocumentEditor] Reload-Broadcast fehlgeschlagen: {e}")


def _can_manage_folder(membership, folder) -> bool:
    """
    Ordner verwalten (umbenennen/verschieben/löschen): eigene Ordner mit dem
    bestehenden Dokumente-Erstellrecht, fremde nur mit Organisationsverwaltung.
    Bewusst KEINE neue Berechtigung — konsistent zu motions.*/organization.edit.
    """
    if membership.has_permission("organization.edit"):
        return True
    return folder.created_by_id == membership.id and membership.has_permission("motions.create")
