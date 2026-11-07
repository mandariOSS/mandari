# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bewertung einer Prompt-Fassung an einer festen Stichprobe (Issue #1271, ``manage.py zusammenfassung_stichprobe``).

Bevor eine neue Fassung des Prompts (``prompts.PROMPT_FASSUNG``) in den Betrieb geht, beurteilt ein Mensch ihre
Zusammenfassungen an derselben Auswahl von ``STICHPROBE_ANZAHL`` Vorgängen einer Kommune:

1. **Auswahl** (``auswaehlen``): fest je Kommune und Fassung (Prüfsumme aus Startwert, Fassung und Kennung,
   ``summary_store.stichprobe``); mit ``bis`` kommen auch später keine neuen Vorgänge hinein.
2. **Prüfbogen** (``schreiben``): je Vorgang Titel, Adresse, Kurzfassung, ganze Zusammenfassung, Einfache Sprache,
   Ergebnis der mechanischen Prüfung und leere Spalten ``urteil`` (zutreffend, teilweise, falsch) und ``anmerkung``.
   Ohne Ausführung stehen dort die vorhandenen Zusammenfassungen (``bestand``, kein KI-Aufruf, keine Kosten), mit
   Ausführung neue mit der aktuellen Fassung (``erzeugen``: Einzelaufrufe, nichts wird gespeichert oder angezeigt).
3. **Auswertung** (``auswerten``): Zählung der Urteile gegen die Schwelle ``FREIGABE_ZUTREFFEND`` (und höchstens
   ``FREIGABE_FALSCH`` falsche). Von der Prüfung verworfene zählen gesondert: Sie erschienen im Betrieb nicht.

Die Schwelle ist ein Vorschlag; über die Freigabe entscheidet ein Mensch. Die Spalte ``pruefung`` zeigt das Urteil
des Programms: Wer von Hand beurteilt, sollte sie erst danach lesen.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from . import zusammenfassung_pruefung
from .prompts import PROMPT_FASSUNG

#: Größe der Stichprobe je Fassung
STICHPROBE_ANZAHL: Final = 30
#: Startwert der Auswahl; zusammen mit der Fassung ergibt er je Fassung eine eigene, feste Stichprobe
STARTWERT: Final = "mandari-zusammenfassung"
#: Vorschlag für die Freigabe: so viele zutreffende von ``STICHPROBE_ANZAHL`` und höchstens so viele falsche
FREIGABE_ZUTREFFEND: Final = 27
FREIGABE_FALSCH: Final = 0
URTEILE: Final = ("zutreffend", "teilweise", "falsch")
SPALTEN: Final = (
    "nr",
    "vorgang",
    "vorlagen_nr",
    "titel",
    "adresse",
    "prompt_fassung",
    "herkunft",
    "pruefung",
    "kurzfassung",
    "zusammenfassung",
    "einfache_sprache",
    "urteil",
    "anmerkung",
)
#: Ergebnis der Prüfung ohne Befund
BESTANDEN: Final = "bestanden"


class _OhneAnbieter:
    """Platzhalter für ``SummaryService``, wenn nur der Prompt gebaut wird (kein KI-Aufruf)."""

    def is_available(self) -> bool:
        return False


@dataclass
class Zeile:
    """Eine Zeile des Prüfbogens."""

    nr: int
    vorgang: str
    vorlagen_nr: str
    titel: str
    adresse: str
    prompt_fassung: int
    #: „bestand“ (gespeicherte Zusammenfassung) oder „neu“ (mit der aktuellen Fassung erzeugt)
    herkunft: str
    pruefung: str = ""
    zusammenfassung: str = ""
    einfache_sprache: str = ""

    def werte(self) -> list[Any]:
        from insight_core.services.kurzfassung import kurzfassung

        return [
            self.nr,
            self.vorgang,
            self.vorlagen_nr,
            self.titel,
            self.adresse,
            self.prompt_fassung,
            self.herkunft,
            self.pruefung,
            kurzfassung(self.zusammenfassung),
            self.zusammenfassung,
            self.einfache_sprache,
            "",
            "",
        ]


