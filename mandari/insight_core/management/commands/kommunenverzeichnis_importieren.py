# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenverzeichnis für den Kommunenwechsel im Bürgerportal füllen (Issue #783, docs/INSIGHT_KOMMUNENWECHSEL.md).

Verwendung:
    python manage.py kommunenverzeichnis_importieren --datei kommunen.csv
    python manage.py kommunenverzeichnis_importieren --datei kommunen.csv --ersetzen
    python manage.py kommunenverzeichnis_importieren --aus-koerperschaften
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from insight_core.services.kommunenverzeichnis_import import Ergebnis, aus_koerperschaften, importieren


class Command(BaseCommand):
    help = "Kommunenverzeichnis aus einer CSV-Datei oder aus den gelisteten Kommunen füllen"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--datei", help="CSV-Datei (UTF-8, Semikolon, Kopfzeile; Format siehe Dokumentation)")
        parser.add_argument(
            "--ersetzen", action="store_true", help="Einträge entfernen, die nicht mehr in der Datei stehen"
        )
        parser.add_argument(
            "--aus-koerperschaften",
            action="store_true",
            help="Gelistete Kommunen mit Regionalschlüssel oder AGS übernehmen, soweit sie im Verzeichnis fehlen",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["datei"] and not options["aus_koerperschaften"]:
            raise CommandError("Bitte --datei oder --aus-koerperschaften angeben.")
        if options["datei"]:
            pfad = Path(options["datei"])
            if not pfad.is_file():
                raise CommandError(f"Datei nicht gefunden: {pfad}")
            with pfad.open(encoding="utf-8-sig", newline="") as datei:
                try:
                    ergebnis = importieren(datei, ersetzen=options["ersetzen"])
                except ValueError as fehler:
                    raise CommandError(str(fehler)) from fehler
            self._bericht("Datei", ergebnis)
        if options["aus_koerperschaften"]:
            self._bericht("Gelistete Kommunen", aus_koerperschaften())

    def _bericht(self, quelle: str, ergebnis: Ergebnis) -> None:
        self.stdout.write(
            f"{quelle}: {ergebnis.neu} neu, {ergebnis.aktualisiert} aktualisiert, {ergebnis.entfernt} entfernt, "
            f"{len(ergebnis.uebersprungen)} übersprungen"
        )
        for hinweis in ergebnis.uebersprungen[:20]:
            self.stdout.write(f"  {hinweis}")
        if len(ergebnis.uebersprungen) > 20:
            self.stdout.write(f"  … und {len(ergebnis.uebersprungen) - 20} weitere")
