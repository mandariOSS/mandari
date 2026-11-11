# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stand der zurückgenommenen Datensätze einer Kommune, für zwischengespeicherte öffentliche Ausgaben.

Feeds und Kartenpunkte liegen einige Minuten im Cache. Ohne weiteres lieferte ein solcher Eintrag einen Datensatz
noch aus, nachdem der Herausgeber ihn zurückgenommen hat oder er in der Quelle entfallen ist. Der Stand hier geht
deshalb in den Schlüssel dieser Einträge ein: Ändert sich die Zahl der als gelöscht markierten Vorgänge, Sitzungen
oder Tagesordnungspunkte, entsteht die Ausgabe neu.

- Rücknahmen in der Anwendung (``hub.ris.retraction.retract``) verwerfen den Stand sofort (:func:`forget`).
- Markierungen des Ingestors (eigener Prozess) werden spätestens nach ``SECONDS`` gesehen, weil der Stand selbst nur
  so lange gilt.
"""

from __future__ import annotations

from typing import Any

from django.core.cache import cache

#: So lange gilt ein gelesener Stand, bevor die Zahlen neu gezählt werden
SECONDS = 60

_PREFIX = "insight:ruecknahme-stand:v1:"
ALL = "alle"


def _key(body_id: Any) -> str:
    return f"{_PREFIX}{body_id or ALL}"


def stand(body_id: Any = None) -> str:
    """Kurzer Stand für eine Kommune (``None``: über alle Kommunen), höchstens ``SECONDS`` alt."""
    key = _key(body_id)
    value = cache.get(key)
    if value is None:
        from insight_core.models import OParlAgendaItem, OParlMeeting, OParlPaper

        papers = OParlPaper.objects.filter(deleted=True)
        meetings = OParlMeeting.objects.filter(deleted=True)
        items = OParlAgendaItem.objects.filter(deleted=True)
        if body_id:
            papers = papers.filter(body_id=body_id)
            meetings = meetings.filter(body_id=body_id)
            items = items.filter(meeting__body_id=body_id)
        value = f"{papers.count()}.{meetings.count()}.{items.count()}"
        cache.set(key, value, SECONDS)
    return str(value)


def forget(body_id: Any = None) -> None:
    """Stand einer Kommune (und den über alle) verwerfen: Die nächste Ausgabe zählt neu."""
    cache.delete_many([_key(body_id), _key(None)])
