# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Übernahme offizieller OParl-Locations in das `locations`-JSON von Papers.

Das OParl-Feld `paper.location` wird vom Ingestor als M2M
(OParlPaper.oparl_locations) verknüpft. Dieser Service übernimmt daraus
Koordinaten (geojson) und Adressen in `paper.locations` mit
source="oparl" — der Quelle höchster Priorität. Manuelle Einträge
(source="manual") werden nie überschrieben.
"""

from __future__ import annotations

import logging

from insight_core.services.georeferencing import CONFIDENCE, deduplicate_locations

logger = logging.getLogger(__name__)


def _point_from_geojson(geojson: dict | None) -> tuple[float, float] | None:
    """Extrahiert einen Punkt (lat, lon) aus einem GeoJSON-Objekt."""
    if not isinstance(geojson, dict):
        return None

    geometry = geojson
    if geojson.get("type") == "Feature":
        geometry = geojson.get("geometry") or {}
    elif geojson.get("type") == "FeatureCollection":
        features = geojson.get("features") or []
        geometry = (features[0].get("geometry") or {}) if features else {}

    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    try:
        if gtype == "Point":
            lon, lat = coords[0], coords[1]
            return float(lat), float(lon)
        if gtype in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
            # Grober Zentroid: Mittelwert aller Koordinaten der Geometrie
            points: list[tuple[float, float]] = []

            def _collect(node):
                if (
                    isinstance(node, (list, tuple))
                    and len(node) >= 2
                    and isinstance(node[0], (int, float))
                    and isinstance(node[1], (int, float))
                ):
                    points.append((float(node[0]), float(node[1])))
                elif isinstance(node, (list, tuple)):
                    for child in node:
                        _collect(child)

            _collect(coords)
            if points:
                lon = sum(p[0] for p in points) / len(points)
                lat = sum(p[1] for p in points) / len(points)
                return lat, lon
    except (TypeError, ValueError, IndexError):
        return None
    return None


def build_oparl_location_entries(paper) -> list[dict]:
    """Erzeugt locations-Einträge aus den offiziellen OParl-Locations eines Papers."""
    entries = []
    for location in paper.oparl_locations.all():
        point = _point_from_geojson(location.geojson)
        if not point:
            continue
        name = location.street_address or location.description or location.locality or "Offizieller Ort"
        entries.append(
            {
                "lat": round(point[0], 7),
                "lon": round(point[1], 7),
                "name": name,
                "source": "oparl",
                "confidence": CONFIDENCE["oparl"],
            }
        )
    return entries


def apply_oparl_locations(paper, save: bool = True) -> bool:
    """
    Übernimmt offizielle OParl-Locations in paper.locations (idempotent).

    - source="oparl" hat höchste Priorität (steht vorn, gewinnt bei Dedup <50m)
    - bestehende Einträge (manual, extrahierte) bleiben erhalten
    - Rückgabe: True, wenn sich paper.locations geändert hat
    """
    oparl_entries = build_oparl_location_entries(paper)
    if not oparl_entries:
        return False

    existing = paper.locations if isinstance(paper.locations, list) else []
    # Alte oparl-Einträge ersetzen (Quelle ist die M2M-Verknüpfung)
    others = [
        loc
        for loc in existing
        if isinstance(loc, dict)
        and loc.get("source") != "oparl"
        and loc.get("lat") is not None
        and loc.get("lon") is not None
    ]
    merged = deduplicate_locations(oparl_entries + others)

    if merged == existing:
        return False

    paper.locations = merged
    if save:
        paper.save(update_fields=["locations", "updated_at"])
    return True
