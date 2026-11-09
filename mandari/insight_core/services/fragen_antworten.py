# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fragen einer Anfrage und die Antworten der Verwaltung als Paare (Issue #1273, Abschnitt „Gefragt und geantwortet“).

Ohne KI und ohne Abruf: Grundlage ist der erkannte Text der Dokumente, wie er gespeichert ist. Eine Anfrage nummeriert
ihre Fragen meist („1. Wann beginnt …?“), die Antwort greift die Nummern auf („Zu 1.:“, „Zu Frage 1“) oder wiederholt
jede Frage und antwortet darunter („1. Wann …? Die Sanierung beginnt …“, „Frage 1: … Antwort: …“). Daraus entstehen
Paare im Wortlaut der Dokumente.

Die Regeln sind streng: Die Nummern laufen lückenlos ab 1, jede Frage enthält ein Fragezeichen, zu jeder Frage gibt es
genau eine Antwort. Passt etwas nicht, gibt es keine Paare (``None``), und die Seite verweist nur auf die Dokumente.
Lieber nichts als eine falsche Zuordnung.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Höchstens so viele Fragen; längere Kataloge gehören ins Dokument
MAX_FRAGEN: Final = 15
#: Längste Frage bzw. Antwort auf der Seite (Zeichen); der Rest steht im Dokument
MAX_FRAGE_ZEICHEN: Final = 500
MAX_ANTWORT_ZEICHEN: Final = 1200

