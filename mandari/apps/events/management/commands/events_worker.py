# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Worker: Sequenzierer, Zustellung, Aufträge und Zeitpläne in einem Prozess (``apps.events.worker``).

Rollen und Warteschlangen sind wählbar. ``--queues`` gilt für Aufträge und für Abonnements (nach
deren Warteschlange). Mehrere Worker teilen sich die Arbeit: Leader-Rollen über Leases in
``events_lease``, Aufträge per ``SKIP LOCKED``.

    manage.py events_worker                                   # alle Rollen, alle Warteschlangen
    manage.py events_worker --queues default,mail,index,ai,adapter
    manage.py events_worker --roles tasks --queues ocr --max-memory-mb 900   # zweiter Worker
    manage.py events_worker --heartbeat-file /tmp/mandari-worker.heartbeat --metrics-port 9091

SIGTERM und SIGINT: laufende Batches festschreiben (die Zustellung samt Cursor), laufende Aufträge
bis ``--shutdown-timeout`` zu Ende führen, Leases freigeben, beenden. Ein zweites Signal gibt
laufende Aufträge sofort frei. Will der Runner neu starten (``TASKS_MAX_TASKS_PER_PROCESS``,
``TASKS_MAX_MEMORY_MB``, Zeitgrenze), ersetzt sich der Prozess per ``exec``; mit ``--no-restart``
endet er mit Exit-Code 75. Fällt eine Rolle aus, endet er mit Exit-Code 1.

``/metrics`` und ``/health`` liefert der Worker auf ``--metrics-port`` (Standard 9091, 0 = aus).
Die Einzelbefehle ``events_sequencer``, ``events_dispatch``, ``events_tasks`` und
``events_scheduler`` bleiben für Betrieb und Fehlersuche (``--once``, ``--list``, geparkte
Ereignisse).
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
from django.db import connection

from apps.events import leases
from apps.events.dispatch import Dispatcher
from apps.events.management.commands._optionen import liste, nicht_negativ, parallelitaet
from apps.events.registry import Subscriber, load_subscribers
from apps.events.schedule import autodiscover
from apps.events.scheduler import Scheduler
from apps.events.sequencer import Sequencer
from apps.events.task_runner import TaskRunner, ensure_pool_capacity
from apps.events.tasks_backend import journal_options
from apps.events.worker import (
    EXIT_RESTART,
    ROLE_DISPATCH,
    ROLE_SCHEDULER,
    ROLE_SEQUENCER,
    ROLE_TASKS,
    ROLES,
    SHUTDOWN_TIMEOUT,
    STALE_AFTER,
    ExitReason,
    Worker,
    connection_budget,
    replace_process,
)

logger = logging.getLogger(__name__)

#: Standardport für ``/metrics`` und ``/health`` des Workers
METRICS_PORT = 9091


