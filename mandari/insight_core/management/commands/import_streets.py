# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Straßenverzeichnis (Gazetteer) einer Kommune aus OSM importieren.

Lädt alle benannten highway-Ways innerhalb der Kommunengrenze
(osm_relation_id am OParlBody) über die Overpass-API und legt sie
idempotent (Upsert per osm_id) als Street-Einträge ab.

Mit ``--with-addresses`` werden zusätzlich die Hausnummern-Punkte
(OSM-Objekte mit addr:street + addr:housenumber) als Address-Einträge
importiert; die Georeferenzierung bevorzugt sie bei „Straße Hausnummer“
im Text, das Nachbarschafts-Autocomplete schlägt sie vor (Issue #54).

Verwendung:
    python manage.py import_streets --body muenster
    python manage.py import_streets --all
    python manage.py import_streets --body muenster --with-geometry
    python manage.py import_streets --body muenster --with-addresses
"""

import time

import httpx
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from insight_core.models import Address, OParlBody, Street
from insight_core.services.gazetteer import normalize_house_number, normalize_street_name

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Straßenklassen, die für Ortsbezüge in Vorlagen relevant sind
HIGHWAY_FILTER = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
    "living_street|pedestrian|service|track|footway|cycleway|path|steps|road"
)


def parse_address_element(el: dict) -> dict | None:
    """
    Wandelt ein Overpass-Element mit addr:*-Tags in Address-Felder um.

    Nodes tragen lat/lon direkt, Ways/Relations liefern über ``out center`` einen
    Mittelpunkt. Ohne Straße, Hausnummer oder Koordinaten: None.
    """
    osm_type = el.get("type")
    if osm_type not in ("node", "way", "relation"):
        return None
    tags = el.get("tags") or {}
    street = (tags.get("addr:street") or "").strip()
    house_number = (tags.get("addr:housenumber") or "").strip()
    if not street or not house_number:
        return None
    if osm_type == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    normalized_street = normalize_street_name(street)
    normalized_number = normalize_house_number(house_number)
    if not normalized_street or not normalized_number:
        return None
    return {
        "osm_type": osm_type,
        "osm_id": int(el["id"]),
        "street": street[:255],
        "normalized_street": normalized_street[:255],
        "house_number": house_number[:20],
        "normalized_house_number": normalized_number[:20],
        "postal_code": (tags.get("addr:postcode") or "").strip()[:20],
        "latitude": lat,
        "longitude": lon,
    }


class Command(BaseCommand):
    help = "Importiert das Straßenverzeichnis (und optional Adressen) einer Kommune aus OpenStreetMap (Overpass)."

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
            "--with-addresses",
            action="store_true",
            help="Zusätzlich Hausnummern-Punkte (addr:street + addr:housenumber) als Adressen importieren",
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
        with_addresses = options["with_addresses"]
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
                raise CommandError(f"Kommune mit ID/Slug '{body_id}' nicht gefunden.") from None
            except (ValueError, ValidationError):
                # UUID-Parse-Fehler bei Slug-Angabe
                body = OParlBody.objects.filter(slug=body_id).first()
                if not body:
                    raise CommandError(f"Kommune mit Slug '{body_id}' nicht gefunden.") from None
                bodies = [body]

        regional = [body for body in bodies if body.is_regional_level]
        for body in regional:
            self.stdout.write(
                self.style.WARNING(
                    f"{body.get_display_name()}: Regionalebene (AGS {body.ags}) – kein Straßen- und Adressimport."
                )
            )
        bodies = [body for body in bodies if not body.is_regional_level]

        for i, body in enumerate(bodies):
            if i > 0:
                time.sleep(5)  # Overpass-Rate-Limit zwischen Kommunen
            self._import_body(body, with_geometry, timeout)
            if with_addresses and body.osm_relation_id:
                time.sleep(5)
                self._import_addresses(body, timeout)

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

    def _import_addresses(self, body, timeout: int):
        """Hausnummern-Punkte (addr:*-Tags) der Kommune als Address-Einträge upserten."""
        self.stdout.write(f"{body.get_display_name()}: lade Adressen (addr:*-Tags)...")
        elements = self._fetch_overpass_addresses(body.osm_relation_id, timeout)
        if elements is None:
            self.stdout.write(self.style.ERROR("  Overpass-Abfrage (Adressen) fehlgeschlagen."))
            return

        existing = {(a.osm_type, a.osm_id): a for a in Address.objects.filter(body=body)}
        to_create: list[Address] = []
        to_update: list[Address] = []
        skipped = 0
        seen: set[tuple[str, int]] = set()
        for el in elements:
            parsed = parse_address_element(el)
            if parsed is None:
                skipped += 1
                continue
            key = (parsed["osm_type"], parsed["osm_id"])
            if key in seen:
                continue
            seen.add(key)
            address = existing.get(key)
            if address:
                for field, value in parsed.items():
                    setattr(address, field, value)
                to_update.append(address)
            else:
                to_create.append(Address(body=body, **parsed))

        if to_create:
            Address.objects.bulk_create(to_create, batch_size=1000, ignore_conflicts=True)
        if to_update:
            Address.objects.bulk_update(
                to_update,
                [
                    "street",
                    "normalized_street",
                    "house_number",
                    "normalized_house_number",
                    "postal_code",
                    "latitude",
                    "longitude",
                    "updated_at",
                ],
                batch_size=1000,
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"  {len(to_create)} Adressen neu, {len(to_update)} aktualisiert, {skipped} übersprungen "
                f"— {Address.objects.filter(body=body).count()} Adressen im Verzeichnis."
            )
        )

    def _fetch_overpass_addresses(self, relation_id: int, timeout: int):
        area_id = 3600000000 + relation_id
        query = (
            f"[out:json][timeout:{timeout}];"
            f"area({area_id})->.searchArea;"
            'nwr["addr:housenumber"]["addr:street"](area.searchArea);'
            "out tags center;"
        )
        return self._run_overpass(query, timeout)

    def _fetch_overpass(self, relation_id: int, with_geometry: bool, timeout: int):
        """Overpass-Abfrage (Straßen) mit Endpoint-Fallback und Retry bei 429/504."""
        area_id = 3600000000 + relation_id
        output = "geom" if with_geometry else ""
        query = (
            f"[out:json][timeout:{timeout}];"
            f"area({area_id})->.searchArea;"
            f'way["highway"~"^({HIGHWAY_FILTER})$"]["name"](area.searchArea);'
            f"out tags center {output};"
        )
        return self._run_overpass(query, timeout)

    def _run_overpass(self, query: str, timeout: int):
        """Overpass-Abfrage mit Endpoint-Fallback und Retry bei 429/504."""
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
