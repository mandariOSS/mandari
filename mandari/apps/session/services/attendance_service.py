# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheits-Service für das Session RIS (Issue #30).

Zentrale Logik für:
- Erzeugen der Anwesenheitsliste aus der aktuellen Gremienbesetzung
  (inkl. Vertreter/Nachrücker; Stimmrecht wird aus der Besetzung übernommen)
- Beschlussfähigkeits-Berechnung (Quorum: mehr als die Hälfte der
  stimmberechtigten Mitglieder anwesend)
"""

from django.db.models import Q
from django.utils import timezone

from apps.session.models import SessionAttendance, SessionMeeting

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


def active_memberships(meeting: SessionMeeting):
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
        notes = ""
        if membership.substitute_for_id:
            notes = f"Vertretung für {membership.substitute_for.display_name}"
        _attendance, created = SessionAttendance.objects.get_or_create(
            meeting=meeting,
            person=membership.person,
            defaults={
                "status": "invited",
                "role": _ROLE_MAP.get(membership.role, "member"),
                "has_voting_rights": membership.has_voting_rights,
                "notes": notes,
            },
        )
        if created:
            created_count += 1
    return created_count


def quorum_status(meeting: SessionMeeting) -> dict:
    """
    Beschlussfähigkeit (Quorum) live berechnen.

    Grundlage: alle Anwesenheitszeilen mit Stimmrecht; anwesend zählt
    present/joined_late. Beschlussfähig ab mehr als der Hälfte.

    Returns:
        dict: voting_total, voting_present, required, met, has_list
    """
    attendances = meeting.attendances.all()
    voting = [a for a in attendances if a.has_voting_rights]
    present = [a for a in voting if a.status in PRESENT_STATUSES]
    total = len(voting)
    required = (total // 2) + 1 if total else 0
    return {
        "has_list": bool(attendances),
        "voting_total": total,
        "voting_present": len(present),
        "required": required,
        "met": total > 0 and len(present) >= required,
    }
