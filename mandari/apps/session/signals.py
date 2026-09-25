# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signal-Registrierung für das Session RIS.

Verbindet die Audit-Receiver (apps/session/audit.py) mit den zentralen
Session-Models. Wird über SessionConfig.ready() geladen.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from apps.session import audit, oparl_publication
from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAllowanceRate,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionFile,
    SessionFileBlob,
    SessionFileVersion,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMeetingPackage,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPaperVersionFile,
    SessionPerson,
    SessionProtocol,
    SessionTenant,
    SessionUser,
)
from apps.session.services import four_eyes_service

# Zentrale Models, deren Änderungen revisionssicher protokolliert werden
AUDITED_MODELS = [
    SessionMeeting,
    SessionAgendaItem,
    SessionPaper,
    SessionApplication,
    SessionProtocol,
    SessionPerson,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionAttendance,
    SessionConsultation,
    SessionLegislativeTerm,
    SessionFile,
    SessionUser,
    # Sitzungsgeld (Issue #38): Positionen und Sätze revisionssicher
    SessionAllowance,
    SessionAllowanceRate,
]

for _model in AUDITED_MODELS:
    _uid = f"session_audit_{_model.__name__}"
    pre_save.connect(audit.audit_pre_save, sender=_model, dispatch_uid=f"{_uid}_pre_save")
    post_save.connect(audit.audit_post_save, sender=_model, dispatch_uid=f"{_uid}_post_save")
    post_delete.connect(audit.audit_post_delete, sender=_model, dispatch_uid=f"{_uid}_post_delete")

# Issue #56: Mandanten-Kaskadenlöschung erkennen — während des Löschens eines
# SessionTenant dürfen die Audit-Receiver keine neuen Log-Zeilen für den
# verschwindenden Mandanten anlegen (IntegrityError/hängende Fremdschlüssel).
pre_delete.connect(audit.tenant_pre_delete, sender=SessionTenant, dispatch_uid="session_audit_tenant_pre_delete")
post_delete.connect(audit.tenant_post_delete, sender=SessionTenant, dispatch_uid="session_audit_tenant_post_delete")


# =============================================================================
# Vier-Augen-Prinzip (Issue #222): letzte inhaltliche Bearbeitung festhalten
# =============================================================================
# Nach den Audit-Receivern registriert: Sie nutzen den dort geladenen Altzustand.

post_save.connect(four_eyes_service.track_paper_edit, sender=SessionPaper, dispatch_uid="session_four_eyes_paper")
post_save.connect(
    four_eyes_service.track_protocol_edit, sender=SessionProtocol, dispatch_uid="session_four_eyes_protocol"
)
post_save.connect(
    four_eyes_service.track_agenda_protocol_edit, sender=SessionAgendaItem, dispatch_uid="session_four_eyes_agenda"
)
post_save.connect(four_eyes_service.track_file_save, sender=SessionFile, dispatch_uid="session_four_eyes_file_save")
post_delete.connect(
    four_eyes_service.track_file_delete, sender=SessionFile, dispatch_uid="session_four_eyes_file_delete"
)


# =============================================================================
# OParl-Tombstones (Issue #35): Löschungen/Ö->NÖ-Wechsel nachhalten
# =============================================================================

for _model in oparl_publication.KIND_BY_MODEL:
    _uid = f"session_oparl_tombstone_{_model.__name__}"
    post_save.connect(oparl_publication.tombstone_post_save, sender=_model, dispatch_uid=f"{_uid}_post_save")
    post_delete.connect(oparl_publication.tombstone_post_delete, sender=_model, dispatch_uid=f"{_uid}_post_delete")


# =============================================================================
# Insight-Durchstich (Issue #36): Veröffentlichungs-Schalter -> OParl-Quelle
# =============================================================================


def tenant_publication_pre_save(sender, instance, **kwargs):
    """Alten Veröffentlichungs-Stand merken (Provisioning-Hook)."""
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list("insight_publish", flat=True).first()
        instance._insight_publish_old = old
    else:
        instance._insight_publish_old = None


