# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheits-Service für das Session RIS (Issue #30).

Zentrale Logik für:
- Erzeugen der Anwesenheitsliste aus der aktuellen Gremienbesetzung
  (inkl. Vertreter/Nachrücker; Stimmrecht wird aus der Besetzung übernommen)
- Beschlussfähigkeits-Berechnung (Quorum: mehr als die Hälfte der
  stimmberechtigten Mitglieder anwesend)
"""

from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.session.models import SessionAttendance, SessionMeeting, SessionOrganizationMembership, SessionPerson

# Besetzungs-Funktion -> Anwesenheits-Funktion
_ROLE_MAP = {
    "member": "member",
    "chair": "chair",
    "deputy_chair": "deputy_chair",
    "expert_citizen": "expert",
    "advisor": "expert",
    "guest": "guest",
}

# Status, die für die Beschlussfähigkeit als "im Raum" zählen
PRESENT_STATUSES = ("present", "joined_late")


def active_memberships(meeting: SessionMeeting) -> QuerySet[SessionOrganizationMembership]:
    """Aktive Mitgliedschaften der Besetzung zum Sitzungsdatum."""
    meeting_date = timezone.localtime(meeting.start).date()
    return (
        meeting.organization.memberships.select_related("person", "substitute_for")
        .filter(person__is_active=True)
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=meeting_date))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=meeting_date))
        .order_by("person__family_name", "person__given_name")
    )


def generate_attendance(meeting: SessionMeeting) -> int:
    """
    Anwesenheitsliste aus der Gremienbesetzung vorbefüllen.

    - je Person genau eine Zeile (get_or_create — mehrfacher Aufruf ergänzt
      nur neu hinzugekommene Mitglieder, ohne erfasste Stati zu überschreiben)
    - Funktion und Stimmrecht aus der Besetzung
    - Vertreter (substitute_for) erhalten einen Hinweis, für wen sie
      vertreten — korrekt für Sitzungsgeld-Abrechnung

    Returns:
        Anzahl neu angelegter Zeilen
    """
    created_count = 0
    for membership in active_memberships(meeting):
        _attendance, created = SessionAttendance.objects.get_or_create(
            meeting=meeting,
            person=membership.person,
            defaults=attendance_defaults(membership),
        )
        if created:
            created_count += 1
    return created_count


def attendance_defaults(membership: SessionOrganizationMembership | None) -> dict[str, Any]:
    """
    Vorbelegung einer Anwesenheitszeile aus der Besetzung.

    Funktion und Stimmrecht kommen aus der Mitgliedschaft; Vertreter erhalten den Hinweis,
    für wen sie vertreten. Ohne Mitgliedschaft (z. B. ausgeschieden) gilt: Mitglied ohne Notiz.
    """
    if membership is None:
        return {"status": "invited", "role": "member", "has_voting_rights": True, "notes": ""}
    notes = ""
    if membership.substitute_for_id and membership.substitute_for is not None:
        notes = f"Vertretung für {membership.substitute_for.display_name}"
    return {
        "status": "invited",
        "role": _ROLE_MAP.get(membership.role, "member"),
        "has_voting_rights": membership.has_voting_rights,
        "notes": notes,
    }


def ensure_attendance(meeting: SessionMeeting, person: SessionPerson) -> SessionAttendance:
    """Anwesenheitszeile einer Person holen oder aus ihrer Besetzung anlegen (Issue #225)."""
    membership = active_memberships(meeting).filter(person=person).first()
    attendance, _created = SessionAttendance.objects.get_or_create(
        meeting=meeting, person=person, defaults=attendance_defaults(membership)
    )
    return attendance


def quorum_status(meeting: SessionMeeting) -> dict:
    """
    Beschlussfähigkeit (Quorum) live berechnen.

    Grundlage: alle Anwesenheitszeilen mit Stimmrecht; anwesend zählt
    present/joined_late. Beschlussfähig ab mehr als der Hälfte —
    Berechnung über den gemeinsamen Baustein apps/common/quorum.py
    (Issue #69, generalisiert aus dieser Session-Implementierung).

    Returns:
        dict: voting_total, voting_present, required, met, has_list, rule
    """
    from apps.common.quorum import quorum_status as common_quorum_status

    attendances = meeting.attendances.all()
    voting = [a for a in attendances if a.has_voting_rights]
    present = [a for a in voting if a.status in PRESENT_STATUSES]
    return common_quorum_status(
        voting_total=len(voting),
        voting_present=len(present),
        has_list=bool(attendances),
    )
