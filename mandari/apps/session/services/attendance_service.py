# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheits-Service für das Session RIS (Issue #30).

Zentrale Logik für:
- Erzeugen der Anwesenheitsliste aus der aktuellen Gremienbesetzung
  (inkl. Vertreter/Nachrücker; Stimmrecht wird aus der Besetzung übernommen).
  Bei gemeinsamen Sitzungen mehrerer Gremien (Issue #317) zählt die Besetzung aller beteiligten
  Gremien; wer mehreren angehört, erhält eine Zeile (und damit eine Stimme).
- Beschlussfähigkeits-Berechnung (Quorum: mehr als die Hälfte der
  stimmberechtigten Mitglieder anwesend). Zugeschaltete zählen mit, außer während einer Störung und bei
  Abstimmungen, von denen das Landesprofil nur sie ausschließt (Issue #139, participation_service). Ist der
  Vorgang mit Zugeschalteten in der ganzen Sitzung unzulässig (Issue #754), zählen sie weiter mit.
- Sitze und Stellvertretungen (:func:`seat_split`): Grundgesamtheit sind die Sitze der Mitglieder.
  Eine Stellvertretung (Mitgliedschaft mit „Vertretung für“) zählt nur, wenn sie für eine nicht
  anwesende Person nachrückt – je vertretener Person höchstens eine.
"""

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Roster:
    """Besetzung zum Sitzungsdatum aus Sicht der Stimmen: Mitglieder mit Stimmrecht und Stellvertretungen."""

    #: Personen mit Stimmrecht in einem beteiligten Gremium (ohne reine Stellvertretungen)
    voting_members: frozenset[Any] = frozenset()
    #: Stellvertretung -> vertretene Personen; nur wer in keinem beteiligten Gremium selbst Mitglied ist
    substitutes: dict[Any, frozenset[Any]] = field(default_factory=dict)


def roster(meeting: SessionMeeting) -> Roster:
    """Besetzung für Beschlussfähigkeit und Stimmrecht – eine Abfrage."""
    rows = list(active_memberships(meeting).values_list("person_id", "substitute_for_id", "has_voting_rights"))
    own = {person for person, principal, _voting in rows if principal is None}
    substitutes: dict[Any, set[Any]] = {}
    for person, principal, _voting in rows:
        if principal is not None and person not in own and principal != person:
            substitutes.setdefault(person, set()).add(principal)
    return Roster(
        voting_members=frozenset(person for person, _principal, voting in rows if voting and person not in substitutes),
        substitutes={person: frozenset(principals) for person, principals in substitutes.items()},
    )


@dataclass
class SeatSplit:
    """Stimmberechtigte Zeilen der Anwesenheitsliste, aufgeteilt nach Sitzen."""

    #: Mitglieder mit Stimmrecht: je Zeile ein Sitz (Grundgesamtheit der Beschlussfähigkeit)
    members: list[Any] = field(default_factory=list)
    #: Stellvertretungen, die für eine nicht anwesende Person nachrücken (zählen und stimmen an deren Stelle)
    stepping_in: list[Any] = field(default_factory=list)
    #: übrige Stellvertretungen: kein freier Sitz, zählen nicht und stimmen nicht ab
    standby: list[Any] = field(default_factory=list)
    #: Personen-IDs der Mitglieder, die nicht anwesend sind (vor dem Nachrücken)
    vacant: frozenset[Any] = frozenset()


def seat_split(
    voting: list[Any], substitutes: dict[Any, frozenset[Any]], *, active_statuses: tuple[str, ...]
) -> SeatSplit:
    """
    Stimmberechtigte Zeilen nach Sitzen aufteilen.

    Mitglieder halten je einen Sitz. Eine Stellvertretung rückt nach, wenn sie selbst anwesend ist
    (``active_statuses``) und eine von ihr vertretene Person nicht anwesend ist (``PRESENT_STATUSES``);
    jede vertretene Person wird höchstens einmal vertreten. Die Reihenfolge der Zeilen entscheidet,
    wer zuerst nachrückt.
    """
    members = [a for a in voting if a.person_id not in substitutes]
    vacant = frozenset(a.person_id for a in members if a.status not in PRESENT_STATUSES)
    free = set(vacant)
    split = SeatSplit(members=members, vacant=vacant)
    for attendance in voting:
        principals = substitutes.get(attendance.person_id)
        if principals is None:
            continue
        open_seats = sorted((principal for principal in principals if principal in free), key=str)
        if attendance.status in active_statuses and open_seats:
            free.discard(open_seats[0])
            split.stepping_in.append(attendance)
        else:
            split.standby.append(attendance)
    return split


def voting_rows(attendances: list[Any]) -> list[Any]:
    """Zeilen mit Stimmrecht – ohne Gäste und Protokollführung, die nie abstimmen."""
    from apps.session.services.voting_service import NON_VOTING_ROLES

    return [a for a in attendances if a.has_voting_rights and a.role not in NON_VOTING_ROLES]


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


def quorum_status(
    meeting: SessionMeeting,
    item: Any = None,
    *,
    attendances: list[Any] | None = None,
    substitutes: dict[Any, frozenset[Any]] | None = None,
) -> dict[str, Any]:
    """
    Beschlussfähigkeit (Quorum) live berechnen.

    Grundlage: die Sitze, also die Anwesenheitszeilen der Mitglieder mit Stimmrecht (ohne Gäste und
    Protokollführung, wie bei der Stimmabgabe); anwesend zählt present/joined_late. Stellvertretungen
    erhöhen die Zahl der Sitze nicht; anwesend zählen sie nur, wenn sie für ein nicht anwesendes Mitglied
    nachrücken (:func:`seat_split`, ``substitutes`` aus :func:`roster`, sonst nachgeladen). Beschlussfähig
    ab mehr als der Hälfte — Berechnung über den gemeinsamen Baustein apps/common/quorum.py
    (Issue #69, generalisiert aus dieser Session-Implementierung).

    Teilnahmeart (Issue #139): Zugeschaltete zählen wie Anwesende im Raum, außer während einer
    andauernden Störung (nicht erreichbar). Für einen TOP (``item``) zählen sie nicht, wenn das Landesprofil
    nur sie von dieser Abstimmung ausschließt („für Zugeschaltete ausgeschlossen“, z. B. Bayern: anwesend
    *und stimmberechtigt*, Art. 47 Abs. 2 GO). Ist der Vorgang mit Zugeschalteten in der ganzen Sitzung
    unzulässig („in der Sitzung unzulässig“, Issue #754), zählen sie mit: Sie gelten als anwesend (z. B.
    § 64 Abs. 3 Satz 5 NKomVG); ``remote_rule`` liefert dann Sperre und Hinweis.

    In einer Präsenzsitzung als zugeschaltet erfasste Personen zählen bewusst weiter mit: Meist ist das
    eine überholte Teilnahmeart nach einer Änderung des Sitzungsformats. Die Beschlussfähigkeit soll nicht
    stillschweigend kippen; die Sitzungsseite warnt stattdessen und zeigt die Spalte „Teilnahme“ zum
    Korrigieren (``attendance_panel``).

    Returns:
        dict: voting_total, voting_present, required, met, has_list, rule sowie remote_present
        (davon zugeschaltet), disrupted und remote_excluded (Namen, nicht mitgezählt) und remote_rule
    """
    from apps.common.quorum import quorum_status as common_quorum_status

    if attendances is None:
        attendances = list(
            participation_service.with_disruptions(meeting.attendances.select_related("person"), meeting)
        )
    if substitutes is None:
        substitutes = roster(meeting).substitutes if attendances else {}

    rule = participation_service.remote_vote_rule(meeting, item, attendances=attendances) if item is not None else None
    # Gäste und Protokollführung stimmen nie ab – auch mit gesetztem Stimmrecht nicht (wie voting_service)
    split = seat_split(voting_rows(attendances), substitutes, active_statuses=PRESENT_STATUSES)
    present: list[Any] = []
    disrupted: list[str] = []
    excluded: list[str] = []
    for attendance in split.members + split.stepping_in:
        if attendance.status not in PRESENT_STATUSES:
            continue
        if participation_service.is_disrupted(attendance):
            disrupted.append(attendance.person.display_name)
        elif rule is not None and rule.excluded and attendance.is_remote:
            excluded.append(attendance.person.display_name)
        else:
            present.append(attendance)
    status = common_quorum_status(
        voting_total=len(split.members),
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
        # Spalte „Teilnahme“: in hybriden und digitalen Sitzungen, sonst sobald jemand zugeschaltet erfasst ist –
        # dann lässt sich die Teilnahmeart in der Zeile korrigieren (Hinweis ``remote_in_presence``)
        "mode_column": show_mode,
        "remote_attendances": remote,
        "remote_in_presence": bool(remote) and not participation_service.remote_allowed(meeting),
        "chair_hint": participation_service.chair_hint(meeting, attendances),
    }
