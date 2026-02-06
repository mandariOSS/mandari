"""
Management Command: Geodaten von OpenStreetMap für eine Kommune abrufen.

Verwendet die OSM Nominatim API um Zentrum und Bounding Box
für eine OSM Relation ID abzurufen.

Usage:
    python manage.py fetch_osm_geodata --body-id <UUID> --osm-id <OSM_RELATION_ID>
    python manage.py fetch_osm_geodata --all  # Für alle Kommunen mit OSM ID
"""

import time

import httpx
from django.core.management.base import BaseCommand, CommandError

from insight_core.models import OParlBody


class Command(BaseCommand):
    help = "Fetch geographic data from OpenStreetMap for municipalities"

    def add_arguments(self, parser):
        parser.add_argument(
            "--body-id",
            type=str,
            help="UUID of the body to fetch geo data for",
        )
        parser.add_argument(
            "--osm-id",
            type=int,
            help="OpenStreetMap relation ID (will be stored in body)",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Fetch geo data for all bodies with osm_relation_id set",
        )

    def fetch_osm_data(self, osm_relation_id):
        """Fetch geographic data from OSM Nominatim API."""
        # Nominatim API mit relation ID (Präfix R für Relation)
        url = "https://nominatim.openstreetmap.org/lookup"
        params = {
            "osm_ids": f"R{osm_relation_id}",
            "format": "json",
            "extratags": 1,
            "addressdetails": 1,
        }

        headers = {"User-Agent": "Mandari/1.0 (https://mandari.dev; contact@mandari.dev)"}

        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                response = client.get(url, params=params)

                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0:
                        return data[0]
                    else:
                        self.stdout.write(self.style.WARNING(f"No data found for OSM relation {osm_relation_id}"))
                        return None
                else:
                    self.stdout.write(self.style.ERROR(f"HTTP {response.status_code} from Nominatim"))
                    return None
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error fetching OSM data: {e}"))
            return None

    def update_body_geodata(self, body, osm_data):
        """Update body with geographic data from OSM."""
        if not osm_data:
            return False

        # Zentrum (lat/lon)
        if "lat" in osm_data and "lon" in osm_data:
            body.latitude = float(osm_data["lat"])
            body.longitude = float(osm_data["lon"])
            self.stdout.write(f"  Center: {body.latitude}, {body.longitude}")

        # Bounding Box
        if "boundingbox" in osm_data:
            bbox = osm_data["boundingbox"]
            # Format: [south, north, west, east]
            body.bbox_south = float(bbox[0])
            body.bbox_north = float(bbox[1])
            body.bbox_west = float(bbox[2])
            body.bbox_east = float(bbox[3])
            self.stdout.write(
                f"  Bounding Box: N{body.bbox_north}, S{body.bbox_south}, E{body.bbox_east}, W{body.bbox_west}"
            )

        body.save()
        return True

    def handle(self, *args, **options):
        if options["all"]:
            # Alle Kommunen mit OSM ID
            bodies = OParlBody.objects.exclude(osm_relation_id__isnull=True)

            if not bodies.exists():
                self.stdout.write(self.style.WARNING("No bodies with osm_relation_id found"))
                return

            self.stdout.write(f"Processing {bodies.count()} bodies...")

            for body in bodies:
                self.stdout.write(f"\n{body.get_display_name()} (OSM: {body.osm_relation_id})")

                osm_data = self.fetch_osm_data(body.osm_relation_id)
                if osm_data:
                    self.update_body_geodata(body, osm_data)
                    self.stdout.write(self.style.SUCCESS("  Updated!"))
                else:
                    self.stdout.write(self.style.WARNING("  No data found"))

                # Rate limiting für Nominatim (max 1 req/sec)
                time.sleep(1.5)

        elif options["body_id"]:
            # Einzelne Kommune
            try:
                body = OParlBody.objects.get(id=options["body_id"])
            except OParlBody.DoesNotExist:
                raise CommandError(f"Body with ID {options['body_id']} not found")

            osm_id = options.get("osm_id") or body.osm_relation_id

            if not osm_id:
                raise CommandError("No OSM relation ID provided. Use --osm-id or set osm_relation_id on the body.")

            self.stdout.write(f"Fetching geo data for {body.get_display_name()}...")
            self.stdout.write(f"OSM Relation ID: {osm_id}")

            # OSM ID speichern falls neu
            if options.get("osm_id"):
                body.osm_relation_id = osm_id
                body.save(update_fields=["osm_relation_id"])

            osm_data = self.fetch_osm_data(osm_id)

            if osm_data:
                # Debug: Zeige verfügbare Daten
                self.stdout.write(f"  Display Name: {osm_data.get('display_name', 'N/A')}")
                self.stdout.write(f"  Type: {osm_data.get('type', 'N/A')}")
                self.stdout.write(f"  Class: {osm_data.get('class', 'N/A')}")

                if self.update_body_geodata(body, osm_data):
                    self.stdout.write(self.style.SUCCESS("\nGeo data updated successfully!"))
                else:
                    self.stdout.write(self.style.WARNING("\nNo geo data to update"))
            else:
                self.stdout.write(self.style.ERROR("Failed to fetch OSM data"))

        else:
            raise CommandError("Please specify --body-id with --osm-id, or use --all")
