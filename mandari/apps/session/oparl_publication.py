# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Publikationslogik der Session-OParl-API (Issue #35).

Zwei Aufgaben:

1. **Sichtbarkeits-Querysets**: Welche Objekte eines Mandanten sind über
   die öffentliche OParl-API sichtbar? Grundsatz: NUR öffentliche Daten
   (``is_public``), NÖ-Teile von Sitzungen und deren Anlagen niemals.

2. **Tombstone-Buchhaltung** (OParl 1.1 §2.8): Objekte, die einmal
   öffentlich ausgeliefert wurden und danach gelöscht oder auf
   „nicht öffentlich“ gestellt werden, hinterlassen einen
   SessionOParlTombstone. Inkrementelle Clients (modified_since) bekommen
   Löschungen so zuverlässig mit; Objekt-Endpunkte liefern HTTP 200 mit
   dem gekürzten Objekt. Die Receiver werden in signals.py registriert.

Sicherheit: Tombstones enthalten keine Inhalte — nur Typ, ID, Zeitstempel.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOParlTombstone,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
)

# =============================================================================
# Sichtbarkeits-Querysets (Ö/NÖ strikt)
# =============================================================================


def visible_organizations(tenant):
    return SessionOrganization.objects.filter(tenant=tenant)


def visible_persons(tenant):
    return SessionPerson.objects.filter(tenant=tenant)


def visible_memberships(tenant):
    return SessionOrganizationMembership.objects.filter(organization__tenant=tenant)


def visible_meetings(tenant):
    return SessionMeeting.objects.filter(tenant=tenant, is_public=True)


def visible_agenda_items(tenant):
    """Nur Ö-TOPs öffentlicher Sitzungen (NÖ-Teil niemals)."""
    return SessionAgendaItem.objects.filter(
        meeting__tenant=tenant,
        meeting__is_public=True,
        is_public=True,
    )


def visible_papers(tenant):
    return SessionPaper.objects.filter(tenant=tenant, is_public=True)


def visible_files(tenant):
    """
    Nur öffentliche Anlagen, deren übergeordnetes Objekt selbst öffentlich
    ist — eine Ö-Datei an einer NÖ-Vorlage bleibt unsichtbar.
    """
    return SessionFile.objects.filter(tenant=tenant, is_public=True).filter(
        Q(paper__isnull=False, paper__is_public=True)
        | Q(meeting__isnull=False, meeting__is_public=True)
        | Q(
            agenda_item__isnull=False,
            agenda_item__is_public=True,
            agenda_item__meeting__is_public=True,
        )
    )


def visible_consultations(tenant):
    return SessionConsultation.objects.filter(paper__tenant=tenant, paper__is_public=True)


def visible_legislative_terms(tenant):
    return SessionLegislativeTerm.objects.filter(tenant=tenant)


# =============================================================================
# Publikations-Status einzelner Instanzen (für die Tombstone-Receiver)
# =============================================================================


def _is_published(instance) -> bool:
    """War/ist das Objekt über die öffentliche OParl-API sichtbar?"""
    try:
        if isinstance(instance, SessionMeeting):
            return instance.is_public
        if isinstance(instance, SessionPaper):
            return instance.is_public
        if isinstance(instance, SessionAgendaItem):
            return instance.is_public and instance.meeting.is_public
        if isinstance(instance, SessionFile):
            if not instance.is_public:
                return False
            if instance.paper_id:
                return instance.paper.is_public
            if instance.meeting_id:
                return instance.meeting.is_public
            if instance.agenda_item_id:
                item = instance.agenda_item
                return item.is_public and item.meeting.is_public
            return False
        if isinstance(instance, SessionConsultation):
            return instance.paper.is_public
        # Organisationen, Personen, Mitgliedschaften, Wahlperioden sind
        # nicht Ö/NÖ-unterteilt und damit immer Teil der öffentlichen API.
        return True
    except ObjectDoesNotExist:
        return False


def _resolve_tenant_id(instance):
    try:
        if isinstance(instance, (SessionMeeting, SessionPaper, SessionFile, SessionLegislativeTerm)):
            return instance.tenant_id
        if isinstance(instance, (SessionOrganization, SessionPerson)):
            return instance.tenant_id
        if isinstance(instance, SessionAgendaItem):
            return instance.meeting.tenant_id
        if isinstance(instance, SessionConsultation):
            return instance.paper.tenant_id
        if isinstance(instance, SessionOrganizationMembership):
            return instance.organization.tenant_id
    except ObjectDoesNotExist:
        return None
    return None


