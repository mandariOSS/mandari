# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgebrochene oder insgesamt vertagte Sitzung als ein Hinweis (Issue #1269).

Bricht ein Gremium eine Sitzung ab, tragen oft alle offenen Punkte dasselbe Ergebnis („wg. Sitzungsabbruch nicht
behandelt (vertagt)“). Die Tagesordnung zeigte dann 123 Zeilen „vertagt“. ``buendeln`` erkennt den Fall und liefert
einen Satz für die Sitzung; die betroffenen Punkte bekommen ``gebuendelt = True`` und zeigen ihr Etikett nicht mehr
einzeln.

Gebündelt wird, wenn mindestens ``MIN_PUNKTE`` Punkte vertagt oder abgesetzt sind und sie mindestens ``MIN_ANTEIL`` der
Punkte mit Ergebnis ausmachen; einzelne zuvor behandelte Punkte (Eröffnung, Tagesordnung) behalten ihr Etikett.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from insight_core.services.ergebnis import ABGESETZT, VERTAGT, VOKABULAR, Ergebnis

MIN_PUNKTE: Final = 5
MIN_ANTEIL: Final = 0.9
_ABBRUCH: Final = re.compile(r"abbruch|abgebrochen", re.IGNORECASE)


@dataclass(frozen=True)
class Abbruch:
    """Hinweis an der Sitzung: Etikett, Satz, Zahl der gebündelten Punkte und der gemeinsame Rohtext (falls einer)."""

    ergebnis: Ergebnis
    satz: str
    anzahl: int
    roh: str = ""


def _punkte(anzahl: int) -> str:
    return "ein Punkt" if anzahl == 1 else f"{anzahl} Punkte"


def buendeln(punkte: Iterable[Any]) -> Abbruch | None:
    """
    Hinweis für eine abgebrochene bzw. insgesamt vertagte Sitzung oder ``None``. Erwartet Tagesordnungspunkte mit
    ``ergebnis`` (``insight_core.services.ergebnis.ergebnis``) und setzt an den gebündelten ``gebuendelt = True``.
    """
    mit_ergebnis = [punkt for punkt in punkte if getattr(punkt, "ergebnis", None)]
    offen = [punkt for punkt in mit_ergebnis if punkt.ergebnis.schluessel in (VERTAGT, ABGESETZT)]
    if len(offen) < MIN_PUNKTE or len(offen) < MIN_ANTEIL * len(mit_ergebnis):
        return None
    schluessel = Counter(punkt.ergebnis.schluessel for punkt in offen).most_common(1)[0][0]
    rohtexte = {punkt.ergebnis.roh for punkt in offen}
    abgebrochen = any(_ABBRUCH.search(roh) for roh in rohtexte)
    anzahl = len(offen)
    if abgebrochen:
        wie = "vertagt" if schluessel == VERTAGT else "von der Tagesordnung abgesetzt"
        satz = f"Die Sitzung wurde abgebrochen: {_punkte(anzahl)} wurden nicht behandelt und {wie}."
    elif schluessel == VERTAGT:
        satz = f"Die Sitzung wurde insgesamt vertagt: {_punkte(anzahl)} wurden nicht behandelt."
    else:
        zahl = _punkte(anzahl)
        satz = f"{zahl[:1].upper()}{zahl[1:]} wurden von der Tagesordnung abgesetzt und nicht beraten."
    behandelt = len(mit_ergebnis) - anzahl
    if behandelt:
        satz += (
            f" Ein eigenes Ergebnis haben {_punkte(behandelt)}."
            if behandelt > 1
            else " Ein Punkt hat ein eigenes Ergebnis."
        )
    for punkt in offen:
        punkt.gebuendelt = True
    text, erklaerung = VOKABULAR[schluessel]
    return Abbruch(
        ergebnis=Ergebnis(schluessel, text, erklaerung),
        satz=satz,
        anzahl=anzahl,
        roh=next(iter(rohtexte)) if len(rohtexte) == 1 else "",
    )
