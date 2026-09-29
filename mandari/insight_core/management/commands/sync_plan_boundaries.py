# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Amtliche Umringe von Bebauungsplänen abrufen und Vorlagen zuordnen (Issue #598).

Täglich per Cron (docs/INSIGHT_GEO.md):

    python manage.py sync_plan_boundaries                     # alle aktiven Quellen, alle Kommunen
    python manage.py sync_plan_boundaries --body muenster     # nur eine Kommune
    python manage.py sync_plan_boundaries --no-fetch          # nur zuordnen (ohne Netzabruf)
    python manage.py sync_plan_boundaries --dry-run           # abrufen und zählen, nichts speichern
    python manage.py sync_plan_boundaries --add-nrw-source --body muenster   # Landesquelle NRW anlegen

Am Ende steht je Kommune die Abdeckung der letzten ``--months`` Monate: Vorlagen mit Plannummer im
Titel, davon mit Umring, und die Lücken (Vorlagen, deren Plan keine Quelle kennt).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q
from django.utils import timezone

from apps.common.einmalig import EinmaligMixin

NRW_OGC_ITEMS_URL = "https://ogc-api.nrw.de/inspire-lu-bplan/v1/collections/spatialplan/items"
NRW_LICENSE_URL = "https://www.govdata.de/dl-de/by-2-0"
SOURCE_PAUSE_SECONDS = 2.0


