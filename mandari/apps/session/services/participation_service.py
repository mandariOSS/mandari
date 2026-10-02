# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnahmeart in der Anwesenheit (Issue #139): vor Ort oder zugeschaltet.

Zugeschaltete Mitglieder gelten als anwesend, solange sie per Bild und Ton an der Sitzung teilnehmen –
sehen und hören und gesehen und gehört werden. Die Anwesenheit hält dafür fest:

- **Teilnahmeart** (``SessionAttendance.participation_mode``): nur in hybriden und digitalen Sitzungen
  (Sitzungsformat aus Issue #138) darf jemand zugeschaltet sein; in digitalen Sitzungen ist das die
  Vorgabe. Ankunft und Abgang sind bei Zugeschalteten Zuschaltung und Trennung.
- **Störungen** (``SessionAttendanceDisruption``) mit Beginn, Ende und Ursache. Während einer Störung
  zählt die Person nicht zur Beschlussfähigkeit und stimmt nicht ab; nach dem Ende wieder.
- **Landesprofil** (Wahlen, geheime Abstimmungen, geheimhaltungspflichtige Beratungen; Issue #754):
  „für Zugeschaltete ausgeschlossen“ (``excluded``, z. B. Bayern, Hessen) nimmt nur die Zugeschalteten aus
  der Abstimmung; „in der Sitzung unzulässig“ (``meeting``, z. B. § 64 Abs. 3 Satz 6 NKomVG) sperrt den
  Vorgang für die ganze Sitzung ab der ersten Zuschaltung – die Zugeschalteten abzuschalten genügt dort
  nicht, der TOP wird vertagt. „Nur unter Bedingungen“ und „ungeklärt“ ergeben einen Hinweis. Zur
  Beschlussfähigkeit zählen Zugeschaltete immer (außer während einer Störung). Muss die Sitzungsleitung im
  Raum sein (``chair_present``), weist die Anwesenheit auf einen zugeschalteten Vorsitz hin.

Niederschrift, Teilnehmerverzeichnis und Protokoll-PDF nennen die Teilnahmeart je Person mit Zeiten,
Unterbrechungen (gegangen und zurückgekommen, Issue #140) und Störungen (``participation_note``); der freie
Vermerk einer Störung bleibt intern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from django.utils import timezone

from apps.session.models import SessionAttendance, SessionMeeting, SessionStateProfile

REMOTE = SessionAttendance.PARTICIPATION_REMOTE
IN_PERSON = SessionAttendance.PARTICIPATION_IN_PERSON

EXCLUDED = SessionStateProfile.REMOTE_VOTE_EXCLUDED
MEETING = SessionStateProfile.REMOTE_VOTE_MEETING
#: Regeln des Landesprofils für Zugeschaltete bei Wahlen, geheimen Abstimmungen und geheimhaltungspflichtigen
#: Beratungen, nach Strenge
_STRICTNESS = {"allowed": 0, "unclear": 1, "conditional": 2, EXCLUDED: 3, MEETING: 4}
#: Anwesenheitsstatus, mit denen eine zugeschaltete Person an der Sitzung teilnimmt oder teilgenommen hat
PARTICIPATED_STATUSES = ("present", "joined_late", "left_early")
#: Erste Zuschaltung ohne bekannte Uhrzeit: Die Sperre gilt ab Sitzungsbeginn (sicher als Standard)
FROM_START = time.min


def remote_allowed(meeting: Any) -> bool:
    """Dürfen Mitglieder zugeschaltet sein? Nur in hybriden und digitalen Sitzungen."""
    return (getattr(meeting, "format", "") or SessionMeeting.FORMAT_PRESENCE) != SessionMeeting.FORMAT_PRESENCE


def default_mode(meeting: Any) -> str:
    """Vorgabe für neue Anwesenheitszeilen: in digitalen Sitzungen zugeschaltet, sonst vor Ort."""
    return REMOTE if getattr(meeting, "format", "") == SessionMeeting.FORMAT_DIGITAL else IN_PERSON


def now() -> time:
    """Aktuelle Uhrzeit (Ortszeit, Minuten) für Störungen, die jetzt beginnen oder enden."""
    return timezone.localtime().time().replace(second=0, microsecond=0)


#: Ende vor Beginn in einer Sitzung, die nicht über Mitternacht geht – meist ein Tippfehler (18:40–18:04)
END_BEFORE_START = (
    "Das Ende der Störung liegt vor ihrem Beginn. Über Mitternacht geht das nur in einer Sitzung, die bis in "
    "den Folgetag dauert (Sitzungsende eintragen)."
)


def crosses_midnight(meeting: Any, *, live: bool = False) -> bool:
    """
    Reicht die Sitzung über Mitternacht? Maßgeblich sind die tatsächlichen, sonst die geplanten Zeiten.

    ``live``: Die Störung endet jetzt – dann zählt die aktuelle Uhrzeit als Ende (eine laufende Sitzung hat
    oft noch kein Ende eingetragen). Ohne Ende und ohne ``live``: nein.
    """
    start = getattr(meeting, "actual_start", None) or getattr(meeting, "start", None)
    end = timezone.now() if live else getattr(meeting, "actual_end", None) or getattr(meeting, "end", None)
    if start is None or end is None:
        return False
    return bool(timezone.localtime(end).date() > timezone.localtime(start).date())


def period_error(meeting: Any, started_at: time, ended_at: time | None, *, live: bool = False) -> str:
    """
    Fehlermeldung zu Beginn und Ende einer Störung, sonst ``""``.

    Ende vor Beginn gilt als Störung über Mitternacht (``SessionAttendanceDisruption.duration_minutes``,
    ``covers``) – das ist nur in einer Sitzung stimmig, die über Mitternacht reicht.
    """
    if ended_at is None or ended_at >= started_at or crosses_midnight(meeting, live=live):
        return ""
    return END_BEFORE_START


def with_disruptions(queryset: Any, meeting: Any) -> Any:
    """
    Störungsvermerke vorab laden – nur in hybriden und digitalen Sitzungen, in denen es Zugeschaltete gibt.

    In Präsenzsitzungen spart das die Abfrage; Vor-Ort-Zeilen lesen ihre Störungen nie.
    """
    return queryset.prefetch_related("disruptions") if remote_allowed(meeting) else queryset


def disruptions(attendance: Any) -> list[Any]:
    """Störungsvermerke einer Anwesenheit (nutzt vorab geladene Vermerke)."""
    return list(attendance.disruptions.all()) if attendance.pk else []


def is_disrupted(attendance: Any, at: time | None = None) -> bool:
    """
    Ist die zugeschaltete Person gerade (bzw. zur Uhrzeit ``at``) wegen einer Störung nicht erreichbar?

    Ohne ``at`` zählt eine andauernde Störung (ohne Ende) – die Beschlussfähigkeit wird live angezeigt.
    Vor Ort Anwesende haben keine Störungen.
    """
    if not attendance.is_remote:
        return False
    return any(d.ongoing if at is None else d.covers(at) for d in disruptions(attendance))


# =============================================================================
# Landesprofil: Wahlen, geheime Abstimmungen, Sitzungsleitung
# =============================================================================


@dataclass(frozen=True)
class RemoteVoteRule:
    """Regel des Landesprofils für Zugeschaltete bei einer Abstimmung bzw. Beratung."""

    rule: str
    #: Gegenstand im Dativ („an Wahlen“, „an der Beratung …“)
    subject: str
    state: str
    #: Gegenstand als Satzanfang („Geheime Wahlen“) für die Sperre in der Sitzung
    title: str = ""
    norm: str = ""
    #: In der Sitzung unzulässig und schon jemand zugeschaltet: Der Vorgang ist gesperrt (Issue #754)
    barred: bool = False
    #: erste Zuschaltung (``FROM_START``: ohne bekannte Uhrzeit), ``None``: niemand zugeschaltet
    since: time | None = None

    @property
    def excluded(self) -> bool:
        """Nur die Zugeschalteten stimmen nicht ab (die übrigen schon)."""
        return self.rule == EXCLUDED

    @property
    def meeting_wide(self) -> bool:
        """Mit Zugeschalteten in der ganzen Sitzung unzulässig."""
        return self.rule == MEETING

    def _norm(self) -> str:
        return f" ({self.norm})" if self.norm else ""

    @property
    def message(self) -> str:
        """Hinweis für Anwesenheit und Abstimmung; leer, wenn Zugeschaltete ohne Weiteres teilnehmen."""
        if self.rule == MEETING:
            text = (
                f"{self.title} sind nach dem Landesprofil {self.state} unzulässig, sobald Mitglieder zugeschaltet "
                f"teilnehmen{self._norm()}."
            )
            if not self.barred:
                if self.since is None:
                    return f"{text} Bisher nimmt niemand zugeschaltet teil."
                return f"{text} Die Abstimmung lag vor der ersten Zuschaltung ({self.since:%H:%M} Uhr)."
            seit = "" if self.since in (None, FROM_START) else f" seit {self.since:%H:%M} Uhr"
            return (
                f"{text} In dieser Sitzung nehmen{seit} Mitglieder zugeschaltet teil. Die Zugeschalteten "
                "abzuschalten genügt nicht: Bitte den Tagesordnungspunkt vertagen und in einer Präsenzsitzung "
                "behandeln."
            )
        if self.rule == EXCLUDED:
            return (
                f"Zugeschaltete nehmen nach dem Landesprofil {self.state} an {self.subject} nicht teil{self._norm()}."
            )
        if self.rule == "conditional":
            return (
                f"Zugeschaltete nehmen nach dem Landesprofil {self.state} an {self.subject} nur unter Bedingungen "
                "teil – bitte vor der Abstimmung prüfen."
            )
        if self.rule == "unclear":
            return (
                f"Ob Zugeschaltete nach dem Landesprofil {self.state} an {self.subject} teilnehmen, ist ungeklärt – "
                "bitte vor der Abstimmung prüfen."
            )
        return ""


def first_connection(meeting: Any, attendances: list[Any] | None = None) -> time | None:
    """
    Uhrzeit der ersten Zuschaltung in der Sitzung (Issue #754); ``None``, wenn niemand zugeschaltet teilnimmt.

    Maßgeblich ist, ob jemand zugeschaltet teilnimmt oder teilgenommen hat (auch vorzeitig getrennt), nicht schon
    das Format „hybrid“. Fehlt eine Zuschaltzeit oder reicht die Sitzung über Mitternacht, gilt der
    Sitzungsbeginn (``FROM_START``). ``attendances``: bereits geladene Anwesenheitszeilen.
    """
    if not remote_allowed(meeting):
        return None
    arrivals: list[time | None]
    if attendances is None:
        arrivals = list(
            meeting.attendances.filter(participation_mode=REMOTE, status__in=PARTICIPATED_STATUSES).values_list(
                "arrival_time", flat=True
            )
        )
    else:
        arrivals = [a.arrival_time for a in attendances if a.is_remote and a.status in PARTICIPATED_STATUSES]
    if not arrivals:
        return None
    known = [arrival for arrival in arrivals if arrival is not None]
    if len(known) < len(arrivals) or crosses_midnight(meeting):
        return FROM_START
    return min(known)


def vote_moment(item: Any) -> time | None:
    """
    Spätester bekannter Zeitpunkt der Abstimmung zu einem TOP (Ortszeit), sonst ``None``.

    Läuft die Abstimmung im Sitzungscockpit, ist es jetzt; ist sie geschlossen, ihr Ende; sonst das Ende des TOP.
    """
    opened = getattr(item, "vote_opened_at", None)
    closed = getattr(item, "vote_closed_at", None)
    if opened is not None and closed is None:
        return now()
    if closed is not None:
        return timezone.localtime(closed).time().replace(second=0, microsecond=0)
    end: time | None = getattr(item, "end_time", None)
    return end


def remote_vote_rule(
    meeting: Any,
    item: Any = None,
    *,
    voting_method: str | None = None,
    is_election: bool | None = None,
    attendances: list[Any] | None = None,
    at: time | None = None,
) -> RemoteVoteRule | None:
    """
    Regel für Zugeschaltete bei einer Abstimmung bzw. Beratung: Wahlen (``is_election``), geheime Abstimmungen und
    geheimhaltungspflichtige Angelegenheiten (``requires_secrecy``).

    ``None``, wenn nichts davon vorliegt, niemand zugeschaltet sein kann (Präsenzsitzung) oder der Mandant kein
    Landesprofil hat. Gilt die Regel für Wahlen nur für geheime Wahlen (``remote_elections_scope``), bleiben offene
    Wahlen unberührt. Treffen mehrere Regeln zu, gilt die strengste. ``voting_method``/``is_election`` überschreiben
    die gespeicherten Werte des TOP (Formular vor dem Speichern).

    Bei „in der Sitzung unzulässig“ ist der Vorgang gesperrt (``barred``), sobald jemand zugeschaltet teilnimmt –
    ab der ersten Zuschaltung bis zum Sitzungsende. Eine Abstimmung, die laut Sitzungscockpit vorher abgeschlossen
    war, bleibt zulässig; ohne bekannte Zeit gilt die Sperre. ``at``: Zeitpunkt des Vorgangs (sonst
    :func:`vote_moment`).
    """
    method = voting_method if voting_method is not None else getattr(item, "voting_method", "")
    election = is_election if is_election is not None else bool(getattr(item, "is_election", False))
    if not remote_allowed(meeting):
        return None
    profile = meeting.tenant.state_profile
    if profile is None:
        return None
    # (Dativ, Nominativ, Regel); der Nominativ steht im Hinweis am Satzanfang
    rules: list[tuple[str, str, str]] = []
    secret_only = profile.remote_elections_scope == SessionStateProfile.ELECTIONS_SECRET_ONLY
    if election and (not secret_only or method == "secret"):
        if secret_only:
            rules.append(("geheimen Wahlen", "geheime Wahlen", profile.remote_elections))
        else:
            rules.append(("Wahlen", "Wahlen", profile.remote_elections))
    if method == "secret":
        rules.append(("geheimen Abstimmungen", "geheime Abstimmungen", profile.remote_secret_votes))
    if getattr(item, "requires_secrecy", False):
        rules.append(
            (
                "der Beratung geheimhaltungspflichtiger Angelegenheiten",
                "Beratungen geheimhaltungspflichtiger Angelegenheiten",
                profile.remote_secrecy_matters,
            )
        )
    if not rules:
        return None
    rule = max((value for *_labels, value in rules), key=lambda value: _STRICTNESS.get(value, 1))
    chosen = [(dative, nominative) for dative, nominative, value in rules if value == rule]
    heading = " und ".join(nominative for _dative, nominative in chosen)
    since = first_connection(meeting, attendances) if rule == MEETING else None
    moment = at if at is not None else vote_moment(item)
    barred = since is not None and (since == FROM_START or moment is None or moment >= since)
    return RemoteVoteRule(
        rule=rule,
        subject=" und ".join(dative for dative, _nominative in chosen),
        state=profile.name,
        title=heading[:1].upper() + heading[1:],
        norm=profile.remote_vote_norm or profile.norm_regular,
        barred=barred,
        since=since,
    )


def chair_hint(meeting: Any, attendances: list[Any]) -> str:
    """Hinweis, wenn die Sitzungsleitung nach dem Landesprofil im Raum sein muss, aber zugeschaltet ist."""
    if getattr(meeting, "format", "") != SessionMeeting.FORMAT_HYBRID:
        return ""
    profile = meeting.tenant.state_profile
    if profile is None or profile.chair_present != "required":
        return ""
    chairs = [a.person.display_name for a in attendances if a.is_remote and a.role == "chair"]
    if not chairs:
        return ""
    return (
        f"Nach dem Landesprofil {profile.name} muss die Sitzungsleitung im Sitzungsraum anwesend sein; "
        f"zugeschaltet ist der Vorsitz: {', '.join(chairs)}."
    )


# =============================================================================
# Vermerk für Anwesenheitsliste, Teilnehmerverzeichnis und Niederschrift
# =============================================================================


def _span(start: time | None, end: time | None) -> str:
    if start and end:
        return f" {start:%H:%M}–{end:%H:%M} Uhr"
    if start:
        return f" ab {start:%H:%M} Uhr"
    if end:
        return f" bis {end:%H:%M} Uhr"
    return ""


def disruption_label(disruption: Any) -> str:
    """„Störung 18:40–18:44 Uhr (Verbindung abgebrochen)“ – ohne den internen Vermerk."""
    if disruption.ended_at is None:
        zeit = f"ab {disruption.started_at:%H:%M} Uhr"
    else:
        zeit = f"{disruption.started_at:%H:%M}–{disruption.ended_at:%H:%M} Uhr"
    return f"Störung {zeit} ({disruption.get_cause_display()})"


def _presence_note(attendance: Any) -> str:
    """Vermerk vor Ort: verspätet (ab …) bzw. vorzeitig gegangen (bis …)."""
    if attendance.status == "joined_late":
        if attendance.arrival_time:
            return f"verspätet, ab {attendance.arrival_time:%H:%M} Uhr"
        return "verspätet"
    if attendance.status == "left_early":
        if attendance.departure_time:
            return f"vorzeitig gegangen, bis {attendance.departure_time:%H:%M} Uhr"
        return "vorzeitig gegangen"
    return ""


# =============================================================================
# Unterbrechungen der Anwesenheit (Issue #140): gegangen und zurückgekommen
# =============================================================================


def _clock(raw: Any) -> time | None:
    """„18:30“ → Uhrzeit; alles andere (Fremdbestand, Tippfehler im Admin) → ``None``."""
    try:
        return time.fromisoformat(str(raw))
    except ValueError:
        return None


def interruptions(attendance: Any) -> list[tuple[time, time]]:
    """Unterbrechungen der Anwesenheit als (gegangen, zurück), in der Reihenfolge der Erfassung."""
    periods = []
    for entry in getattr(attendance, "interruptions", None) or []:
        if not isinstance(entry, dict):
            continue
        left, returned = _clock(entry.get("left")), _clock(entry.get("returned"))
        if left is not None and returned is not None:
            periods.append((left, returned))
    return periods


def add_interruption(attendance: Any, left: time, returned: time) -> None:
    """Unterbrechung vermerken (Speichern übernimmt der Aufrufer, Feld ``interruptions``)."""
    entries = [entry for entry in (attendance.interruptions or []) if isinstance(entry, dict)]
    entries.append({"left": f"{left:%H:%M}", "returned": f"{returned:%H:%M}"})
    attendance.interruptions = entries


def interruption_labels(attendance: Any) -> list[str]:
    """„abwesend 18:30–18:50 Uhr“ je Unterbrechung; bei Zugeschalteten „getrennt …“."""
    word = "getrennt" if attendance.is_remote else "abwesend"
    return [f"{word} {left:%H:%M}–{returned:%H:%M} Uhr" for left, returned in interruptions(attendance)]


def participation_note(attendance: Any, *, show_mode: bool = False) -> str:
    """
    Vermerk zur Teilnahme: „zugeschaltet 18:03–19:10 Uhr; Störung 18:40–18:44 Uhr (Verbindung abgebrochen)“.

    Zugeschaltete: Zuschaltung und Trennung (Ankunft und Abgang), verspätet bzw. vorzeitig getrennt,
    Unterbrechungen und jede Störung mit Zeiten und Ursache. Vor Ort: verspätet bzw. vorzeitig gegangen und
    Unterbrechungen („abwesend 18:30–18:50 Uhr“, Issue #140); mit ``show_mode`` (hybride und digitale
    Sitzungen) zusätzlich „vor Ort“, damit die Teilnahmeart je Person erkennbar ist.
    """
    away = interruption_labels(attendance)
    if attendance.is_remote:
        text = "verspätet zugeschaltet" if attendance.status == "joined_late" else "zugeschaltet"
        text += _span(attendance.arrival_time, attendance.departure_time)
        if attendance.status == "left_early":
            text += ", vorzeitig getrennt"
        return "; ".join([text, *away, *(disruption_label(d) for d in disruptions(attendance))])
    note = _presence_note(attendance)
    if show_mode:
        note = f"vor Ort, {note}" if note else "vor Ort"
    return "; ".join(part for part in (note, *away) if part)


def show_mode(meeting: Any, attendances: list[Any]) -> bool:
    """Teilnahmeart je Person ausweisen? In hybriden und digitalen Sitzungen bzw. sobald jemand zugeschaltet ist."""
    return remote_allowed(meeting) or any(a.is_remote for a in attendances)
