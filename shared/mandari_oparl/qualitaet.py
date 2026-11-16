# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angaben der Quelle zu einer Sitzung prüfen und bereinigen (Issue #1441).

Ratsinformationssysteme tragen eine Absage oft nicht in ``cancelled`` ein, sondern in den Namen der Sitzung
(„… - ENTFÄLLT -“, „… Sitzung wurde abgesagt | Mail …“), und liefern als ``end`` ein Datum ohne Uhrzeit, das dann vor
dem Beginn liegt. Unverändert übernommen, erscheinen abgesagte Sitzungen in Kalender, Suche und KI-Assistent als
stattfindend, und Sitzungen enden vor ihrem Beginn.

Die Regeln gelten für alle Quellen. Der Ingestor wendet sie beim Schreiben an, die Ausgabe beim Lesen, damit auch der
Bestand vor dem nächsten Vollabgleich richtig erscheint. Was hier abgeleitet wird, kennzeichnet die Ausgabe als
abgeleitet; die Angabe der Quelle bleibt in den Rohdaten.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_BUCHSTABE = "A-Za-zÄÖÜäöüß"
#: Absage im Namen einer Sitzung: „abgesagt“, „entfällt“, „fällt aus“ als ganze Wörter, in jeder Schreibweise.
#: „abgesagte Veranstaltungen“ ist keine Absage der Sitzung.
_ABSAGE_RE = re.compile(
    rf"(?<![{_BUCHSTABE}])(?:(?:Sitzung|Termin)\s+)?(?:(?:wurde|wird|ist)\s+)?"
    rf"(?:abgesagt|entf(?:ä|ae)llt|f(?:ä|ae)llt\s+aus)(?![{_BUCHSTABE}])",
    re.IGNORECASE,
)
#: Zierzeichen, mit denen Quellen den Vermerk absetzen („ - “, „***“, „+++“, Klammern)
_ZIER_VOR_NAME = re.compile(r"^[\s\-–—*+)\]!.:,;]+")
_ZIER_NACH_NAME = re.compile(r"[\s\-–—*+(\[:,;]+$")
_ZIER_AM_VERMERK = re.compile(r"^[\s\-–—*+]+|[\s\-–—*+]+$")
#: Zustände der Quelle, nach denen die Sitzung stattgefunden hat: Dann ist ein Wort im Namen keine Absage
_STATTGEFUNDEN = frozenset({"durchgeführt", "durchgefuehrt", "abgeschlossen", "beendet"})


@dataclass(frozen=True)
class Sitzungsname:
    """Name einer Sitzung ohne Absagevermerk; ``vermerk`` ist der Vermerk im Wortlaut der Quelle."""

    name: str | None
    abgesagt: bool = False
    vermerk: str | None = None


@dataclass(frozen=True)
class Sitzungsangaben:
    """Geprüfte Angaben einer Sitzung: Name, Absage und ob mandari die Absage aus dem Namen abgeleitet hat."""

    name: str | None
    abgesagt: bool
    #: Die Quelle setzt ``cancelled`` nicht, die Absage steht nur im Namen
    abgeleitet: bool = False
    vermerk: str | None = None


def _name(text: str) -> str | None:
    """Namensteil ohne Zierzeichen am Rand; ``None``, wenn nichts übrig bleibt."""
    geputzt = _ZIER_NACH_NAME.sub("", _ZIER_VOR_NAME.sub("", text))
    return geputzt or None


def _vermerk(text: str) -> str:
    geputzt = _ZIER_AM_VERMERK.sub("", text)
    if geputzt.endswith(")") and "(" not in geputzt:
        geputzt = geputzt[:-1].rstrip()
    return geputzt


