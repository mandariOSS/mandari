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
      deaktiviert, geändert oder entfernt, Konto deaktiviert, Rollen oder Einzelrechte geändert, Rechte einer
      Rolle geändert, Rolle gelöscht; Ordner verschoben (alle Personen mit Ordner-Freigaben der Organisation).

    Jede betroffene Verbindung bestimmt ihre Stufe neu (``DocumentCollaborationConsumer.access_recheck``): ohne
    Zugriff wird sie getrennt, mit niedrigerer Stufe herabgestuft; der Client lädt in beiden Fällen neu. Die
    Nachricht geht erst nach dem Commit hinaus, damit die Neuprüfung den neuen Stand sieht. Fehler beim Senden
    brechen die auslösende Änderung nie ab.

    Änderungen ohne Signal (``queryset.update()``, SQL) oder eine verlorene Nachricht (Kanal-Layer gestört)
    fängt die Nachprüfung im Consumer auf: Jede Verbindung, auch lesende, prüft ihren Zugriff spätestens nach
    ``ACCESS_RECHECK_INTERVAL_SECONDS``, sobald sie etwas sendet oder eine Änderung weitergeleitet bekommt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete

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

#: Objekt-Beschreibung eines Gast-Downloads in der Änderungshistorie: ohne Titel (Issue #582)
DOWNLOAD_OBJECT_REPR = "Dokument"


# =============================================================================
# Herunterladen durch Gäste
# =============================================================================


def download_choice(data: Any, *, default: bool = True) -> bool:
    """„Herunterladen erlauben“ aus Formulardaten: „0“ schaltet ab, jeder andere Wert an, ohne Angabe ``default``."""
    value = data.get("allow_download")
    return default if value is None else value != "0"


def download_update(data: Any) -> dict[str, bool]:
    """
    „Herunterladen erlauben“ für ``update_or_create(defaults=…)`` einer Dokument- oder Ordner-Freigabe.

    Nur eine ausdrückliche Angabe ändert den Schalter. Ohne ``allow_download`` behält eine bestehende Freigabe ihren
    Wert, eine neue erhält die Vorgabe des Modells (erlaubt). So stellt etwa eine Stufenänderung im Teilen-Dialog
    oder ein Aufruf ohne das Feld eine abgeschaltete Freigabe nicht unbemerkt wieder auf „erlaubt“. Der Dialog
    sendet das Feld nur, wenn jemand den Schalter bedient hat (``_share_modal.html``).
    """
    if data.get("allow_download") is None:
        return {}
    return {"allow_download": download_choice(data)}


def may_manage_document_share(membership: Membership, share: Any) -> bool:
    """
    Darf ``membership`` eine persönliche Dokument-Freigabe entziehen oder ändern?

    Freigaberecht am Dokument (``Motion.can_share``) oder Gast-Verwaltung (``guests.manage``) bei Freigaben an
    Gäste der Organisation (Issue #77).
    """
    if share.motion.can_share(membership):
        return True
    if share.scope != "user" or not share.user_id or not membership.has_permission("guests.manage"):
        return False
    from apps.tenants.models import Membership as MembershipModel

    return MembershipModel.objects.filter(
        user_id=share.user_id, organization_id=share.motion.organization_id, is_guest=True
    ).exists()


def set_allow_download(share: Any, allowed: bool) -> None:
    """Schalter „Herunterladen erlauben“ einer Dokument- oder Ordner-Freigabe setzen."""
    share.allow_download = allowed
    share.save(update_fields=["allow_download"])


def log_guest_download(
    motion: Motion, membership: Membership, request: Any, kind: str, *, document: MotionDocument | None = None
) -> None:
    """
    Export oder Anhang-Download eines Gastes in die Änderungshistorie der Organisation schreiben.

    Der Eintrag enthält weder Titel noch Dateinamen (``DOWNLOAD_OBJECT_REPR``, Kennungen von Dokument und
    Anhang): Die Hash-Kette lässt sich nicht bereinigen, und nicht jede Person mit Einsicht in die Historie sieht
    das Dokument. Titel und Dateiname ergänzt die Einsichts-View nur für Berechtigte (``guest_download_rows``).
    """
    if not getattr(membership, "is_guest", False):
        return
    from apps.work.faction.audit import log_event

    changes: dict[str, Any] = {"art": kind}
    if document is not None:
        changes["anhang_id"] = str(document.pk)
    cast(Any, log_event)(
        "guest_download",
        motion,
        organization=motion.organization,
        membership=membership,
        request=request,
        changes=changes,
        is_internal=False,
        object_repr=DOWNLOAD_OBJECT_REPR,
    )