def startwert(fassung: int = PROMPT_FASSUNG) -> str:
    return f"{STARTWERT}:{fassung}"


def auswaehlen(kommune_id: Any, *, anzahl: int = STICHPROBE_ANZAHL, bis: date | None = None) -> list[Any]:
    """Feste Stichprobe der Kommune für die aktuelle Prompt-Fassung (``summary_store.stichprobe``)."""
    from insight_core.services import summary_store

    return summary_store.stichprobe(kommune_id, anzahl=anzahl, startwert=startwert(), bis=bis)


def _zeile(nr: int, vorgang: Any, *, herkunft: str, fassung: int) -> Zeile:
    return Zeile(
        nr=nr,
        vorgang=str(vorgang.pk),
        vorlagen_nr=vorgang.reference or "",
        titel=" ".join((vorgang.name or "").split()),
        adresse=f"/insight/vorgaenge/{vorgang.pk}/",
        prompt_fassung=fassung,
        herkunft=herkunft,
    )


def _ergebnis(pruefung: zusammenfassung_pruefung.Pruefung | None) -> str:
    if pruefung is None:
        return "ohne Material"
    teile = []
    if pruefung.befund is not None:
        teile.append(f"verworfen: {pruefung.befund.grund}")
    if pruefung.befund_einfach is not None:
        teile.append(f"Einfache Sprache verworfen: {pruefung.befund_einfach.grund}")
    return "; ".join(teile) or BESTANDEN


def bestand(vorgaenge: Iterable[Any]) -> list[Zeile]:
    """Die gespeicherten Zusammenfassungen der Stichprobe, mechanisch geprüft; ohne KI-Aufruf."""
    from .summarizer import SummaryService

    dienst = SummaryService(provider=_OhneAnbieter())
    zeilen = []
    for nr, vorgang in enumerate(vorgaenge, start=1):
        zeile = _zeile(nr, vorgang, herkunft="bestand", fassung=vorgang.summary_prompt_fassung)
        zeile.zusammenfassung = vorgang.summary or ""
        zeile.einfache_sprache = vorgang.summary_einfach or ""
        if zeile.zusammenfassung:
            vorbereitung = dienst.vorbereiten(vorgang, einfach=bool(zeile.einfache_sprache))
            pruefung = (
                zusammenfassung_pruefung.pruefen_beide(
                    zeile.zusammenfassung, zeile.einfache_sprache, vorbereitung.pruefquelle()
                )
                if vorbereitung is not None
                else None
            )
            zeile.pruefung = _ergebnis(pruefung)
        else:
            zeile.pruefung = "keine Zusammenfassung"
        zeilen.append(zeile)
    return zeilen


def schaetzung(vorgaenge: Iterable[Any], provider: Any) -> Decimal | None:
    """Obergrenze der Kosten für ``erzeugen`` in USD (nur mit der Claude API bekannt, sonst ``None``)."""
    from insight_ai import kosten

    from .summarizer import ZUSATZ_AUSGABE_EINFACH, SummaryService

    endpunkt = getattr(provider, "endpunkt", None)
    modell = getattr(endpunkt, "modell", "")
    if not modell:
        return None
    dienst = SummaryService(provider=provider)
    einfach = bool(getattr(provider, "strukturiert", False))
    grenze = (getattr(provider, "max_output_tokens", 0) or 0) + (ZUSATZ_AUSGABE_EINFACH if einfach else 0)
    summe = Decimal(0)
    for vorgang in vorgaenge:
        vorbereitung = dienst.vorbereiten(vorgang, einfach=einfach)
        if vorbereitung is not None:
            summe += kosten.schaetzung(modell, zeichen=vorbereitung.zeichen, max_ausgabe=grenze, batch=False)
    return summe