class Command(EinmaligMixin, BaseCommand):
    help = "Amtliche Umringe von Bebauungsplänen abrufen (täglich) und Vorlagen mit Plannummer zuordnen"
    sperre = "sync_plan_boundaries"
    sperre_ttl = 2 * 3600

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", help="Nur diese Kommune (Slug oder UUID)")
        parser.add_argument("--no-fetch", action="store_true", help="Keine Quellen abrufen, nur zuordnen")
        parser.add_argument("--dry-run", action="store_true", help="Abrufen und zählen, nichts speichern")
        parser.add_argument("--months", type=int, default=12, help="Zeitraum der Abdeckungsstatistik (Monate)")
        parser.add_argument("--show-gaps", type=int, default=20, help="So viele Lücken je Kommune auflisten")
        parser.add_argument(
            "--add-nrw-source",
            action="store_true",
            help="Landesquelle NRW (OGC API, rechtskräftige Pläne) für --body anlegen; braucht den AGS",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlBody, PlanBoundarySource

        body = self._body(str(options["body"])) if options.get("body") else None
        if options["add_nrw_source"]:
            if body is None:
                raise CommandError("--add-nrw-source braucht --body")
            self._add_nrw_source(body)
            return

        sources = PlanBoundarySource.objects.filter(is_active=True, body__deleted=False).select_related("body")
        if body is not None:
            sources = sources.filter(body=body)
        sources = sources.order_by("body__name", "priority", "pk")
        body_ids = list(dict.fromkeys(sources.values_list("body_id", flat=True)))
        if not body_ids:
            self.stdout.write("Keine aktive Umring-Quelle – nichts zu tun.")
            return

        dry_run = bool(options["dry_run"])
        if not options["no_fetch"]:
            self._refresh(list(sources), dry_run=dry_run)
        for current in OParlBody.objects.filter(pk__in=body_ids).order_by("name"):
            self._link(current, dry_run=dry_run)
            self._report(current, months=max(1, int(options["months"])), show_gaps=max(0, int(options["show_gaps"])))

    def _body(self, value: str) -> Any:
        from insight_core.models import OParlBody

        query = Q(slug=value)
        if len(value) == 36:
            query |= Q(pk=value)
        body = OParlBody.objects.filter(query).first()
        if body is None:
            raise CommandError(f"Kommune „{value}“ nicht gefunden")
        return body

    def _add_nrw_source(self, body: Any) -> None:
        from insight_core.models import PlanBoundarySource

        ags = (body.ags or "").strip()
        if len(ags) != 8 or not ags.startswith("05"):
            raise CommandError("Die Landesquelle NRW braucht einen achtstelligen AGS aus NRW (beginnt mit 05)")
        source, created = PlanBoundarySource.objects.get_or_create(
            body=body,
            kind=PlanBoundarySource.KIND_OGC_API,
            url=NRW_OGC_ITEMS_URL,
            defaults={
                "name": "Land NRW – rechtskräftige Bebauungspläne",
                "query_params": {"gkz": ags},
                "property_filter": {"planTypeName.code": [1000]},
                "number_property": "nr",
                "title_property": "officialTitle",
                "link_property": "officialDocument",
                "plan_status": PlanBoundarySource.PLAN_STATUS_IN_FORCE,
                "attribution": "Land NRW (Bauleitplanung), Datenlizenz Deutschland – Namensnennung – 2.0",
                "license_url": NRW_LICENSE_URL,
                "priority": 200,
            },
        )
        state = "angelegt" if created else "bereits vorhanden"
        self.stdout.write(
            self.style.SUCCESS(f"Landesquelle NRW für {body.get_display_name()} {state} (ID {source.pk}).")
        )

    def _refresh(self, sources: list[Any], *, dry_run: bool) -> None:
        from insight_core.services.plan_boundaries import refresh_source

        for index, source in enumerate(sources):
            if index:
                time.sleep(SOURCE_PAUSE_SECONDS)
            result = refresh_source(source, dry_run=dry_run)
            prefix = "[Probelauf] " if dry_run else ""
            if result.error:
                self.stdout.write(self.style.WARNING(f"{prefix}{source}: {result.error}"))
                continue
            skipped = ", ".join(f"{reason}: {count}" for reason, count in sorted(result.skipped.items()))
            self.stdout.write(
                f"{prefix}{source}: {result.fetched} Objekte, {result.stored} Umringe "
                f"(neu {result.created}, geändert {result.updated}, entfernt {result.deleted})"
                + (f"; übersprungen – {skipped}" if skipped else "")
            )

    def _link(self, body: Any, *, dry_run: bool) -> None:
        from insight_core.services.plan_boundaries import link_body_papers

        result = link_body_papers(body, dry_run=dry_run)
        prefix = "[Probelauf] " if dry_run else ""
        self.stdout.write(
            f"{prefix}{body.get_display_name()}: {result.papers} Vorgänge mit Plannummer, "
            f"{result.references} Bezüge ({result.matched} mit Umring, {result.unmatched} ohne), "
            f"{result.papers_changed} Zuordnungen und {result.locations_changed} Verortungen geändert"
        )

    def _report(self, body: Any, *, months: int, show_gaps: int) -> None:
        from insight_core.models import PaperPlanReference

        since = timezone.localdate() - timedelta(days=round(months * 30.44))
        references = PaperPlanReference.objects.filter(
            body=body, paper__deleted=False, paper__date__gte=since
        ).select_related("paper")
        papers: dict[Any, bool] = {}
        gaps: list[PaperPlanReference] = []
        for reference in references.order_by("-paper__date", "paper__reference"):
            matched = reference.match != PaperPlanReference.MATCH_NONE
            papers[reference.paper_id] = papers.get(reference.paper_id, False) or matched
            if not matched:
                gaps.append(reference)
        total = len(papers)
        covered = sum(1 for value in papers.values() if value)
        share = f"{covered / total * 100:.1f} %".replace(".", ",") if total else "–"
        self.stdout.write(
            f"  Abdeckung seit {since:%d.%m.%Y}: {total} Vorlagen mit Plannummer, {covered} mit Umring ({share}), "
            f"{len(gaps)} Lücken"
        )
        for reference in gaps[:show_gaps]:
            paper = reference.paper
            date = f"{paper.date:%d.%m.%Y}" if paper.date else "ohne Datum"
            self.stdout.write(f"    Lücke: {paper.reference or paper.pk} ({date}) – {reference}")
