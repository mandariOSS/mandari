# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne als eigener Prozess: legt für fällige Termine Aufträge an (``apps.events.scheduler``).

Genau ein Prozess plant (Lease ``scheduler``); weitere warten und übernehmen spätestens 30 s nach
dem Ausfall des Inhabers. Ausgeführt werden die Aufträge vom Runner (``events_tasks``). SIGTERM
und SIGINT beenden den Dauerbetrieb nach dem laufenden Durchlauf und geben die Lease frei. Im Betrieb
übernimmt ``events_worker`` diese Rolle (``--roles scheduler``); der Befehl bleibt für Betrieb und
Fehlersuche.

    manage.py events_scheduler            # Dauerbetrieb
    manage.py events_scheduler --once     # einmal fällige Termine anlegen, dann Ende
    manage.py events_scheduler --list     # registrierte Zeitpläne mit zuletzt geplantem Termin
"""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.events.models import ScheduleState
from apps.events.schedule import autodiscover, registry
from apps.events.scheduler import LEASE_NAME, POLL_INTERVAL, Scheduler


class Command(BaseCommand):
    help = "Legt für fällige Zeitpläne Aufträge an (Leader-Rolle 'scheduler')."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Einmal fällige Termine anlegen, dann beenden.")
        parser.add_argument("--list", action="store_true", help="Registrierte Zeitpläne anzeigen.")
        parser.add_argument("--interval", type=float, default=POLL_INTERVAL, help="Sekunden zwischen zwei Durchläufen.")

    def handle(self, *args: Any, **options: Any) -> None:
        autodiscover()
        if options["list"]:
            self._liste()
            return

        planer = Scheduler()
        if options["once"]:
            if not planer.ensure_lease():
                raise CommandError(f"Ein anderer Prozess hält gerade die Lease '{LEASE_NAME}'.")
            try:
                angelegt = planer.tick()
            finally:
                planer.release()
            self.stdout.write(f"{len(angelegt)} Aufträge aus Zeitplänen angelegt.")
            return

        stop = threading.Event()

        def _anhalten(signum: int, frame: FrameType | None) -> None:
            stop.set()

        vorher = {}
        if threading.current_thread() is threading.main_thread():
            vorher = {signum: signal.signal(signum, _anhalten) for signum in (signal.SIGTERM, signal.SIGINT)}
        self.stdout.write(f"Zeitpläne: gestartet ({planer.holder}, {len(registry)} Zeitpläne).")
        try:
            planer.run(stop, interval=max(0.5, float(options["interval"])))
        finally:
            for signum, handler in vorher.items():
                signal.signal(signum, handler)
        self.stdout.write("Zeitpläne: beendet.")

    def _liste(self) -> None:
        staende = dict(ScheduleState.objects.values_list("name", "last_slot"))
        if not len(registry):
            self.stdout.write("Keine Zeitpläne registriert.")
        for eintrag in registry:
            zuletzt = staende.get(eintrag.name)
            self.stdout.write(
                f"{eintrag.name}: {eintrag.trigger.describe()}, {eintrag.catchup}, "
                f"Auftrag {eintrag.task.module_path} (Warteschlange {eintrag.task.queue_name}), "
                f"zuletzt geplant {zuletzt.isoformat() if zuletzt else '–'}"
            )
