# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheits-Service für das Session RIS (Issue #30).

Zentrale Logik für:
- Erzeugen der Anwesenheitsliste aus der aktuellen Gremienbesetzung
  (inkl. Vertreter/Nachrücker; Stimmrecht wird aus der Besetzung übernommen).
  Bei gemeinsamen Sitzungen mehrerer Gremien (Issue #317) zählt die Besetzung aller beteiligten
  Gremien; wer mehreren angehört, erhält eine Zeile (und damit eine Stimme).
- Beschlussfähigkeits-Berechnung (Quorum: mehr als die Hälfte der
  stimmberechtigten Mitglieder anwesend)
"""

from typing import Any

from django.db.models import QuerySet

from apps.session.models import SessionAttendance, SessionMeeting, SessionOrganizationMembership, SessionPerson
from apps.session.services import joint_meeting_service

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
    """Aktive Mitgliedschaften der Besetzung zum Sitzungsdatum – aller beteiligten Gremien (Issue #317)."""
    return joint_meeting_service.active_memberships(meeting)


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
    # Je Person ein Sitz, auch bei gemeinsamen Sitzungen mehrerer Gremien (Issue #317)
    for seat in joint_meeting_service.seats(meeting):
        _attendance, created = SessionAttendance.objects.get_or_create(
            meeting=meeting,
            person=seat.person,
            defaults=attendance_defaults(seat.membership, has_voting_rights=seat.has_voting_rights),
        )
        if created:
            created_count += 1
    return created_count


def attendance_defaults(
    membership: SessionOrganizationMembership | None, *, has_voting_rights: bool | None = None
) -> dict[str, Any]:
    """
    Vorbelegung einer Anwesenheitszeile aus der Besetzung.

    Funktion und Stimmrecht kommen aus der Mitgliedschaft; Vertreter erhalten den Hinweis,
    für wen sie vertreten. Ohne Mitgliedschaft (z. B. ausgeschieden) gilt: Mitglied ohne Notiz.
    ``has_voting_rights`` überschreibt das Stimmrecht (gemeinsame Sitzung: Stimmrecht in einem
    der beteiligten Gremien genügt).
    """
    if membership is None:
        return {"status": "invited", "role": "member", "has_voting_rights": True, "notes": ""}
    notes = ""
    if membership.substitute_for_id and membership.substitute_for is not None:
        notes = f"Vertretung für {membership.substitute_for.display_name}"
    return {
        "status": "invited",
        "role": _ROLE_MAP.get(membership.role, "member"),
        "has_voting_rights": membership.has_voting_rights if has_voting_rights is None else has_voting_rights,
        "notes": notes,
    }


def ensure_attendance(meeting: SessionMeeting, person: SessionPerson) -> SessionAttendance:
    """Anwesenheitszeile einer Person holen oder aus ihrer Besetzung anlegen (Issue #225)."""
    seat = joint_meeting_service.seat_for(meeting, person)
    defaults = (
        attendance_defaults(seat.membership, has_voting_rights=seat.has_voting_rights)
        if seat is not None
        else attendance_defaults(None)
    )
    attendance, _created = SessionAttendance.objects.get_or_create(meeting=meeting, person=person, defaults=defaults)
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
