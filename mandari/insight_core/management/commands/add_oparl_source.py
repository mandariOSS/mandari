# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: OParl-Quelle hinzufügen.

Registriert eine neue OParl-Datenquelle und legt die zugehörigen Bodies an.

Eine neue Quelle ist inaktiv und eine neue Kommune nicht gelistet (Issue #555): Erst die bewusste Aktivierung im
Admin lässt den Ingestor die Quelle abgleichen, erst das Listen macht eine Kommune im Bürgerportal sichtbar. Ist die
Quelle schon registriert, aktualisiert der Befehl ihre Angaben aus dem System-Objekt; ``is_active`` bleibt, wie es
ist, auch eine abgeschaltete Quelle bleibt abgeschaltet. Ebenso bleibt ``is_listed`` vorhandener Kommunen.

Jede Anfrage hält den Takt je Host ein wie alle anderen Abrufe (``insight_core.services.host_pacing``, Issue
#1027): höchstens eine Anfrage je ``RIS_REQUEST_INTERVAL`` (Standard eine Sekunde) an denselben Host, über alle
Prozesse, auch jede Weiterleitung. Ist die Quelle schon registriert, gelten ihr ``request_interval`` und ihre
robots-Ausnahme; hat eine registrierte Quelle auf dem Host einen größeren ``request_interval``, gilt dieser. Ziele
von Weiterleitungen rufen wir nur ab, wenn die robots.txt ihres Hosts es erlaubt.

Usage:
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system"
    python manage.py add_oparl_source "https://example.com/oparl/v1.1/system" --osm-id 12345
"""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx
from django.core.management.base import BaseCommand, CommandError
from mandari_oparl.pacing import REQUEST_INTERVAL_KEY, host_intervals, host_of

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

    #: sync_config der bereits registrierten Quelle (Takt, robots-Ausnahme); bei einer neuen Quelle leer
    sync_config: dict[str, Any] | None = None
    #: Größter ``request_interval`` registrierter Quellen je Host (``mandari_oparl.pacing.host_intervals``); in
    #: ``handle()`` je Lauf neu gesetzt
    host_intervals: Mapping[str, float] = MappingProxyType({})

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

    def _config_for(self, url: str) -> dict[str, Any] | None:
        """
        ``sync_config`` für Anfragen an den Host von ``url``: die der registrierten Quelle (robots-Ausnahme, Takt).
        Hat eine registrierte Quelle auf diesem Host einen größeren ``request_interval``, gilt dieser (Issue #1027).
        """
        from insight_core.services import host_pacing

        known = self.host_intervals.get(host_of(url))
        if known is None:
            return self.sync_config
        return {**(self.sync_config or {}), REQUEST_INTERVAL_KEY: max(known, host_pacing.interval(self.sync_config))}

    def _robots_allows(self, url) -> bool:
        """robots.txt (RFC 9309) gilt auch beim Anlegen einer Quelle: gesperrte Adressen nicht abrufen."""
        from insight_core.services import robots

        decision = robots.check(url, robots.KIND_API, sync_config=self._config_for(url))
        if not decision.allowed:
            self.stdout.write(self.style.ERROR(f"{url}: {decision.reason} – kein Abruf"))
        return decision.allowed

    def _wait_for_slot(self, url: str) -> None:
        """Drossel je Host (Redis über alle Prozesse): erst zum reservierten Zeitpunkt anfragen."""
        from insight_core.services import host_pacing

        host_pacing.wait(url, sync_config=self._config_for(url))

    def _before_redirect(self, request: httpx.Request) -> None:
        """Vor jeder Weiterleitung: robots.txt des Ziels (Art ``api``), dann im Takt seines Hosts (Issue #1027)."""
        from insight_core.services import redirects, robots

        url = str(request.url)
        decision = robots.check(url, robots.KIND_API, sync_config=self._config_for(url))
        if not decision.allowed:
            raise redirects.RedirectBlockedError(decision, request=request)
        self._wait_for_slot(url)

    def _get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """GET im Takt; Weiterleitungen folgen wir selbst, nach der robots.txt ihres Ziels und im Takt."""
        from insight_core.services import redirects

        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=False) as client:
            self._wait_for_slot(url)
            response = redirects.send(client, client.build_request("GET", url), before_redirect=self._before_redirect)
            response.read()
            return response

    def fetch_oparl_system(self, url):
        """Fetch OParl system data."""
        from insight_core.services import redirects, robots

        if not self._robots_allows(url):
            return None
        # Dieselbe Kennung wie alle automatischen Abrufe (mandari_oparl.crawler)
        headers = {"User-Agent": robots.USER_AGENT, "Accept": "application/json"}

        try:
            response = self._get(url, headers)
            if response.status_code == 200:
                return response.json()
            self.stdout.write(self.style.ERROR(f"HTTP {response.status_code} from OParl API"))
            return None
        except redirects.RedirectBlockedError as e:
            self.stdout.write(self.style.ERROR(f"{e.url}: {e.decision.reason} (Ziel einer Weiterleitung) – kein Abruf"))
            return None
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error fetching OParl data: {e}"))
            return None

    def fetch_oparl_body(self, url):
        """Fetch OParl body data."""
        from insight_core.services import redirects, robots

        if not self._robots_allows(url):
            return None
        headers = {"User-Agent": robots.USER_AGENT, "Accept": "application/json"}

        try:
            response = self._get(url, headers)
            if response.status_code == 200:
                return response.json()
            return None
        except redirects.RedirectBlockedError as e:
            self.stdout.write(
                self.style.WARNING(f"{e.url}: {e.decision.reason} (Ziel einer Weiterleitung) – kein Abruf")
            )
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
                    if pages > 1:
                        # Die übrigen Kommunen legt der Ingestor beim Abgleich an (ungelistet, Issue #869)
                        self.stdout.write(self.style.WARNING(f"  Kommunenliste unvollständig ab Seite {next_url}"))
                    break
                if isinstance(data.get("data"), list):
                    bodies.extend(item for item in data["data"] if isinstance(item, dict))
                    links = data.get("links") or {}
                    next_url = links.get("next") if isinstance(links, dict) else None
                else:
                    bodies.append(data)
                    next_url = None
            else:
                if next_url:
                    self.stdout.write(
                        self.style.WARNING(f"  Kommunenliste nach {MAX_BODY_PAGES} Seiten nicht weiter gelesen")
                    )
        return bodies

    def handle(self, *args, **options):
        url = options["url"]
        # Bereits registrierte Quelle: ihr Takt (request_interval) und ihre robots-Ausnahme gelten auch hier
        self.sync_config = OParlSource.objects.filter(url=url).values_list("sync_config", flat=True).first()
        # Größter Abstand registrierter Quellen je Host (auch inaktiver): Er gilt für Anfragen an diesen Host
        self.host_intervals = host_intervals(OParlSource.objects.values_list("url", "sync_config"))

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

        # Quelle erstellen oder aktualisieren. Neu: inaktiv; vorhanden: is_active bleibt (Issue #555)
        source_fields = {
            "name": system_data.get("name", "Unbenannte Quelle"),
            "contact_email": system_data.get("contactEmail"),
            "contact_name": system_data.get("contactName"),
            "website": system_data.get("website"),
            "oparl_version": oparl_version,
            "raw_json": system_data,
        }
        source, created = OParlSource.objects.update_or_create(
            url=url,
            defaults=source_fields,
            create_defaults={**source_fields, "is_active": False},
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f"\nSource created: {source.name}"))
            self.stdout.write(
                self.style.WARNING(
                    "  Quelle inaktiv angelegt: Der Ingestor gleicht sie erst ab, wenn sie im Admin aktiviert ist."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"\nSource updated: {source.name}"))
            self.stdout.write(f"  Status unverändert: {'aktiv' if source.is_active else 'inaktiv'}")

        # Bodies verarbeiten: "body" ist laut Spec eine Listen-URL; manche
        # Server liefern stattdessen eine Liste von Body-URLs oder eingebettete
        # Body-Objekte. Listen-URLs (auch 1.0: /oparl/Body) werden paginiert.
        bodies = self.resolve_bodies(system_data.get("body", []))

        self.stdout.write(f"\nFound {len(bodies)} body/bodies")

        for i, body_data in enumerate(bodies):
            body_url = body_data.get("id", "?")
            self.stdout.write(f"\nProcessing body {i + 1}: {body_url}")

            # Body erstellen oder aktualisieren. Neu: nicht gelistet; vorhanden: is_listed bleibt (Issue #555)
            body_fields = {
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
            body, body_created = OParlBody.objects.update_or_create(
                external_id=body_data.get("id", body_url),
                defaults=body_fields,
                create_defaults={**body_fields, "is_listed": False},
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
                self.stdout.write("  Nicht gelistet: im Bürgerportal sichtbar erst nach dem Listen im Admin")
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
