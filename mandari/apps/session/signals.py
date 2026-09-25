# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signal-Registrierung für das Session RIS.

Verbindet die Audit-Receiver (apps/session/audit.py) mit den zentralen
Session-Models. Wird über SessionConfig.ready() geladen.
"""

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save

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
    SessionMonthlyAllowance,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPaperVersionFile,
    SessionPerson,
    SessionProtocol,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)
from apps.session.services import four_eyes_service, joint_meeting_service, leitstelle_service

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
    # Monatspauschalen (Issue #221): jeder Posten direkt – festgesetzt, genehmigt, ausgezahlt
    SessionMonthlyAllowance,
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
# Öffentliche Niederschrift (Issue #318): nach Änderungen neu erzeugen oder zurücknehmen
# =============================================================================
#
# Die öffentliche Fassung (OParl resultsProtocol, Bürgerportal) ist eine gespeicherte Datei. Ändert
# sich danach ihr Inhalt – ein TOP wird nichtöffentlich, die Sitzung wird nichtöffentlich, eine
# Berichtigung wird wirksam, die Anwesenheit wird korrigiert –, entsteht nach dem Commit eine neue
# Fassung; die alte wird sofort zurückgenommen. Der Hook vergleicht mit dem Altzustand aus
# audit.audit_pre_save und fragt nur bei inhaltlichen Änderungen einmal nach einer veröffentlichten
# Niederschrift.

#: Felder, deren Änderung den Inhalt der öffentlichen Fassung berührt
PUBLIC_PROTOCOL_FIELDS = {
    SessionAgendaItem: (
        "is_public",
        "name",
        "number",
        "order",
        "parent_id",
        "paper_id",
        "is_withdrawn",
        "withdrawn_reason",
        "is_supplementary",
        "resolution_text",
        "protocol_note",
        "vote_result",
        "voting_method",
        "votes_yes",
        "votes_no",
        "votes_abstain",
    ),
    SessionMeeting: ("is_public", "name", "start", "end", "location", "room", "organization_id"),
    SessionAttendance: ("status", "role", "person_id"),
    SessionProtocol: ("status", "content", "chair_name", "recorder_name", "approval_note"),
}


def _public_protocol_meeting_id(instance):
    return instance.pk if isinstance(instance, SessionMeeting) else instance.meeting_id


def public_protocol_post_save(sender, instance, created, **kwargs):
    """TOP, Sitzung, Anwesenheit oder Niederschrift geändert: öffentliche Fassung nachziehen."""
    if kwargs.get("raw"):
        return
    meeting_id = _public_protocol_meeting_id(instance)
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is not None and audit.is_tenant_deleting(tenant_id):
        return
    if not created:
        old = getattr(instance, "_audit_old", None)
        if old is not None and all(
            getattr(old, name) == getattr(instance, name) for name in PUBLIC_PROTOCOL_FIELDS[sender]
        ):
            return
    from apps.session.services import protocol_publication

    if protocol_publication.has_public_protocol(meeting_id):
        protocol_publication.schedule_refresh(meeting_id)


def public_protocol_post_delete(sender, instance, **kwargs):
    """Anwesenheitszeile gelöscht: Teilnehmerverzeichnis der öffentlichen Fassung nachziehen."""
    from apps.session.services import protocol_publication

    if protocol_publication.has_public_protocol(instance.meeting_id):
        protocol_publication.schedule_refresh(instance.meeting_id)


for _model in PUBLIC_PROTOCOL_FIELDS:
    post_save.connect(
        public_protocol_post_save, sender=_model, dispatch_uid=f"session_public_protocol_{_model.__name__}"
    )
post_delete.connect(
    public_protocol_post_delete, sender=SessionAttendance, dispatch_uid="session_public_protocol_attendance_delete"
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


# =============================================================================
# Mandantengruppen und gemeinsame Sitzungen (Issue #317)
# =============================================================================
# Leitstellen-Rechte erteilen, ändern oder entziehen steht im Protokoll jedes betroffenen Mandanten;
# weitere Gremien einer Sitzung nur aus demselben Mandanten, Änderungen mit Audit-Eintrag.

post_save.connect(
    leitstelle_service.membership_saved,
    sender=SessionTenantGroupMembership,
    dispatch_uid="session_leitstelle_membership_saved",
)
post_delete.connect(
    leitstelle_service.membership_deleted,
    sender=SessionTenantGroupMembership,
    dispatch_uid="session_leitstelle_membership_deleted",
)
post_save.connect(
    leitstelle_service.group_tenant_saved,
    sender=SessionTenantGroupTenant,
    dispatch_uid="session_leitstelle_tenant_saved",
)
post_delete.connect(
    leitstelle_service.group_tenant_deleted,
    sender=SessionTenantGroupTenant,
    dispatch_uid="session_leitstelle_tenant_deleted",
)
pre_delete.connect(
    leitstelle_service.group_pre_delete, sender=SessionTenantGroup, dispatch_uid="session_leitstelle_group_pre_delete"
)
m2m_changed.connect(
    joint_meeting_service.joint_organizations_changed,
    sender=SessionMeeting.joint_organizations.through,
    dispatch_uid="session_joint_organizations_changed",
)
