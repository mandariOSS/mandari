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


@register.simple_tag
def ki_fassung(zusammenfassung: str | None) -> str:
    """Prüfsumme der Zusammenfassung, wie sie die Bewertung erwartet; leer ohne Zusammenfassung."""
    return fassung(zusammenfassung)


@register.filter
def ki_absaetze(text: str | None) -> list[str]:
    """
    Absätze einer Zusammenfassung: getrennt an Leerzeilen, sonst an einzelnen Zeilenumbrüchen; Leerraum zusammengefasst.
    Ältere Zusammenfassungen kamen oft nur mit einfachen Zeilenumbrüchen.
    """
    roh = (text or "").replace("\r\n", "\n").strip()
    if not roh:
        return []
    teile = re.split(r"\n\s*\n", roh) if re.search(r"\n\s*\n", roh) else roh.split("\n")
    return [" ".join(teil.split()) for teil in teile if teil.strip()]
