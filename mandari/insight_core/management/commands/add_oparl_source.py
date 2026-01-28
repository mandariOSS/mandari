"""
Management Command: OParl-Quelle hinzufügen.

Registriert eine neue OParl-Datenquelle und legt die zugehörigen Bodies an.

Usage:
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system"
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system" --osm-id 12345
"""

import httpx
from django.core.management.base import BaseCommand, CommandError

from insight_core.models import OParlSource, OParlBody


class Command(BaseCommand):
    help = "Add a new OParl source and its bodies"

    def add_arguments(self, parser):
        parser.add_argument(
            "url",
            type=str,
            help="OParl system URL (e.g., https://example.com/oparl/v1.1/system)",
        )
        parser.add_argument(
            "--osm-id",
            type=int,
            help="OpenStreetMap relation ID for the body",
        )
        parser.add_argument(
            "--display-name",
            type=str,
            help="Custom display name for the body",
        )

    def fetch_oparl_system(self, url):
        """Fetch OParl system data."""
        headers = {
            "User-Agent": "Mandari/1.0 (https://mandari.dev)",
            "Accept": "application/json"
        }

        try:
            with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                response = client.get(url)

                if response.status_code == 200:
                    return response.json()
                else:
                    self.stdout.write(self.style.ERROR(
                        f"HTTP {response.status_code} from OParl API"
                    ))
                    return None
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error fetching OParl data: {e}"))
            return None

    def fetch_oparl_body(self, url):
        """Fetch OParl body data."""
        headers = {
            "User-Agent": "Mandari/1.0 (https://mandari.dev)",
            "Accept": "application/json"
        }

        try:
            with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                response = client.get(url)

                if response.status_code == 200:
                    return response.json()
                else:
                    return None
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Error fetching body: {e}"))
            return None

    def handle(self, *args, **options):
        url = options["url"]

        self.stdout.write(f"Fetching OParl system from: {url}")

        # System-Daten abrufen
        system_data = self.fetch_oparl_system(url)

        if not system_data:
            raise CommandError("Failed to fetch OParl system data")

        # System-Info anzeigen
        self.stdout.write(self.style.SUCCESS("\nOParl System Info:"))
        self.stdout.write(f"  Name: {system_data.get('name', 'N/A')}")
        self.stdout.write(f"  OParl Version: {system_data.get('oparlVersion', 'N/A')}")
        self.stdout.write(f"  Contact: {system_data.get('contactEmail', 'N/A')}")
        self.stdout.write(f"  Website: {system_data.get('website', 'N/A')}")

        # Quelle erstellen oder aktualisieren
        source, created = OParlSource.objects.update_or_create(
            url=url,
            defaults={
                "name": system_data.get("name", "Unbenannte Quelle"),
                "contact_email": system_data.get("contactEmail"),
                "contact_name": system_data.get("contactName"),
                "website": system_data.get("website"),
                "is_active": True,
                "raw_json": system_data,
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f"\nSource created: {source.name}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nSource updated: {source.name}"))

        # Bodies verarbeiten
        body_urls = system_data.get("body", [])
        if isinstance(body_urls, str):
            body_urls = [body_urls]

        self.stdout.write(f"\nFound {len(body_urls)} body/bodies")

        for i, body_url in enumerate(body_urls):
            self.stdout.write(f"\nFetching body {i+1}: {body_url}")

            body_data = self.fetch_oparl_body(body_url)

            if not body_data:
                self.stdout.write(self.style.WARNING(f"  Could not fetch body data"))
                continue

            # Body erstellen oder aktualisieren
            body, body_created = OParlBody.objects.update_or_create(
                external_id=body_data.get("id", body_url),
                defaults={
                    "source": source,
                    "name": body_data.get("name", "Unbenannte Kommune"),
                    "short_name": body_data.get("shortName"),
                    "website": body_data.get("website"),
                    "license": body_data.get("license"),
                    "classification": body_data.get("classification"),
                    # List URLs für Sync
                    "organization_list_url": body_data.get("organization"),
                    "person_list_url": body_data.get("person"),
                    "meeting_list_url": body_data.get("meeting"),
                    "paper_list_url": body_data.get("paper"),
                    "membership_list_url": body_data.get("membership"),
                    "agenda_item_list_url": body_data.get("agendaItem"),
                    "file_list_url": body_data.get("file"),
                    # Rohdaten
                    "raw_json": body_data,
                }
            )

            # OSM ID setzen falls angegeben
            if options.get("osm_id"):
                body.osm_relation_id = options["osm_id"]

            # Display Name setzen falls angegeben
            if options.get("display_name"):
                body.display_name = options["display_name"]

            body.save()

            if body_created:
                self.stdout.write(self.style.SUCCESS(f"  Body created: {body.name}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"  Body updated: {body.name}"))

            self.stdout.write(f"  ID: {body.id}")
            self.stdout.write(f"  Classification: {body.classification or 'N/A'}")

            if options.get("osm_id"):
                self.stdout.write(f"  OSM Relation ID: {body.osm_relation_id}")

        self.stdout.write(self.style.SUCCESS("\nDone!"))

        # Hinweis für nächste Schritte
        if options.get("osm_id"):
            self.stdout.write("\nNext steps:")
            self.stdout.write("  1. Fetch geo data: python manage.py fetch_osm_geodata --all")
            self.stdout.write("  2. Prefetch tiles: python manage.py prefetch_tiles")
            self.stdout.write("  3. Run sync: (via ingestor service)")
