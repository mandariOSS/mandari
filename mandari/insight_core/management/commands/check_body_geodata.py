# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Kommunen ohne OSM-Zuordnung bzw. mit Geo-Lücken auflisten.

Zeigt je Kommune, was für die Georeferenzierung fehlt: OSM-Relation-ID, AGS,
Bounding-Box, Straßenverzeichnis, Adressen. Gebiete oberhalb der Gemeinde (AGS kürzer
als acht Stellen) brauchen nur Relation und Bounding-Box; Körperschaften ohne eigenes Gebiet
(Zweckverband, GmbH …) sind keine Lücke. Es werden keine Daten geändert – zuordnen und laden
erledigt ``resolve_body_geodata`` (Issue #351), Einzelfälle der Admin (Kommune → „Geografische
Daten“, Geo-Vorschläge).

Verwendung:
    python manage.py check_body_geodata           # nur Kommunen mit Lücken
    python manage.py check_body_geodata --all     # alle Kommunen
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from insight_core.models import OParlBodyGeoSuggestion
from insight_core.services.body_geo_resolver import body_label
from insight_core.services.geo_coverage import BodyGeoStatus, geo_status_for_bodies


class Command(BaseCommand):
    help = "Listet Kommunen ohne OSM-Relation-ID/AGS bzw. ohne Straßen- und Adressverzeichnis."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--all", action="store_true", help="Auch vollständig gepflegte Kommunen anzeigen")

    def handle(self, *args: Any, **options: Any) -> None:
        statuses = geo_status_for_bodies(only_gaps=not options["all"])
        if not statuses:
            self.stdout.write(self.style.SUCCESS("Alle Kommunen sind vollständig an OSM angebunden."))
            return

        def level(status: BodyGeoStatus) -> str:
            if status.non_territorial:
                return "o. Gebiet"
            if status.regional:
                return "Region"
            # Verbandsgemeinde, Amt, Samtgemeinde: nur 9-stelliger Regionalschlüssel
            return "Verband" if not status.body.ags and len(status.body.rgs or "") == 9 else "Gemeinde"

        header = f"{'Kommune':<40} {'Ebene':<9} {'OSM-Rel.':>10} {'AGS/RGS':>12} {'BBox':>5} {'Straßen':>8} {'Adressen':>9}  fehlt"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for status in statuses:
            body = status.body
            line = (
                f"{body_label(body)[:40]:<40} "
                f"{level(status):<9} "
                f"{(str(body.osm_relation_id) if body.osm_relation_id else '—'):>10} "
                f"{(body.ags or body.rgs or '—'):>12} "
                f"{('ja' if status.has_bbox else '—'):>5} "
                f"{status.street_count:>8} "
                f"{status.address_count:>9}  "
                f"{', '.join(status.missing) or '—'}"
            )
            self.stdout.write(self.style.WARNING(line) if status.missing else line)

        with_gaps = sum(1 for s in statuses if s.missing)
        self.stdout.write("")
        self.stdout.write(
            f"{with_gaps} Kommune(n) mit Lücken. Zuordnen und laden: resolve_body_geodata --source <Quelle>"
        )
        self.stdout.write("(erst mit --dry-run); Einzelfälle im Admin → Kommune → „Geografische Daten“.")
        suggested = OParlBodyGeoSuggestion.objects.values("body_id").distinct().count()
        if suggested:
            self.stdout.write(f"{suggested} Kommune(n) mit Zuordnungsvorschlag: Admin → Geo-Vorschläge.")
        self.stdout.write("Regionalebene (AGS kürzer als 8 Stellen): nur OSM-Relation und Bounding-Box nötig.")
        self.stdout.write("Körperschaften ohne eigenes Gebiet (Zweckverband, GmbH …) sind keine Lücke.")
