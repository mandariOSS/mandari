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
from typing import Any

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
    """post_delete(Organization): Kaskadenlöschung abgeschlossen, Kettenkopf entfernen (Issue #221)."""
    from apps.common import audit_chain

    audit_core.unmark_root_deleting(_ORG_SCOPE, instance.pk)
    audit_chain.drop_head(audit_chain.FACTION, instance.pk)


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


def _top_ist_intern(item) -> bool:
    """
    Gehört der TOP zum nichtöffentlichen Teil? Wie alle Lesewege über die Elternkette (``visibility.is_item_internal``):
    Unterpunkte eines nichtöffentlichen TOPs sind nichtöffentlich, auch wenn an ihnen selbst „öffentlich“ gespeichert
    ist.

    Wird ein TOP samt Unterpunkten gelöscht, ist der übergeordnete TOP beim Eintrag des Unterpunkts schon entfernt.
    Dann sagt seine Historie, ob er nichtöffentlich war; ohne diesen Rückgriff gälte jeder mitgelöschte Unterpunkt
    eines öffentlichen TOPs im Zweifel als nichtöffentlich.
    """
    from django.core.exceptions import ObjectDoesNotExist

    from .models import FactionAuditLog

    node = item
    while node is not None:
        if node.visibility == "internal":
            return True
        if node.parent_id is None:
            return False
        try:
            node = node.parent
        except ObjectDoesNotExist:
            return FactionAuditLog.objects.filter(
                model_name="FactionAgendaItem", object_id=node.parent_id, is_internal=True
            ).exists()
    return False


def resolve_is_internal(instance) -> bool:
    """Betrifft das Objekt den nicht-öffentlichen Teil (Issue #64)?"""
    try:
        model_name = instance.__class__.__name__
        if model_name == "FactionStandardAgendaItem":
            return instance.visibility == "internal"
        if model_name == "FactionAgendaItem":
            return _top_ist_intern(instance)
        agenda_item = getattr(instance, "agenda_item", None)
        if agenda_item is not None:
            return _top_ist_intern(agenda_item)
    except Exception:
        # Im Zweifel (z. B. Relation bereits gelöscht) als NÖ behandeln,
        # damit niemals Inhalte an Nicht-Vereidigte durchsickern
        return True
    return False


def unsichtbare_eintraege(organization: Any, membership: Any) -> Any:
    """
    Bedingung (``Q``) für Einträge zu Sitzungen (``meeting_id_ref``) sowie zu Reihen samt Ausnahmen und Ausfallregeln
    (``object_id``), die das Mitglied nicht sieht (Sitzungsarten, Issue #1239); die Historie lässt sie ganz weg.
    Gelöschte Sitzungen lassen sich keiner Art mehr zuordnen: Ihre Einträge (samt Titel, Datum und Anwesenheiten aus
    dem Löschen) sieht nur, wer ohnehin jede Sitzung sieht (``faction.manage``).
    """
    from django.db.models import Q

    from .models import FactionMeeting, FactionMeetingException, FactionMeetingSchedule, FactionSuspensionRule
    from .sitzungsarten import sichtbar_fuer, sichtbare_arten

    sitzungen = FactionMeeting.objects.filter(organization=organization)
    unsichtbar = sitzungen.exclude(pk__in=sichtbar_fuer(membership, sitzungen).values("pk"))
    reihen = FactionMeetingSchedule.objects.filter(organization=organization).exclude(
        meeting_type__in=sichtbare_arten(membership).values("pk")
    )
    ausnahmen = FactionMeetingException.objects.filter(schedule__in=reihen.values("pk"))
    regeln = FactionSuspensionRule.objects.filter(schedule__in=reihen.values("pk"))
    bedingung = (
        Q(meeting_id_ref__in=unsichtbar.values("pk"))
        | Q(model_name="FactionMeetingSchedule", object_id__in=reihen.values("pk"))
        | Q(model_name="FactionMeetingException", object_id__in=ausnahmen.values("pk"))
        | Q(model_name="FactionSuspensionRule", object_id__in=regeln.values("pk"))
    )
    if not membership.has_permission("faction.manage"):
        bedingung |= Q(meeting_id_ref__isnull=False) & ~Q(meeting_id_ref__in=sitzungen.values("pk"))
    return bedingung


def locked_object_ids(organization: Any, object_ids: set[Any]) -> set[Any]:
    """
    Objekte mit mindestens einem gesperrten Eintrag (Issue #1160).

    Ein TOP kann nach früheren Einträgen nicht-öffentlich geworden sein. Einträge sind unveränderlich, deshalb gilt die
    Sperre beim Lesen für alle Einträge des Objekts, auch wenn es später wieder öffentlich wird oder gelöscht ist.
    """
    from apps.work.faction.models import FactionAuditLog

    return set(
        FactionAuditLog.objects.filter(
            organization=organization, is_internal=True, object_id__in=object_ids
        ).values_list("object_id", flat=True)
    )


#: Objekte, die über ``agenda_item`` zu einem TOP gehören (Protokolleintrag, Beschluss, Anlage)
_TOP_BEZUG = ("FactionProtocolEntry", "FactionDecision", "FactionAgendaItemAttachment")


