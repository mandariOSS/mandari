# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grundgerüst von ``manage.py doctor`` (Issue #964): Prüfungen, ihr Register und der Lauf mit Zeitgrenze.

Eine Prüfung ist eine Funktion ``(Kontext) -> Befund``. Sie meldet ``ok``, ``warnung``, ``fehler`` oder
``uebersprungen`` (nicht eingestellt) mit einem festen Text, ändert nichts und nennt nie einen Wert aus
Zugangsdaten: Ausnahmetexte fremder Bibliotheken gehören in keinen Befund, nur die Art des Fehlers.

Jede Prüfung läuft in einem eigenen Thread, der das Ende des Befehls nicht aufhält (``daemon``). Antwortet sie
nicht innerhalb der Zeitgrenze, gilt sie als gescheitert; der Befehl hängt also nie länger als diese Grenze.

Zweites Netz gegen Geheimnisse in der Ausgabe: Bekannte geheime Werte (``umgebung.geheime_werte``) werden in
jedem Text ersetzt, bevor er den Lauf verlässt. Das fängt nur Werte aus der Umgebung ab ``MINDESTLAENGE`` Zeichen;
Zugangsdaten aus der Datenbank schützt allein die Regel oben.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from django.utils.module_loading import autodiscover_modules

from apps.common.db_connections import releases_db_connections

#: Zeitgrenze je Prüfung in Sekunden, wenn der Aufruf keine nennt
ZEITGRENZE: Final = 10.0
#: Kürzere Werte ersetzt das zweite Netz nicht: Ein kurzes Passwort wie „mandari“ zerstückelte sonst jede Meldung
MINDESTLAENGE: Final = 12
ENTFERNT: Final = "[entfernt]"


class Status(StrEnum):
    OK = "ok"
    WARNUNG = "warnung"
    FEHLER = "fehler"
    UEBERSPRUNGEN = "uebersprungen"


@dataclass(frozen=True)
class Befund:
    """Ergebnis einer Prüfung: ``meldung`` sagt, was ist, ``hinweis`` wie es sich beheben lässt."""

    status: Status
    meldung: str
    hinweis: str = ""
    einzelheiten: tuple[str, ...] = ()


def ok(meldung: str) -> Befund:
    return Befund(Status.OK, meldung)


def warnung(meldung: str, hinweis: str = "", einzelheiten: Sequence[str] = ()) -> Befund:
    return Befund(Status.WARNUNG, meldung, hinweis, tuple(einzelheiten))


def fehler(meldung: str, hinweis: str = "", einzelheiten: Sequence[str] = ()) -> Befund:
    return Befund(Status.FEHLER, meldung, hinweis, tuple(einzelheiten))


def uebersprungen(meldung: str, hinweis: str = "") -> Befund:
    return Befund(Status.UEBERSPRUNGEN, meldung, hinweis)


@dataclass(frozen=True)
class Kontext:
    """Was jede Prüfung vom Aufruf wissen muss."""

    #: Sekunden; Prüfungen geben sie an ihre Verbindungen weiter, der Lauf setzt sie zusätzlich selbst durch
    zeitgrenze: float = ZEITGRENZE
    #: Zuweisungen aus ``--env-datei`` (Name → Wert), ``None`` ohne die Option. Werte nie ausgeben.
    env_datei: Mapping[str, str] | None = field(default=None, repr=False)


Funktion = Callable[[Kontext], Befund]


@dataclass(frozen=True)
class Pruefung:
    name: str
    titel: str
    funktion: Funktion
    #: Reihenfolge in der Ausgabe (kleiner zuerst)
    rang: int = 100


@dataclass(frozen=True)
class Ergebnis:
    """Befund einer Prüfung samt Name und Dauer, bereit für die Ausgabe."""

    name: str
    titel: str
    status: Status
    meldung: str
    hinweis: str
    einzelheiten: tuple[str, ...]
    dauer_ms: int

    def als_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "titel": self.titel,
            "status": self.status.value,
            "meldung": self.meldung,
            "hinweis": self.hinweis,
            "einzelheiten": list(self.einzelheiten),
            "dauer_ms": self.dauer_ms,
        }


# ---------------------------------------------------------------------------------------------- Register

_REGISTER: dict[str, Pruefung] = {}


