# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meldungen des Cockpits an den Protokolldienst (Postausgang ``SessionProtokolldienstMeldung``).

Das Cockpit ruft die Funktionen in der Transaktion seiner Aktion auf. Sie legen Zeilen an und reihen nach dem Commit
den Auftrag ``auftraege.zustellen`` ein; das Cockpit wartet nie auf den Dienst. Ohne eingeschaltete Anbindung des
Mandanten und bei nichtöffentlichen Sitzungen entsteht nichts.

**Nur öffentliche Angaben, nur was der Dienst braucht:**

- TOP aufgerufen (``agenda_item_changed``): TOP-Nummer; die Kennung beim Dienst setzt die Zustellung ein.
- Abstimmungsergebnis (``vote_result``): Summen Ja, Nein, Enthaltung, „einstimmig“ und das Ergebnis, nur für
  öffentliche TOPs, nie Einzelstimmen oder Namen.
- Pause (``pause_started``, ``pause_ended``) mit Grund ``unterbrechung``, ``nichtoeffentlich`` oder
  ``einwohnerfragestunde``.

**Geschützte Teile:** Ein nichtöffentlicher TOP erscheint nur als Pause, ohne Nummer. Die Einwohnerfragestunde
erscheint als TOP-Aufruf (öffentlich) und als Pause. Der Dienst verarbeitet Pausen nie, auch wenn er den TOP nicht
kennt. Eine Pause dauert, bis ein öffentlicher TOP aufgerufen wird und die Sitzung nicht unterbrochen ist: Das Ende
eines geschützten TOP allein beendet sie nicht. Pausen überlappen nie: Es gibt höchstens eine offene, gleich ob die
Sitzung unterbrochen ist oder ein geschützter Teil läuft (sonst beendete das Ende der einen die andere).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from django.db import transaction
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionProtokolldienstMeldung

from . import einstellungen

Meldung = SessionProtokolldienstMeldung

UNTERBRECHUNG: Final = "unterbrechung"
NICHTOEFFENTLICH: Final = "nichtoeffentlich"
EINWOHNERFRAGESTUNDE: Final = "einwohnerfragestunde"

#: Ergebnis in Session → Ergebnis beim Protokolldienst; „Ausstehend“ wird nicht gemeldet
ERGEBNISSE: Final[dict[str, str]] = {
    "approved": "adopted",
    "rejected": "rejected",
    "deferred": "postponed",
    "withdrawn": "withdrawn",
    "noted": "noted",
}


def _oeffentlich(meeting: SessionMeeting, item: SessionAgendaItem) -> bool:
    return bool(meeting.is_public and item.is_public)


def schutzgrund(meeting: SessionMeeting, item: SessionAgendaItem) -> str:
    """Grund, aus dem der Dienst den TOP nie verarbeiten darf, sonst ``""``."""
    if not _oeffentlich(meeting, item):
        return NICHTOEFFENTLICH
    if item.kind == SessionAgendaItem.KIND_RESIDENTS_QUESTIONS:
        return EINWOHNERFRAGESTUNDE
    return ""


def _angebunden(meeting: SessionMeeting) -> bool:
    return bool(meeting.is_public) and einstellungen.aktiv(meeting.tenant) is not None


