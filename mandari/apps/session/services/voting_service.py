# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Digitale Abstimmung und Umlaufbeschlüsse (Issue #41).

- Einzelstimmen-Erfassung je TOP (offen/namentlich) mit automatischer
  Summenbildung; Befangene (Mitwirkungsverbot) zählen nicht mit.
- Bei geheimer Abstimmung werden nur Summen gespeichert; Einzelstimmen
  werden verworfen, Befangenheits-Vermerke bleiben dokumentierbar.
- Umlaufbeschlüsse: Rücklauf-Erfassung, Auszählung gegen die
  stimmberechtigte Besetzung, Nummernvergabe U/<Jahr>/<lfd>.
- Stimmrecht (Issue #318): Stimmen erfasst der Service nur von Anwesenden mit Stimmrecht;
  beratende Mitglieder und Gäste werden ausgewiesen, zählen aber nicht mit. Summen dürfen die
  Zahl der stimmberechtigten Anwesenden nicht übersteigen – als harter Fehler, wenn die
  Anwesenheit vollständig erfasst ist, sonst als Warnung.
- Sperre (Issue #318): Nach der Genehmigung der Niederschrift schreibt der Service keine
  Einzelstimmen mehr (``protocol_lock``), außer innerhalb einer Berichtigung.
- Teilnahmeart (Issue #139): Zugeschaltete stimmen ab wie Anwesende im Raum – außer während einer
  andauernden Störung und bei Wahlen bzw. geheimen Abstimmungen, von denen das Landesprofil sie
  ausschließt (``participation_service.remote_vote_rule``).
- Sperre in der Sitzung (Issue #754): Ist ein Vorgang nach dem Landesprofil unzulässig, sobald jemand
  zugeschaltet teilnimmt (Niedersachsen: geheime Wahlen und Abstimmungen), schreibt der Service keine Stimmen.
"""

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.utils import timezone

from ..models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionCircularResolution,
    SessionMeeting,
    SessionOrganizationMembership,
    SessionTenant,
    SessionVote,
)
from . import membership_service, participation_service, protocol_lock

INDIVIDUAL_METHODS = ("open", "roll_call")

#: Anwesenheitsstatus, in denen eine Person abstimmen kann. Verspätet gekommene und vorzeitig
#: gegangene Mitglieder waren zeitweise im Raum; ob sie beim jeweiligen TOP dabei waren,
#: verantwortet der Sitzungsdienst.
VOTING_PRESENT_STATUSES = ("present", "joined_late", "left_early")
#: Rückmeldungen, bei denen die Anwesenheit noch nicht festgestellt ist
UNDECIDED_STATUSES = ("invited", "confirmed")
#: Funktionen ohne Stimme, auch wenn das Stimmrecht-Häkchen gesetzt ist
NON_VOTING_ROLES = ("guest", "recorder")


#: Summen einer Abstimmung
COUNT_FIELDS = ("votes_yes", "votes_no", "votes_abstain")
#: Felder, die die Abstimmungserfassung an einem TOP speichert – nie Zeiten oder Zeitpunkte des
#: Sitzungscockpits (``start_time``, ``vote_opened_at`` …, Issue #140)
CAPTURE_FIELDS = ("voting_method", "is_election", "vote_result", *COUNT_FIELDS, "updated_at")

#: Präfix der versteckten Formularfelder mit dem Stand beim Laden (``geladen_vote_result`` usw.)
LOADED_PREFIX = "geladen_"


def form_value(data: Any, name: str, current: Any) -> Any:
    """
    Wert eines Abstimmungsfelds aus einem Formular, das neben dem Sitzungscockpit offen sein kann (Issue #140).

    Abstimmungserfassung und Niederschrift schicken Ergebnis, Art und Summen immer mit – mit dem Stand beim
    Laden der Seite. Übernommen wird der eingegebene Wert nur, wenn die Person ihn gegenüber diesem Stand
    (verstecktes Feld ``geladen_<name>``) geändert hat; sonst bleibt der aktuelle Wert, etwa ein Ergebnis,
    das die Sitzungsleitung inzwischen im Cockpit festgestellt hat. Fehlt das Feld im Formular, bleibt der
    aktuelle Wert; Formulare ohne Ladestand (vor dem Update geöffnet) übernehmen den Wert wie bisher.
    """
    if name not in data:
        return current
    submitted = data.get(name)
    loaded = f"{LOADED_PREFIX}{name}"
    if loaded in data and submitted == data.get(loaded):
        return current
    return submitted


def form_flag(data: Any, name: str, current: bool) -> bool:
    """Wie :func:`form_value` für ein Kontrollkästchen (nicht angehakt = nicht im Formular)."""
    submitted = bool(data.get(name))
    loaded = f"{LOADED_PREFIX}{name}"
    if loaded in data and submitted == bool(data.get(loaded)):
        return current
    return submitted


class VotingRightsError(ValueError):
    """Stimme einer Person ohne Stimmrecht oder ohne Anwesenheit; Meldung für die Oberfläche."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        #: fester, für Nutzer formulierter Text – nur diesen in Antworten ausgeben
        self.user_message = user_message


@dataclass
class Eligibility:
    """Wer darf in dieser Sitzung abstimmen? (Issue #318)."""

    #: stimmberechtigt und anwesend
    voting: list[SessionAttendance] = field(default_factory=list)
    #: anwesend ohne Stimmrecht (beratende Mitglieder, Gäste, Protokollführung)
    advisory: list[SessionAttendance] = field(default_factory=list)
    #: übrige Zeilen der Anwesenheitsliste (nicht anwesend, noch offen)
    others: list[SessionAttendance] = field(default_factory=list)
    #: zugeschaltet, aber wegen einer andauernden Störung nicht erreichbar (Issue #139)
    unreachable: list[SessionAttendance] = field(default_factory=list)
    #: zugeschaltet und nach dem Landesprofil von dieser Abstimmung ausgeschlossen (Issue #139)
    remote_excluded: list[SessionAttendance] = field(default_factory=list)
    #: Stellvertretungen, deren vertretene Person anwesend ist: anwesend ohne Stimme, nicht mitgezählt
    standby: list[SessionAttendance] = field(default_factory=list)
    #: Stellvertretung -> vertretene Personen (für die Beschlussfähigkeit ohne erneutes Laden)
    substitutes: dict[Any, frozenset[Any]] = field(default_factory=dict)
    #: Regel des Landesprofils für Zugeschaltete bei dieser Abstimmung (nur mit TOP); ``remote_rule.barred``:
    #: in dieser Sitzung unzulässig, weil jemand zugeschaltet teilnimmt (Issue #754)
    remote_rule: Any = None
    #: alle Zeilen der Anwesenheitsliste (für die Beschlussfähigkeit ohne erneutes Laden)
    attendances: list[SessionAttendance] = field(default_factory=list)
    #: Anwesenheit vollständig erfasst (Besetzung bekannt, alle Stimmberechtigten mit Status)
    complete: bool = False
    has_list: bool = False

    @property
    def voting_person_ids(self) -> set[Any]:
        return {attendance.person_id for attendance in self.voting}


def can_vote(attendance: SessionAttendance) -> bool:
    """Stimmrecht und Anwesenheit (Status) in dieser Sitzung."""
    return (
        attendance.has_voting_rights
        and attendance.role not in NON_VOTING_ROLES
        and attendance.status in VOTING_PRESENT_STATUSES
    )


def eligibility(
    meeting: SessionMeeting,
    item: SessionAgendaItem | None = None,
    *,
    voting_method: str | None = None,
    is_election: bool | None = None,
) -> Eligibility:
    """
    Stimmberechtigte Anwesende, beratende Anwesende und Vollständigkeit der Anwesenheit.

    Vollständig ist die Anwesenheit, wenn die Besetzung des Gremiums stimmberechtigte Mitglieder
    kennt, jedes davon eine Zeile in der Anwesenheitsliste hat und keine stimmberechtigte Zeile
    mehr nur „eingeladen“ oder „zugesagt“ ist. Nur dann ist die Zahl der Stimmberechtigten eine
    verlässliche Obergrenze für die Stimmenzahlen.

    Stellvertretungen (``attendance_service.seat_split``) stimmen nur ab, wenn sie für ein nicht
    anwesendes Mitglied nachrücken; sonst sind sie anwesend ohne Stimme (``standby``). Für die
    Vollständigkeit zählen sie nur, solange eine von ihnen vertretene Person nicht anwesend ist.

    Teilnahmeart (Issue #139): Zugeschaltete mit andauernder Störung stimmen nicht ab
    (``unreachable``). Mit TOP (``item``, Abstimmungsart und Wahl ggf. aus dem Formular) gilt die Regel
    des Landesprofils für Wahlen und geheime Abstimmungen (``remote_excluded``).
    """
    from . import attendance_service

    attendances = list(
        participation_service.with_disruptions(meeting.attendances.select_related("person"), meeting).order_by(
            "person__family_name", "person__given_name"
        )
    )
    rule = (
        participation_service.remote_vote_rule(
            meeting, item, voting_method=voting_method, is_election=is_election, attendances=attendances
        )
        if item is not None
        else None
    )
    result = Eligibility(has_list=bool(attendances), remote_rule=rule, attendances=attendances)
    if not attendances:
        return result
    roster = attendance_service.roster(meeting)
    result.substitutes = roster.substitutes
    split = attendance_service.seat_split(
        attendance_service.voting_rows(attendances), roster.substitutes, active_statuses=VOTING_PRESENT_STATUSES
    )
    standby = {attendance.pk for attendance in split.standby}
    for attendance in attendances:
        if attendance.pk in standby:
            # Stellvertretung ohne freien Sitz: anwesend ohne Stimme bzw. übrige Zeile
            if attendance.status in VOTING_PRESENT_STATUSES:
                result.standby.append(attendance)
                result.advisory.append(attendance)
            else:
                result.others.append(attendance)
        elif can_vote(attendance) and participation_service.is_disrupted(attendance):
            result.unreachable.append(attendance)
        elif can_vote(attendance) and rule is not None and rule.excluded and attendance.is_remote:
            result.remote_excluded.append(attendance)
        elif can_vote(attendance):
            result.voting.append(attendance)
        elif attendance.status in VOTING_PRESENT_STATUSES:
            result.advisory.append(attendance)
        else:
            result.others.append(attendance)
    # Offen ist eine Rückmeldung, solange ein Mitglied noch nicht erfasst ist – bei Stellvertretungen nur,
    # solange eine von ihnen vertretene Person nicht anwesend ist (sonst kommt es auf sie nicht an)
    undecided = any(a.status in UNDECIDED_STATUSES for a in split.members) or any(
        a.status in UNDECIDED_STATUSES and roster.substitutes[a.person_id] & split.vacant
        for a in split.stepping_in + split.standby
    )
    members = set(roster.voting_members)
    listed = {a.person_id for a in attendances}
    result.complete = bool(members) and not undecided and members <= listed
    return result


@dataclass(frozen=True)
class CountCheck:
    """Prüfung der Stimmenzahlen gegen die stimmberechtigten Anwesenden."""

    exceeded: bool = False
    hard: bool = False
    message: str = ""


def check_counts(
    agenda_item: SessionAgendaItem,
    yes: int,
    no: int,
    abstain: int,
    *,
    assessed: Eligibility | None = None,
    excluded: int | None = None,
) -> CountCheck:
    """
    Übersteigen Ja + Nein + Enthaltung die Zahl der stimmberechtigten Anwesenden (ohne Befangene)?

    Harter Fehler bei vollständig erfasster Anwesenheit; sonst Warnung – die Zahl der erfassten
    Stimmberechtigten ist dann keine verlässliche Obergrenze. Ohne Anwesenheitsliste keine Prüfung.
    """
    assessed = assessed or eligibility(agenda_item.meeting, agenda_item)
    if not assessed.has_list:
        return CountCheck()
    # Vom Landesprofil ausgeschlossene Zugeschaltete zählen nicht (auch bei einer Prüfung ohne TOP-Bezug)
    rule = participation_service.remote_vote_rule(agenda_item.meeting, agenda_item, attendances=assessed.attendances)
    voting = [a for a in assessed.voting if not (rule is not None and rule.excluded and a.is_remote)]
    total = yes + no + abstain
    if excluded is None:
        # Befangene (Mitwirkungsverbot) zählen nicht; ohne Angabe der gespeicherte Stand
        excluded = 0
        if agenda_item.pk:
            excluded = agenda_item.votes.filter(vote="excluded", person_id__in={a.person_id for a in voting}).count()
    limit = max(0, len(voting) - excluded)
    if total <= limit:
        return CountCheck()
    base = (
        f"TOP {agenda_item.number}: {total} Stimmen (Ja {yes}, Nein {no}, Enthaltung {abstain}) übersteigen "
        f"die Zahl der stimmberechtigten Anwesenden ({limit})"
    )
    if assessed.complete:
        return CountCheck(exceeded=True, hard=True, message=f"{base}. Die Stimmenzahlen wurden nicht übernommen.")
    return CountCheck(
        exceeded=True,
        message=f"{base}. Die Anwesenheit ist nicht vollständig erfasst – bitte prüfen Sie die Zahlen.",
    )


def capture_votes(
    agenda_item: SessionAgendaItem,
    votes_by_person: dict,
    *,
    recorded_by,
    assessed: Eligibility | None = None,
) -> dict:
    """
    Einzelstimmen für einen TOP speichern und Summen neu berechnen.

    Args:
        votes_by_person: {person: vote_value} — leere Werte löschen die Stimme.
        assessed: bereits ermittelte Stimmberechtigung (sonst aus der Anwesenheit)

    Bei geheimer Abstimmung werden nur „befangen"-Vermerke gespeichert
    (der Ausschluss ist dokumentationspflichtig, das Stimmverhalten nicht).

    Raises:
        VotingRightsError: neue oder geänderte Stimme einer Person, die nicht stimmberechtigt
            anwesend ist (Issue #318). Löschen geht immer.
        protocol_lock.ProtocolLockedError: Niederschrift genehmigt, außerhalb einer Berichtigung.
    """
    secret = agenda_item.voting_method == "secret"
    valid_votes = {value for value, _ in SessionVote.VOTE_CHOICES}
    labels = dict(SessionVote.VOTE_CHOICES)

    def _aenderung(person: Any, alt: str | None, neu_wert: str | None) -> dict[str, Any]:
        """Eine Stimmänderung für das Protokoll (Issue #221): Person, alte und neue Stimme."""
        return {
            "person": person.display_name,
            "alt": labels.get(alt, alt) if alt else None,
            "neu": labels.get(neu_wert, neu_wert) if neu_wert else None,
        }

    # Gesammelt statt je Person einzeln (Issue #291): Bei 90 Ratsmitgliedern kosteten
    # update_or_create-Aufrufe rund zwei Sekunden je Erfassung; jetzt eine Leseabfrage,
    # ein Löschen, ein bulk_create und ein bulk_update in einer Transaktion.
    personen = list(votes_by_person)
    geaendert: list[dict[str, Any]] = []
    with transaction.atomic():
        vorhanden = {
            stimme.person_id: stimme
            for stimme in SessionVote.objects.select_for_update().filter(agenda_item=agenda_item, person__in=personen)
        }
        loeschen: list[Any] = []
        neu: list[SessionVote] = []
        aendern: list[SessionVote] = []
        # Neue oder geänderte Stimmen: nur von stimmberechtigten Anwesenden (Issue #318)
        schreibende: list[Any] = []
        jetzt = timezone.now()
        for person, vote_value in votes_by_person.items():
            bestehend = vorhanden.get(person.pk)
            ungueltig = vote_value not in valid_votes
            # Geheime Abstimmung: kein individuelles Stimmverhalten speichern, nur Vermerke
            verboten = secret and vote_value not in ("excluded", "not_participating")
            if ungueltig or verboten:
                if bestehend is not None:
                    loeschen.append(bestehend.pk)
                    geaendert.append(_aenderung(person, bestehend.vote, None))
                continue
            if bestehend is None:
                neu.append(
                    SessionVote(agenda_item=agenda_item, person=person, vote=vote_value, recorded_by=recorded_by)
                )
                geaendert.append(_aenderung(person, None, vote_value))
                schreibende.append(person)
            elif bestehend.vote != vote_value or bestehend.recorded_by_id != getattr(recorded_by, "pk", None):
                if bestehend.vote != vote_value:
                    geaendert.append(_aenderung(person, bestehend.vote, vote_value))
                    schreibende.append(person)
                bestehend.vote = vote_value
                bestehend.recorded_by = recorded_by
                bestehend.updated_at = jetzt  # bulk_update setzt auto_now nicht selbst
                aendern.append(bestehend)
        if not (loeschen or neu or geaendert):
            # Inhaltlich unverändert (etwa ein erneuter Lauf mit denselben Stimmen): nichts schreiben,
            # auch nicht den Erfasser-Vermerk – die Niederschrift kann längst genehmigt sein
            aendern = []
        else:
            protocol_lock.ensure_unlocked(agenda_item.meeting_id)
        if schreibende:
            beurteilt = assessed or eligibility(agenda_item.meeting, agenda_item)
            sperre = beurteilt.remote_rule
            if sperre is not None and sperre.barred:
                # In dieser Sitzung unzulässig (Issue #754): keine Stimmen, auch keine Vermerke
                raise VotingRightsError(sperre.message)
            berechtigt = beurteilt.voting_person_ids
            ohne = [p for p in schreibende if p.pk not in berechtigt]
            if ohne:
                raise VotingRightsError(rights_message(beurteilt, ohne))
        if loeschen:
            SessionVote.objects.filter(pk__in=loeschen).delete()
        if neu:
            SessionVote.objects.bulk_create(neu)
        if aendern:
            SessionVote.objects.bulk_update(aendern, ["vote", "recorded_by", "updated_at"])

        # Summen aus Einzelstimmen nur bei offener und namentlicher Abstimmung; bei „Nur Summen“ und geheimer
        # Abstimmung sind die erfassten Summen maßgeblich und werden nicht überschrieben
        if agenda_item.voting_method in INDIVIDUAL_METHODS:
            recompute_sums(agenda_item)
    result = tally(agenda_item)
    # Einzelne Stimmänderungen für den direkten Protokolleintrag „Stimmabgabe erfasst“ (Issue #221)
    result["changed"] = geaendert
    return result


def rights_message(assessed: Eligibility, persons: list[Any]) -> str:
    """Meldung für Stimmen von Personen, die bei dieser Abstimmung nicht abstimmen (feste Sätze, Namen)."""
    ausgeschlossen = {a.person_id for a in assessed.remote_excluded}
    gestoert = {a.person_id for a in assessed.unreachable}
    bereit = {a.person_id for a in assessed.standby}
    saetze = ["Stimmen werden nur von stimmberechtigten Anwesenden erfasst."]
    gruppen = (
        ([p for p in persons if p.pk in ausgeschlossen], assessed.remote_rule.message if assessed.remote_rule else ""),
        ([p for p in persons if p.pk in gestoert], "Wegen einer Störung nicht erreichbar:"),
        ([p for p in persons if p.pk in bereit], "Stellvertretung, deren vertretenes Mitglied selbst anwesend ist:"),
        (
            [p for p in persons if p.pk not in ausgeschlossen | gestoert | bereit],
            "Ohne Stimmrecht oder laut Anwesenheitsliste nicht anwesend:",
        ),
    )
    for gruppe, text in gruppen:
        if gruppe:
            namen = ", ".join(sorted(p.display_name for p in gruppe))
            saetze.append(f"{text} {namen}." if text.endswith(":") else f"{text} Betroffen: {namen}.")
    return " ".join(saetze)


def recompute_sums(agenda_item: SessionAgendaItem) -> None:
    """Summen (Ja/Nein/Enthaltung) aus den Einzelstimmen ableiten."""
    counts = dict.fromkeys(SessionVote.COUNTED_VOTES, 0)
    for vote in agenda_item.votes.filter(vote__in=SessionVote.COUNTED_VOTES).values_list("vote", flat=True):
        counts[vote] += 1
    agenda_item.votes_yes = counts["yes"]
    agenda_item.votes_no = counts["no"]
    agenda_item.votes_abstain = counts["abstain"]
    agenda_item.save(update_fields=["votes_yes", "votes_no", "votes_abstain", "updated_at"])


def tally(agenda_item: SessionAgendaItem, votes: list[SessionVote] | None = None) -> dict:
    """Übersicht: Summen + Befangene/Nicht-Teilnehmende (``votes``: bereits geladene Einzelstimmen)."""
    if votes is None:
        votes = list(agenda_item.votes.select_related("person"))
    return {
        "yes": agenda_item.votes_yes,
        "no": agenda_item.votes_no,
        "abstain": agenda_item.votes_abstain,
        "excluded": [v.person for v in votes if v.vote == "excluded"],
        "not_participating": [v.person for v in votes if v.vote == "not_participating"],
    }


# --- Umlaufbeschlüsse --------------------------------------------------------


def voting_members(circular: SessionCircularResolution):
    """
    Aktive stimmberechtigte Besetzung des Gremiums – ohne Stellvertretungen, die keinen eigenen Sitz haben.

    „Aktiv“ nach der gemeinsamen Regel (``membership_service.active_q``) wie Ladung und Anwesenheit:
    laufende Besetzung einer aktiven Person; deaktivierte Personen stimmen nicht mehr mit ab.
    """
    return (
        SessionOrganizationMembership.objects.filter(
            membership_service.active_q(timezone.localdate()),
            organization=circular.organization,
            has_voting_rights=True,
            substitute_for__isnull=True,
        )
        .select_related("person")
        .order_by("person__family_name", "person__given_name")
    )


def circular_tally(circular: SessionCircularResolution) -> dict:
    """Auszählung eines Umlaufbeschlusses gegen die stimmberechtigte Besetzung."""
    votes = {v.person_id: v for v in circular.votes.select_related("person")}
    members = list(voting_members(circular))
    counts = {"yes": 0, "no": 0, "abstain": 0}
    outstanding = []
    for membership in members:
        vote = votes.get(membership.person_id)
        if vote is None:
            outstanding.append(membership.person)
        else:
            counts[vote.vote] += 1
    total = len(members)
    responded = total - len(outstanding)
    return {
        **counts,
        "total_members": total,
        "responded": responded,
        "outstanding": outstanding,
        "quorum_met": total > 0 and responded > total / 2,
        "suggestion": "adopted" if counts["yes"] > counts["no"] else "rejected",
    }


def assign_circular_number(circular: SessionCircularResolution) -> bool:
    """Umlauf-Nummer U/<Jahr>/<lfd> vergeben (idempotent, transaktionssicher)."""
    if circular.reference:
        return False

    year = timezone.localdate().year
    prefix = f"U/{year}/"
    with transaction.atomic():
        SessionTenant.objects.select_for_update().get(pk=circular.tenant_id)
        max_num = 0
        refs = SessionCircularResolution.objects.filter(
            tenant_id=circular.tenant_id, reference__startswith=prefix
        ).values_list("reference", flat=True)
        for ref in refs:
            try:
                max_num = max(max_num, int(ref.rsplit("/", 1)[-1]))
            except (TypeError, ValueError):
                continue
        circular.reference = f"{prefix}{max_num + 1:04d}"
        circular.save(update_fields=["reference", "updated_at"])
    return True
