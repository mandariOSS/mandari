# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Punkte der Vorgangskarten (Issue #853): eine Abfrage für die Karte in Insight (``views.maps.map_markers``) und die
Karte der Recherche in Work (``apps.work.ris.services.karte_daten``).

Grundlage ist die Lese-Fassade ``hub.ris.selectors.paper_places``: die Tabelle der Verortungen (Index auf Kommune,
Breite, Länge) statt des JSON am Vorgang, ohne gelöschte bzw. zurückgenommene Vorgänge und ohne im Admin entfernte
Verortungen, neueste Vorgänge zuerst. Beide Karten fragen je Kartenausschnitt (``bbox`` aus Leaflet
``toBBoxString``) und Zeitraum ab; liegen mehr Punkte im Ausschnitt, als eine Antwort trägt, steht ``truncated`` in
der Antwort und die Karte bittet ums Hineinzoomen bzw. lädt je Ausschnitt nach. Das Zeichnen übernimmt das gemeinsame
Kartenmodul ``frontend/js/vorgangskarte.ts``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from hub.ris import selectors as ris

#: Höchstens so viele Punkte je Antwort; mehr im Ausschnitt meldet die Antwort mit ``truncated``
HOECHSTENS = 2000


def ausschnitt(raw: str | None) -> ris.Area | None:
    """Kartenausschnitt aus ``west,süd,ost,nord`` (Leaflet ``toBBoxString``); ungültig oder leer ergibt ``None``."""
    if not raw:
        return None
    try:
        west, south, east, north = (float(teil) for teil in raw.split(","))
    except ValueError:
        return None
    gueltig = -180 <= west <= east <= 180 and -90 <= south <= north <= 90
    return (west, south, east, north) if gueltig else None


def geojson(
    bodies: ris.Bodies,
    *,
    area: ris.Area | None = None,
    since: date | None = None,
    url: Callable[[uuid.UUID], str] | None = None,
    hoechstens: int = HOECHSTENS,
) -> dict[str, Any]:
    """
    Verortete Vorgänge der Kommunen als GeoJSON-``FeatureCollection``: je Ort ein Punkt mit Kennung, Titel,
    Vorlagen-Nr., Datum und Ortsbezeichnung des Vorgangs; mit ``url`` zusätzlich die Adresse des Vorgangs (Insight
    gibt sie mit, Work setzt sie im Browser aus einer Vorlage zusammen). ``truncated``, wenn mehr als ``hoechstens``
    Punkte im Ausschnitt liegen.
    """
    orte = ris.paper_places(bodies, area=area, since=since, limit=hoechstens + 1)
    features = []
    for ort in orte[:hoechstens]:
        eigenschaften: dict[str, Any] = {
            "id": str(ort.paper_id),
            "title": ort.title,
            "reference": ort.reference,
            "date": ort.paper_date.isoformat() if ort.paper_date else None,
            "location_name": ort.place,
        }
        if url is not None:
            eigenschaften["url"] = url(ort.paper_id)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [ort.longitude, ort.latitude]},
                "properties": eigenschaften,
            }
        )
    return {"type": "FeatureCollection", "features": features, "truncated": len(orte) > hoechstens}