def tenant_publication_post_save(sender, instance, created, **kwargs):
    """
    Auto-Registrierung der OParl-Quelle bei Veröffentlichung (Issue #36).

    Sobald ein Mandant insight_publish aktiviert (Settings-UI, Admin oder
    Provisioning), wird seine OParl-API als Insight-Quelle registriert;
    beim Deaktivieren wird die Quelle inaktiv gesetzt.
    """
    if kwargs.get("raw"):
        return
    old = getattr(instance, "_insight_publish_old", None)
    if created and not instance.insight_publish:
        return
    if not created and old == instance.insight_publish:
        return
    from apps.session.services import insight_service

    insight_service.sync_publication_state(instance)


pre_save.connect(
    tenant_publication_pre_save,
    sender=SessionTenant,
    dispatch_uid="session_tenant_publication_pre_save",
)
post_save.connect(
    tenant_publication_post_save,
    sender=SessionTenant,
    dispatch_uid="session_tenant_publication_post_save",
)


def tenant_numbering_post_save(sender, instance, created, **kwargs):
    """Neuer Mandant bekommt sofort einen Nummernkreis – Vorlagen sind nie ohne klare Nummer (Issue #150)."""
    if created and not kwargs.get("raw"):
        from apps.session.services import numbering_service

        numbering_service.ensure_default(instance)


post_save.connect(
    tenant_numbering_post_save,
    sender=SessionTenant,
    dispatch_uid="session_tenant_numbering_post_save",
)


# =============================================================================
# Beratungsfolge (Issue #34): Beschlussergebnis an die Station zurückschreiben
# =============================================================================


def sync_consultation_result(sender, instance, **kwargs):
    """
    Schreibt das Abstimmungsergebnis eines TOP an die verknüpfte
    Beratungsstation zurück (Issue #34).

    Wird das Ergebnis am TOP erfasst (Niederschrift/Beschlussregister,
    Issues #31/#32), spiegelt die Station der Beratungsfolge den Stand —
    so ist z. B. das Vorberatungsergebnis in der Ratssitzung sichtbar.
    """
    if kwargs.get("raw"):
        return
    try:
        consultation = instance.consultation
    except SessionConsultation.DoesNotExist:
        return
    if consultation.result != instance.vote_result:
        consultation.result = instance.vote_result
        consultation.save(update_fields=["result", "updated_at"])


post_save.connect(
    sync_consultation_result,
    sender=SessionAgendaItem,
    dispatch_uid="session_consultation_result_sync",
)


# =============================================================================
# Sitzungsmappe (Issue #218): Dateien einer gelöschten Fassung entfernen
# =============================================================================


def meeting_package_post_delete(sender, instance, **kwargs):
    """
    Gesamt-PDF und ZIP-Paket aus dem Speicher löschen, sobald die Fassung gelöscht ist –
    auch bei Kaskaden (Sitzung oder Mandant gelöscht). Die Mappe enthält ggf. NÖ-Inhalte
    und darf ihre Datenbankzeile nicht überleben.
    """
    names = [field.name for field in (instance.pdf_file, instance.zip_file) if field]
    if not names:
        return
    storage = instance.pdf_file.storage

    def delete_files():
        for name in names:
            storage.delete(name)

    transaction.on_commit(delete_files)


post_delete.connect(
    meeting_package_post_delete,
    sender=SessionMeetingPackage,
    dispatch_uid="session_meeting_package_files_post_delete",
)


# =============================================================================
# Fassungen (Issue #226): bei Workflow-Übergängen und Beratungsergebnissen sichern
# =============================================================================
#
# Die Hooks hängen am Speichern der Modelle, nicht am Workflow-Code: Jeder Statuswechsel der
# Vorlage (Freigabelauf, Mitzeichnung, Terminierung, Bearbeiten-Formular) und jedes erfasste
# Beratungsergebnis erzeugt eine Fassung. Der Alt-Zustand kommt aus audit.audit_pre_save.


