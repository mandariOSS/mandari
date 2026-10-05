# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Daten nach einer Neuveröffentlichung im RIS umhängen (Issue #547, ``apps.work.ris.verknuepfungen``).

Läuft als Zeitplan im Worker; der Befehl ist für Prüfung und Betrieb:

    python manage.py ris_verknuepfungen_abgleichen --dry-run --alle   # nur melden, alle Sitzungen und Vorlagen
    python manage.py ris_verknuepfungen_abgleichen                    # wie der Zeitplan (WORK_RIS_RELINK)

``--dry-run`` legt fehlende Anker an (Kennung des heutigen Stands), hängt aber nichts um.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from apps.work.ris.verknuepfungen import abgleichen


class Command(BaseCommand):
    help = "Hängt Notizen, Positionen und Kommentare nach einer Neuveröffentlichung im RIS an den Nachfolger um."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur melden, was umgehängt würde.")
        parser.add_argument(
            "--alle", action="store_true", help="Alle Sitzungen und Vorlagen prüfen, nicht nur geänderte."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        modus = "probe" if options["dry_run"] else settings.WORK_RIS_RELINK
        bericht = abgleichen(modus=modus, alle=options["alle"])
        if bericht.gesperrt:
            self.stdout.write(self.style.WARNING("Ein anderer Abgleich läuft gerade; nichts getan."))
            return
        for name, wert in bericht.as_dict().items():
            self.stdout.write(f"{name}: {wert}")
        for art, von, ergebnis, nach in bericht.geplant:
            self.stdout.write(f"  {art} {von}: {ergebnis}" + (f" → {nach}" if nach else ""))