class _Stand:
    """Stand der Meldungen einer Sitzung: offene Pause, geschützter Teil; neue Zeilen in Reihenfolge."""

    def __init__(self, meeting: SessionMeeting, at: datetime) -> None:
        self.meeting = meeting
        self.at = at
        zeilen = Meldung.objects.filter(meeting=meeting)
        letzte = zeilen.order_by("-occurred_at").values_list("geschuetzt", flat=True).first()
        self.geschuetzt = bool(letzte)
        pause = (
            zeilen.filter(kind__in=(Meldung.PAUSE_BEGONNEN, Meldung.PAUSE_BEENDET))
            .order_by("-occurred_at")
            .values_list("kind", "payload")
            .first()
        )
        self.pause_offen = pause is not None and pause[0] == Meldung.PAUSE_BEGONNEN
        self.pause_grund = str((pause[1] or {}).get("reason", "")) if self.pause_offen and pause else ""
        self.neu: list[Meldung] = []

    def melden(self, kind: str, payload: dict[str, Any], item: SessionAgendaItem | None = None) -> None:
        # Meldungen einer Aktion je eine Mikrosekunde auseinander: Reihenfolge hier und beim Dienst eindeutig
        self.neu.append(
            Meldung(
                meeting=self.meeting,
                agenda_item=item,
                kind=kind,
                occurred_at=self.at + timedelta(microseconds=len(self.neu)),
                payload=payload,
                geschuetzt=self.geschuetzt,
            )
        )

    def pause_angleichen(self, grund: str) -> None:
        """Offene Pause nach dem Stand: unterbrochen oder geschützter Teil → offen, sonst beendet."""
        soll = self.meeting.interrupted_at is not None or self.geschuetzt
        if soll and not self.pause_offen:
            self.melden(Meldung.PAUSE_BEGONNEN, {"reason": grund})
            self.pause_offen, self.pause_grund = True, grund
        elif not soll and self.pause_offen:
            self.melden(Meldung.PAUSE_BEENDET, {"reason": self.pause_grund or grund})
            self.pause_offen, self.pause_grund = False, ""

    def speichern(self) -> None:
        if not self.neu:
            return
        for zeile in self.neu:
            zeile.save(force_insert=True)
        meeting_id = self.meeting.pk
        transaction.on_commit(lambda: einreihen(meeting_id), robust=True)


def top_aufgerufen(meeting: SessionMeeting, item: SessionAgendaItem, *, at: datetime | None = None) -> None:
    """TOP im Cockpit aufgerufen (auch „Nächster TOP“ und Wiederaufnahme)."""
    if not _angebunden(meeting):
        return
    stand = _Stand(meeting, at or timezone.now())
    grund = schutzgrund(meeting, item)
    stand.geschuetzt = bool(grund)
    if grund == NICHTOEFFENTLICH:
        # Ohne Nummer: Der nichtöffentliche Teil erscheint nur als Pause
        stand.pause_angleichen(grund)
    elif grund:
        # Einwohnerfragestunde: erst der Wechsel (Zeitmarke des TOP), dann die Pause
        stand.melden(Meldung.TOP_AUFGERUFEN, {"number": item.number}, item)
        stand.pause_angleichen(grund)
    else:
        # Erst eine offene Pause beenden, dann der Wechsel
        stand.pause_angleichen(UNTERBRECHUNG)
        stand.melden(Meldung.TOP_AUFGERUFEN, {"number": item.number}, item)
    stand.speichern()


def unterbrechung(meeting: SessionMeeting, *, at: datetime | None = None) -> None:
    """Sitzung unterbrochen oder fortgesetzt (``meeting.interrupted_at`` ist schon gesetzt bzw. geleert)."""
    if not _angebunden(meeting):
        return
    stand = _Stand(meeting, at or timezone.now())
    stand.pause_angleichen(UNTERBRECHUNG)
    stand.speichern()


def abstimmung(meeting: SessionMeeting, item: SessionAgendaItem, *, at: datetime | None = None) -> None:
    """Ergebnis festgestellt (Schließen der Abstimmung, Vertagen): Summen und Ergebnis, nur öffentlicher TOPs."""
    if not _angebunden(meeting) or not _oeffentlich(meeting, item):
        return
    ergebnis = ERGEBNISSE.get(item.vote_result)
    if ergebnis is None:
        return
    stand = _Stand(meeting, at or timezone.now())
    payload: dict[str, Any] = {"number": item.number, "outcome": ergebnis}
    if item.vote_result in ("approved", "rejected"):
        ja, nein, enthaltung = int(item.votes_yes), int(item.votes_no), int(item.votes_abstain)
        payload.update(
            {"yes": ja, "no": nein, "abstain": enthaltung, "unanimous": ja > 0 and nein == 0 and enthaltung == 0}
        )
    stand.melden(Meldung.ABSTIMMUNG, payload, item)
    stand.speichern()


def einreihen(meeting_id: Any) -> None:
    """
    Zustellung im Hintergrund einreihen, immer im Journal der Aufträge (nie sofort in der Anfrage, auch ohne
    umgestelltes Backend). Scheitert das Einreihen, bleiben die Zeilen wartend; die nächste Meldung reiht erneut ein.
    """
    from apps.events.tasks_backend import journal_backend

    from .auftraege import zustellen

    with transaction.atomic():
        journal_backend().enqueue(zustellen, [str(meeting_id)], {})