def erzeugen(vorgaenge: Iterable[Any], provider: Any) -> list[Zeile]:
    """
    Zusammenfassungen der Stichprobe mit der aktuellen Prompt-Fassung, je Vorgang ein Einzelaufruf, geprüft; nichts
    wird gespeichert, angezeigt oder protokolliert. Ein Fehler des Anbieters steht in der Zeile; ein ausgeschöpftes
    Monatsbudget beendet den Lauf (die übrigen Zeilen bleiben leer).
    """
    from insight_ai.providers.claude import BudgetErschoepftError

    from .summarizer import SummaryService

    dienst = SummaryService(provider=provider)
    einfach = bool(getattr(provider, "strukturiert", False))
    zeilen = []
    gestoppt = False
    for nr, vorgang in enumerate(vorgaenge, start=1):
        zeile = _zeile(nr, vorgang, herkunft="neu", fassung=PROMPT_FASSUNG)
        zeilen.append(zeile)
        if gestoppt:
            zeile.pruefung = "nicht erzeugt (Monatsbudget ausgeschöpft)"
            continue
        vorbereitung = dienst.vorbereiten(vorgang, einfach=einfach)
        if vorbereitung is None:
            zeile.pruefung = "ohne Material"
            continue
        try:
            entwurf = dienst.entwurf_erstellen(vorbereitung)
        except BudgetErschoepftError:
            gestoppt = True
            zeile.pruefung = "nicht erzeugt (Monatsbudget ausgeschöpft)"
            continue
        except Exception as fehler:
            zeile.pruefung = f"Fehler: {type(fehler).__name__}"
            continue
        zeile.zusammenfassung = entwurf.zusammenfassung
        zeile.einfache_sprache = entwurf.einfach
        zeile.pruefung = _ergebnis(entwurf.pruefung)
    return zeilen


def schreiben(zeilen: Iterable[Zeile], pfad: Path) -> None:
    """
    Prüfbogen als TSV (UTF-8, Tabulator), Spalten ``SPALTEN``; ``urteil`` und ``anmerkung`` bleiben leer. Jede Zelle
    ist gegen Formel-Injektion geschützt (``apps.common.csv_safety``): Der Bogen wird in einer Tabellenkalkulation
    ausgefüllt.
    """
    from apps.common import csv_safety

    with pfad.open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv_safety.writer(datei, delimiter="\t", lineterminator="\n")
        schreiber.writerow(SPALTEN)
        for zeile in zeilen:
            schreiber.writerow(zeile.werte())


@dataclass(frozen=True)
class Auswertung:
    """Zählung eines ausgefüllten Prüfbogens."""

    urteile: dict[str, int] = field(default_factory=dict)
    #: Ohne Urteil (noch offen oder unbekannter Eintrag)
    offen: int = 0
    #: Von der mechanischen Prüfung verworfen: im Betrieb nicht angezeigt
    verworfen: int = 0
    zeilen: int = 0

    @property
    def freigabe_erreicht(self) -> bool:
        return (
            self.offen == 0
            and self.urteile.get("zutreffend", 0) >= FREIGABE_ZUTREFFEND
            and self.urteile.get("falsch", 0) <= FREIGABE_FALSCH
        )


def auswerten(pfad: Path) -> Auswertung:
    """Zählt die Urteile eines ausgefüllten Prüfbogens (``schreiben``)."""
    zaehler: Counter[str] = Counter()
    offen = verworfen = zeilen = 0
    with pfad.open(encoding="utf-8", newline="") as datei:
        for eintrag in csv.DictReader(datei, delimiter="\t"):
            zeilen += 1
            ist_verworfen = str(eintrag.get("pruefung") or "").startswith("verworfen")
            verworfen += ist_verworfen
            urteil = str(eintrag.get("urteil") or "").strip().casefold()
            if urteil in URTEILE:
                zaehler[urteil] += 1
            elif not ist_verworfen:
                # Verworfene erschienen im Betrieb nicht; ihr Urteil ist freiwillig (stimmte die Prüfung?)
                offen += 1
    return Auswertung(
        urteile={urteil: zaehler.get(urteil, 0) for urteil in URTEILE}, offen=offen, verworfen=verworfen, zeilen=zeilen
    )
