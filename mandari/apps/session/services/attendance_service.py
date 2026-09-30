# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheits-Service für das Session RIS (Issue #30).

Zentrale Logik für:
- Erzeugen der Anwesenheitsliste aus der aktuellen Gremienbesetzung
  (inkl. Vertreter/Nachrücker; Stimmrecht wird aus der Besetzung übernommen).
  Bei gemeinsamen Sitzungen mehrerer Gremien (Issue #317) zählt die Besetzung aller beteiligten
  Gremien; wer mehreren angehört, erhält eine Zeile (und damit eine Stimme).
- Beschlussfähigkeits-Berechnung (Quorum: mehr als die Hälfte der
  stimmberechtigten Mitglieder anwesend). Zugeschaltete zählen mit, außer während einer Störung und
  bei Abstimmungen, von denen das Landesprofil sie ausschließt (Issue #139, participation_service).
"""

from typing import Any

from django.db.models import QuerySet

from apps.session.models import SessionAttendance, SessionMeeting, SessionOrganizationMembership, SessionPerson
from apps.session.services import joint_meeting_service, participation_service

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
    # Digitale Sitzung (Issue #139): alle zugeschaltet
    mode = participation_service.default_mode(meeting)
    # Je Person ein Sitz, auch bei gemeinsamen Sitzungen mehrerer Gremien (Issue #317)
    for seat in joint_meeting_service.seats(meeting):
        defaults = attendance_defaults(seat.membership, has_voting_rights=seat.has_voting_rights)
        defaults["participation_mode"] = mode
        _attendance, created = SessionAttendance.objects.get_or_create(
            meeting=meeting,
            person=seat.person,
            defaults=defaults,
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
    defaults["participation_mode"] = participation_service.default_mode(meeting)
    attendance, _created = SessionAttendance.objects.get_or_create(meeting=meeting, person=person, defaults=defaults)
    return attendance


def quorum_status(meeting: SessionMeeting, item: Any = None, *, attendances: list[Any] | None = None) -> dict[str, Any]:
    """
    Beschlussfähigkeit (Quorum) live berechnen.

    Grundlage: alle Anwesenheitszeilen mit Stimmrecht (ohne Gäste und Protokollführung, wie bei der
    Stimmabgabe); anwesend zählt present/joined_late. Beschlussfähig ab mehr als der Hälfte —
    Berechnung über den gemeinsamen Baustein apps/common/quorum.py
    (Issue #69, generalisiert aus dieser Session-Implementierung).

    Teilnahmeart (Issue #139): Zugeschaltete zählen wie Anwesende im Raum, außer während einer
    andauernden Störung (nicht erreichbar). Für einen TOP (``item``) zählen sie nicht, wenn das
    Landesprofil sie von dieser Abstimmung ausschließt (Wahl, geheime Abstimmung).

    Returns:
        dict: voting_total, voting_present, required, met, has_list, rule sowie remote_present
        (davon zugeschaltet), disrupted und remote_excluded (Namen, nicht mitgezählt) und remote_rule
    """
    from apps.common.quorum import quorum_status as common_quorum_status

    if attendances is None:
        attendances = list(
            participation_service.with_disruptions(meeting.attendances.select_related("person"), meeting)
        )
    from apps.session.services.voting_service import NON_VOTING_ROLES

    rule = participation_service.remote_vote_rule(meeting, item) if item is not None else None
    # Gäste und Protokollführung stimmen nie ab – auch mit gesetztem Stimmrecht nicht (wie voting_service)
    voting = [a for a in attendances if a.has_voting_rights and a.role not in NON_VOTING_ROLES]
    present: list[Any] = []
    disrupted: list[str] = []
    excluded: list[str] = []
    for attendance in voting:
        if attendance.status not in PRESENT_STATUSES:
            continue
        if participation_service.is_disrupted(attendance):
            disrupted.append(attendance.person.display_name)
        elif rule is not None and rule.excluded and attendance.is_remote:
            excluded.append(attendance.person.display_name)
        else:
            present.append(attendance)
    status = common_quorum_status(
        voting_total=len(voting),
        voting_present=len(present),
        has_list=bool(attendances),
    )
    status.update(
        {
            "remote_present": sum(1 for a in present if a.is_remote),
            "disrupted": disrupted,
            "remote_excluded": excluded,
            "remote_rule": rule,
        }
    )
    return status


def attendance_panel(meeting: SessionMeeting) -> dict[str, Any]:
    """
    Anwesenheitsliste für die Sitzungsseite: Zeilen mit Teilnahme-Vermerk, Beschlussfähigkeit und Hinweise
    zur Teilnahmeart (Issue #139). Lädt Personen und Störungsvermerke in je einer Abfrage.
    """
    attendances = list(
        participation_service.with_disruptions(meeting.attendances.select_related("person"), meeting).order_by(
            "person__family_name"
        )
    )
    show_mode = participation_service.show_mode(meeting, attendances)
    for attendance in attendances:
        attendance.participation_note = participation_service.participation_note(  # type: ignore[attr-defined]
            attendance, show_mode=show_mode
        )
    remote = [a for a in attendances if a.is_remote]
    return {
        "attendances": attendances,
        "quorum": quorum_status(meeting, attendances=attendances),
        "remote_allowed": participation_service.remote_allowed(meeting),
        "remote_attendances": remote,
        "remote_in_presence": bool(remote) and not participation_service.remote_allowed(meeting),
        "chair_hint": participation_service.chair_hint(meeting, attendances),
    }
