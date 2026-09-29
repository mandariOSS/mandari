# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sequenzierer als eigener Prozess: vergibt Folgenummern an festgeschriebene Ereignisse.

Genau ein Prozess arbeitet (Lease ``sequencer``); weitere warten und übernehmen spätestens 30 s
nach dem Ausfall des Inhabers. SIGTERM und SIGINT beenden den Dauerbetrieb nach dem laufenden
Lauf und geben die Lease frei. Später übernimmt ``events_worker`` diese Rolle; der Befehl bleibt
für Betrieb und Fehlersuche.

    manage.py events_sequencer            # Dauerbetrieb
    manage.py events_sequencer --once     # einmal alles Vergebbare nummerieren, dann Ende
"""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.events.sequencer import BATCH_SIZE, POLL_INTERVAL, Sequencer, SequencerUnavailableError, require_postgresql


class Command(BaseCommand):
    help = "Vergibt Folgenummern an festgeschriebene Ereignisse (Leader-Rolle 'sequencer')."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Einmal alles Vergebbare nummerieren, dann beenden.")
        parser.add_argument(
            "--interval", type=float, default=POLL_INTERVAL, help="Sekunden zwischen zwei Läufen (Dauerbetrieb)."
        )
        parser.add_argument("--batch", type=int, default=BATCH_SIZE, help="Ereignisse je Lauf.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            require_postgresql()
        except SequencerUnavailableError as exc:
            raise CommandError("Der Sequenzierer braucht PostgreSQL.") from exc

        sequencer = Sequencer(batch_size=max(1, int(options["batch"])))
        if options["once"]:
            self._once(sequencer)
            return

        stop = threading.Event()

        def _anhalten(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _anhalten)
        signal.signal(signal.SIGINT, _anhalten)
        self.stdout.write(f"Sequenzierer gestartet ({sequencer.holder}).")
        sequencer.run(stop, interval=max(0.1, float(options["interval"])))
        self.stdout.write("Sequenzierer beendet.")

    def _once(self, sequencer: Sequencer) -> None:
        if not sequencer.ensure_lease():
            raise CommandError("Ein anderer Prozess hält gerade die Lease 'sequencer'.")
        try:
            anzahl = sequencer.drain()
        finally:
            sequencer.release()
        self.stdout.write(f"{anzahl} Folgenummern vergeben.")
