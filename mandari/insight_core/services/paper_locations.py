# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verortungen von Vorgängen als eigene Tabelle (``PaperLocation``), Issue #54.

``OParlPaper.locations`` (JSON) bleibt das Anzeigeformat für Karte und Detailseite.
Dieser Service spiegelt die Einträge in ``PaperLocation`` (Index auf Kommune, Breite,
Länge) und setzt den Korrektur-Workflow um:

- ``sync_paper_locations``: JSON → Tabelle, idempotent. Bestätigte Zeilen bleiben auch
  dann erhalten, wenn ein neuer Georef-Lauf sie nicht mehr liefert. Entfernte Zeilen
  sperren ihren Punkt: Der automatische Lauf legt ihn nicht wieder an und der Eintrag
  verschwindet auch aus dem JSON.
- ``confirm_location`` / ``remove_location``: die beiden Admin-Aktionen.
- ``nearby_papers``: Umkreissuche mit Bounding-Box-Vorfilter (nutzt den Index) und
  Haversine-Feinfilter in SQL; läuft auf PostgreSQL und SQLite gleich.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import Expression, ExpressionWrapper, F, FloatField, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin
from django.utils import timezone

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlPaper, PaperLocation

EARTH_RADIUS_M = 6_371_000.0
# Gleiche Schwelle wie deduplicate_locations: ein entfernter Punkt sperrt seine Umgebung
SUPPRESS_RADIUS_M = 50.0
# Meter je Grad Breite (Näherung; für den Vorfilter ausreichend)
METERS_PER_DEGREE_LAT = 111_320.0
COORD_DECIMALS = 7

PointKey = tuple[float, float]


@dataclass
class SyncResult:
    """Zähler eines Abgleichs JSON → Tabelle."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    suppressed: int = 0
    json_changed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deleted or self.json_changed)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung zweier Punkte in Metern (Haversine)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_key(lat: Any, lon: Any) -> PointKey:
    """Gerundeter Punkt als Schlüssel (7 Nachkommastellen, etwa 1 cm)."""
    return (round(float(lat), COORD_DECIMALS), round(float(lon), COORD_DECIMALS))


def _json_entries(paper: OParlPaper) -> list[dict[str, Any]]:
    """Gültige Einträge aus ``paper.locations`` (Dicts mit numerischen Koordinaten)."""
    raw = paper.locations if isinstance(paper.locations, list) else []
    entries: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            lat = float(entry["lat"])
            lon = float(entry["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isnan(lat) or math.isnan(lon):
            continue
        entries.append(entry)
    return entries


def _entry_from_row(row: PaperLocation) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "lat": row.latitude,
        "lon": row.longitude,
        "name": row.name,
        "source": row.source,
    }
    if row.confidence is not None:
        entry["confidence"] = row.confidence
    return entry


def _entry_fields(entry: dict[str, Any]) -> tuple[str, str, float | None]:
    name = str(entry.get("name") or "")[:500]
    source = str(entry.get("source") or "street_match")[:20]
    confidence = entry.get("confidence")
    confidence_value = float(confidence) if isinstance(confidence, (int, float)) else None
    return name, source, confidence_value


def _row_needs_update(row: PaperLocation, entry: dict[str, Any]) -> bool:
    name, source, confidence = _entry_fields(entry)
    if row.name == name and row.source == source and row.confidence == confidence:
        return False
    row.name = name
    row.source = source
    row.confidence = confidence
    return True


def sync_paper_locations(paper: OParlPaper, save: bool = True) -> SyncResult:
    """
    Gleicht ``paper.locations`` (JSON) mit der Tabelle ``PaperLocation`` ab.

    - Einträge im Umkreis (≤ 50 m) einer entfernten Zeile werden verworfen (Sperre).
    - Bestätigte Zeilen, die im JSON fehlen, werden dort wieder aufgenommen.
    - Automatische Zeilen ohne JSON-Gegenstück werden gelöscht.

    Bei ``save=True`` wird ein verändertes JSON am Vorgang gespeichert.
    """
    from insight_core.models import PaperLocation

    result = SyncResult()
    entries = _json_entries(paper)
    existing = list(PaperLocation.objects.filter(paper=paper))
    removed = [r for r in existing if r.status == PaperLocation.STATUS_REMOVED]
    confirmed = {point_key(r.latitude, r.longitude): r for r in existing if r.status == PaperLocation.STATUS_CONFIRMED}
    auto = {point_key(r.latitude, r.longitude): r for r in existing if r.status == PaperLocation.STATUS_AUTO}

    kept: list[dict[str, Any]] = []
    kept_keys: set[PointKey] = set()
    for entry in entries:
        key = point_key(entry["lat"], entry["lon"])
        if any(haversine_m(key[0], key[1], r.latitude, r.longitude) <= SUPPRESS_RADIUS_M for r in removed):
            result.suppressed += 1
            continue
        if key in kept_keys:
            continue
        kept_keys.add(key)
        kept.append({**entry, "lat": key[0], "lon": key[1]})

    for key, confirmed_row in confirmed.items():
        if key not in kept_keys:
            kept_keys.add(key)
            kept.append(_entry_from_row(confirmed_row))

    to_create: list[PaperLocation] = []
    to_update: list[PaperLocation] = []
    for entry in kept:
        key = point_key(entry["lat"], entry["lon"])
        row: PaperLocation | None = confirmed.get(key) or auto.pop(key, None)
        if row is None:
            name, source, confidence = _entry_fields(entry)
            to_create.append(
                PaperLocation(
                    paper=paper,
                    body_id=paper.body_id,
                    name=name,
                    source=source,
                    confidence=confidence,
                    latitude=key[0],
                    longitude=key[1],
                )
            )
        elif _row_needs_update(row, entry):
            to_update.append(row)

    stale_ids = [r.pk for r in auto.values()]
    if stale_ids:
        result.deleted = PaperLocation.objects.filter(pk__in=stale_ids).delete()[0]
    if to_create:
        PaperLocation.objects.bulk_create(to_create, batch_size=500)
        result.created = len(to_create)
    if to_update:
        PaperLocation.objects.bulk_update(to_update, ["name", "source", "confidence", "updated_at"], batch_size=500)
        result.updated = len(to_update)

    new_json: list[dict[str, Any]] | None = kept or None
    if new_json != paper.locations:
        paper.locations = new_json
        result.json_changed = True
        if save:
            paper.save(update_fields=["locations", "updated_at"])
    return result


def confirm_location(location: PaperLocation) -> None:
    """Verortung bestätigen; war sie entfernt, kehrt sie ins JSON des Vorgangs zurück."""
    from insight_core.models import PaperLocation

    location.status = PaperLocation.STATUS_CONFIRMED
    location.reviewed_at = timezone.now()
    location.save(update_fields=["status", "reviewed_at", "updated_at"])
    sync_paper_locations(location.paper)


def remove_location(location: PaperLocation) -> None:
    """Verortung entfernen: Sperrmerkmal setzen und aus dem JSON des Vorgangs streichen."""
    from insight_core.models import PaperLocation

    location.status = PaperLocation.STATUS_REMOVED
    location.reviewed_at = timezone.now()
    location.save(update_fields=["status", "reviewed_at", "updated_at"])
    sync_paper_locations(location.paper)


def bounding_box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(Süd, Nord, West, Ost) eines Rechtecks um den Punkt, das den Umkreis sicher enthält."""
    lat_delta = radius_m / METERS_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    lon_delta = radius_m / (METERS_PER_DEGREE_LAT * cos_lat)
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def _distance_expression(lat: float, lon: float) -> Expression:
    """Großkreis-Entfernung (acos-Form der Haversine) als SQL-Ausdruck, Ergebnis in Metern."""
    lat_value = Value(lat, output_field=FloatField())
    lon_value = Value(lon, output_field=FloatField())
    cos_term = Cos(Radians(lat_value)) * Cos(Radians(F("latitude"))) * Cos(Radians(F("longitude")) - Radians(lon_value))
    sin_term = Sin(Radians(lat_value)) * Sin(Radians(F("latitude")))
    clamped = Least(
        Value(1.0, output_field=FloatField()),
        Greatest(Value(-1.0, output_field=FloatField()), cos_term + sin_term),
    )
    return ExpressionWrapper(
        Value(EARTH_RADIUS_M, output_field=FloatField()) * ACos(clamped), output_field=FloatField()
    )