def nichtoeffentliche_eintraege(organization: Any, entries: Any) -> set[Any]:
    """
    Schlüssel der Einträge aus ``entries``, die zum nichtöffentlichen Teil gehören (Sperre beim Lesen).

    Einträge sind unveränderlich und tragen den Stand ihres Schreibens (``is_internal``). Dazu kommen beim Lesen:

    - alle Einträge eines Objekts mit mindestens einem gesperrten Eintrag (:func:`locked_object_ids`),
    - Einträge zu einem TOP, der heute über seine Elternkette nichtöffentlich ist,
    - Einträge zu Protokolleinträgen, Beschlüssen und Anlagen, deren TOP heute nichtöffentlich ist oder gesperrte
      Einträge hat: Dateiname, Beschlusstext und Anmerkungen stünden sonst weiter offen in der Historie, nachdem der
      TOP in den nichtöffentlichen Teil gelegt wurde.

    Der Bezug zum TOP entsteht aus dem heutigen Objekt, der Eintrag selbst kennt ihn nicht. Für gelöschte Protokoll-
    einträge, Beschlüsse und Anlagen gilt deshalb allein der Stand beim Schreiben.
    """
    from .models import (
        FactionAgendaItem,
        FactionAgendaItemAttachment,
        FactionDecision,
        FactionProtocolEntry,
    )
    from .visibility import is_item_internal

    entries = list(entries)
    gesperrt = {entry.pk for entry in entries if entry.is_internal}
    # Objekt des Eintrags → sein TOP (der TOP selbst oder der TOP des Protokolleintrags, Beschlusses, der Anlage)
    top_von: dict[Any, Any] = {
        entry.object_id: entry.object_id
        for entry in entries
        if entry.model_name == "FactionAgendaItem" and entry.object_id
    }
    modelle = {
        "FactionProtocolEntry": FactionProtocolEntry,
        "FactionDecision": FactionDecision,
        "FactionAgendaItemAttachment": FactionAgendaItemAttachment,
    }
    for name in _TOP_BEZUG:
        ids = {entry.object_id for entry in entries if entry.model_name == name and entry.object_id}
        if not ids:
            continue
        bezuege = (
            modelle[name]
            .objects.filter(pk__in=ids, agenda_item__meeting__organization=organization)
            .values_list("pk", "agenda_item_id")
        )
        top_von.update({pk: top_id for pk, top_id in bezuege if top_id})

    objekt_ids = {entry.object_id for entry in entries if entry.object_id}
    top_ids = set(top_von.values())
    mit_sperre = locked_object_ids(organization, objekt_ids | top_ids)
    heute_intern = {
        top.pk
        for top in FactionAgendaItem.objects.filter(pk__in=top_ids, meeting__organization=organization).select_related(
            "parent__parent"
        )
        if is_item_internal(top)
    }
    for entry in entries:
        top_id = top_von.get(entry.object_id)
        if entry.object_id in mit_sperre or (top_id is not None and (top_id in mit_sperre or top_id in heute_intern)):
            gesperrt.add(entry.pk)
    return gesperrt


# =============================================================================
# Eintrag schreiben
# =============================================================================


def log_event(
    action: str,
    instance: Any,
    *,
    organization: Any = None,
    membership: Any = None,
    changes: dict[str, Any] | None = None,
    request: Any = None,
    is_internal: bool | None = None,
    object_repr: str | None = None,
) -> Any:
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
        object_repr: Objekt-Beschreibung (sonst ``audit_repr`` bzw. ``str(instance)``), etwa ohne Titel
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
    if object_repr is None:
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
        # Finale Teilnahme-Bestätigung durch den Vorstand (Issue #67)
        if old_instance.attendance_confirmed_at is None and new_instance.attendance_confirmed_at is not None:
            return "attendance_confirmed"
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

    # Gesicherte Namen (Issue #591) nicht in die unveränderbare Historie: Der Wechsel der Person
    # steht dort schon als Verweis, der Name wäre zusätzlicher Personenbezug.
    snapshot_fields = set(getattr(instance, "MEMBER_NAME_SNAPSHOTS", {}).values())
    skip_fields = audit_core.DEFAULT_SKIP_FIELDS | snapshot_fields
    # Mit ``update_fields`` wurden nur diese Felder geschrieben. Die übrigen trägt das Objekt im Stand, in dem es
    # gelesen wurde, der alte Zustand ist aber frisch aus der Datenbank: Ein Vergleich meldete Änderungen anderer
    # (etwa eine inzwischen erteilte Genehmigung als zurückgenommen), die dieses Speichern nicht vorgenommen hat.
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        skip_fields = skip_fields | {
            field.name
            for field in instance._meta.concrete_fields
            if field.name not in update_fields and field.attname not in update_fields
        }
    changes = audit_core.build_changes(old_instance, instance, skip_fields=skip_fields)
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
    # Endgültiges Löschen mit Freigabe (Issue #897): Zahlen der mitgelöschten Daten im Eintrag des Objekts
    log_event("delete", instance, changes=getattr(instance, "_audit_delete_changes", None))


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
        FactionStandardAgendaItem,
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
        FactionStandardAgendaItem,
    ]

    for model in audited_models:
        uid = f"faction_audit_{model.__name__}"
        pre_save.connect(audit_pre_save, sender=model, dispatch_uid=f"{uid}_pre_save")
        post_save.connect(audit_post_save, sender=model, dispatch_uid=f"{uid}_post_save")
        post_delete.connect(audit_post_delete, sender=model, dispatch_uid=f"{uid}_post_delete")

    # Kaskadenlösch-Schutz: Organisation wird gelöscht -> nichts protokollieren
    pre_delete.connect(organization_pre_delete, sender=Organization, dispatch_uid="faction_audit_org_pre_delete")
    post_delete.connect(organization_post_delete, sender=Organization, dispatch_uid="faction_audit_org_post_delete")