def pruefung(name: str, titel: str, *, rang: int = 100) -> Callable[[Funktion], Funktion]:
    """Dekorator: nimmt die Funktion unter ``name`` ins Register auf (ein Name nur einmal)."""

    def registrieren(funktion: Funktion) -> Funktion:
        vorhanden = _REGISTER.get(name)
        if vorhanden is not None and vorhanden.funktion is not funktion:
            raise ValueError(f"Prüfung {name} ist schon registriert")
        _REGISTER[name] = Pruefung(name, titel, funktion, rang)
        return funktion

    return registrieren


def registrierte() -> list[Pruefung]:
    """Alle Prüfungen in der Reihenfolge der Ausgabe; lädt ``doctor`` jeder App (wie ``schedules.py``)."""
    autodiscover_modules("doctor")
    return sorted(_REGISTER.values(), key=lambda p: (p.rang, p.name))


# ---------------------------------------------------------------------------------------------- Lauf


def schwaerze(text: str, geheimnisse: Collection[str]) -> str:
    """``text`` ohne die bekannten geheimen Werte (längste zuerst, damit kein Rest eines Wertes stehen bleibt)."""
    for wert in sorted(geheimnisse, key=len, reverse=True):
        if len(wert) >= MINDESTLAENGE and wert in text:
            text = text.replace(wert, ENTFERNT)
    return text


class _Lauf:
    """Eine Prüfung in ihrem eigenen Thread."""

    def __init__(self, eintrag: Pruefung, kontext: Kontext) -> None:
        self.eintrag = eintrag
        self.kontext = kontext
        self.befund: Befund | None = None
        self.beginn = time.monotonic()
        self.dauer_ms = 0
        # daemon: Eine hängende Prüfung (Dienst nimmt die Verbindung an und antwortet nie) hält das Ende des
        # Befehls nicht auf
        self.thread = threading.Thread(target=self._laufen, name=f"doctor-{eintrag.name}", daemon=True)
        self.thread.start()

    @releases_db_connections  # eigener Thread: Verbindung zurückgeben (Issue #344)
    def _laufen(self) -> None:
        try:
            befund = self.eintrag.funktion(self.kontext)
        except Exception as exc:  # noqa: BLE001 - jede Ausnahme ist ein Befund, kein Absturz
            # Nur die Art: Ausnahmetexte können Adressen oder Zugangsdaten enthalten
            befund = fehler(
                f"Prüfung abgebrochen ({type(exc).__name__})",
                "Unerwarteter Fehler in der Prüfung selbst; bitte mit der Art des Fehlers melden.",
            )
        self.dauer_ms = int((time.monotonic() - self.beginn) * 1000)
        self.befund = befund

    def ergebnis(self, frist: float, geheimnisse: Collection[str]) -> Ergebnis:
        self.thread.join(max(0.0, frist - time.monotonic()))
        befund = self.befund
        if befund is None:
            befund = fehler(
                f"keine Antwort innerhalb von {self.kontext.zeitgrenze:g} s",
                "Ist der Dienst erreichbar (Name, Port, Firewall)? Mehr Zeit gibt --zeitgrenze.",
            )
            dauer_ms = int((time.monotonic() - self.beginn) * 1000)
        else:
            dauer_ms = self.dauer_ms
        return Ergebnis(
            name=self.eintrag.name,
            titel=self.eintrag.titel,
            status=befund.status,
            meldung=schwaerze(befund.meldung, geheimnisse),
            hinweis=schwaerze(befund.hinweis, geheimnisse),
            einzelheiten=tuple(schwaerze(zeile, geheimnisse) for zeile in befund.einzelheiten),
            dauer_ms=dauer_ms,
        )


def ausfuehren(pruefungen: Sequence[Pruefung], kontext: Kontext, geheimnisse: Collection[str] = ()) -> list[Ergebnis]:
    """Alle Prüfungen nebeneinander; der Lauf dauert höchstens ``kontext.zeitgrenze`` (plus Anlauf)."""
    laeufe = [_Lauf(eintrag, kontext) for eintrag in pruefungen]
    frist = time.monotonic() + kontext.zeitgrenze
    return [lauf.ergebnis(frist, geheimnisse) for lauf in laeufe]


def gesamtstatus(ergebnisse: Sequence[Ergebnis]) -> Status:
    """``fehler`` vor ``warnung`` vor ``ok``; übersprungene Prüfungen zählen nicht."""
    vorhanden = {ergebnis.status for ergebnis in ergebnisse}
    for status in (Status.FEHLER, Status.WARNUNG):
        if status in vorhanden:
            return status
    return Status.OK
