# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Flächengeometrie in WGS84 ohne GIS-Bibliothek (Issue #598).

Umringe von Bebauungsplänen sind Flächen von einigen hundert Metern. Dafür genügt eine lokale
Näherung: Längen und Breiten werden um einen Bezugspunkt in Meter umgerechnet (abstandstreue
Zylinderprojektion). Auf Gemeindeebene liegt der Fehler weit unter einem Meter – genug für
Umkreissuche und Kartenpunkt, ohne shapely oder pyproj als neue Abhängigkeit.

Koordinaten folgen GeoJSON: ``(Länge, Breite)``. Ein Polygon ist eine Liste von Ringen; der erste
Ring ist die Außengrenze, weitere Ringe sind Löcher.
"""

from __future__ import annotations

import math
from typing import Any

Point = tuple[float, float]
Ring = list[Point]
Polygon = list[Ring]

METERS_PER_DEGREE = 111_320.0
# Obergrenze gegen übergroße Objekte (ein detaillierter Umring hat einige hundert Stützpunkte)
MAX_VERTICES = 50_000


class GeometryError(ValueError):
    """Die Geometrie ist keine gültige Fläche in Länge/Breite."""


def _point(raw: Any) -> Point:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise GeometryError("Koordinate ohne Länge und Breite")
    try:
        lon = float(raw[0])
        lat = float(raw[1])
    except (TypeError, ValueError):
        raise GeometryError("Koordinate ist keine Zahl") from None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise GeometryError("Koordinate ist keine endliche Zahl")
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise GeometryError("Koordinate liegt nicht in Länge/Breite (anderes Koordinatensystem?)")
    return (lon, lat)


def _ring(raw: Any) -> Ring:
    if not isinstance(raw, list):
        raise GeometryError("Ring ist keine Liste")
    ring = [_point(item) for item in raw]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        raise GeometryError("Ring hat weniger als drei Eckpunkte")
    return ring


def polygons_from_geojson(geometry: Any) -> list[Polygon]:
    """Polygone aus einer GeoJSON-Geometrie (``Polygon`` oder ``MultiPolygon``)."""
    if not isinstance(geometry, dict):
        raise GeometryError("Geometrie fehlt")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise GeometryError("Geometrie ohne Koordinaten")
    if kind == "Polygon":
        raw_polygons = [coordinates]
    elif kind == "MultiPolygon":
        raw_polygons = coordinates
    else:
        raise GeometryError("Nur Polygon und MultiPolygon werden unterstützt")
    polygons: list[Polygon] = []
    vertices = 0
    for raw_polygon in raw_polygons:
        if not isinstance(raw_polygon, list) or not raw_polygon:
            raise GeometryError("Polygon ohne Ringe")
        polygon = [_ring(raw_ring) for raw_ring in raw_polygon]
        vertices += sum(len(ring) for ring in polygon)
        if vertices > MAX_VERTICES:
            raise GeometryError("Geometrie hat zu viele Stützpunkte")
        polygons.append(polygon)
    return polygons


def swap_axes(polygons: list[Polygon]) -> list[Polygon]:
    """Achsen tauschen (Dienste, die Breite vor Länge liefern)."""
    return [[[(lat, lon) for lon, lat in ring] for ring in polygon] for polygon in polygons]


def to_geojson(polygons: list[Polygon], decimals: int = 7) -> dict[str, Any]:
    """GeoJSON-Geometrie; gerundet (7 Stellen ≈ 1 cm), ein einzelnes Polygon als ``Polygon``."""
    rounded = [
        [[[round(lon, decimals), round(lat, decimals)] for lon, lat in ring] for ring in polygon]
        for polygon in polygons
    ]
    if len(rounded) == 1:
        return {"type": "Polygon", "coordinates": rounded[0]}
    return {"type": "MultiPolygon", "coordinates": rounded}


def bounding_box(polygons: list[Polygon]) -> tuple[float, float, float, float]:
    """(Süd, Nord, West, Ost) aller Außenringe."""
    lons = [lon for polygon in polygons for lon, _lat in polygon[0]]
    lats = [lat for polygon in polygons for _lon, lat in polygon[0]]
    return (min(lats), max(lats), min(lons), max(lons))


def _scale(lat0: float) -> tuple[float, float]:
    """Meter je Grad Länge und Breite am Bezugspunkt."""
    return (METERS_PER_DEGREE * max(math.cos(math.radians(lat0)), 0.01), METERS_PER_DEGREE)


def _ring_area_centroid(ring: Ring, lon0: float, lat0: float) -> tuple[float, float, float]:
    """Vorzeichenbehaftete Fläche (m²) und Schwerpunkt (Meter relativ zum Bezugspunkt) eines Rings."""
    kx, ky = _scale(lat0)
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:], strict=False):
        x1, y1 = (lon1 - lon0) * kx, (lat1 - lat0) * ky
        x2, y2 = (lon2 - lon0) * kx, (lat2 - lat0) * ky
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if area2 == 0:
        return 0.0, 0.0, 0.0
    return area2 / 2, cx / (3 * area2), cy / (3 * area2)


def _polygon_area(polygon: Polygon, lon0: float, lat0: float) -> float:
    outer = abs(_ring_area_centroid(polygon[0], lon0, lat0)[0])
    holes = sum(abs(_ring_area_centroid(ring, lon0, lat0)[0]) for ring in polygon[1:])
    return max(outer - holes, 0.0)


def area_m2(polygons: list[Polygon]) -> float:
    """Fläche in Quadratmetern (Löcher abgezogen)."""
    lon0, lat0 = polygons[0][0][0]
    return sum(_polygon_area(polygon, lon0, lat0) for polygon in polygons)


def _in_ring(lon: float, lat: float, ring: Ring) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


def polygon_contains(polygon: Polygon, lon: float, lat: float) -> bool:
    return _in_ring(lon, lat, polygon[0]) and not any(_in_ring(lon, lat, hole) for hole in polygon[1:])


def contains(polygons: list[Polygon], lon: float, lat: float) -> bool:
    """Liegt der Punkt in einer der Flächen (Rand zählt nicht sicher dazu)?"""
    return any(polygon_contains(polygon, lon, lat) for polygon in polygons)


def _scanline_point(polygon: Polygon, lat: float) -> Point | None:
    """Mitte des breitesten Abschnitts, den die Waagerechte auf Höhe ``lat`` im Polygon schneidet."""
    crossings: list[float] = []
    for ring in polygon:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
            if (y1 > lat) != (y2 > lat):
                crossings.append(x1 + (lat - y1) * (x2 - x1) / (y2 - y1))
    crossings.sort()
    best: tuple[float, float] | None = None
    for start, end in zip(crossings[0::2], crossings[1::2], strict=False):
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
    if best is None or best[1] <= best[0]:
        return None
    return ((best[0] + best[1]) / 2, lat)


def representative_point(polygons: list[Polygon]) -> Point:
    """Ein Punkt sicher innerhalb der größten Fläche – der Schwerpunkt, falls er darin liegt.

    Der Schwerpunkt eines L- oder U-förmigen Umrings liegt oft außerhalb; dann dient die Mitte des
    breitesten Abschnitts auf einer Waagerechten durch die Fläche.
    """
    lon0, lat0 = polygons[0][0][0]
    largest = max(polygons, key=lambda polygon: _polygon_area(polygon, lon0, lat0))
    ref_lon, ref_lat = largest[0][0]
    kx, ky = _scale(ref_lat)
    area, cx, cy = _ring_area_centroid(largest[0], ref_lon, ref_lat)
    if area:
        centroid = (ref_lon + cx / kx, ref_lat + cy / ky)
        if polygon_contains(largest, *centroid):
            return centroid
        start_lat = centroid[1]
    else:
        start_lat = ref_lat
    south, north, _west, _east = bounding_box([largest])
    candidates = [start_lat] + [south + (north - south) * step / 10 for step in range(1, 10)]
    for lat in candidates:
        point = _scanline_point(largest, lat)
        if point is not None and polygon_contains(largest, *point):
            return point
    return largest[0][0]


def _segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def distance_m(polygons: list[Polygon], lon: float, lat: float) -> float:
    """Abstand eines Punkts zur Fläche in Metern; 0, wenn der Punkt darin liegt."""
    if contains(polygons, lon, lat):
        return 0.0
    kx, ky = _scale(lat)
    best = math.inf
    for polygon in polygons:
        for ring in polygon:
            for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:], strict=False):
                distance = _segment_distance(
                    0.0,
                    0.0,
                    (lon1 - lon) * kx,
                    (lat1 - lat) * ky,
                    (lon2 - lon) * kx,
                    (lat2 - lat) * ky,
                )
                best = min(best, distance)
    return best
