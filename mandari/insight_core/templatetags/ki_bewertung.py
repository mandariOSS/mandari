# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prüfsumme der angezeigten KI-Zusammenfassung für den Bewertungsschalter (Issue #1221, ``services.ki_bewertung``)."""

from __future__ import annotations

from django import template

from ..services.ki_bewertung import fassung

register = template.Library()


@register.simple_tag
def ki_fassung(zusammenfassung: str | None) -> str:
    """Prüfsumme der Zusammenfassung, wie sie die Bewertung erwartet; leer ohne Zusammenfassung."""
    return fassung(zusammenfassung)
