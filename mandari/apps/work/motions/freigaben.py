# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Freigaben an Dokumenten: Herunterladen durch Gäste und Entzug in geöffneten Bearbeitungen (Issue #582).

Herunterladen
    Gäste exportieren ab der Stufe „Lesen“ (PDF, DOCX) und laden Anhänge herunter, solange eine ihrer wirksamen
    Freigaben das erlaubt (``Motion.can_download``, Schalter ``allow_download`` je Freigabe). Jeder Download
    eines Gastes steht in der Änderungshistorie der Organisation (``FactionAuditLog``, Hash-Kette).

Entzug wirkt sofort
    Der Kollaborations-Consumer prüft den Zugriff beim Verbinden. Damit ein Entzug auch eine schon geöffnete
    Bearbeitung erreicht, melden Signale jede Änderung, die Zugriff nehmen kann, an die offenen Verbindungen:

    - je Dokument (Gruppe ``doc_<id>``): persönliche Freigabe geändert oder entfernt, Sichtbarkeit, Ordner,
      Autor:in, Federführung, Status oder Mitarbeit geändert, Dokument gelöscht;
    - je Person (Gruppe ``collab_user_<id>``): Ordner-Freigabe geändert oder entfernt, Mitgliedschaft
      deaktiviert, geändert oder entfernt, Rollen oder Einzelrechte geändert, Rechte einer Rolle geändert;
      Ordner verschoben (alle Personen mit Ordner-Freigaben der Organisation).

    Jede betroffene Verbindung bestimmt ihre Stufe neu (``DocumentCollaborationConsumer.access_recheck``): ohne
    Zugriff wird sie getrennt, mit niedrigerer Stufe herabgestuft; der Client lädt in beiden Fällen neu. Die
    Nachricht geht erst nach dem Commit hinaus, damit die Neuprüfung den neuen Stand sieht. Fehler beim Senden
    brechen die auslösende Änderung nie ab; zusätzlich prüft der Consumer schreibende Verbindungen beim
    Speichern regelmäßig nach.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import Motion, MotionDocument

logger = logging.getLogger(__name__)

#: Nachricht an offene Bearbeitungen: Zugriff neu prüfen (DocumentCollaborationConsumer.access_recheck)
RECHECK_EVENT = {"type": "access.recheck"}

#: Felder eines Dokuments, deren Änderung Zugriff nehmen kann
MOTION_ACCESS_FIELDS = frozenset({"visibility", "folder", "author", "responsible", "status", "organization"})

#: Art des Downloads in der Änderungshistorie
DOWNLOAD_PDF = "PDF-Export"
DOWNLOAD_DOCX = "DOCX-Export"
DOWNLOAD_ATTACHMENT = "Anhang"


# =============================================================================
# Herunterladen durch Gäste
# =============================================================================


def log_guest_download(
    motion: Motion, membership: Membership, request: Any, kind: str, *, document: MotionDocument | None = None
) -> None:
    """Export oder Anhang-Download eines Gastes in die Änderungshistorie der Organisation schreiben."""
    if not getattr(membership, "is_guest", False):
        return
    from apps.work.faction.audit import log_event

    changes: dict[str, Any] = {"art": kind}
    if document is not None:
        changes["anhang"] = document.filename
    cast(Any, log_event)(
        "guest_download",
        motion,
        organization=motion.organization,
        membership=membership,
        request=request,
        changes=changes,
        is_internal=False,
    )


# =============================================================================
# Entzug in offenen Bearbeitungen
# =============================================================================


def document_group(motion_id: Any) -> str:
    """Gruppe aller offenen Bearbeitungen eines Dokuments (wie im Consumer)."""
    return f"doc_{motion_id}"


def user_group(user_id: Any) -> str:
    """Gruppe aller offenen Bearbeitungen einer Person, organisationsübergreifend."""
    return f"collab_user_{user_id}"


def recheck_open_editors(*, motion_ids: Iterable[Any] = (), user_ids: Iterable[Any] = ()) -> None:
    """Offene Bearbeitungen der Dokumente bzw. Personen nach dem Commit ihren Zugriff neu prüfen lassen."""
    groups = {document_group(motion_id) for motion_id in motion_ids if motion_id}
    groups |= {user_group(user_id) for user_id in user_ids if user_id}
    if groups:
        transaction.on_commit(lambda: _send(sorted(groups)), robust=True)


def _send(groups: list[str]) -> None:
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        send = async_to_sync(channel_layer.group_send)
        for group in groups:
            send(group, dict(RECHECK_EVENT))
    except Exception:
        # Best Effort: Schreibende prüft der Consumer beim Speichern zusätzlich nach
        logger.warning("Neuprüfung offener Bearbeitungen nicht gesendet", exc_info=True)


def _motion_saved(
    sender: Any, instance: Any, created: bool, raw: bool = False, update_fields: Any = None, **kwargs: Any
) -> None:
    if created or raw:
        return
    if update_fields is not None:
        changed = {field.removesuffix("_id") for field in update_fields}
        if not changed & MOTION_ACCESS_FIELDS:
            return
    recheck_open_editors(motion_ids=[instance.pk])


