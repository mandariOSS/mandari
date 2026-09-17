# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Service-Level-Schwellen prüfen und Alarme verschicken (Issue #231).

Cronjob (täglich):
    python manage.py check_service_levels
Nur Bericht ausgeben:
    python manage.py check_service_levels --report
Alarme anzeigen, nicht senden und keine 24-h-Sperre setzen:
    python manage.py check_service_levels --dry-run

Prüfungen und Schwellen: apps/common/service_levels.py, Einstellungen SERVICE_LEVEL_*.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    sperre = "check_service_levels"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    help = "Prüft Speicherplatz, TLS-Laufzeiten, Fehlerquote und Warteschlange; verschickt Alarme per E-Mail"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--report", action="store_true", help="Befunde ausgeben, keine Alarme senden")
        parser.add_argument("--dry-run", action="store_true", help="Fällige Alarme nur anzeigen, nicht senden")

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.common.service_levels import alle_pruefungen, sende_alarme

        befunde = alle_pruefungen()
        for befund in befunde:
            zeile = f"  [{befund.label:5}] {befund.titel}: {befund.detail}"
            self.stdout.write(zeile if befund.ok else self.style.WARNING(zeile))
        if options["report"]:
            return

        alarme = sende_alarme(befunde, dry_run=options["dry_run"])
        verb = "fällig" if options["dry_run"] else "gesendet"
        self.stdout.write(self.style.SUCCESS(f"Alarme {verb}: {len(alarme)}"))
