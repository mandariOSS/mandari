# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit (Issue #140): die laufende Sitzung live steuern und mitlesen.

Sitzungsleitung und Protokollführung (Recht ``conduct_meetings``) steuern die Sitzung:

- **Sitzung eröffnen und schließen**: tatsächlicher Beginn und Ende, Status „Laufend“ bzw. „Abgeschlossen“.
  Beim Schließen enden der laufende TOP und andauernde Störungen mit der Uhrzeit des Schließens.
- **TOP aufrufen und beenden**: füllt ``start_time``/``end_time``. Ein neu aufgerufener TOP beendet den
  laufenden; der erste Aufruf eröffnet die Sitzung, falls das noch niemand getan hat. Nach der Sitzung
  sind damit alle Zeiten der behandelten TOPs ohne Nacharbeit gefüllt. Ein erneuter Aufruf (Wiederaufnahme)
  behält den Beginn und setzt das Ende neu.
- **Anwesenheitswechsel**: anwesend, kommt (verspätet, mit Uhrzeit), geht (vorzeitig, mit Uhrzeit),
  kommt zurück, abwesend.
- **Störungsprotokoll** Zugeschalteter (Issue #139): Beginn „ab jetzt“ mit Ursache und internem Vermerk –
  etwa „telefonisch gemeldet“, wenn die Person über einen zweiten Weg Bescheid gibt –, Ende „jetzt“.
- **Abstimmung öffnen und schließen** zum aufgerufenen TOP. Das Ergebnis steht wie bisher in
  ``vote_result`` und den Summen; Einzelstimmen (offen, namentlich) erfasst die Abstimmungserfassung
  (``voting_service``), deren Summen das Schließen übernimmt. Summen prüft ``voting_service.check_counts``
  gegen die stimmberechtigten Anwesenden.

Alle anderen mit dem Sichtrecht für Sitzungen sehen denselben Stand als Mitlese-Ansicht, nichtöffentliche
TOPs nur mit dem NÖ-Recht (sonst nur „nichtöffentlicher Teil“). Jede Aktion läuft in einer Transaktion mit
Sperre auf der Sitzung, damit zwei Steuernde nicht gleichzeitig zwei TOPs aufrufen. Das Audit-Log entsteht
über die Modell-Signale (Sitzung, TOP, Anwesenheit, Störung) und den direkten Eintrag „Abstimmungsergebnis
festgestellt“. Nach dem Commit erhalten offene Ansichten über Channels nur einen Hinweis; jede holt ihren
Stand mit den eigenen Rechten ab (``state_version`` erspart dabei unveränderte Antworten).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, OuterRef, Subquery
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionMeeting,
    SessionProtocol,
    SessionTenant,
    SessionUser,
)
from apps.session.services import attendance_service, participation_service, protocol_lock, voting_service
from apps.session.visibility import NON_PUBLIC_MEETINGS

#: Recht, das Cockpit zu steuern (Sitzungsleitung, Protokollführung)
CONTROL_PERMISSION = "conduct_meetings"
#: Recht, das Cockpit mitzulesen
VIEW_PERMISSION = "view_meetings"

RUNNING = "in_progress"
COMPLETED = "completed"
#: Anwesend im Raum bzw. zugeschaltet (wie die Beschlussfähigkeit)
PRESENT_STATUSES = attendance_service.PRESENT_STATUSES
#: Ergebnisse, die das Schließen einer Abstimmung feststellt
VOTE_RESULTS = ("approved", "rejected")
MAX_VOTES = 9999


