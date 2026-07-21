# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungshistorie (Audit) für Fraktionssitzungen (Issue #66).

Nutzt den gemeinsamen Audit-Baustein :mod:`apps.common.audit_core`
(generalisiert aus der Session-Audit-Infrastruktur, Issue #23) und
protokolliert automatisch alle Aktionen rund um Fraktionssitzungen:
create/update/delete der Sitzungs-Models sowie Spezial-Ereignisse
(Einladungsversand, Protokoll-Genehmigung, Teilnahme-Änderungen,
TOP-Vorschläge, Abstimmungen, automatische Erzeugung/Ausfälle).

Kaskadenlösch-Schutz (Muster aus Session-Issue #56): Während der
Kaskadenlöschung einer Organization werden keine neuen Log-Zeilen für
die verschwindende Organisation angelegt.

Sicherheit (Issue #64): Einträge zu nicht-öffentlichen TOPs werden mit
``is_internal=True`` markiert — die Einsichts-View maskiert sie für
Nicht-Vereidigte vollständig ("Gesperrte Information").
"""

import logging

from apps.common import audit_core

logger = logging.getLogger(__name__)

# Kaskadenlösch-Schutz-Scope für Work-Organisationen
_ORG_SCOPE = "work_organization"


# =============================================================================
# Organisation-Kaskadenlöschung (Muster aus Session-Issue #56)
# =============================================================================


def organization_pre_delete(sender, instance, **kwargs):
    """pre_delete(Organization): Kaskadenlöschung beginnt."""
    audit_core.mark_root_deleting(_ORG_SCOPE, instance.pk)


def organization_post_delete(sender, instance, **kwargs):
    """post_delete(Organization): Kaskadenlöschung abgeschlossen."""
    audit_core.unmark_root_deleting(_ORG_SCOPE, instance.pk)


def is_organization_deleting(org_pk) -> bool:
    """Läuft für diese Organisation gerade eine Kaskadenlöschung?"""
    return audit_core.is_root_deleting(_ORG_SCOPE, org_pk)


# =============================================================================
# Auflösung: Organisation / Sitzung / NÖ-Kennzeichnung je Objekt
# =============================================================================


def resolve_organization(instance):
    """Organisation eines Fraktions-Objekts ermitteln (direkt oder über Relation)."""
    organization = getattr(instance, "organization", None)
    if organization is not None:
        return organization
    meeting = getattr(instance, "meeting", None)
    if meeting is not None:
        return meeting.organization
    agenda_item = getattr(instance, "agenda_item", None)
    if agenda_item is not None:
        return agenda_item.meeting.organization
    schedule = getattr(instance, "schedule", None)
    if schedule is not None:
        return schedule.organization
    return None


def resolve_meeting_id(instance):
    """Zugehörige Sitzungs-ID ermitteln (für Filter/Deep-Links)."""
    model_name = instance.__class__.__name__
    if model_name == "FactionMeeting":
        return instance.pk
    meeting_id = getattr(instance, "meeting_id", None)
    if meeting_id is not None:
        return meeting_id
    agenda_item = getattr(instance, "agenda_item", None)
    if agenda_item is not None:
        return agenda_item.meeting_id
    return None


def resolve_is_internal(instance) -> bool:
    """Betrifft das Objekt den nicht-öffentlichen Teil (Issue #64)?"""
    try:
        model_name = instance.__class__.__name__
        if model_name == "FactionAgendaItem":
            return instance.visibility == "internal"
        agenda_item = getattr(instance, "agenda_item", None)
        if agenda_item is not None:
            return agenda_item.visibility == "internal"
    except Exception:
        # Im Zweifel (z. B. Relation bereits gelöscht) als NÖ behandeln,
        # damit niemals Inhalte an Nicht-Vereidigte durchsickern
        return True
    return False


# =============================================================================
# Eintrag schreiben
# =============================================================================


def log_event(action, instance, *, organization=None, membership=None, changes=None, request=None, is_internal=None):
    """
    Audit-Eintrag für ein Fraktions-Objekt schreiben.

    Args:
        action: Aktion aus den FactionAuditLog-Choices
        instance: Betroffenes Model-Objekt
        organization: Organization (sonst aus instance abgeleitet)
        membership: Auslösende Membership (sonst aus dem aktuellen Request)
        changes: Optionaler Änderungs-Diff (dict)
        request: Optionaler Request (sonst Thread-Local)
        is_internal: NÖ-Kennzeichnung (sonst aus instance abgeleitet)
    """
    from apps.work.faction.models import FactionAuditLog

    try:
        organization = organization or resolve_organization(instance)
    except Exception:
        return None
    if organization is None:
        return None

    # Während einer Organisations-Kaskadenlöschung nichts protokollieren
    if is_organization_deleting(organization.pk):
        return None

    request = request or audit_core.get_current_request()
    if membership is None and request is not None:
        request_membership = getattr(request, "membership", None)
        if request_membership is not None and request_membership.organization_id == organization.pk:
            membership = request_membership
    ip_address, user_agent = audit_core.get_client_meta(request)

    actor_label = ""
    if membership is not None:
        user = getattr(membership, "user", None)
        if user is not None:
            actor_label = (user.get_display_name() if hasattr(user, "get_display_name") else "") or user.email

    if is_internal is None:
        is_internal = resolve_is_internal(instance)

    # Verschlüsselte Inhalte niemals in die Objekt-Beschreibung übernehmen —
    # Models mit sensitivem __str__ liefern eine sichere audit_repr
    try:
        object_repr = getattr(instance, "audit_repr", None) or str(instance)
    except Exception:
        object_repr = instance.__class__.__name__

    try:
        return FactionAuditLog.objects.create(
            organization=organization,
            membership=membership,
            actor_label=actor_label[:200],
            ip_address=ip_address,
            user_agent=user_agent,
            action=action,
            model_name=instance.__class__.__name__,
            object_id=instance.pk,
            object_repr=object_repr[:500],
            meeting_id_ref=resolve_meeting_id(instance),
            is_internal=bool(is_internal),
            changes=changes or {},
        )
    except Exception:
        # Audit darf die eigentliche Aktion niemals zum Scheitern bringen
        logger.exception("Fraktions-Audit-Eintrag konnte nicht geschrieben werden")
        return None


# =============================================================================
# Spezial-Ereignisse aus Statuswechseln ableiten
# =============================================================================


def _special_action(old_instance, new_instance) -> str | None:
    """Spezial-Ereignisse aus Feld-/Statuswechseln ableiten."""
    model_name = new_instance.__class__.__name__

    if model_name == "FactionMeeting":
        # Einladungsversand hat Vorrang vor dem begleitenden Statuswechsel
        if old_instance.invitation_sent_at is None and new_instance.invitation_sent_at is not None:
            return "invitation_sent"
        if old_instance.invitation_updated_at != new_instance.invitation_updated_at:
            return "invitation_updated"
        if old_instance.reminder_sent_at is None and new_instance.reminder_sent_at is not None:
            return "reminder_sent"
        # Freigabe des Einladungsversands (Issue #62) — auditiert WER freigab
        if old_instance.invitation_released_at is None and new_instance.invitation_released_at is not None:
            return "invitation_released"
        # Freigabe-Hinweis an Vorstand/Vorsitz versandt (Issue #62)
        if (
            old_instance.release_notice_first_sent_at != new_instance.release_notice_first_sent_at
            or old_instance.release_notice_final_sent_at != new_instance.release_notice_final_sent_at
        ):
            return "release_notice_sent"
        if not old_instance.protocol_approved and new_instance.protocol_approved:
            return "protocol_approved"
        if old_instance.protocol_status != new_instance.protocol_status and new_instance.protocol_status == "pending":
            return "protocol_submitted"
        if old_instance.status != new_instance.status:
            return "status"
        return None

    if model_name == "FactionAttendance":
        if old_instance.status != new_instance.status:
            return "participation"
        if old_instance.checked_in_at != new_instance.checked_in_at:
            return "participation"
        if old_instance.checked_out_at != new_instance.checked_out_at:
            return "participation"
        return None

    if model_name == "FactionAgendaItem":
        if old_instance.proposal_status != new_instance.proposal_status:
            if new_instance.proposal_status == "active":
                return "proposal_accepted"
            if new_instance.proposal_status == "rejected":
                return "proposal_rejected"
            if new_instance.proposal_status == "proposed":
                return "proposal"
        if not old_instance.has_decision and new_instance.has_decision:
            return "decision"
        return None

    return None


# =============================================================================
# Signal-Receiver (in apps.work.apps.WorkConfig.ready() registriert)
# =============================================================================


def audit_pre_save(sender, instance, **kwargs):
    """Alten Zustand für den Diff laden (gemeinsamer Baustein)."""
    audit_core.capture_old_state(sender, instance)


def audit_post_save(sender, instance, created, **kwargs):
    """create/update (inkl. Spezial-Ereignisse) protokollieren."""
    if kwargs.get("raw"):
        return
    if created:
        # Explizite Erzeugungs-Aktion (z.B. "generated"/"auto_cancelled" aus
        # der Sitzungserzeugung, Issue #61) hat Vorrang
        override = getattr(instance, "_audit_created_action", None)
        if override:
            log_event(override, instance)
            return
        if instance.__class__.__name__ == "FactionAgendaItem" and instance.proposal_status == "proposed":
            log_event("proposal", instance)
            return
        log_event("create", instance)
        return

    old_instance = getattr(instance, "_audit_old", None)
    if old_instance is None:
        log_event("update", instance)
        return

    changes = audit_core.build_changes(old_instance, instance)
    if not changes:
        return
    action = _special_action(old_instance, instance) or "update"
    log_event(action, instance, changes=changes)


def audit_post_delete(sender, instance, **kwargs):
    """delete protokollieren (übersprungen während Organisations-Kaskadenlöschung)."""
    # Schneller Pfad ohne DB-Zugriff: Objekte mit direkter organization_id
    organization_id = getattr(instance, "organization_id", None)
    if organization_id is not None and is_organization_deleting(organization_id):
        return
    log_event("delete", instance)


def register():
    """Audit-Receiver für die Fraktions-Models registrieren (WorkConfig.ready)."""
    from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

    from apps.tenants.models import Organization
    from apps.work.faction.models import (
        FactionAgendaItem,
        FactionAgendaItemAttachment,
        FactionAttendance,
        FactionDecision,
        FactionMeeting,
        FactionMeetingException,
        FactionMeetingSchedule,
        FactionProtocolEntry,
        FactionSuspensionRule,
    )

    audited_models = [
        FactionMeeting,
        FactionAgendaItem,
        FactionAttendance,
        FactionProtocolEntry,
        FactionDecision,
        FactionAgendaItemAttachment,
        FactionMeetingSchedule,
        FactionMeetingException,
        FactionSuspensionRule,
    ]

    for model in audited_models:
        uid = f"faction_audit_{model.__name__}"
        pre_save.connect(audit_pre_save, sender=model, dispatch_uid=f"{uid}_pre_save")
        post_save.connect(audit_post_save, sender=model, dispatch_uid=f"{uid}_post_save")
        post_delete.connect(audit_post_delete, sender=model, dispatch_uid=f"{uid}_post_delete")

    # Kaskadenlösch-Schutz: Organisation wird gelöscht -> nichts protokollieren
    pre_delete.connect(organization_pre_delete, sender=Organization, dispatch_uid="faction_audit_org_pre_delete")
    post_delete.connect(organization_post_delete, sender=Organization, dispatch_uid="faction_audit_org_post_delete")