def nearby_papers(body: OParlBody, lat: float, lon: float, radius_m: int, limit: int = 50) -> list[dict[str, Any]]:
    """
    Vorgänge im Umkreis, nächster Punkt je Vorgang, sortiert nach Entfernung.

    Zwei Stufen: Bounding-Box auf den Index (body, latitude, longitude), dann
    Haversine-Feinfilter in SQL. Entfernte Verortungen und gelöschte Vorgänge bleiben außen vor.
    """
    from insight_core.models import PaperLocation

    south, north, west, east = bounding_box(lat, lon, float(radius_m))
    rows = (
        PaperLocation.objects.filter(
            body=body,
            latitude__gte=south,
            latitude__lte=north,
            longitude__gte=west,
            longitude__lte=east,
            paper__deleted=False,
        )
        .exclude(status=PaperLocation.STATUS_REMOVED)
        .annotate(distance=_distance_expression(lat, lon))
        .filter(distance__lte=float(radius_m))
        .order_by("distance")
        .values(
            "paper_id",
            "latitude",
            "longitude",
            "distance",
            "paper__name",
            "paper__reference",
            "paper__paper_type",
            "paper__date",
        )
    )

    results: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for row in rows:
        paper_id = row["paper_id"]
        if paper_id in seen:
            continue
        seen.add(paper_id)
        dist_int = int(row["distance"])
        results.append(
            {
                "id": str(paper_id),
                "name": row["paper__name"],
                "reference": row["paper__reference"],
                "paper_type": row["paper__paper_type"],
                "date": row["paper__date"],
                "distance": dist_int,
                "distance_km": f"{dist_int / 1000:.1f}",
                "lat": row["latitude"],
                "lon": row["longitude"],
                "url": f"/insight/vorgaenge/{paper_id}/",
            }
        )
        if len(results) >= limit:
            break
    return results