class CockpitError(ValueError):
    """Aktion nicht möglich; die Meldung ist für die Oberfläche formuliert."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        #: fester, für Nutzer formulierter Text – nur diesen in Antworten ausgeben
        self.user_message = user_message


@dataclass(frozen=True)
class Outcome:
    """Ergebnis einer Aktion für die Rückmeldung (Toast bzw. Meldung)."""

    message: str
    level: str = "success"


@dataclass
class CockpitState:
    """Stand der Sitzung für Cockpit und Mitlese-Ansicht (je Person nach ihren Rechten)."""

    meeting: SessionMeeting
    can_control: bool
    can_capture_votes: bool
    locked: bool
    #: sichtbare TOPs in Sitzungsreihenfolge (Unterpunkte hinter ihrem TOP), je mit ``cockpit_status``
    items: list[SessionAgendaItem]
    current: SessionAgendaItem | None
    #: ein TOP läuft, ist für diese Person aber nicht sichtbar (nichtöffentlicher Teil)
    current_hidden: bool
    next_item: SessionAgendaItem | None
    vote_item: SessionAgendaItem | None
    vote_hidden: bool
    quorum: dict[str, Any]
    item_quorum: dict[str, Any] | None
    attendances: list[SessionAttendance]
    #: (Anwesenheit, Störung) – andauernde zuerst, dann die jüngsten
    disruptions: list[tuple[SessionAttendance, SessionAttendanceDisruption]]
    remote_allowed: bool
    version: str
    disruption_causes: list[tuple[str, str]] = field(
        default_factory=lambda: list(SessionAttendanceDisruption.CAUSE_CHOICES)
    )
    voting_methods: list[tuple[str, str]] = field(default_factory=lambda: list(SessionAgendaItem.VOTING_METHOD_CHOICES))

    @property
    def running(self) -> bool:
        return self.meeting.meeting_state == RUNNING

    @property
    def completed(self) -> bool:
        return self.meeting.meeting_state == COMPLETED

    @property
    def done_count(self) -> int:
        return sum(1 for item in self.items if item.end_time is not None and not item.is_withdrawn)


# =============================================================================
# Zugang
# =============================================================================


def can_control(permissions: Collection[str]) -> bool:
    """Darf die Person das Cockpit steuern? (Sitzungsleitung, Protokollführung)."""
    return CONTROL_PERMISSION in permissions


def meeting_for_viewer(user: Any, tenant_slug: str, meeting_id: str) -> Any:
    """
    Sitzung, deren Cockpit die Person mitlesen darf – sonst ``None`` (WebSocket-Anmeldung).

    Dieselben Regeln wie die Seite: aktiver Mandant, aktives Konto im Mandanten, Sichtrecht für Sitzungen,
    nichtöffentliche Sitzungen nur mit dem NÖ-Recht.
    """
    from apps.session.permissions import SessionPermissionChecker

    if not getattr(user, "is_authenticated", False):
        return None
    session_user = (
        SessionUser.objects.filter(user=user, tenant__slug=tenant_slug, tenant__is_active=True, is_active=True)
        .prefetch_related("roles")
        .first()
    )
    if session_user is None:
        return None
    permissions = SessionPermissionChecker(session_user).permissions
    if VIEW_PERMISSION not in permissions:
        return None
    return (
        SessionMeeting.objects.visible_to(permissions)
        .filter(pk=meeting_id, tenant_id=session_user.tenant_id)
        .values_list("pk", flat=True)
        .first()
    )


# =============================================================================
# Stand
# =============================================================================


def _status(item: SessionAgendaItem) -> str:
    if item.is_withdrawn:
        return "withdrawn"
    if item.start_time is not None and item.end_time is None:
        return "running"
    if item.end_time is not None:
        return "done"
    return "open"


def _is_running(item: SessionAgendaItem) -> bool:
    return not item.is_withdrawn and item.start_time is not None and item.end_time is None


def _ordered(items: list[SessionAgendaItem], include_non_public: bool) -> list[SessionAgendaItem]:
    """Sichtbare TOPs wie die Tagesordnung: öffentlicher Teil, dann nichtöffentlicher, Unterpunkte dahinter."""
    children: dict[Any, list[SessionAgendaItem]] = {}
    for item in items:
        if item.parent_id and (include_non_public or item.is_public):
            children.setdefault(item.parent_id, []).append(item)
    top_level = [i for i in items if i.parent_id is None and (include_non_public or i.is_public)]
    ordered: list[SessionAgendaItem] = []
    for item in sorted(top_level, key=lambda i: not i.is_public):
        ordered.append(item)
        ordered.extend(children.get(item.pk, []))
    return ordered


def _visible_items(
    meeting: SessionMeeting, all_items: list[SessionAgendaItem], permissions: Collection[str]
) -> list[SessionAgendaItem]:
    include_non_public = NON_PUBLIC_MEETINGS in permissions
    if not include_non_public and not meeting.is_public:
        return []
    return _ordered(all_items, include_non_public)


def _next_item(items: list[SessionAgendaItem], current: SessionAgendaItem | None) -> SessionAgendaItem | None:
    """Nächster nicht abgesetzter, noch nicht beendeter TOP nach dem laufenden (sonst der erste offene)."""
    start = 0
    if current is not None:
        positions = [index for index, item in enumerate(items) if item.pk == current.pk]
        start = positions[0] + 1 if positions else 0
    for item in items[start:]:
        if not item.is_withdrawn and item.end_time is None and item.pk != getattr(current, "pk", None):
            return item
    return None


def _all_items(meeting: SessionMeeting) -> list[SessionAgendaItem]:
    return list(meeting.agenda_items.order_by("order", "number"))


def _per_meeting(queryset: Any, group: str, function: Any) -> Subquery:
    """Kennzahl je Sitzung als Unterabfrage (Anzahl bzw. letzte Änderung)."""
    return Subquery(queryset.order_by().values(group).annotate(value=function).values("value")[:1])


def state_version(meeting: SessionMeeting) -> str:
    """
    Merkmal des Stands: ändert sich mit jeder Änderung an Sitzung, TOPs, Anwesenheit, Störungen und Niederschrift.

    Die Ansicht schickt es beim Nachladen mit; ist es unverändert, antwortet der Server ohne Inhalt
    (Polling ohne Rendern). Eine Abfrage mit Unterabfragen über indizierte Fremdschlüssel; Löschungen
    erkennt die Anzahl.
    """
    items = SessionAgendaItem.objects.filter(meeting_id=OuterRef("pk"))
    attendances = SessionAttendance.objects.filter(meeting_id=OuterRef("pk"))
    disruptions = SessionAttendanceDisruption.objects.filter(attendance__meeting_id=OuterRef("pk"))
    row = (
        SessionMeeting.objects.filter(pk=meeting.pk)
        .annotate(
            items_n=_per_meeting(items, "meeting_id", Count("id")),
            items_t=_per_meeting(items, "meeting_id", Max("updated_at")),
            attendances_n=_per_meeting(attendances, "meeting_id", Count("id")),
            attendances_t=_per_meeting(attendances, "meeting_id", Max("updated_at")),
            disruptions_n=_per_meeting(disruptions, "attendance__meeting_id", Count("id")),
            disruptions_t=_per_meeting(disruptions, "attendance__meeting_id", Max("updated_at")),
            protocol_status=Subquery(SessionProtocol.objects.filter(meeting_id=OuterRef("pk")).values("status")[:1]),
        )
        .values_list(
            "updated_at",
            "meeting_state",
            "items_n",
            "items_t",
            "attendances_n",
            "attendances_t",
            "disruptions_n",
            "disruptions_t",
            "protocol_status",
        )
        .first()
    )
    return hashlib.sha256(repr(row).encode("utf-8")).hexdigest()[:20]


def build_state(meeting: SessionMeeting, permissions: Collection[str]) -> CockpitState:
    """Stand für die Ansicht – nichtöffentliche TOPs nur mit dem NÖ-Recht."""
    # Merkmal vor den Daten: Ändert sich danach etwas, weicht es ab und die Ansicht lädt erneut
    version = state_version(meeting)
    all_items = _all_items(meeting)
    items = _visible_items(meeting, all_items, permissions)
    visible_ids = {item.pk for item in items}
    for item in items:
        item.cockpit_status = _status(item)  # type: ignore[attr-defined]

    running = [item for item in all_items if _is_running(item)]
    current = next((item for item in items if _is_running(item)), None)
    open_votes = [item for item in all_items if item.vote_open]
    vote_item = next((item for item in items if item.vote_open), None)

    attendances: list[SessionAttendance] = list(
        participation_service.with_disruptions(meeting.attendances.select_related("person"), meeting).order_by(
            "person__family_name", "person__given_name"
        )
    )
    show_mode = participation_service.show_mode(meeting, attendances)
    disruptions: list[tuple[SessionAttendance, SessionAttendanceDisruption]] = []
    for attendance in attendances:
        attendance.participation_note = participation_service.participation_note(  # type: ignore[attr-defined]
            attendance, show_mode=show_mode
        )
        attendance.disrupted = participation_service.is_disrupted(attendance)  # type: ignore[attr-defined]
        if attendance.is_remote:
            disruptions.extend((attendance, d) for d in participation_service.disruptions(attendance))
    disruptions.sort(key=lambda pair: (not pair[1].ongoing, -_minutes(pair[1].started_at)))

    quorum = attendance_service.quorum_status(meeting, attendances=attendances)
    focus = vote_item or current
    item_quorum = (
        attendance_service.quorum_status(meeting, focus, attendances=attendances) if focus is not None else None
    )
    return CockpitState(
        meeting=meeting,
        can_control=can_control(permissions),
        can_capture_votes="edit_protocols" in permissions,
        locked=protocol_lock.is_locked(meeting.pk),
        items=items,
        current=current,
        current_hidden=current is None and any(item.pk not in visible_ids for item in running),
        next_item=_next_item(items, current),
        vote_item=vote_item,
        vote_hidden=vote_item is None and any(item.pk not in visible_ids for item in open_votes),
        quorum=quorum,
        item_quorum=item_quorum,
        attendances=attendances,
        disruptions=disruptions,
        remote_allowed=participation_service.remote_allowed(meeting),
        version=version,
    )


def _minutes(moment: time) -> int:
    return moment.hour * 60 + moment.minute


# =============================================================================
# Aktionen
# =============================================================================


def _now() -> time:
    """Uhrzeit für TOP-, Anwesenheits- und Störungszeiten (Ortszeit, Minuten)."""
    return participation_service.now()


def _item(meeting: SessionMeeting, raw_id: Any, permissions: Collection[str]) -> SessionAgendaItem:
    """Sichtbarer TOP dieser Sitzung – nichtöffentliche nur mit dem NÖ-Recht."""
    from apps.common.params import uuid_param

    item_id = uuid_param(raw_id)
    item = (
        SessionAgendaItem.objects.visible_to(permissions).filter(pk=item_id, meeting=meeting).first()
        if item_id is not None
        else None
    )
    if item is None:
        raise CockpitError("Der Tagesordnungspunkt wurde nicht gefunden.")
    return item


def _attendance(meeting: SessionMeeting, raw_id: Any) -> SessionAttendance:
    from apps.common.params import uuid_param

    attendance_id = uuid_param(raw_id)
    attendance = (
        meeting.attendances.select_related("person").filter(pk=attendance_id).first()
        if attendance_id is not None
        else None
    )
    if attendance is None:
        raise CockpitError("Die Person steht nicht auf der Anwesenheitsliste.")
    return attendance


def _open_vote_item(meeting: SessionMeeting) -> SessionAgendaItem | None:
    return (
        meeting.agenda_items.filter(vote_opened_at__isnull=False, vote_closed_at__isnull=True)
        .order_by("order", "number")
        .first()
    )


def _ensure_no_open_vote(meeting: SessionMeeting) -> None:
    open_item = _open_vote_item(meeting)
    if open_item is not None:
        raise CockpitError(f"Bitte zuerst die Abstimmung zu TOP {open_item.number} schließen.")


def _start(meeting: SessionMeeting) -> None:
    if meeting.actual_start is None:
        meeting.actual_start = timezone.now()
    meeting.actual_end = None
    meeting.meeting_state = RUNNING
    meeting.save(update_fields=["actual_start", "actual_end", "meeting_state", "updated_at"])


def _end_running(meeting: SessionMeeting, moment: time, *, keep: Any = None) -> list[SessionAgendaItem]:
    """Laufende TOPs beenden (einzeln gespeichert: Audit-Log je TOP)."""
    ended = []
    running = meeting.agenda_items.filter(start_time__isnull=False, end_time__isnull=True, is_withdrawn=False)
    for item in running.exclude(pk=keep) if keep is not None else running:
        item.end_time = moment
        item.save(update_fields=["end_time", "updated_at"])
        ended.append(item)
    return ended


def open_meeting(meeting: SessionMeeting, data: Mapping[str, Any], **_: Any) -> Outcome:
    if meeting.meeting_state == RUNNING:
        raise CockpitError("Die Sitzung läuft bereits.")
    resumed = meeting.meeting_state == COMPLETED
    _start(meeting)
    if resumed:
        return Outcome("Die Sitzung wird fortgesetzt.")
    started = timezone.localtime(meeting.actual_start or timezone.now())
    return Outcome(f"Sitzung um {started:%H:%M} Uhr eröffnet.")


def close_meeting(meeting: SessionMeeting, data: Mapping[str, Any], **_: Any) -> Outcome:
    if meeting.meeting_state != RUNNING:
        raise CockpitError("Die Sitzung ist nicht eröffnet.")
    _ensure_no_open_vote(meeting)
    moment = _now()
    _end_running(meeting, moment)
    # Andauernde Störungen enden mit der Sitzung – sonst stünde in der Niederschrift „Störung ab …“ ohne Ende
    ongoing = SessionAttendanceDisruption.objects.filter(attendance__meeting=meeting, ended_at__isnull=True)
    for disruption in ongoing.select_related("attendance__meeting"):
        disruption.ended_at = moment
        disruption.save(update_fields=["ended_at", "updated_at"])
    ended = timezone.now()
    meeting.actual_end = ended
    meeting.meeting_state = COMPLETED
    meeting.save(update_fields=["actual_end", "meeting_state", "updated_at"])
    return Outcome(f"Sitzung um {timezone.localtime(ended):%H:%M} Uhr geschlossen.")


def call_item(meeting: SessionMeeting, data: Mapping[str, Any], *, permissions: Collection[str], **_: Any) -> Outcome:
    item = _item(meeting, data.get("item"), permissions)
    return _call(meeting, item)


def _call(meeting: SessionMeeting, item: SessionAgendaItem) -> Outcome:
    if item.is_withdrawn:
        raise CockpitError(f"TOP {item.number} ist abgesetzt.")
    if meeting.meeting_state == COMPLETED:
        raise CockpitError("Die Sitzung ist geschlossen. Zum Fortsetzen bitte erneut eröffnen.")
    if _is_running(item):
        raise CockpitError(f"TOP {item.number} ist bereits aufgerufen.")
    _ensure_no_open_vote(meeting)
    opened = meeting.meeting_state != RUNNING
    if opened:
        _start(meeting)
    moment = _now()
    _end_running(meeting, moment, keep=item.pk)
    fields = ["end_time", "updated_at"]
    if item.start_time is None:
        item.start_time = moment
        fields.append("start_time")
    item.end_time = None
    item.save(update_fields=fields)
    prefix = "Sitzung eröffnet. " if opened else ""
    return Outcome(f"{prefix}TOP {item.number} aufgerufen ({moment:%H:%M} Uhr).")


def next_item(meeting: SessionMeeting, data: Mapping[str, Any], *, permissions: Collection[str], **_: Any) -> Outcome:
    items = _visible_items(meeting, _all_items(meeting), permissions)
    current = next((item for item in items if _is_running(item)), None)
    following = _next_item(items, current)
    if following is None:
        raise CockpitError("Es gibt keinen weiteren offenen Tagesordnungspunkt.")
    return _call(meeting, following)


def end_item(meeting: SessionMeeting, data: Mapping[str, Any], *, permissions: Collection[str], **_: Any) -> Outcome:
    item = _item(meeting, data.get("item"), permissions)
    if not _is_running(item):
        raise CockpitError(f"TOP {item.number} ist nicht aufgerufen.")
    if item.vote_open:
        raise CockpitError(f"Bitte zuerst die Abstimmung zu TOP {item.number} schließen.")
    moment = _now()
    item.end_time = moment
    item.save(update_fields=["end_time", "updated_at"])
    return Outcome(f"TOP {item.number} beendet ({moment:%H:%M} Uhr).")


# --- Anwesenheit ---------------------------------------------------------------


def _append_note(attendance: SessionAttendance, text: str) -> None:
    attendance.notes = f"{attendance.notes}; {text}" if attendance.notes else text


def change_attendance(meeting: SessionMeeting, data: Mapping[str, Any], **_: Any) -> Outcome:
    """Anwesenheitswechsel: ``wechsel`` = anwesend | kommt | geht | zurueck | abwesend."""
    attendance = _attendance(meeting, data.get("attendance"))
    name = attendance.person.display_name
    change = str(data.get("wechsel", ""))
    moment = _now()
    present = attendance.status in PRESENT_STATUSES
    fields = ["status", "updated_at"]
    if change == "anwesend":
        if present or attendance.status == "left_early":
            raise CockpitError(f"{name} ist bereits als anwesend erfasst.")
        attendance.status = "present"
        message = f"{name} ist anwesend."
    elif change == "kommt":
        if present or attendance.status == "left_early":
            raise CockpitError(f"{name} ist bereits als anwesend erfasst.")
        attendance.status = "joined_late"
        attendance.arrival_time = moment
        fields.append("arrival_time")
        message = f"{name} ist ab {moment:%H:%M} Uhr anwesend."
    elif change == "geht":
        if not present:
            raise CockpitError(f"{name} ist nicht als anwesend erfasst.")
        attendance.status = "left_early"
        attendance.departure_time = moment
        fields.append("departure_time")
        message = f"{name} hat die Sitzung um {moment:%H:%M} Uhr verlassen."
    elif change == "zurueck":
        if attendance.status != "left_early":
            raise CockpitError(f"{name} hat die Sitzung nicht verlassen.")
        # Die Anwesenheit kennt einen Abgang; die Unterbrechung bleibt als Notiz und im Audit-Log
        if attendance.departure_time is not None:
            _append_note(attendance, f"abwesend {attendance.departure_time:%H:%M}–{moment:%H:%M} Uhr")
            fields.append("notes")
        attendance.status = "joined_late" if attendance.arrival_time else "present"
        attendance.departure_time = None
        fields.append("departure_time")
        message = f"{name} ist ab {moment:%H:%M} Uhr wieder anwesend."
    elif change == "abwesend":
        if present or attendance.status == "left_early":
            raise CockpitError(f"{name} war anwesend – bitte „geht“ verwenden.")
        attendance.status = "absent"
        message = f"{name} ist abwesend."
    else:
        raise CockpitError("Unbekannter Anwesenheitswechsel.")
    attendance.save(update_fields=fields)
    return Outcome(message)


# --- Störungen -----------------------------------------------------------------


def _cause(raw: Any) -> str:
    causes = {value for value, _ in SessionAttendanceDisruption.CAUSE_CHOICES}
    return str(raw) if raw in causes else SessionAttendanceDisruption.CAUSE_CONNECTION


def start_disruption(meeting: SessionMeeting, data: Mapping[str, Any], **_: Any) -> Outcome:
    attendance = _attendance(meeting, data.get("attendance"))
    name = attendance.person.display_name
    if not attendance.is_remote or not participation_service.remote_allowed(meeting):
        raise CockpitError("Störungen lassen sich nur für zugeschaltete Personen vermerken.")
    if attendance.disruptions.filter(ended_at__isnull=True).exists():
        raise CockpitError(f"Für {name} ist bereits eine andauernde Störung vermerkt.")
    moment = _now()
    SessionAttendanceDisruption.objects.create(
        attendance=attendance,
        started_at=moment,
        cause=_cause(data.get("cause")),
        note=str(data.get("note") or "").strip()[:255],
    )
    return Outcome(
        f"Störung bei {name} ab {moment:%H:%M} Uhr vermerkt – bis zum Ende zählt die Person nicht zur "
        "Beschlussfähigkeit.",
        level="warning",
    )


def end_disruption(meeting: SessionMeeting, data: Mapping[str, Any], **_: Any) -> Outcome:
    from apps.common.params import uuid_param

    disruption_id = uuid_param(data.get("disruption"))
    disruption = (
        SessionAttendanceDisruption.objects.select_related("attendance__person")
        .filter(pk=disruption_id, attendance__meeting=meeting)
        .first()
        if disruption_id is not None
        else None
    )
    if disruption is None:
        raise CockpitError("Der Störungsvermerk wurde nicht gefunden.")
    if not disruption.ongoing:
        raise CockpitError("Die Störung ist bereits beendet.")
    moment = _now()
    problem = participation_service.period_error(meeting, disruption.started_at, moment, live=True)
    if problem:
        raise CockpitError(problem)
    disruption.ended_at = moment
    disruption.save(update_fields=["ended_at", "updated_at"])
    return Outcome(f"Störung bei {disruption.attendance.person.display_name} beendet ({moment:%H:%M} Uhr).")


# --- Abstimmung ----------------------------------------------------------------


def open_vote(meeting: SessionMeeting, data: Mapping[str, Any], *, permissions: Collection[str], **_: Any) -> Outcome:
    item = _item(meeting, data.get("item"), permissions)
    if not _is_running(item):
        raise CockpitError("Abgestimmt wird über den aufgerufenen Tagesordnungspunkt.")
    _ensure_no_open_vote(meeting)
    if item.vote_result != "pending":
        raise CockpitError(
            f"Für TOP {item.number} ist bereits ein Ergebnis festgestellt ({item.get_vote_result_display()}). "
            "Korrekturen laufen über die Abstimmungserfassung."
        )
    methods = {value for value, _ in SessionAgendaItem.VOTING_METHOD_CHOICES}
    method = str(data.get("voting_method", ""))
    item.voting_method = method if method in methods else item.voting_method
    item.is_election = bool(data.get("is_election"))
    item.vote_opened_at = timezone.now()
    item.vote_closed_at = None
    item.save(update_fields=["voting_method", "is_election", "vote_opened_at", "vote_closed_at", "updated_at"])
    return Outcome(f"Abstimmung zu TOP {item.number} geöffnet ({item.get_voting_method_display()}).", level="info")


def cancel_vote(meeting: SessionMeeting, data: Mapping[str, Any], *, permissions: Collection[str], **_: Any) -> Outcome:
    item = _item(meeting, data.get("item"), permissions)
    if not item.vote_open:
        raise CockpitError(f"Zu TOP {item.number} läuft keine Abstimmung.")
    item.vote_opened_at = None
    item.save(update_fields=["vote_opened_at", "updated_at"])
    return Outcome(f"Abstimmung zu TOP {item.number} abgebrochen – es wurde kein Ergebnis festgestellt.", level="info")


def _count(data: Mapping[str, Any], name: str) -> int:
    try:
        value = int(str(data.get(name, "0") or "0"))
    except (TypeError, ValueError):
        raise CockpitError("Bitte die Stimmen als ganze Zahlen angeben.") from None
    if value < 0 or value > MAX_VOTES:
        raise CockpitError("Bitte die Stimmen als ganze Zahlen angeben.")
    return value


def close_vote(
    meeting: SessionMeeting,
    data: Mapping[str, Any],
    *,
    permissions: Collection[str],
    session_user: SessionUser | None = None,
    tenant: SessionTenant | None = None,
    **_: Any,
) -> Outcome:
    item = _item(meeting, data.get("item"), permissions)
    if not item.vote_open:
        raise CockpitError(f"Zu TOP {item.number} läuft keine Abstimmung.")
    result = str(data.get("vote_result", ""))
    if result not in VOTE_RESULTS:
        raise CockpitError("Bitte das Ergebnis wählen: angenommen oder abgelehnt.")
    warning = ""
    if item.voting_method in voting_service.INDIVIDUAL_METHODS:
        # Summen aus den Einzelstimmen der Abstimmungserfassung (eine Wahrheit)
        voting_service.recompute_sums(item)
    else:
        yes, no, abstain = _count(data, "votes_yes"), _count(data, "votes_no"), _count(data, "votes_abstain")
        check = voting_service.check_counts(item, yes, no, abstain)
        if check.hard:
            raise CockpitError(check.message)
        warning = check.message if check.exceeded else ""
        item.votes_yes, item.votes_no, item.votes_abstain = yes, no, abstain
    item.vote_result = result
    item.vote_closed_at = timezone.now()
    item.save(update_fields=["votes_yes", "votes_no", "votes_abstain", "vote_result", "vote_closed_at", "updated_at"])
    audit.log_event(
        "vote_result",
        item,
        tenant=tenant or meeting.tenant,
        user=session_user,
        changes={
            "abstimmung": item.get_voting_method_display(),
            "wahl": item.is_election,
            "ergebnis": item.get_vote_result_display(),
            "ja": item.votes_yes,
            "nein": item.votes_no,
            "enthaltung": item.votes_abstain,
            "quelle": "Sitzungscockpit",
        },
    )
    message = (
        f"TOP {item.number}: {item.get_vote_result_display()} "
        f"(Ja {item.votes_yes} / Nein {item.votes_no} / Enthaltung {item.votes_abstain})."
    )
    if warning:
        return Outcome(f"{message} {warning}", level="warning")
    return Outcome(message)


# =============================================================================
# Ausführung
# =============================================================================

Handler = Callable[..., Outcome]

ACTIONS: dict[str, Handler] = {
    "sitzung_eroeffnen": open_meeting,
    "sitzung_schliessen": close_meeting,
    "top_aufrufen": call_item,
    "naechster_top": next_item,
    "top_beenden": end_item,
    "anwesenheit": change_attendance,
    "stoerung_beginn": start_disruption,
    "stoerung_ende": end_disruption,
    "abstimmung_oeffnen": open_vote,
    "abstimmung_abbrechen": cancel_vote,
    "abstimmung_schliessen": close_vote,
}


def perform(
    meeting: SessionMeeting,
    action: str,
    data: Mapping[str, Any],
    *,
    permissions: Collection[str],
    session_user: SessionUser | None = None,
) -> Outcome:
    """
    Aktion des Cockpits ausführen – nur mit dem Steuerungsrecht, nie bei abgesagter Sitzung oder genehmigter
    Niederschrift. Alles oder nichts; nach dem Commit erhalten offene Ansichten einen Hinweis.

    Raises:
        CockpitError: Aktion nicht möglich (Meldung für die Oberfläche).
    """
    handler = ACTIONS.get(action)
    if handler is None:
        raise CockpitError("Unbekannte Aktion.")
    if not can_control(permissions):
        raise CockpitError("Steuern dürfen nur Sitzungsleitung und Protokollführung.")
    with transaction.atomic():
        # Sperre auf der Sitzung: Aktionen zweier Steuernder laufen nacheinander (ein laufender TOP)
        # (nur die Sitzungszeile – nicht den mitgeladenen Mandanten, den z. B. die Nummernvergabe sperrt)
        locked = SessionMeeting.objects.select_for_update(of=("self",)).select_related("tenant").get(pk=meeting.pk)
        if locked.cancelled:
            raise CockpitError("Die Sitzung ist abgesagt.")
        if protocol_lock.is_locked(locked.pk):
            raise CockpitError(
                "Die Niederschrift dieser Sitzung ist genehmigt. Das Cockpit zeigt den Stand nur noch an."
            )
        outcome = handler(locked, data, permissions=permissions, session_user=session_user, tenant=locked.tenant)
        meeting_id = locked.pk
        transaction.on_commit(lambda: notify(meeting_id))
    return outcome


def notify(meeting_id: Any) -> None:
    """Offene Ansichten der Sitzung benachrichtigen (WebSocket); scheitert nie an der Anfrage."""
    from apps.session.consumers import broadcast_cockpit

    broadcast_cockpit(meeting_id)
