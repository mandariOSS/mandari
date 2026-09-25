# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Geo-Daten von Kommunen automatisch zuordnen und laden (Issue #351).

Ein Aufruf beim Freischalten einer Quelle ersetzt die Handpflege aus #54:

1. Körperschaften ohne eigenes Gebiet (Zweckverband, GmbH, Waldgemarkung …) kennzeichnen,
   mit Verweis auf die übergeordnete Körperschaft derselben Quelle.
2. Gemeindeschlüssel (AGS) und Regionalschlüssel aus OParl übernehmen.
3. OSM-Grenze per AGS suchen, sonst per Name in der übergeordneten Körperschaft bzw. bundesweit.
   Mehrdeutiges wird nicht geraten, sondern im Admin als „Geo-Vorschlag“ abgelegt.
4. Für neu zugeordnete oder unvollständige Gemeinden ``fetch_osm_geodata`` (Zentrum, Bounding-Box)
   und ``import_streets --with-addresses`` anstoßen; Regionalebenen überspringt ``import_streets``.
   Körperschaften ohne Gebiet übernehmen Zentrum und Kartenausschnitt der übergeordneten.

Verwendung:
    python manage.py resolve_body_geodata --source <uuid|Teil der URL> --dry-run
    python manage.py resolve_body_geodata --source <uuid|Teil der URL>
    python manage.py resolve_body_geodata --body <uuid|slug>
    python manage.py resolve_body_geodata --all --no-import

Ohne ``--dry-run`` wird je Kommune sofort gespeichert; ein abgebrochener Lauf kann einfach erneut
gestartet werden. Bereits vollständige Kommunen lösen keine Abfragen aus.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from insight_core.models import Address, OParlBody, OParlSource, Street
from insight_core.services.body_geo_resolver import (
    STATUS_ASSIGNED,
    STATUS_FAILED,
    STATUS_NON_TERRITORIAL,
    STATUS_NOT_FOUND,
    STATUS_SKIPPED,
    STATUS_SUGGESTED,
    STATUS_UNCHANGED,
    BodyGeoResolver,
    BodyResolution,
    apply_parent_area,
    body_label,
)

# Pause zwischen zwei Kommunen beim Straßen- und Adressimport (wie import_streets --all)
IMPORT_PAUSE_SECONDS = 5.0
# Nominatim erlaubt höchstens eine Anfrage je Sekunde
NOMINATIM_PAUSE_SECONDS = 1.5
# Abbruch, wenn so viele Straßenimporte in Folge leer bleiben (Overpass nicht erreichbar)
MAX_IMPORT_FAILURES = 3


def _has_bbox(body: OParlBody) -> bool:
    return bool(body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west)


