# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Umzüge nach einer Neuveröffentlichung im RIS zurückdrehen (Issue #547, ``apps.work.ris.verknuepfungen``).

Rückweg im Betrieb: Erst ``WORK_RIS_RELINK=aus`` bzw. ``probe`` setzen und den Worker neu starten, dann

    python manage.py ris_neuzuordnung_zurueckdrehen --seit 2026-10-06T08:00:00+02:00 --dry-run
    python manage.py ris_neuzuordnung_zurueckdrehen --seit 2026-10-06T08:00:00+02:00
    python manage.py ris_neuzuordnung_zurueckdrehen --eintrag <Kennung aus work_risneuzuordnung>

Die Datensätze kommen an ihren früheren Tagesordnungspunkt bzw. ihre frühere Vorlage zurück und ziehen von dort
nie wieder automatisch um. Inhalte bleiben unberührt.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils.dateparse import parse_datetime

from apps.work.ris.models import RisNeuzuordnung
from apps.work.ris.verknuepfungen import zurueckdrehen


class Command(BaseCommand):
    help = "Dreht Umzüge von Notizen, Positionen und Kommentaren nach einer Neuveröffentlichung im RIS zurück."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--seit", help="Alle Umzüge ab diesem Zeitpunkt (ISO 8601, mit Zeitzone).")
        parser.add_argument("--eintrag", action="append", default=[], help="Einzelner Protokolleintrag (mehrfach).")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts ändern.")

    def handle(self, *args: Any, **options: Any) -> None:
        eintraege = RisNeuzuordnung.objects.filter(zurueckgedreht_am__isnull=True, nach__isnull=False)
        if options["seit"]:
            seit = parse_datetime(options["seit"])
            if seit is None or seit.tzinfo is None:
                raise CommandError("--seit braucht einen Zeitpunkt mit Zeitzone, etwa 2026-10-06T08:00:00+02:00.")
            eintraege = eintraege.filter(erfolgt_am__gte=seit)
        if options["eintrag"]:
            try:
                kennungen = [uuid.UUID(wert) for wert in options["eintrag"]]
            except ValueError as fehler:
                raise CommandError("--eintrag erwartet die Kennung eines Protokolleintrags.") from fehler
            eintraege = eintraege.filter(pk__in=kennungen)
        if not options["seit"] and not options["eintrag"]:
            raise CommandError("Bitte --seit oder --eintrag angeben.")

        bericht = zurueckdrehen(list(eintraege), probe=options["dry_run"])
        if bericht.gesperrt:
            self.stdout.write(self.style.WARNING("Ein Abgleich läuft gerade; nichts getan."))
            return
        vorsilbe = "Würde zurückdrehen" if options["dry_run"] else "Zurückgedreht"
        self.stdout.write(f"{vorsilbe}: {bericht.eintraege} Umzüge, {bericht.datensaetze} Datensätze")
        for eintrag, name, pk in bericht.nicht_moeglich:
            self.stdout.write(self.style.WARNING(f"  nicht zurückgedreht: {eintrag} {name} {pk}"))
        for eintrag in bericht.offen:
            self.stdout.write(self.style.WARNING(f"  bleibt offen (Eindeutigkeit, später erneut versuchen): {eintrag}"))
