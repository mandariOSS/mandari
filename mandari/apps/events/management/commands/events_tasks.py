# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Runner für Aufträge aus ``events_task`` (``apps.events.task_runner``).

Holt Aufträge der gewählten Warteschlangen mit ihrer Parallelität, achtet auf Zeitgrenzen und
startet den Prozess nach ``max_tasks_per_process`` Aufträgen, oberhalb der Speichergrenze oder
nach einer Zeitüberschreitung neu (derselbe Befehl per ``exec``, die Prozessnummer bleibt). Mit
``--no-restart`` endet er stattdessen mit Exit-Code 75, damit eine eigene Überwachung neu startet.

SIGTERM und SIGINT: keine neuen Aufträge, laufende zu Ende führen, dann beenden. Ein zweites
Signal gibt laufende Aufträge sofort frei und beendet.

    manage.py events_tasks                                   # alle Warteschlangen
    manage.py events_tasks --queues default,mail,index,ai,adapter
    manage.py events_tasks --queues ocr --max-memory-mb 900  # eigener Container für Texterkennung
    manage.py events_tasks --burst                           # Fälliges abarbeiten, dann Ende

Im Betrieb übernimmt ``events_worker`` diese Rolle (``--roles tasks``); der Befehl bleibt für
Betrieb und Fehlersuche.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.events.management.commands._optionen import liste, nicht_negativ, parallelitaet
from apps.events.task_runner import POLL_INTERVAL, TaskRunner, ensure_pool_capacity
from apps.events.tasks_backend import journal_options
from apps.events.worker import EXIT_RESTART, replace_process

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Arbeitet Aufträge aus events_task ab (Tasks-Backend JournalBackend)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--queues", help="Warteschlangen, kommagetrennt (Standard: alle aus TASKS).")
        parser.add_argument(
            "--concurrency",
            action="append",
            default=[],
            metavar="WARTESCHLANGE=N",
            help="Parallelität einer Warteschlange übersteuern, mehrfach möglich.",
        )
        parser.add_argument("--max-tasks", type=int, help="Neustart nach so vielen Aufträgen (0 = nie).")
        parser.add_argument("--max-memory-mb", type=int, help="Neustart oberhalb dieses RSS (0 = keine Grenze).")
        parser.add_argument(
            "--interval", type=float, default=POLL_INTERVAL, help="Sekunden zwischen zwei Abfragen im Leerlauf."
        )
        parser.add_argument("--burst", action="store_true", help="Fällige Aufträge abarbeiten, dann beenden.")
        parser.add_argument("--heartbeat-file", help="Datei, deren Änderungszeit jede Runde erneuert wird.")
        parser.add_argument("--backend", default="default", help="Alias in TASKS (Standard: default).")
        parser.add_argument(
            "--no-restart",
            action="store_true",
            help=f"Statt eines Neustarts mit Exit-Code {EXIT_RESTART} beenden.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        config, alle = journal_options(options["backend"])
        queues = liste(options["queues"]) or list(alle)
        fremd = sorted(set(queues) - set(alle))
        if fremd:
            raise CommandError(f"Unbekannte Warteschlangen: {', '.join(fremd)} (bekannt: {', '.join(alle)})")
        parallel = parallelitaet(options["concurrency"], queues)
        nicht_negativ(max_tasks=options["max_tasks"], max_memory_mb=options["max_memory_mb"])

        runner = TaskRunner(
            config,
            queues,
            concurrency=parallel,
            backend_alias=options["backend"],
            poll_interval=max(0.1, float(options["interval"])),
            max_tasks=options["max_tasks"],
            max_memory_mb=options["max_memory_mb"],
            burst=options["burst"],
            heartbeat_file=Path(options["heartbeat_file"]) if options["heartbeat_file"] else None,
        )
        if not runner.slot_count:
            raise CommandError("Keine Warteschlange mit Parallelität größer 0 gewählt.")
        ensure_pool_capacity(runner.slot_count + 2)

        stop = threading.Event()
        force = threading.Event()

        def _anhalten(signum: int, frame: FrameType | None) -> None:
            (force if stop.is_set() else stop).set()
            runner.wake()

        # Signale nur im Hauptthread; die bisherigen Handler kommen danach zurück (Tests, call_command)
        vorher = {}
        if threading.current_thread() is threading.main_thread():
            vorher = {signum: signal.signal(signum, _anhalten) for signum in (signal.SIGTERM, signal.SIGINT)}

        verteilung = ", ".join(f"{q}={runner.concurrency[q]}" for q in queues)
        self.stdout.write(f"Aufträge: Runner gestartet ({runner.worker_id}; {verteilung}).")
        try:
            grund = runner.run(stop, force)
        finally:
            for signum, handler in vorher.items():
                signal.signal(signum, handler)
        self.stdout.write(f"Aufträge: Runner beendet ({grund}, {runner.completed} Aufträge).")
        if grund.restart and not options["burst"]:
            if options["no_restart"] or os.name != "posix":
                raise SystemExit(EXIT_RESTART)
            logger.info("Aufträge: Runner startet neu (%s)", grund)
            replace_process()
