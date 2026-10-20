# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlage und Anlagen je Vorgang für die Vorgangslisten (Issue #1103), wie in der Trefferliste von SessionNet.

Der gemeinsame Baustein ``c-liste.vorgaenge`` (Insight und Work) zeigt unter dem Titel das Hauptdokument (OParl
``mainFile``, sonst das erste Dokument) und die Anlagen (``c-liste.vorgang-dokumente``). Die Dateien einer Seite
kommen in **einer** Abfrage (``hub.ris.selectors.visible_files_of_papers``) mit den Regeln der Vorgangsseiten:
keine zurückgenommenen, keine durch den Löschabgleich gesperrten (#787) und nur Dateien mit Adresse, denn ohne sie
liefert der Dateiabruf nichts.

Aufrufer rufen ``dokumente_anhaengen`` für die Vorgänge der Seite auf. Ohne diesen Aufruf zeigt eine Zeile keine
Dokumente; der Baustein fragt nie selbst nach (kein N+1).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from hub.ris import selectors as ris

#: So viele Anlagen nennt die aufgeklappte Kurzliste; für weitere führt ein Link zum Vorgang
ANLAGEN_IN_DER_LISTE = 10


@dataclass(frozen=True)
class Dokumentzeile:
    """Hauptdokument und Anlagen eines Vorgangs in der Reihenfolge der Quelle."""

    haupt: Any
    anlagen: list[Any] = field(default_factory=list)

    @property
    def anlagen_kurz(self) -> list[Any]:
        """Die Anlagen der aufgeklappten Kurzliste."""
        return self.anlagen[:ANLAGEN_IN_DER_LISTE]

    @property
    def weitere(self) -> int:
        """Anlagen, die die Kurzliste nicht mehr nennt."""
        return max(0, len(self.anlagen) - ANLAGEN_IN_DER_LISTE)


def _kennung(wert: Any) -> str:
    """Kennung einer Datei in den Rohdaten des Vorgangs: Objekt mit ``id`` oder die Adresse selbst."""
    if isinstance(wert, dict):
        return str(wert.get("id") or "")
    return wert if isinstance(wert, str) else ""


def _reihenfolge(vorgang: Any) -> tuple[str, list[str]]:
    """
    Kennung des Hauptdokuments und der Anlagen laut Quelle (OParl ``mainFile``, ``auxiliaryFile``).

    Sind die Rohdaten nicht geladen (``defer``), bleibt es bei der Reihenfolge nach Namen, statt sie je Zeile
    nachzuladen.
    """
    zurueckgestellt = getattr(vorgang, "get_deferred_fields", None)
    if zurueckgestellt is not None and "raw_json" in zurueckgestellt():
        return "", []
    roh = getattr(vorgang, "raw_json", None)
    if not isinstance(roh, dict):
        return "", []
    anlagen = roh.get("auxiliaryFile")
    return _kennung(roh.get("mainFile")), [_kennung(a) for a in anlagen] if isinstance(anlagen, list) else []


def dokumentzeile(vorgang: Any, dateien: Iterable[Any]) -> Dokumentzeile | None:
    """Ordnet die Dateien eines Vorgangs: Hauptdokument vorn, Anlagen wie in der Quelle, sonst nach Namen."""
    liste = list(dateien)
    if not liste:
        return None
    haupt, anlagen = _reihenfolge(vorgang)
    rang = {kennung: nummer for nummer, kennung in enumerate(anlagen) if kennung}

    def schluessel(datei: Any) -> tuple[int, int, str]:
        kennung = datei.external_id or ""
        return (
            0 if haupt and kennung == haupt else 1,
            rang.get(kennung, len(rang)),
            (datei.name or datei.file_name or "").casefold(),
        )

    liste.sort(key=schluessel)
    return Dokumentzeile(liste[0], liste[1:])


def dokumente_anhaengen(vorgaenge: Iterable[Any]) -> None:
    """
    Setzt ``dokumentzeile`` (``Dokumentzeile`` oder ``None``) an jedem Vorgang, mit einer Abfrage für alle.

    Die Vorgänge müssen dieselben Objekte sein, über die die Vorlage läuft (Liste oder Seite eines Paginators).
    """
    liste = list(vorgaenge)
    if not liste:
        return
    je_vorgang: dict[Any, list[Any]] = defaultdict(list)
    for datei in ris.visible_files_of_papers(vorgang.pk for vorgang in liste):
        if datei.download_url or datei.access_url:
            je_vorgang[datei.paper_id].append(datei)
    for vorgang in liste:
        vorgang.dokumentzeile = dokumentzeile(vorgang, je_vorgang.get(vorgang.pk, ()))
