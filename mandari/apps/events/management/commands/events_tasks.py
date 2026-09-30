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

Später übernimmt ``events_worker`` diese Rolle; der Befehl bleibt für Betrieb und Fehlersuche.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connections

from apps.events.task_runner import POLL_INTERVAL, StopReason, TaskRunner, ensure_pool_capacity
from apps.events.tasks_backend import journal_options

logger = logging.getLogger(__name__)

#: Exit-Code „bitte neu starten“ (EX_TEMPFAIL), wenn der Prozess sich nicht selbst ersetzt
EXIT_RESTART = 75


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
        queues = _liste(options["queues"]) or list(alle)
        fremd = sorted(set(queues) - set(alle))
        if fremd:
            raise CommandError(f"Unbekannte Warteschlangen: {', '.join(fremd)} (bekannt: {', '.join(alle)})")
        parallel = _parallelitaet(options["concurrency"], queues)
        for name, wert in (("--max-tasks", options["max_tasks"]), ("--max-memory-mb", options["max_memory_mb"])):
            if wert is not None and wert < 0:
                raise CommandError(f"{name} darf nicht negativ sein.")

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
            _neu_starten(grund)


def _liste(wert: str | None) -> list[str]:
    return [teil.strip() for teil in (wert or "").split(",") if teil.strip()]


def _parallelitaet(angaben: list[str], queues: list[str]) -> dict[str, int]:
    ergebnis: dict[str, int] = {}
    for angabe in angaben:
        name, _, zahl = angabe.partition("=")
        name = name.strip()
        if name not in queues or not zahl.strip().isdigit():
            raise CommandError(f"--concurrency erwartet WARTESCHLANGE=N mit einer gewählten Warteschlange: {angabe}")
        ergebnis[name] = int(zahl)
    return ergebnis


def _neu_starten(grund: StopReason) -> NoReturn:
    """Ersetzt den Prozess durch denselben Befehl: gibt Speicher frei und beendet hängende Threads."""
    logger.info("Aufträge: Runner startet neu (%s)", grund)
    connections.close_all()
    for strom in (sys.stdout, sys.stderr):
        strom.flush()
    for handler in logging.getLogger().handlers:
        handler.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