def paper_version_on_status_change(sender, instance, created, **kwargs):
    """Workflow-Übergang: Statuswechsel der Vorlage -> neue Fassung."""
    if created or kwargs.get("raw"):
        return
    old = getattr(instance, "_audit_old", None)
    if old is None or old.status == instance.status:
        return
    from apps.session.services import paper_version_service

    paper_version_service.record_transition(instance, old.status)


def paper_version_on_consultation_result(sender, instance, created, **kwargs):
    """Beratungsergebnis an der Station erfasst (direkt oder vom TOP zurückgeschrieben) -> neue Fassung."""
    if created or kwargs.get("raw") or instance.result == "pending":
        return
    old = getattr(instance, "_audit_old", None)
    if old is None or old.result == instance.result:
        return
    from apps.session.services import paper_version_service

    paper_version_service.record_consultation(
        instance.paper,
        result=instance.result,
        result_label=instance.get_result_display(),
        consultation=instance,
        agenda_item=instance.agenda_item,
    )


def paper_version_on_agenda_result(sender, instance, created, **kwargs):
    """Ergebnis an einem TOP mit Vorlage, aber ohne Beratungsstation -> neue Fassung."""
    if created or kwargs.get("raw") or not instance.paper_id or instance.vote_result == "pending":
        return
    old = getattr(instance, "_audit_old", None)
    if old is None or old.vote_result == instance.vote_result:
        return
    if SessionConsultation.objects.filter(agenda_item=instance).exists():
        return  # übernimmt paper_version_on_consultation_result
    from apps.session.services import paper_version_service

    paper_version_service.record_consultation(
        instance.paper,
        result=instance.vote_result,
        result_label=instance.get_vote_result_display(),
        agenda_item=instance,
    )


post_save.connect(paper_version_on_status_change, sender=SessionPaper, dispatch_uid="session_paper_version_status")
post_save.connect(
    paper_version_on_consultation_result,
    sender=SessionConsultation,
    dispatch_uid="session_paper_version_consultation",
)
post_save.connect(
    paper_version_on_agenda_result,
    sender=SessionAgendaItem,
    dispatch_uid="session_paper_version_agenda_item",
)


# =============================================================================
# Speicher der Anlagen (Issue #226): Inhalte ohne Verweis entfernen
# =============================================================================


def file_storage_post_delete(sender, instance, **kwargs):
    """Anlage gelöscht: ihre Datei verschwindet, sofern kein Inhalt und keine andere Anlage darauf zeigt."""
    from apps.session.services import file_version_service

    file_version_service.release_storage_names([instance.file.name] if instance.file else [])


def blob_reference_post_delete(sender, instance, **kwargs):
    """Anlagen- oder Vorlagen-Fassung gelöscht: Inhalt prüfen, ob noch jemand darauf verweist."""
    from apps.session.services import file_version_service

    file_version_service.collect_garbage([instance.blob_id])


def blob_post_delete(sender, instance, **kwargs):
    """Inhalt gelöscht (Aufräumen oder Mandanten-Löschung): Datei aus dem Speicher entfernen."""
    from apps.session.services import file_version_service

    file_version_service.release_storage_names([instance.file.name] if instance.file else [])


post_delete.connect(file_storage_post_delete, sender=SessionFile, dispatch_uid="session_file_storage_post_delete")
post_delete.connect(
    blob_reference_post_delete, sender=SessionFileVersion, dispatch_uid="session_file_version_post_delete"
)
post_delete.connect(
    blob_reference_post_delete,
    sender=SessionPaperVersionFile,
    dispatch_uid="session_paper_version_file_post_delete",
)
post_delete.connect(blob_post_delete, sender=SessionFileBlob, dispatch_uid="session_file_blob_post_delete")
