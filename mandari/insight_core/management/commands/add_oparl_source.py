# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: OParl-Quelle hinzufügen.

Registriert eine neue OParl-Datenquelle und legt die zugehörigen Bodies an.

Usage:
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system"
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system" --osm-id 12345
"""

import re

import httpx
from django.core.management.base import BaseCommand, CommandError

from insight_core.models import OParlBody, OParlSource

_NAMESPACE_VERSION = re.compile(r"^https?://schema\.oparl\.org/(1\.[01])/")

# Obergrenze für paginierte Body-Listen (Verbandsgemeinden haben Dutzende Bodies)
MAX_BODY_PAGES = 20


def is_oparl_error(data) -> bool:
    """OParl-Fehlerobjekt (HTTP 200, type .../Error) — Verhalten von more! rubin bei 1.1-Pfaden."""
    if not isinstance(data, dict):
        return False
    type_url = data.get("type")
    if isinstance(type_url, str) and type_url.rstrip("/").endswith("/Error"):
        return True
    return "error" in data and "id" not in data and "type" not in data


def detect_oparl_version(data) -> str | None:
    """OParl-Version ("1.0"/"1.1") aus oparlVersion oder dem Namespace der type-URL."""
    if not isinstance(data, dict):
        return None
    for key in ("oparlVersion", "type"):
        value = data.get(key)
        if isinstance(value, str):
            match = _NAMESPACE_VERSION.match(value)
            if match:
                return match.group(1)
    return None


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
        headers = {"User-Agent": "Mandari/1.0 (https://mandari.dev)", "Accept": "application/json"}

        try:
            with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                response = client.get(url)

                if response.status_code == 200:
                    return response.json()
                self.stdout.write(self.style.ERROR(f"HTTP {response.status_code} from OParl API"))
                return None
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error fetching OParl data: {e}"))
            return None

    def fetch_oparl_body(self, url):
        """Fetch OParl body data."""
        headers = {"User-Agent": "Mandari/1.0 (https://mandari.dev)", "Accept": "application/json"}

        try:
            with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                response = client.get(url)

                if response.status_code == 200:
                    return response.json()
                return None
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Error fetching body: {e}"))
            return None

    def resolve_bodies(self, body_ref) -> list[dict]:
        """
        Löst das ``body``-Feld des System-Objekts in Body-Dicts auf.

        - String: Listen-URL (``{"data": [...], "links": {"next": ...}}``,
          Standard und OParl 1.0 ``/oparl/Body``) oder ein einzelnes Body-Objekt
        - Liste: Body-URLs (werden einzeln geholt) oder eingebettete Body-Objekte
        """
        refs = [body_ref] if isinstance(body_ref, str) else list(body_ref or [])
        bodies: list[dict] = []
        for ref in refs:
            if isinstance(ref, dict):
                bodies.append(ref)
                continue
            if not isinstance(ref, str):
                continue
            next_url: str | None = ref
            pages = 0
            while next_url and pages < MAX_BODY_PAGES:
                self.stdout.write(f"Fetching bodies: {next_url}")
                data = self.fetch_oparl_body(next_url)
                pages += 1
                if not data or is_oparl_error(data):
                    self.stdout.write(self.style.WARNING("  Could not fetch body data"))
                    break
                if isinstance(data.get("data"), list):
                    bodies.extend(item for item in data["data"] if isinstance(item, dict))
                    links = data.get("links") or {}
                    next_url = links.get("next") if isinstance(links, dict) else None
                else:
                    bodies.append(data)
                    next_url = None
        return bodies

    def handle(self, *args, **options):
        url = options["url"]

        self.stdout.write(f"Fetching OParl system from: {url}")

        # System-Daten abrufen
        system_data = self.fetch_oparl_system(url)

        if not system_data:
            raise CommandError("Failed to fetch OParl system data")

        # OParl 1.0 (more! rubin auf gremien.info) liefert für unbekannte Pfade
        # ein Fehlerobjekt mit HTTP 200 statt 404 — z. B. für /oparl/v1.1/system.
        if is_oparl_error(system_data):
            raise CommandError(
                f"OParl-Fehlerobjekt statt System: {system_data.get('message') or system_data}. "
                "Bei gremien.info die URL https://<mandant>.gremien.info/oparl/system verwenden."
            )

        oparl_version = detect_oparl_version(system_data)

        # System-Info anzeigen
        self.stdout.write(self.style.SUCCESS("\nOParl System Info:"))
        self.stdout.write(f"  Name: {system_data.get('name', 'N/A')}")
        self.stdout.write(f"  OParl Version: {oparl_version or system_data.get('oparlVersion', 'N/A')}")
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
                "oparl_version": oparl_version,
                "raw_json": system_data,
            },
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f"\nSource created: {source.name}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nSource updated: {source.name}"))

        # Bodies verarbeiten: "body" ist laut Spec eine Listen-URL; manche
        # Server liefern stattdessen eine Liste von Body-URLs oder eingebettete
        # Body-Objekte. Listen-URLs (auch 1.0: /oparl/Body) werden paginiert.
        bodies = self.resolve_bodies(system_data.get("body", []))

        self.stdout.write(f"\nFound {len(bodies)} body/bodies")

        for i, body_data in enumerate(bodies):
            body_url = body_data.get("id", "?")
            self.stdout.write(f"\nProcessing body {i + 1}: {body_url}")

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
                },
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