def sitzungsname(roh: str | None) -> Sitzungsname:
    """
    Absagevermerk aus dem Namen einer Sitzung lösen.

    Ohne Vermerk bleibt der Name, wie er ist. Mit Vermerk:

    * Der Teil hinter einem senkrechten Strich fällt weg. Dort stehen Verwaltungsvermerke der Quelle (etwa der Verweis
      auf eine interne Mail), die nicht zum Namen gehören.
    * Steht der Vermerk hinter dem Namen („Rat - ENTFÄLLT -“, „Beirat Sitzung wurde abgesagt“), ist der Name der Teil
      davor und der Vermerk alles ab dem Absagewort („Sitzung fällt aus und wurde auf den 24.06. verlegt“).
    * Steht er davor („abgesagt - Sitzung des Rates“, „***ENTFÄLLT***Sitzung …***ENTFÄLLT***“), ist der Name der Rest.
    * Besteht der Name nur aus dem Vermerk („Sitzung entfällt!“), gibt es keinen Namen.
    """
    if not roh or not isinstance(roh, str):
        return Sitzungsname(roh if isinstance(roh, str) and roh else None)
    text = " ".join(roh.split())
    if _ABSAGE_RE.search(text) is None:
        return Sitzungsname(roh)
    vor_strich = text.split("|", 1)[0].strip()
    treffer = _ABSAGE_RE.search(vor_strich)
    if treffer is None:
        # Der Vermerk steht hinter dem Strich: Der Name ist der Teil davor
        hinten = _ABSAGE_RE.search(text)
        return Sitzungsname(_name(vor_strich), True, _vermerk(hinten.group(0)) if hinten else None)
    davor = _name(vor_strich[: treffer.start()])
    if davor:
        return Sitzungsname(davor, True, _vermerk(vor_strich[treffer.start() :]))
    rest = _name(" ".join(_ABSAGE_RE.sub(" ", vor_strich[treffer.end() :]).split()))
    return Sitzungsname(rest, True, _vermerk(treffer.group(0)))


def hat_stattgefunden(zustand: Any) -> bool:
    """Nennt ``meetingState`` der Quelle eine Sitzung, die stattgefunden hat?"""
    return isinstance(zustand, str) and zustand.strip().casefold() in _STATTGEFUNDEN


def sitzungsangaben(roh: Mapping[str, Any]) -> Sitzungsangaben:
    """
    Name und Absage einer Sitzung aus den Rohdaten der Quelle.

    Abgesagt ist die Sitzung, wenn die Quelle ``cancelled`` setzt oder ihr Name eine Absage nennt
    (:func:`sitzungsname`). Hat die Sitzung laut ``meetingState`` stattgefunden, gilt ein Absagewort im Namen nicht,
    und der Name bleibt, wie er ist.
    """
    wert = roh.get("name")
    name = wert if isinstance(wert, str) and wert else None
    von_quelle = roh.get("cancelled") is True
    befund = sitzungsname(name)
    if not befund.abgesagt or hat_stattgefunden(roh.get("meetingState")):
        return Sitzungsangaben(name, von_quelle)
    return Sitzungsangaben(befund.name, True, abgeleitet=not von_quelle, vermerk=befund.vermerk)


def plausibles_ende(start: datetime | None, ende: datetime | None) -> datetime | None:
    """
    Ende einer Sitzung, wenn es nach dem Beginn liegt, sonst ``None``.

    Viele Quellen liefern als ``end`` nur das Datum; als Zeitpunkt gelesen ist das Mitternacht am Sitzungstag und liegt
    vor dem Beginn. Ein falsches Ende schadet mehr als ein fehlendes („17:00 bis 00:00 Uhr“, Kalendereinträge mit
    negativer Dauer). Ohne Beginn bleibt das Ende, wie es ist. Verglichen wird auf die Sekunde, so genau wie die
    Ausgabe: Ein Ende in derselben Sekunde wie der Beginn stünde dort mit demselben Zeitpunkt.
    """
    if ende is None or start is None:
        return ende
    try:
        return ende if ende.replace(microsecond=0) > start.replace(microsecond=0) else None
    except TypeError:
        # Zeitpunkte mit und ohne Zeitzone lassen sich nicht vergleichen: nicht raten, nichts verwerfen
        return ende
