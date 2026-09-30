# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: kanonische Kennungen im RIS-Bestand prüfen (nur lesend).

Zählt je Quelle und Entität, wo ``id`` von ``uuid5(NS_MANDARI_RIS, external_id)`` abweicht, wo Objekte
aus mandari Session nicht unter der öffentlichen OParl-Adresse (``SITE_URL``) liegen, wo die URI fehlt
und wo die kanonische Kennung schon an ein anderes Objekt vergeben ist (ADR
``docs/adr/20260929-kanonisches-modell.md``). Der Befehl ändert nichts; ``--dry-run`` ist nur der
Deutlichkeit halber vorhanden.

Verwendung:
    python manage.py check_ris_ids --dry-run
    python manage.py check_ris_ids --dry-run --source <Quelle: UUID oder URL> --examples 5
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_core.services.ris_ids import ENTITIES, NO_SOURCE, Count, Report, SourceInfo, check_ris_ids

HEADER = f"  {'Entität':<16} {'Objekte':>10} {'Kennung abw.':>13} {'URI abw.':>9} {'ohne URI':>9} {'Kollision':>10}"


class Command(BaseCommand):
    help = "Prüft die kanonischen Kennungen des RIS-Bestands je Quelle und Entität (liest nur)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur lesen (Standard; der Befehl ändert nie etwas)")
        parser.add_argument("--source", help="Nur diese Quelle (UUID oder URL)")
        parser.add_argument("--examples", type=int, default=0, help="Bis zu N Beispiele je Quelle und Entität")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            report = check_ris_ids(only_source=options.get("source"), examples=max(0, options["examples"]))
        except LookupError as exc:
            raise CommandError("Quelle nicht gefunden (UUID oder URL angeben).") from exc

        self.stdout.write("Prüfung der kanonischen RIS-Kennungen (nur lesend, keine Änderungen)")
        self.stdout.write(f"Öffentliche Adresse für Session-Objekte (SITE_URL): {settings.SITE_URL}")
        for key in [*report.sources, NO_SOURCE]:
            self._write_source(report, key)

        self.stdout.write("")
        self.stdout.write("Summe je Entität")
        self.stdout.write(HEADER)
        for entity, count in sorted(report.totals_by_entity().items(), key=lambda item: _order(item[0])):
            self._write_line(entity, count)
        self._write_result(report.total())

    def _write_source(self, report: Report, key: str) -> None:
        rows = [(e.name, report.counts[(key, e.name)]) for e in ENTITIES if (key, e.name) in report.counts]
        if not rows:
            return
        info = report.sources.get(key)
        self.stdout.write("")
        self.stdout.write(_source_title(info))
        if info is not None and info.session_base and info.url != info.session_base:
            self.stdout.write(
                self.style.WARNING(f"  Quelle registriert unter {info.url}, erwartet {info.session_base}")
            )
        self.stdout.write(HEADER)
        for entity, count in rows:
            self._write_line(entity, count)
            for example in count.examples:
                self.stdout.write(f"      {example}")

    def _write_line(self, entity: str, count: Count) -> None:
        line = (
            f"  {entity:<16} {count.objects:>10} {count.id_deviations:>13} {count.uri_deviations:>9} "
            f"{count.without_uri:>9} {count.collisions:>10}"
        )
        abweichend = count.id_deviations or count.uri_deviations or count.without_uri or count.collisions
        self.stdout.write(self.style.WARNING(line) if abweichend else line)

    def _write_result(self, total: Count) -> None:
        self.stdout.write("")
        summary = (
            f"Ergebnis: {total.objects} Objekte, davon {total.id_deviations} mit abweichender Kennung, "
            f"{total.uri_deviations} mit abweichender URI (Session), {total.without_uri} ohne URI, "
            f"{total.collisions} Kollision(en)."
        )
        clean = not (total.id_deviations or total.uri_deviations or total.without_uri or total.collisions)
        self.stdout.write(self.style.SUCCESS(summary) if clean else self.style.WARNING(summary))
        if total.id_deviations:
            self.stdout.write(
                "Abweichende Kennungen bleiben unverändert gültig (Links, Lesezeichen, Suchindex). "
                "Neue Objekte erhalten die kanonische Kennung."
            )
        if total.uri_deviations:
            self.stdout.write(
                "Session-Objekte außerhalb der öffentlichen Adresse: Die Session-OParl-Schnittstelle vergibt URIs "
                "auf Basis von SITE_URL. Ein Abgleich legt diese Objekte sonst unter der neuen URI erneut an."
            )


def _order(entity: str) -> int:
    names = [e.name for e in ENTITIES]
    return names.index(entity) if entity in names else len(names)


def _source_title(info: SourceInfo | None) -> str:
    if info is None:
        return "Ohne Quelle (keine Körperschaft zugeordnet)"
    art = f"Session-Mandant {info.session_tenant}" if info.session_tenant else "Fremd-RIS"
    return f"Quelle: {info.name} ({art}) – {info.url} [{info.key}]"
