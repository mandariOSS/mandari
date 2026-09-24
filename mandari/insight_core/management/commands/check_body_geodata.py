# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Kommunen ohne OSM-Zuordnung bzw. mit Geo-Lücken auflisten.

Zeigt je Kommune, was für die Georeferenzierung fehlt: OSM-Relation-ID, AGS,
Bounding-Box, Straßenverzeichnis, Adressen. Gebiete oberhalb der Gemeinde (AGS kürzer
als acht Stellen) brauchen nur Relation und Bounding-Box. Es werden keine Daten geändert –
die Pflege erfolgt im Admin (Kommune → „Geografische Daten“) und anschließend mit
``fetch_osm_geodata`` sowie ``import_streets --with-addresses`` (Issue #54).

Verwendung:
    python manage.py check_body_geodata           # nur Kommunen mit Lücken
    python manage.py check_body_geodata --all     # alle Kommunen
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from insight_core.services.geo_coverage import geo_status_for_bodies


class Command(BaseCommand):
    help = "Listet Kommunen ohne OSM-Relation-ID/AGS bzw. ohne Straßen- und Adressverzeichnis."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--all", action="store_true", help="Auch vollständig gepflegte Kommunen anzeigen")

    def handle(self, *args: Any, **options: Any) -> None:
        statuses = geo_status_for_bodies(only_gaps=not options["all"])
        if not statuses:
            self.stdout.write(self.style.SUCCESS("Alle Kommunen sind vollständig an OSM angebunden."))
            return

        header = f"{'Kommune':<40} {'Ebene':<8} {'OSM-Rel.':>10} {'AGS':>9} {'BBox':>5} {'Straßen':>8} {'Adressen':>9}  fehlt"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for status in statuses:
            body = status.body
            line = (
                f"{(body.display_name or body.short_name or body.name)[:40]:<40} "
                f"{('Region' if status.regional else 'Gemeinde'):<8} "
                f"{(str(body.osm_relation_id) if body.osm_relation_id else '—'):>10} "
                f"{(body.ags or '—'):>9} "
                f"{('ja' if status.has_bbox else '—'):>5} "
                f"{status.street_count:>8} "
                f"{status.address_count:>9}  "
                f"{', '.join(status.missing) or '—'}"
            )
            self.stdout.write(self.style.WARNING(line) if status.missing else line)

        with_gaps = sum(1 for s in statuses if s.missing)
        self.stdout.write("")
        self.stdout.write(f"{with_gaps} Kommune(n) mit Lücken. Pflege: Admin → Kommune → „Geografische Daten“,")
        self.stdout.write("danach fetch_osm_geodata --all und import_streets --all --with-addresses.")
        self.stdout.write("Regionalebene (AGS kürzer als 8 Stellen): nur OSM-Relation und Bounding-Box nötig.")