class Command(BaseCommand):
    help = "Worker: Sequenzierer, Zustellung, Aufträge und Zeitpläne in einem Prozess (Rollen wählbar)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--roles", help=f"Rollen, kommagetrennt (Standard: alle: {','.join(ROLES)}).")
        parser.add_argument(
            "--queues", help="Warteschlangen für Aufträge und Abonnements, kommagetrennt (Standard: alle)."
        )
        parser.add_argument(
            "--subscription",
            action="append",
            dest="subscriptions",
            default=[],
            metavar="NAME",
            help="Nur dieses Abonnement zustellen (mehrfach möglich).",
        )
        parser.add_argument(
            "--concurrency",
            action="append",
            default=[],
            metavar="WARTESCHLANGE=N",
            help="Parallelität einer Warteschlange übersteuern, mehrfach möglich.",
        )
        parser.add_argument("--max-tasks", type=int, help="Neustart nach so vielen Aufträgen (0 = nie).")
        parser.add_argument("--max-memory-mb", type=int, help="Neustart oberhalb dieses RSS (0 = keine Grenze).")
        parser.add_argument("--backend", default="default", help="Alias in TASKS (Standard: default).")
        parser.add_argument("--no-listen", action="store_true", help="Ohne Weckruf per LISTEN, nur Abfrage.")
        parser.add_argument(
            "--heartbeat-file", help="Datei, deren Änderungszeit alle 5 s erneuert wird, solange jede Rolle arbeitet."
        )
        parser.add_argument(
            "--metrics-port",
            type=int,
            default=METRICS_PORT,
            help=f"Port für /metrics und /health (Standard {METRICS_PORT}, 0 = aus).",
        )
        parser.add_argument(
            "--metrics-addr", default="0.0.0.0", help="Adresse für /metrics und /health (Standard 0.0.0.0)."
        )
        parser.add_argument(
            "--stale-after",
            type=float,
            default=STALE_AFTER,
            help=f"Sekunden ohne Lebenszeichen, nach denen eine Rolle als hängend gilt (Standard {STALE_AFTER:.0f}).",
        )
        parser.add_argument(
            "--shutdown-timeout",
            type=float,
            default=SHUTDOWN_TIMEOUT,
            help=f"Beim Beenden so lange auf laufende Aufträge warten (Standard {SHUTDOWN_TIMEOUT:.0f} s).",
        )
        parser.add_argument(
            "--no-restart",
            action="store_true",
            help=f"Statt eines Neustarts per exec mit Exit-Code {EXIT_RESTART} beenden.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        nicht_negativ(
            max_tasks=options["max_tasks"], max_memory_mb=options["max_memory_mb"], metrics_port=options["metrics_port"]
        )
        explizit = options["roles"] is not None
        rollen = self._rollen(options["roles"], explizit)
        config, task_queues = journal_options(options["backend"])
        abonnements = load_subscribers() if ROLE_DISPATCH in rollen else []
        gewaehlt = liste(options["queues"])
        bekannt = set(task_queues) | {spec.queue for spec in abonnements}
        fremd = sorted(set(gewaehlt) - bekannt)
        if fremd:
            raise CommandError(f"Unbekannte Warteschlangen: {', '.join(fremd)} (bekannt: {', '.join(sorted(bekannt))})")
        auswahl = self._abonnements(abonnements, options["subscriptions"], gewaehlt, rollen)
        if explizit and ROLE_DISPATCH in rollen and not auswahl:
            # Wie bei tasks ohne Parallelität: Die Rolle hätte keinen Faden, der Worker liefe ohne Arbeit
            raise CommandError("Rolle dispatch: kein registriertes Abonnement passt zu --queues bzw. --subscription.")

        runner: TaskRunner | None = None
        if ROLE_TASKS in rollen:
            queues = [queue for queue in task_queues if not gewaehlt or queue in gewaehlt]
            runner = TaskRunner(
                config,
                queues,
                concurrency=parallelitaet(options["concurrency"], queues),
                backend_alias=options["backend"],
                max_tasks=options["max_tasks"],
                max_memory_mb=options["max_memory_mb"],
            )
            if not runner.slot_count:
                if explizit:
                    raise CommandError("Rolle tasks: keine Warteschlange mit Parallelität größer 0 gewählt.")
                self.stdout.write("Rolle tasks entfällt: keine Warteschlange mit Parallelität größer 0 gewählt.")
                rollen.remove(ROLE_TASKS)
                runner = None
        elif options["concurrency"]:
            raise CommandError("--concurrency gilt nur für die Rolle tasks.")

        holder = leases.new_holder_id()
        sequencer = Sequencer(holder=holder) if ROLE_SEQUENCER in rollen else None
        dispatcher = Dispatcher(auswahl) if ROLE_DISPATCH in rollen else None
        scheduler: Scheduler | None = None
        if ROLE_SCHEDULER in rollen:
            autodiscover()
            scheduler = Scheduler(holder=holder, backend_alias=options["backend"])

        weckbar = sequencer is not None or bool(auswahl)
        listen = weckbar and not options["no_listen"] and connection.vendor == "postgresql"
        port = options["metrics_port"] or None
        budget = connection_budget(
            rollen,
            subscriptions=len(auswahl),
            task_slots=runner.slot_count if runner is not None else 0,
            listener=listen,
            metrics=port is not None,
        )
        ensure_pool_capacity(budget)

        worker = Worker(
            sequencer=sequencer,
            dispatcher=dispatcher,
            runner=runner,
            scheduler=scheduler,
            roles=rollen,
            queues=gewaehlt,
            holder=holder,
            listen=listen,
            heartbeat_file=Path(options["heartbeat_file"]) if options["heartbeat_file"] else None,
            metrics_addr=options["metrics_addr"],
            metrics_port=port,
            stale_after=max(1.0, float(options["stale_after"])),
            shutdown_timeout=max(0.0, float(options["shutdown_timeout"])),
        )
        grund = self._laufen(worker, rollen, gewaehlt, auswahl, runner, budget)
        if grund == ExitReason.RESTART:
            if options["no_restart"] or os.name != "posix":
                raise SystemExit(EXIT_RESTART)
            replace_process()
        if grund == ExitReason.FAILURE:
            raise CommandError("Eine Rolle ist ausgefallen (Einzelheiten im Protokoll); der Worker beendet sich.")

    def _rollen(self, angabe: str | None, explizit: bool) -> list[str]:
        rollen: list[str] = [rolle for rolle in ROLES if rolle in liste(angabe)] if explizit else list(ROLES)
        unbekannt = sorted(set(liste(angabe)) - set(ROLES))
        if unbekannt:
            raise CommandError(f"Unbekannte Rollen: {', '.join(unbekannt)} (bekannt: {', '.join(ROLES)})")
        if not rollen:
            raise CommandError(f"Mindestens eine Rolle angeben ({', '.join(ROLES)}).")
        if ROLE_SEQUENCER in rollen and connection.vendor != "postgresql":
            if explizit:
                raise CommandError("Der Sequenzierer braucht PostgreSQL.")
            self.stdout.write("Ohne PostgreSQL entfällt die Rolle sequencer.")
            rollen.remove(ROLE_SEQUENCER)
        return rollen

    def _abonnements(
        self, alle: list[Subscriber], namen: list[str], queues: list[str], rollen: list[str]
    ) -> list[Subscriber]:
        if namen and ROLE_DISPATCH not in rollen:
            raise CommandError("--subscription gilt nur für die Rolle dispatch.")
        unbekannt = sorted(set(namen) - {spec.name for spec in alle})
        if unbekannt:
            raise CommandError(f"Nicht registriert: {', '.join(unbekannt)}")
        return [spec for spec in alle if (not namen or spec.name in namen) and (not queues or spec.queue in queues)]

    def _laufen(
        self,
        worker: Worker,
        rollen: list[str],
        queues: list[str],
        abonnements: list[Subscriber],
        runner: TaskRunner | None,
        budget: int,
    ) -> ExitReason:
        stop = threading.Event()
        force = threading.Event()

        def _anhalten(signum: int, frame: FrameType | None) -> None:
            (force if stop.is_set() else stop).set()
            worker.wake()

        # Signale nur im Hauptthread; die bisherigen Handler kommen danach zurück (Tests, call_command)
        vorher = {}
        if threading.current_thread() is threading.main_thread():
            vorher = {signum: signal.signal(signum, _anhalten) for signum in (signal.SIGTERM, signal.SIGINT)}

        teile = [f"Rollen {', '.join(rollen)}", f"Warteschlangen {', '.join(queues) or 'alle'}"]
        if ROLE_DISPATCH in rollen:
            teile.append(f"Abonnements {', '.join(spec.name for spec in abonnements) or 'keine'}")
        if runner is not None:
            teile.append("Aufträge " + ", ".join(f"{q}={runner.concurrency[q]}" for q in runner.queues))
        teile.append(f"bis zu {budget} Datenbankverbindungen")
        self.stdout.write(f"Worker gestartet ({worker.holder}; {'; '.join(teile)}).")
        try:
            grund = worker.run(stop, force)
        finally:
            for signum, handler in vorher.items():
                signal.signal(signum, handler)
        self.stdout.write(f"Worker beendet ({grund}).")
        return grund
