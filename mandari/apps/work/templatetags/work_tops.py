# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Tags für Tagesordnungspunkte der Fraktionssitzung.

``{% beschreibung_grenze "b-{id}" ziel="panel" item_id=item.pk %}`` zeigt unter dem Feld der TOP-Beschreibung die
Grenze aus ``agenda.BESCHREIBUNG_MAX`` und verweist lange Texte an die Anhänge des TOPs. ``ziel`` sagt, wie man dorthin
kommt: ``panel`` öffnet „Unterlagen und Weiteres“ des TOPs (``item_id``), ``anker`` springt im Panel zum Abschnitt
„Anhänge“ (``anker`` = dessen Kennung), ``neu`` (TOP wird gerade angelegt) nennt den Weg nach dem Anlegen,
``vorschlag`` die Sitzungsleitung (wer nur vorschlägt, kann keine Anhänge hochladen), leer nur die Regel. ``modell``:
Alpine-Ausdruck, über den ``x-model`` das Feld füllt; der Zähler folgt dann auch Zuweisungen ohne Eingabe. Die Vorlage
ist ``work/faction/partials/_beschreibung_grenze.html``.
"""

from __future__ import annotations

from typing import Any

from django import template

from apps.work.faction.agenda import BESCHREIBUNG_MAX

register = template.Library()


@register.inclusion_tag("work/faction/partials/_beschreibung_grenze.html")
def beschreibung_grenze(
    feld: str, ziel: str = "", item_id: Any = "", anker: str = "", modell: str = ""
) -> dict[str, Any]:
    """``{id}`` in ``feld`` und ``anker`` steht für ``item_id`` (Kennungen wie ``b-<TOP>`` je TOP)."""
    kennung = str(item_id or "")
    return {
        "feld": feld.replace("{id}", kennung),
        "ziel": ziel,
        "item_id": kennung,
        "anker": anker.replace("{id}", kennung),
        "modell": modell,
        "max": BESCHREIBUNG_MAX,
        "max_text": f"{BESCHREIBUNG_MAX:,}".replace(",", "."),
    }
