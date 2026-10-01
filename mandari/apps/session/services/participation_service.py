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
- **Landesprofil**: Schließt es Zugeschaltete von Wahlen bzw. geheimen Abstimmungen aus
  (``remote_elections``/``remote_secret_votes``), zählen sie bei solchen Abstimmungen nicht mit und
  können keine Stimme abgeben; „nur unter Bedingungen“ und „ungeklärt“ ergeben einen Hinweis. Muss die
  Sitzungsleitung im Raum sein (``chair_present``), weist die Anwesenheit auf einen zugeschalteten
  Vorsitz hin.

Niederschrift, Teilnehmerverzeichnis und Protokoll-PDF nennen die Teilnahmeart je Person mit Zeiten,
Unterbrechungen (gegangen und zurückgekommen, Issue #140) und Störungen (``participation_note``); der freie
Vermerk einer Störung bleibt intern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from django.utils import timezone

from apps.session.models import SessionAttendance, SessionMeeting

REMOTE = SessionAttendance.PARTICIPATION_REMOTE
IN_PERSON = SessionAttendance.PARTICIPATION_IN_PERSON

#: Regeln des Landesprofils für Zugeschaltete bei Wahlen und geheimen Abstimmungen, nach Strenge
_STRICTNESS = {"allowed": 0, "unclear": 1, "conditional": 2, "excluded": 3}


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
    """Regel des Landesprofils für Zugeschaltete bei einer Abstimmung."""

    rule: str
    subject: str
    state: str

    @property
    def excluded(self) -> bool:
        return self.rule == "excluded"

    @property
    def message(self) -> str:
        """Hinweis für Anwesenheit und Abstimmung; leer, wenn Zugeschaltete ohne Weiteres teilnehmen."""
        if self.rule == "excluded":
            return f"Zugeschaltete nehmen nach dem Landesprofil {self.state} an {self.subject} nicht teil."
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


def remote_vote_rule(
    meeting: Any, item: Any = None, *, voting_method: str | None = None, is_election: bool | None = None
) -> RemoteVoteRule | None:
    """
    Regel für Zugeschaltete bei einer Abstimmung: Wahlen (``is_election``) und geheime Abstimmungen.

    ``None``, wenn es weder eine Wahl noch eine geheime Abstimmung ist, niemand zugeschaltet sein kann
    (Präsenzsitzung) oder der Mandant kein Landesprofil hat. Bei einer geheimen Wahl gilt die strengere
    der beiden Regeln. ``voting_method``/``is_election`` überschreiben die gespeicherten Werte des TOP
    (Formular vor dem Speichern).
    """
    method = voting_method if voting_method is not None else getattr(item, "voting_method", "")
    election = is_election if is_election is not None else bool(getattr(item, "is_election", False))
    if not remote_allowed(meeting):
        return None
    profile = meeting.tenant.state_profile
    if profile is None:
        return None
    rules = []
    if election:
        rules.append(("Wahlen", profile.remote_elections))
    if method == "secret":
        rules.append(("geheimen Abstimmungen", profile.remote_secret_votes))
    if not rules:
        return None
    rule = max((value for _, value in rules), key=lambda value: _STRICTNESS.get(value, 1))
    return RemoteVoteRule(rule=rule, subject=" und ".join(label for label, _ in rules), state=profile.name)


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
