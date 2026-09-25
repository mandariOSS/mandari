# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angeforderte Sitzungsmappen erzeugen (Issue #218).

Die Oberfläche legt nur Anforderungen an; dieser Lauf (Cron, z. B. jede Minute) erzeugt
Gesamt-PDF und ZIP-Paket der Reihe nach, älteste Anforderung zuerst:

    python manage.py build_meeting_packages
    python manage.py build_meeting_packages --limit 3 --max-seconds 240

Abgebrochene Läufe (Absturz, Speichermangel) erkennt der nächste Lauf an Mappen, die länger
als eine Stunde „in Arbeit“ sind, und reiht sie erneut ein – nach drei Versuchen bleiben sie
fehlgeschlagen, bis jemand sie neu anfordert. Die Singleton-Sperre verhindert Doppelläufe.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.common.einmalig import EinmaligMixin
from apps.session.services import meeting_package_service


class Command(EinmaligMixin, BaseCommand):
    sperre = "build_meeting_packages"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    help = "Erzeugt angeforderte Sitzungsmappen (Gesamt-PDF und ZIP-Paket) im Hintergrund."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=10, help="Höchstens so viele Mappen je Lauf (Standard: 10)")
        parser.add_argument(
            "--max-seconds",
            type=int,
            default=None,
            help="Nach dieser Laufzeit keine weitere Mappe mehr beginnen",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        result = meeting_package_service.process_requested(limit=options["limit"], max_seconds=options["max_seconds"])
        if result.reset:
            self.stdout.write(f"Abgebrochene Erzeugungen zurückgesetzt: {result.reset}")
        if not result.built and not result.failed:
            # Läuft minütlich: im Leerlauf nur auf Nachfrage (-v 2) melden, sonst füllt sich das Log
            if options["verbosity"] >= 2:
                self.stdout.write("Keine angeforderten Sitzungsmappen.")
            return
        self.stdout.write(f"Erzeugt: {result.built}, fehlgeschlagen: {result.failed}")
        if result.failed:
            self.stderr.write(self.style.WARNING("Mindestens eine Sitzungsmappe ist fehlgeschlagen (siehe Log)."))
