# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Straßenverzeichnis (Gazetteer) einer Kommune aus OSM importieren.

Lädt alle benannten highway-Ways innerhalb der Kommunengrenze
(osm_relation_id am OParlBody) über die Overpass-API und legt sie
idempotent (Upsert per osm_id) als Street-Einträge ab.

Verwendung:
    python manage.py import_streets --body muenster
    python manage.py import_streets --all
    python manage.py import_streets --body muenster --with-geometry
"""

import time

import httpx
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from insight_core.models import OParlBody, Street
from insight_core.services.gazetteer import normalize_street_name

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Straßenklassen, die für Ortsbezüge in Vorlagen relevant sind
HIGHWAY_FILTER = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
    "living_street|pedestrian|service|track|footway|cycleway|path|steps|road"
)


class Command(BaseCommand):
    help = "Importiert das Straßenverzeichnis einer Kommune aus OpenStreetMap (Overpass)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--body",
            type=str,
            default=None,
            help="UUID oder Slug der Kommune",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Alle Kommunen mit osm_relation_id importieren",
        )
        parser.add_argument(
            "--with-geometry",
            action="store_true",
            help="Zusätzlich die Way-Geometrie (LineString) speichern (größere Antwort)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=180,
            help="Overpass-Timeout in Sekunden (Standard: 180)",
        )

    def handle(self, *args, **options):
        body_id = options["body"]
        import_all = options["all"]
        with_geometry = options["with_geometry"]
        timeout = options["timeout"]

        if not body_id and not import_all:
            raise CommandError("Bitte --body <uuid|slug> oder --all angeben.")

        if import_all:
            bodies = list(OParlBody.objects.exclude(osm_relation_id__isnull=True))
            if not bodies:
                self.stdout.write(self.style.WARNING("Keine Kommunen mit osm_relation_id gefunden."))
                return
        else:
            try:
                bodies = [OParlBody.objects.get(Q(id=body_id) | Q(slug=body_id))]
            except OParlBody.DoesNotExist:
                raise CommandError(f"Kommune mit ID/Slug '{body_id}' nicht gefunden.")
            except (ValueError, ValidationError):
                # UUID-Parse-Fehler bei Slug-Angabe
                body = OParlBody.objects.filter(slug=body_id).first()
                if not body:
                    raise CommandError(f"Kommune mit Slug '{body_id}' nicht gefunden.")
                bodies = [body]

        for i, body in enumerate(bodies):
            if i > 0:
                time.sleep(5)  # Overpass-Rate-Limit zwischen Kommunen
            self._import_body(body, with_geometry, timeout)

    def _import_body(self, body, with_geometry: bool, timeout: int):
        if not body.osm_relation_id:
            self.stdout.write(
                self.style.WARNING(
                    f"{body.get_display_name()}: keine osm_relation_id gesetzt — übersprungen. "
                    "(Setzen im Admin oder via fetch_osm_geodata.)"
                )
            )
            return

        self.stdout.write(f"{body.get_display_name()} (OSM-Relation {body.osm_relation_id}): lade Straßen...")

        elements = self._fetch_overpass(body.osm_relation_id, with_geometry, timeout)
        if elements is None:
            self.stdout.write(self.style.ERROR("  Overpass-Abfrage fehlgeschlagen."))
            return

        created, updated, skipped = 0, 0, 0
        existing = {s.osm_id: s for s in Street.objects.filter(body=body)}

        to_create = []
        to_update = []
        for el in elements:
            if el.get("type") != "way":
                continue
            name = (el.get("tags") or {}).get("name", "").strip()
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
            if not name or lat is None or lon is None:
                skipped += 1
                continue

            normalized = normalize_street_name(name)
            if not normalized:
                skipped += 1
                continue

            geometry = None
            if with_geometry and el.get("geometry"):
                geometry = {
                    "type": "LineString",
                    "coordinates": [[pt["lon"], pt["lat"]] for pt in el["geometry"]],
                }

            osm_id = el["id"]
            street = existing.get(osm_id)
            if street:
                street.name = name
                street.normalized_name = normalized
                street.latitude = lat
                street.longitude = lon
                if geometry is not None:
                    street.geometry = geometry
                to_update.append(street)
                updated += 1
            else:
                to_create.append(
                    Street(
                        body=body,
                        osm_id=osm_id,
                        name=name,
                        normalized_name=normalized,
                        latitude=lat,
                        longitude=lon,
                        geometry=geometry,
                    )
                )
                created += 1

        if to_create:
            Street.objects.bulk_create(to_create, batch_size=1000, ignore_conflicts=True)
        if to_update:
            Street.objects.bulk_update(
                to_update,
                ["name", "normalized_name", "latitude", "longitude", "geometry", "updated_at"],
                batch_size=1000,
            )

        distinct_names = Street.objects.filter(body=body).values("normalized_name").distinct().count()
        self.stdout.write(
            self.style.SUCCESS(
                f"  {created} neu, {updated} aktualisiert, {skipped} übersprungen "
                f"— {distinct_names} eindeutige Straßennamen im Verzeichnis."
            )
        )

    def _fetch_overpass(self, relation_id: int, with_geometry: bool, timeout: int):
        """Overpass-Abfrage mit Endpoint-Fallback und Retry bei 429/504."""
        area_id = 3600000000 + relation_id
        output = "geom" if with_geometry else ""
        query = (
            f"[out:json][timeout:{timeout}];"
            f"area({area_id})->.searchArea;"
            f'way["highway"~"^({HIGHWAY_FILTER})$"]["name"](area.searchArea);'
            f"out tags center {output};"
        )

        for endpoint in OVERPASS_ENDPOINTS:
            for attempt in range(3):
                try:
                    response = httpx.post(
                        endpoint,
                        data={"data": query},
                        timeout=float(timeout + 30),
                        headers={"User-Agent": "Mandari/1.0 (https://mandari.de)"},
                    )
                    if response.status_code == 200:
                        return response.json().get("elements", [])
                    if response.status_code in (429, 504):
                        wait = 15 * (attempt + 1)
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Overpass {response.status_code} — warte {wait}s (Versuch {attempt + 1}/3)..."
                            )
                        )
                        time.sleep(wait)
                        continue
                    self.stdout.write(self.style.WARNING(f"  Overpass HTTP {response.status_code} ({endpoint})"))
                    break
                except httpx.HTTPError as e:
                    self.stdout.write(self.style.WARNING(f"  Overpass-Fehler ({endpoint}): {e}"))
                    time.sleep(5)
        return None