def _motion_deleted(sender: Any, instance: Any, **kwargs: Any) -> None:
    recheck_open_editors(motion_ids=[instance.pk])


def _contributors_changed(
    sender: Any, instance: Any, action: str, reverse: bool, pk_set: set[Any] | None, **kwargs: Any
) -> None:
    """Mitarbeit entfernt: vorwärts (Dokument.contributors) und rückwärts (Mitgliedschaft.contributing_motions)."""
    if action not in ("post_remove", "post_clear"):
        return
    if reverse:
        recheck_open_editors(motion_ids=pk_set or ())
    else:
        recheck_open_editors(motion_ids=[instance.pk])


def _motion_share_changed(sender: Any, instance: Any, created: bool = False, raw: bool = False, **kwargs: Any) -> None:
    if created or raw:
        return
    recheck_open_editors(motion_ids=[instance.motion_id])


def _folder_share_changed(sender: Any, instance: Any, created: bool = False, raw: bool = False, **kwargs: Any) -> None:
    if created or raw:
        return
    recheck_open_editors(user_ids=[instance.user_id])


def _folder_saved(
    sender: Any, instance: Any, created: bool, raw: bool = False, update_fields: Any = None, **kwargs: Any
) -> None:
    """Ordner verschoben: Ordner-Freigaben darüber erfassen seine Dokumente womöglich nicht mehr."""
    if created or raw:
        return
    if update_fields is not None and not {field.removesuffix("_id") for field in update_fields} & {"parent"}:
        return
    from .models import FolderGuestShare

    recheck_open_editors(
        user_ids=FolderGuestShare.objects.filter(folder__organization_id=instance.organization_id)
        .values_list("user_id", flat=True)
        .distinct()
    )


def _membership_changed(sender: Any, instance: Any, created: bool = False, raw: bool = False, **kwargs: Any) -> None:
    if created or raw:
        return
    recheck_open_editors(user_ids=[instance.user_id])


def _membership_rights_changed(
    sender: Any, instance: Any, action: str, reverse: bool, pk_set: set[Any] | None, **kwargs: Any
) -> None:
    """Rollen, Einzelrechte oder verweigerte Rechte einer Mitgliedschaft geändert."""
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        recheck_open_editors(user_ids=[instance.user_id])
    elif pk_set:
        from apps.tenants.models import Membership

        recheck_open_editors(user_ids=Membership.objects.filter(pk__in=pk_set).values_list("user_id", flat=True))


def _role_users(role: Any) -> Any:
    from apps.tenants.models import Membership

    return Membership.objects.filter(roles=role).values_list("user_id", flat=True)


def _role_saved(sender: Any, instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
    if created or raw:
        return
    recheck_open_editors(user_ids=_role_users(instance))


def _role_permissions_changed(sender: Any, instance: Any, action: str, reverse: bool, **kwargs: Any) -> None:
    if action not in ("post_add", "post_remove", "post_clear") or reverse:
        return
    recheck_open_editors(user_ids=_role_users(instance))


def register() -> None:
    from apps.tenants.models import Membership, Role

    from .models import DocumentFolder, FolderGuestShare, Motion, MotionShare

    uid = "freigaben_entzug"
    post_save.connect(_motion_saved, sender=Motion, dispatch_uid=f"{uid}_motion_save")
    post_delete.connect(_motion_deleted, sender=Motion, dispatch_uid=f"{uid}_motion_delete")
    m2m_changed.connect(
        _contributors_changed, sender=Motion.contributors.through, dispatch_uid=f"{uid}_motion_contributors"
    )
    post_save.connect(_motion_share_changed, sender=MotionShare, dispatch_uid=f"{uid}_motion_share_save")
    post_delete.connect(_motion_share_changed, sender=MotionShare, dispatch_uid=f"{uid}_motion_share_delete")
    post_save.connect(_folder_share_changed, sender=FolderGuestShare, dispatch_uid=f"{uid}_folder_share_save")
    post_delete.connect(_folder_share_changed, sender=FolderGuestShare, dispatch_uid=f"{uid}_folder_share_delete")
    post_save.connect(_folder_saved, sender=DocumentFolder, dispatch_uid=f"{uid}_folder_save")
    post_save.connect(_membership_changed, sender=Membership, dispatch_uid=f"{uid}_membership_save")
    post_delete.connect(_membership_changed, sender=Membership, dispatch_uid=f"{uid}_membership_delete")
    for field in ("roles", "individual_permissions", "denied_permissions"):
        m2m_changed.connect(
            _membership_rights_changed,
            sender=getattr(Membership, field).through,
            dispatch_uid=f"{uid}_membership_{field}",
        )
    post_save.connect(_role_saved, sender=Role, dispatch_uid=f"{uid}_role_save")
    m2m_changed.connect(
        _role_permissions_changed, sender=Role.permissions.through, dispatch_uid=f"{uid}_role_permissions"
    )
