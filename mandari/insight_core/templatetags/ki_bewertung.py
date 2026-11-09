# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Zusammenfassung in der Vorlage: Prüfsumme für den Bewertungsschalter (Issue #1221, ``services.ki_bewertung``) und
Absätze für die Anzeige mit „Weiterlesen“.
"""

from __future__ import annotations

from django import template

from ..services.ki_bewertung import fassung
from ..services.kurzfassung import absaetze

register = template.Library()


@register.simple_tag
def ki_fassung(zusammenfassung: str | None) -> str:
    """Prüfsumme der Zusammenfassung, wie sie die Bewertung erwartet; leer ohne Zusammenfassung."""
    return fassung(zusammenfassung)


@register.filter
def ki_absaetze(text: str | None, ortsschutz: str | None = None) -> list[str]:
    """
    Absätze einer Zusammenfassung für die Anzeige mit „Weiterlesen“ (``services.kurzfassung.absaetze``). Mit
    ``ortsschutz`` (Ergebnis von ``ortsschutz.grund``, Bürgerportal) ohne Hausnummern (Issue #1269); die Prüfsumme
    bleibt die des gespeicherten Texts.
    """
    if ortsschutz:
        from ..services.ortsschutz import hausnummern_auslassen

        text = hausnummern_auslassen(text)
    return absaetze(text)