class Command(BaseCommand):
    help = (
        "Ordnet Kommunen automatisch ihre OSM-Grenze und ihren Gemeindeschlüssel zu, kennzeichnet "
        "Körperschaften ohne Gebiet und lädt danach Kartenausschnitt, Straßen und Adressen."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--source",
            action="append",
            default=[],
            help="Quelle als UUID oder eindeutiger Teil von URL/Name (mehrfach möglich)",
        )
        parser.add_argument("--body", action="append", default=[], help="Kommune als UUID oder Slug (mehrfach)")
        parser.add_argument("--all", action="store_true", help="Alle nicht gelöschten Kommunen")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nichts ändern, nur zeigen, was passieren würde (fragt Overpass trotzdem ab)",
        )
        parser.add_argument(
            "--no-import",
            action="store_true",
            help="Nur zuordnen; fetch_osm_geodata und import_streets nicht anstoßen",
        )
        parser.add_argument(
            "--pause",
            type=float,
            default=3.0,
            help="Pause in Sekunden zwischen zwei Overpass-Abfragen der Zuordnung (Standard: 3)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=90,
            help="Overpass-Timeout der Zuordnungsabfragen in Sekunden (Standard: 90)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        bodies = self._select_bodies(options["source"], options["body"], options["all"])
        if not bodies:
            self.stdout.write(self.style.WARNING("Keine Kommunen ausgewählt."))
            return
        dry_run: bool = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("Probelauf (--dry-run): Es wird nichts gespeichert."))
        self.stdout.write(f"{len(bodies)} Kommune(n) werden geprüft.\n")

        resolver = BodyGeoResolver(
            dry_run=dry_run,
            pause=max(options["pause"], 0.0),
            timeout=options["timeout"],
            log=lambda message: self.stdout.write(self.style.WARNING(message)),
        )
        results = resolver.resolve(bodies)
        for result in results:
            self._print_result(result, dry_run)

        if resolver.aborted:
            self._print_summary(results, dry_run)
            raise CommandError(
                "Overpass mehrfach nicht erreichbar – Lauf abgebrochen. Bereits Zugeordnetes ist gespeichert; "
                "den Befehl später erneut aufrufen."
            )

        if not options["no_import"]:
            self._follow_up(results, dry_run)
        self._copy_parent_areas(results, dry_run)
        self._print_summary(results, dry_run)

    # --- Auswahl --------------------------------------------------------------

    def _select_bodies(self, sources: list[str], body_refs: list[str], select_all: bool) -> list[OParlBody]:
        if not (sources or body_refs or select_all):
            raise CommandError("Bitte --source, --body oder --all angeben.")
        queryset = OParlBody.objects.filter(deleted=False).select_related("source", "territory_parent")
        if select_all:
            return list(queryset.order_by("name"))
        selected: dict[Any, OParlBody] = {}
        for ref in sources:
            source = self._find_source(ref)
            for body in queryset.filter(source=source).order_by("name"):
                selected[body.id] = body
        for ref in body_refs:
            body = self._find_body(queryset, ref)
            selected[body.id] = body
        return list(selected.values())

    def _find_source(self, ref: str) -> OParlSource:
        try:
            return OParlSource.objects.get(id=ref)
        except (OParlSource.DoesNotExist, ValueError, ValidationError):
            pass
        matches = list(OParlSource.objects.filter(Q(url__icontains=ref) | Q(name__icontains=ref))[:5])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CommandError(f"Quelle „{ref}“ nicht gefunden.")
        raise CommandError(f"Quelle „{ref}“ ist nicht eindeutig: " + ", ".join(s.url for s in matches))

    def _find_body(self, queryset: Any, ref: str) -> OParlBody:
        try:
            body: OParlBody = queryset.get(id=ref)
            return body
        except (OParlBody.DoesNotExist, ValueError, ValidationError):
            pass
        found: OParlBody | None = queryset.filter(slug=ref).first()
        if found is None:
            raise CommandError(f"Kommune „{ref}“ nicht gefunden.")
        return found

    # --- Ausgabe --------------------------------------------------------------

    def _print_result(self, result: BodyResolution, dry_run: bool) -> None:
        body = result.body
        label = f"{body.name} [{body.id}]"
        styles: dict[str, Callable[[str], str]] = {
            STATUS_ASSIGNED: self.style.SUCCESS,
            STATUS_SUGGESTED: self.style.WARNING,
            STATUS_NOT_FOUND: self.style.WARNING,
            STATUS_FAILED: self.style.ERROR,
            STATUS_SKIPPED: self.style.ERROR,
        }
        status = result.status
        if dry_run and status == STATUS_ASSIGNED:
            status = "würde zugeordnet"
        style = styles.get(result.status, str)
        self.stdout.write(style(f"{label}: {status}"))
        for note in result.notes:
            self.stdout.write(f"    {note}")
        if result.changed_fields:
            verb = "würde ändern" if dry_run else "geändert"
            self.stdout.write(f"    {verb}: {', '.join(sorted(result.changed_fields))}")

    def _print_summary(self, results: list[BodyResolution], dry_run: bool) -> None:
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        self.stdout.write("")
        self.stdout.write("Zusammenfassung" + (" (Probelauf)" if dry_run else "") + ":")
        for status in (
            STATUS_ASSIGNED,
            STATUS_SUGGESTED,
            STATUS_NON_TERRITORIAL,
            STATUS_NOT_FOUND,
            STATUS_FAILED,
            STATUS_SKIPPED,
            STATUS_UNCHANGED,
        ):
            if counts.get(status):
                self.stdout.write(f"  {status}: {counts[status]}")
        if counts.get(STATUS_SUGGESTED):
            self.stdout.write(
                "Vorschläge prüfen: Admin → Geo-Vorschläge → „Vorschlag übernehmen“, "
                "danach diesen Befehl für die Kommune erneut aufrufen."
            )

    # --- Schritt 5: Kartenausschnitt, Straßen, Adressen -----------------------

    def _follow_up(self, results: list[BodyResolution], dry_run: bool) -> None:
        """Zentrum/BBox (Nominatim) und Straßen/Adressen (Overpass) für neue oder lückenhafte Gemeinden."""
        territorial = [r for r in results if not r.body.is_non_territorial and r.body.osm_relation_id]
        fetch = [r.body for r in territorial if r.new_relation or not _has_bbox(r.body)]
        ids = [r.body.id for r in territorial]
        # DISTINCT in der Datenbank: Köln allein hat 167.000 Adressen
        streets = set(Street.objects.filter(body_id__in=ids).values_list("body_id", flat=True).distinct())
        addresses = set(Address.objects.filter(body_id__in=ids).values_list("body_id", flat=True).distinct())
        imports = [
            r.body
            for r in territorial
            if not r.body.is_regional_level
            and (r.new_relation or r.body.id not in streets or r.body.id not in addresses)
        ]
        if not fetch and not imports:
            return

        self.stdout.write("")
        if dry_run:
            for body in fetch:
                self.stdout.write(f"würde Zentrum und Bounding-Box laden: {body.name}")
            for body in imports:
                self.stdout.write(f"würde Straßen und Adressen importieren: {body.name}")
            return

        for i, body in enumerate(fetch):
            if i > 0:
                time.sleep(NOMINATIM_PAUSE_SECONDS)
            call_command("fetch_osm_geodata", body_id=str(body.id), stdout=self.stdout, stderr=self.stderr)
            body.refresh_from_db()
        failures = 0
        for i, body in enumerate(imports):
            if i > 0 or fetch:
                time.sleep(IMPORT_PAUSE_SECONDS)
            call_command(
                "import_streets", body=str(body.id), with_addresses=True, stdout=self.stdout, stderr=self.stderr
            )
            # Jede Gemeinde hat benannte Straßen; bleibt das Verzeichnis leer, antwortet Overpass nicht
            failures = 0 if Street.objects.filter(body_id=body.id).exists() else failures + 1
            if failures >= MAX_IMPORT_FAILURES:
                raise CommandError(
                    f"Straßenimport {failures}-mal in Folge ohne Ergebnis – Overpass prüfen und den Befehl "
                    "später erneut aufrufen. Zuordnungen sind gespeichert, der Import setzt bei den fehlenden "
                    "Gemeinden fort."
                )

    def _copy_parent_areas(self, results: list[BodyResolution], dry_run: bool) -> None:
        """Körperschaften ohne Gebiet: Kartenausschnitt der übergeordneten Körperschaft übernehmen."""
        for result in results:
            body = result.body
            if not body.is_non_territorial or body.territory_parent is None:
                continue
            if not dry_run:
                body.territory_parent.refresh_from_db()
            parent = apply_parent_area(body, dry_run=dry_run)
            if parent is not None:
                verb = "würde Kartenausschnitt übernehmen" if dry_run else "Kartenausschnitt übernommen"
                self.stdout.write(f"{body.name}: {verb} von {body_label(parent)}")
