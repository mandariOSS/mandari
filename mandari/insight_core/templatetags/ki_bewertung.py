# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Zusammenfassung in der Vorlage: Prüfsumme für den Bewertungsschalter (Issue #1221, ``services.ki_bewertung``) und
Absätze für die Anzeige mit „Weiterlesen“.
"""

from __future__ import annotations

import re

from django import template

from ..services.ki_bewertung import fassung

register = template.Library()

#: Ab so vielen Wörtern wird ein Block ohne Absätze für die Anzeige geteilt
LANGER_BLOCK_WOERTER = 80
#: Sätze je Absatz beim Teilen
SAETZE_JE_ABSATZ = 3
#: Satzende: Punkt, Ausrufe- oder Fragezeichen, dann Leerraum und ein Großbuchstabe; nicht nach Abkürzungen
_SATZENDE = re.compile(r"(?<=[.!?])(?<!\bNr\.)(?<!\bz\.)(?<!\bB\.)(?<!\bca\.)(?<!\bvgl\.)\s+(?=[A-ZÄÖÜ„])")


@register.simple_tag
def ki_fassung(zusammenfassung: str | None) -> str:
    """Prüfsumme der Zusammenfassung, wie sie die Bewertung erwartet; leer ohne Zusammenfassung."""
    return fassung(zusammenfassung)


@register.filter
def ki_absaetze(text: str | None, ortsschutz: str | None = None) -> list[str]:
    """
    Absätze einer Zusammenfassung: getrennt an Leerzeilen, sonst an einzelnen Zeilenumbrüchen; Leerraum zusammengefasst.
    Ältere Zusammenfassungen kamen oft nur mit einfachen Zeilenumbrüchen. Mit ``ortsschutz`` (Ergebnis von
    ``ortsschutz.grund``, Bürgerportal) ohne Hausnummern (Issue #1269); die Prüfsumme bleibt die des gespeicherten Texts.
    """
    if ortsschutz:
        from ..services.ortsschutz import hausnummern_auslassen

        text = hausnummern_auslassen(text)
    roh = (text or "").replace("\r\n", "\n").strip()
    if not roh:
        return []
    teile = re.split(r"\n\s*\n", roh) if re.search(r"\n\s*\n", roh) else roh.split("\n")
    absaetze = [" ".join(teil.split()) for teil in teile if teil.strip()]
    if len(absaetze) == 1 and len(absaetze[0].split()) > LANGER_BLOCK_WOERTER:
        # Ein langer Block ohne Absätze (das Modell hielt sich nicht an die Vorgabe): für die Anzeige nach je
        # SAETZE_JE_ABSATZ Sätzen teilen, damit der Kern vorn steht und der Rest einklappbar ist
        saetze = _SATZENDE.split(absaetze[0])
        absaetze = [" ".join(saetze[i : i + SAETZE_JE_ABSATZ]) for i in range(0, len(saetze), SAETZE_JE_ABSATZ)]
    return absaetze
