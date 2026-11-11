# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stand der zurückgenommenen Datensätze einer Kommune, für zwischengespeicherte öffentliche Ausgaben.

Feeds und Kartenpunkte liegen einige Minuten im Cache. Ohne weiteres lieferte ein solcher Eintrag einen Datensatz
noch aus, nachdem der Herausgeber ihn zurückgenommen hat oder er in der Quelle entfallen ist. Der Stand hier geht
deshalb in den Schlüssel dieser Einträge ein; ändert er sich, entsteht die Ausgabe neu. Er besteht aus zwei Teilen:

- **Generation:** Rücknahmen in der Anwendung (``hub.ris.retraction.retract``, auch von Dateien) setzen sie neu
  (:func:`forget`). Der Stand ändert sich damit sofort, gleich was die Zählung ergibt. Es gibt eine Generation für
  alle Kommunen: Rücknahmen in der Anwendung sind selten, und nicht jeder Datensatz trägt seine Kommune selbst
  (Dateien hängen an Vorgang oder Sitzung). Der gezählte Teil liegt unter einem Schlüssel mit der Generation: Eine
  Ausgabe, die noch vor der Rücknahme gezählt hat, schreibt ihr Ergebnis unter die alte Generation und überdeckt
  den neuen Stand nicht.
- **Zählung:** je Art (Vorgänge, Sitzungen, Tagesordnungspunkte, Dateien, Beratungen) die Zahl der als gelöscht markierten
  Datensätze und der jüngste Zeitpunkt einer Markierung. So fallen auch Markierungen des Ingestors auf (eigener
  Prozess, ohne :func:`forget`), spätestens nach ``SECONDS``; und ebenso der Fall, dass ein Datensatz
  wiederhergestellt und ein anderer zurückgenommen wird (gleiche Zahl, anderer Zeitpunkt).
"""

from __future__ import annotations

import time
from typing import Any

from django.core.cache import cache
from django.db.models import Count, Max, Q

#: So lange gilt eine Zählung, bevor neu gezählt wird
SECONDS = 60
#: So lange bleibt eine Generation stehen: länger als jede Ausgabe, deren Schlüssel sie trägt, im Cache liegt
GENERATION_SECONDS = 24 * 60 * 60

_PREFIX = "insight:ruecknahme-stand:v2:"
ALL = "alle"


_GENERATION_KEY = f"{_PREFIX}generation"


def _generation() -> str:
    return str(cache.get(_GENERATION_KEY) or 0)


def _value_key(body_id: Any, generation: str | None = None) -> str:
    return f"{_PREFIX}{body_id or ALL}:{_generation() if generation is None else generation}"


def _count(body_id: Any) -> str:
    from insight_core.models import OParlAgendaItem, OParlConsultation, OParlFile, OParlMeeting, OParlPaper

    scopes = (
        (OParlPaper, Q(body_id=body_id)),
        (OParlMeeting, Q(body_id=body_id)),
        (OParlAgendaItem, Q(meeting__body_id=body_id)),
        (OParlFile, Q(body_id=body_id) | Q(paper__body_id=body_id) | Q(meeting__body_id=body_id)),
        (OParlConsultation, Q(body_id=body_id) | Q(paper__body_id=body_id)),
    )
    parts = []
    for model, scope in scopes:
        rows = model.objects.filter(deleted=True)
        if body_id:
            rows = rows.filter(scope)
        found = rows.aggregate(count=Count("pk", distinct=True), latest=Max("deleted_at"))
        latest = found["latest"]
        parts.append(f"{found['count']}@{int(latest.timestamp() * 1000) if latest else 0}")
    return ".".join(parts)


def stand(body_id: Any = None) -> str:
    """Kurzer Stand für eine Kommune (``None``: über alle Kommunen); die Zählung darin höchstens ``SECONDS`` alt."""
    generation = _generation()
    key = _value_key(body_id, generation)
    value = cache.get(key)
    if value is None:
        value = _count(body_id)
        cache.set(key, value, SECONDS)
    return f"{generation}:{value}"


def forget() -> None:
    """Stand neu setzen, für alle Kommunen (siehe Moduldokumentation): Die nächste Ausgabe entsteht und zählt neu."""
    # Zeitstempel statt Zähler: eindeutig auch dann, wenn der Cache die bisherige Generation verloren hat
    cache.set(_GENERATION_KEY, time.time_ns(), GENERATION_SECONDS)