# Modell -> API-Objekttyp (URL-Segment)
KIND_BY_MODEL = {
    SessionOrganization: "organization",
    SessionPerson: "person",
    SessionOrganizationMembership: "membership",
    SessionMeeting: "meeting",
    SessionAgendaItem: "agendaitem",
    SessionPaper: "paper",
    SessionFile: "file",
    SessionConsultation: "consultation",
    SessionLegislativeTerm: "legislativeterm",
}


# =============================================================================
# Tombstone-Buchhaltung
# =============================================================================


def _write_tombstone(tenant_id, kind, object_id, object_created_at):
    """Tombstone anlegen/aktualisieren (idempotent; nie während Tenant-Löschung)."""
    if tenant_id is None or audit.is_tenant_deleting(tenant_id):
        return
    SessionOParlTombstone.objects.update_or_create(
        tenant_id=tenant_id,
        oparl_type=kind,
        object_id=object_id,
        defaults={
            "object_created_at": object_created_at or timezone.now(),
            "deleted_at": timezone.now(),
        },
    )


def _clear_tombstones(tenant_id, pairs):
    """Tombstones entfernen (Objekt wurde wieder veröffentlicht)."""
    if tenant_id is None or not pairs:
        return
    query = Q()
    for kind, object_id in pairs:
        query |= Q(oparl_type=kind, object_id=object_id)
    SessionOParlTombstone.objects.filter(tenant_id=tenant_id).filter(query).delete()


def _dependents(instance):
    """
    Abhängige veröffentlichte Objekte, deren Sichtbarkeit an ``instance``
    hängt (für Kaskaden bei Ö->NÖ-Wechseln): (kind, id, created_at)-Tripel.
    """
    result = []
    if isinstance(instance, SessionMeeting):
        for item in instance.agenda_items.filter(is_public=True):
            result.append(("agendaitem", item.id, item.created_at))
            for file in item.files.filter(is_public=True):
                result.append(("file", file.id, file.created_at))
        for file in instance.files.filter(is_public=True):
            result.append(("file", file.id, file.created_at))
    elif isinstance(instance, SessionPaper):
        for file in instance.files.filter(is_public=True):
            result.append(("file", file.id, file.created_at))
        for consultation in instance.consultations.all():
            result.append(("consultation", consultation.id, consultation.created_at))
    elif isinstance(instance, SessionAgendaItem):
        for file in instance.files.filter(is_public=True):
            result.append(("file", file.id, file.created_at))
    return result


def tombstone_post_delete(sender, instance, **kwargs):
    """post_delete: Grabstein für zuvor veröffentlichte Objekte."""
    kind = KIND_BY_MODEL.get(sender)
    if kind is None:
        return
    tenant_id = _resolve_tenant_id(instance)
    if tenant_id is None or audit.is_tenant_deleting(tenant_id):
        return
    if _is_published(instance):
        _write_tombstone(tenant_id, kind, instance.pk, instance.created_at)


def tombstone_post_save(sender, instance, created, **kwargs):
    """
    post_save: Ö/NÖ-Wechsel behandeln.

    - Ö -> NÖ: Tombstones für das Objekt und abhängige Objekte anlegen
    - NÖ -> Ö: Tombstones wieder entfernen (Objekt taucht erneut auf)

    Nutzt den von audit.audit_pre_save gemerkten Alt-Zustand
    (``instance._audit_old``) — die Modelle sind alle auditiert.
    """
    if kwargs.get("raw") or created:
        return
    kind = KIND_BY_MODEL.get(sender)
    if kind is None:
        return
    old = getattr(instance, "_audit_old", None)
    if old is None:
        return

    was_published = _is_published(old)
    is_published = _is_published(instance)
    if was_published == is_published:
        return

    tenant_id = _resolve_tenant_id(instance)
    if tenant_id is None:
        return

    if was_published and not is_published:
        _write_tombstone(tenant_id, kind, instance.pk, instance.created_at)
        for dep_kind, dep_id, dep_created in _dependents(instance):
            _write_tombstone(tenant_id, dep_kind, dep_id, dep_created)
    else:
        pairs = [(kind, instance.pk)]
        pairs.extend((dep_kind, dep_id) for dep_kind, dep_id, _ in _dependents(instance))
        _clear_tombstones(tenant_id, pairs)