def guest_download_rows(entries: Iterable[Any], membership: Membership) -> dict[Any, dict[str, Any]]:
    """
    Anzeige der Gast-Downloads (``guest_download``) in der Änderungshistorie für ``membership``.

    Je Eintrag (Schlüssel ``entry.pk``): Sieht ``membership`` das Dokument (``Motion.visible_to``), stehen dort
    sein aktueller Titel und die Angaben (Art, Dateiname des Anhangs). Sonst ist der Eintrag gesperrt wie ein
    nichtöffentlicher: ohne Beschreibung und Angaben. Das gilt auch für gelöschte Dokumente.
    """
    from apps.common.params import uuid_param

    from .models import Motion, MotionDocument

    downloads = [entry for entry in entries if entry.action == "guest_download"]
    if not downloads:
        return {}
    motion_ids = {entry.object_id for entry in downloads if entry.object_id}
    visible = {
        motion.pk: motion
        for motion in cast(Any, Motion)
        .visible_to(membership, include_deleted=True)
        .filter(pk__in=motion_ids)
        .select_related("document_type")
        .only("id", "title", "motion_type", "document_type")
    }
    # Kennung des Anhangs je Eintrag (nur bei sichtbaren Dokumenten nachschlagen)
    attachments: dict[Any, str | None] = {
        entry.pk: uuid_param(entry.changes.get("anhang_id"))
        for entry in downloads
        if entry.object_id in visible and isinstance(entry.changes, dict) and "anhang_id" in entry.changes
    }
    filenames = {
        str(pk): filename
        for pk, filename in MotionDocument.objects.filter(
            pk__in=[attachment_id for attachment_id in attachments.values() if attachment_id],
            motion_id__in=list(visible),
        ).values_list("pk", "filename")
    }

    rows: dict[Any, dict[str, Any]] = {}
    for entry in downloads:
        motion = visible.get(entry.object_id)
        if motion is None:
            rows[entry.pk] = {"locked": True}
            continue
        changes = entry.changes if isinstance(entry.changes, dict) else {}
        details = [("Art", str(changes.get("art", "")))]
        if entry.pk in attachments:
            details.append(("Anhang", filenames.get(attachments[entry.pk] or "", "gelöscht")))
        rows[entry.pk] = {"locked": False, "object_repr": str(motion), "details": details}
    return rows


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
    except Exception as exc:
        # Best Effort: Schreibende prüft der Consumer beim Speichern zusätzlich nach. Ohne Kanal-Layer (etwa Redis
        # nicht erreichbar) bricht der erste Fehler die übrigen Gruppen ab, eine Zeile im Protokoll genügt.
        logger.warning("Neuprüfung offener Bearbeitungen nicht gesendet: %s", str(exc)[:200])


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


def _role_deleting(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    Rolle wird gelöscht: Ihre Zuordnungen verschwinden per Kaskade ohne ``m2m_changed``.

    Die Personen jetzt sammeln (nach dem Löschen sind sie nicht mehr zu ermitteln); die Nachricht geht wie immer
    erst nach dem Commit hinaus.
    """
    recheck_open_editors(user_ids=list(_role_users(instance)))


def _user_saved(
    sender: Any, instance: Any, created: bool, raw: bool = False, update_fields: Any = None, **kwargs: Any
) -> None:
    """Konto deaktiviert (``User.is_active``): Die Mitgliedschaften bleiben aktiv, der Zugriff endet trotzdem."""
    if created or raw or instance.is_active:
        return
    if update_fields is not None and "is_active" not in update_fields:
        return
    recheck_open_editors(user_ids=[instance.pk])


def _role_permissions_changed(sender: Any, instance: Any, action: str, reverse: bool, **kwargs: Any) -> None:
    if action not in ("post_add", "post_remove", "post_clear") or reverse:
        return
    recheck_open_editors(user_ids=_role_users(instance))


def register() -> None:
    from apps.accounts.models import User
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
    pre_delete.connect(_role_deleting, sender=Role, dispatch_uid=f"{uid}_role_delete")
    post_save.connect(_user_saved, sender=User, dispatch_uid=f"{uid}_user_save")
    m2m_changed.connect(
        _role_permissions_changed, sender=Role.permissions.through, dispatch_uid=f"{uid}_role_permissions"
    )
