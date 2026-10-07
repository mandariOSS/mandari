# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP-Abschnitte einer Übertragung anhand der protokollierten Lesungen neu zuordnen (Teil von Issue #47,
``hub.live.neuzuordnung``, docs/LIVE_UEBERTRAGUNG.md).

    python manage.py live_abschnitte_neu_zuordnen --uebertragung <uuid> [--probelauf]

Korrigiert die Zuordnung, ergänzt gelesene Nummer und Sicherheit, führt Abschnitte desselben TOP zusammen und hängt
Wortmeldungen um. Sendet keine Ereignisse. Idempotent; ``--probelauf`` zeigt nur, was sich ändern würde.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...neuzuordnung import NeuzuordnungError, neu_zuordnen


class Command(BaseCommand):
    help = "TOP-Abschnitte einer Übertragung anhand der protokollierten Lesungen neu zuordnen (ohne Ereignisse)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--uebertragung", required=True, help="UUID der Übertragung")
        parser.add_argument("--probelauf", action="store_true", help="nur anzeigen, was sich ändern würde")

    def handle(self, *args: Any, **optionen: Any) -> None:
        try:
            kennung = uuid.UUID(optionen["uebertragung"])
        except ValueError as fehler:
            raise CommandError("--uebertragung: UUID erwartet") from fehler
        probelauf = bool(optionen["probelauf"])
        try:
            ergebnis = neu_zuordnen(kennung, probelauf=probelauf)
        except NeuzuordnungError as fehler:
            raise CommandError(str(fehler)) from fehler
        vorsatz = "Probelauf, nichts gespeichert" if probelauf else "Gespeichert"
        self.stdout.write(
            f"{vorsatz}: {ergebnis.lesungen} Lesungen; {len(ergebnis.korrigiert)} Abschnitte korrigiert, "
            f"{ergebnis.ergaenzt} ergänzt, {len(ergebnis.zusammengefuehrt)} zusammengeführt, {len(ergebnis.neu)} neu, "
            f"{ergebnis.wortmeldungen} Wortmeldungen umgehängt, {ergebnis.enden} Enden angepasst"
        )
        for art, zeilen in (
            ("korrigiert", ergebnis.korrigiert),
            ("zusammengeführt", ergebnis.zusammengefuehrt),
            ("neu", ergebnis.neu),
        ):
            for zeile in zeilen:
                self.stdout.write(f"  {art}: {zeile}")
        for hinweis in ergebnis.hinweise:
            self.stdout.write(f"Hinweis: {hinweis}")
        if not ergebnis.aenderungen:
            self.stdout.write("Keine Änderungen.")