#: Nummer einer Frage am Zeilenanfang: „1.“, „1)“, „1.)“, „Frage 1:“, „Frage Nr. 1“ (nicht „1.1“, nicht „2026.“)
_FRAGE_MARKE: Final = re.compile(
    r"^(?:frage\s*(?:nr\.?\s*)?(?P<a>\d{1,2})(?!\d)\s*[.):]*|(?P<b>\d{1,2})(?:\.\)|[.)])(?!\d))\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
#: Nummer einer Antwort am Zeilenanfang: „Zu 1.:“, „Zu Frage 1“, „Zur Frage 1:“, „Antwort zu 1)“, „Ad 1)“
_ANTWORT_MARKE: Final = re.compile(
    r"^(?:antwort\s+)?(?:zu|zur|ad)\s*(?:frage\s*)?(?:nr\.?\s*)?(?P<a>\d{1,2})(?!\d)\s*[.):]*\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
#: Kurze Zeilen, mit denen der Fragenteil bzw. die Antworten enden (Begründung, Gruß, Unterschrift, Anlagen); längere
#: Zeilen mit demselben Anfang sind Fließtext („Die Bürgermeisterin hat …“)
_ENDE_ZEICHEN: Final = 40
_ENDE: Final = re.compile(
    r"^(?:begründung|sachverhalt|erläuterung|mit freundlichen|freundliche grüße|gez\.|im auftrag|i\.\s?a\.|"
    r"anlagen?\b|beschlussvorschlag|(?:der|die) (?:ober)?bürgermeister(?:in)?\b|(?:der|die) landrät(?:in)?\b)",
    re.IGNORECASE,
)
#: Seitenzahlen zwischen den Zeilen („- 2 -“, „Seite 2 von 4“)
_SEITENZEILE: Final = re.compile(r"^(?:-\s*\d+\s*-|seite\s+\d+(?:\s+von\s+\d+)?)$", re.IGNORECASE)
#: Vorspann einer Antwort, der nicht zur Antwort gehört
_ANTWORT_VORSPANN: Final = re.compile(
    r"^(?:antwort|stellungnahme|beantwortung)(?:\s+der\s+verwaltung)?\s*[:.]\s*", re.IGNORECASE
)
#: Satzende zum Kürzen
_SATZENDE: Final = re.compile(r"[.!?…](?=\s)")


@dataclass(frozen=True)
class FrageAntwort:
    """Eine Frage mit der Antwort der Verwaltung im Wortlaut; ``gekuerzt``: Frage oder Antwort geht im Dokument weiter."""

    nummer: int
    frage: str
    antwort: str
    gekuerzt: bool = False


@dataclass
class _Block:
    nummer: int
    zeilen: list[str]

    @property
    def text(self) -> str:
        return _verbinden(self.zeilen)


def _zeilen(text: str) -> list[str]:
    """Zeilen ohne doppelten Leerraum und ohne Seitenzahlen; Seitenumbrüche gelten als Zeilenende."""
    zeilen: list[str] = []
    for roh in text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").split("\n"):
        zeile = " ".join(roh.split())
        if zeile and not _SEITENZEILE.match(zeile):
            zeilen.append(zeile)
    return zeilen


def _verbinden(zeilen: list[str]) -> str:
    """Zeilen zu Fließtext; eine Silbentrennung am Zeilenende („Bau-“ / „zeit“) wird aufgehoben."""
    text = ""
    for zeile in zeilen:
        if len(text) > 1 and text.endswith("-") and text[-2].isalpha() and zeile[:1].islower():
            text = text[:-1] + zeile
        else:
            text = f"{text} {zeile}" if text else zeile
    return text.strip()


def _nummer(treffer: re.Match[str]) -> int:
    return int(treffer.group("a") or treffer.groupdict().get("b") or 0)


def _laeufe(zeilen: list[str], marke: re.Pattern[str]) -> list[list[_Block]]:
    """
    Nummerierte Blöcke als Läufe: Eine 1 beginnt einen neuen Lauf, die nächste Zahl einen neuen Block, alles andere
    gehört zum laufenden Block. Eine kurze Schlusszeile (``_ENDE``) beendet den Lauf.
    """
    laeufe: list[list[_Block]] = []
    lauf: list[_Block] | None = None
    for zeile in zeilen:
        treffer = marke.match(zeile)
        nummer = _nummer(treffer) if treffer else 0
        if treffer and nummer == 1:
            lauf = [_Block(1, [treffer.group("rest")] if treffer.group("rest") else [])]
            laeufe.append(lauf)
        elif treffer and lauf is not None and nummer == len(lauf) + 1:
            lauf.append(_Block(nummer, [treffer.group("rest")] if treffer.group("rest") else []))
        elif lauf is not None and len(zeile) <= _ENDE_ZEICHEN and _ENDE.match(zeile):
            lauf = None
        elif lauf is not None:
            lauf[-1].zeilen.append(zeile)
    return laeufe


def _bis_fragezeichen(text: str) -> str:
    """Text bis einschließlich zum letzten Fragezeichen; leer ohne Fragezeichen."""
    ende = text.rfind("?")
    return text[: ende + 1].strip() if ende >= 0 else ""


def _erster_lauf(laeufe: list[list[_Block]], gueltig: Callable[[list[_Block]], bool]) -> list[_Block] | None:
    for lauf in laeufe:
        if 1 <= len(lauf) <= MAX_FRAGEN and gueltig(lauf):
            return lauf
    return None


def fragen(text: str | None) -> list[str] | None:
    """Nummerierte Fragen einer Anfrage im Wortlaut, oder ``None``, wenn sie sich nicht sicher erkennen lassen."""
    if not text:
        return None
    lauf = _erster_lauf(_laeufe(_zeilen(text), _FRAGE_MARKE), lambda bloecke: all("?" in b.text for b in bloecke))
    return [_bis_fragezeichen(block.text) for block in lauf] if lauf else None


def _antwort(text: str) -> str:
    return _ANTWORT_VORSPANN.sub("", text.strip(" :–-")).strip()


def _hat_inhalt(text: str) -> bool:
    return sum(zeichen.isalpha() for zeichen in text) >= 2


def _zu_nummern(text: str) -> tuple[list[str], str] | None:
    """Antworten nach „Zu 1.“-Marken, dazu der Text vor der ersten Marke (dort wiederholt die Antwort oft die Fragen)."""
    zeilen = _zeilen(text)
    lauf = _erster_lauf(_laeufe(zeilen, _ANTWORT_MARKE), lambda bloecke: all(_hat_inhalt(b.text) for b in bloecke))
    if lauf is None:
        return None
    erste = next(i for i, zeile in enumerate(zeilen) if _ANTWORT_MARKE.match(zeile) and _nummer_von(zeile) == 1)
    return [_antwort(block.text) for block in lauf], "\n".join(zeilen[:erste])


def _nummer_von(zeile: str) -> int:
    treffer = _ANTWORT_MARKE.match(zeile)
    return _nummer(treffer) if treffer else 0


def _wiederholt(text: str) -> list[tuple[str, str]] | None:
    """Paare aus einer Antwort, die jede Frage wiederholt und darunter antwortet (Teilung am letzten Fragezeichen)."""

    def teilen(block: _Block) -> tuple[str, str]:
        text = block.text
        ende = text.rfind("?")
        return (text[: ende + 1].strip(), _antwort(text[ende + 1 :])) if ende >= 0 else ("", "")

    def gueltig(bloecke: list[_Block]) -> bool:
        return all(frage and _hat_inhalt(antwort) for frage, antwort in map(teilen, bloecke))

    lauf = _erster_lauf(_laeufe(_zeilen(text), _FRAGE_MARKE), gueltig)
    return [teilen(block) for block in lauf] if lauf else None


def _kuerzen(text: str, grenze: int) -> tuple[str, bool]:
    """Auf höchstens ``grenze`` Zeichen, möglichst an einem Satzende, sonst an einer Wortgrenze, mit „…“."""
    if len(text) <= grenze:
        return text, False
    schnitt = text[:grenze]
    saetze = [treffer.end() for treffer in _SATZENDE.finditer(schnitt + " ")]
    if saetze and saetze[-1] >= grenze // 2:
        return schnitt[: saetze[-1]].rstrip() + " …", True
    if " " in schnitt:
        schnitt = schnitt[: schnitt.rindex(" ")]
    return schnitt.rstrip(" ,;:-–") + " …", True


def _paar(nummer: int, frage: str, antwort: str) -> FrageAntwort:
    frage, frage_gekuerzt = _kuerzen(frage, MAX_FRAGE_ZEICHEN)
    antwort, antwort_gekuerzt = _kuerzen(antwort, MAX_ANTWORT_ZEICHEN)
    return FrageAntwort(nummer, frage, antwort, frage_gekuerzt or antwort_gekuerzt)


def paare(frage_text: str | None, antwort_text: str | None) -> list[FrageAntwort] | None:
    """
    Fragen und Antworten als Paare, oder ``None``, wenn sie sich nicht sicher zuordnen lassen.

    Erst Antworten mit „Zu 1.“-Marken: Die Fragen kommen aus der Anfrage (``frage_text``), ersatzweise aus dem Text vor
    der ersten Antwort, ihre Zahl muss stimmen. Sonst eine Antwort, die jede Frage wiederholt und darunter antwortet.
    """
    if not antwort_text:
        return None
    nach_nummern = _zu_nummern(antwort_text)
    if nach_nummern is not None:
        antworten, vorspann = nach_nummern
        for quelle in (frage_text, vorspann):
            gefragt = fragen(quelle)
            if gefragt and len(gefragt) == len(antworten):
                return [_paar(nummer, f, a) for nummer, (f, a) in enumerate(zip(gefragt, antworten, strict=True), 1)]
        return None
    wiederholt = _wiederholt(antwort_text)
    gefragt = fragen(frage_text)
    # Eine Aufzählung in einer Antwort („1. Schritt …“) beginnt einen neuen Lauf; dann fehlten Fragen. Sind die Fragen
    # der Anfrage erkennbar, muss ihre Zahl stimmen.
    if wiederholt and (gefragt is None or len(gefragt) == len(wiederholt)):
        return [_paar(nummer, f, a) for nummer, (f, a) in enumerate(wiederholt, 1)]
    return None
