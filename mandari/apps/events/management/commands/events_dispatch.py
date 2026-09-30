# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustellung als eigener Prozess: stellt Ereignisse an die registrierten Abonnements zu.

Abonnements registrieren die Apps per ``@subscriber`` in ihrem Modul ``subscribers``
(``apps.events.registry``). Je Abonnement arbeitet genau ein Prozess (Lease ``dispatch:<name>``);
weitere warten und übernehmen spätestens 30 s nach dem Ausfall des Inhabers. SIGTERM und SIGINT
beenden den Dauerbetrieb nach dem laufenden Batch, dessen Cursor noch festgeschrieben wird. Später
übernimmt ``events_worker`` diese Rolle; der Befehl bleibt für Betrieb und Fehlersuche.

    manage.py events_dispatch                          # Dauerbetrieb, alle Abonnements
    manage.py events_dispatch --once                   # einmal alles Fällige zustellen, dann Ende
    manage.py events_dispatch --subscription suchindex --queues index
    manage.py events_dispatch --list                   # Zustand, Cursor und geparkte Ereignisse
    manage.py events_dispatch --retry-parked 17        # geparktes Ereignis sofort erneut zustellen
    manage.py events_dispatch --discard-parked 17      # geparktes Ereignis verwerfen
"""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Count

from apps.events.dispatch import POLL_INTERVAL, Dispatcher, discard_parked, head_seq, retry_parked
from apps.events.models import ParkedEvent, ParkedState, Subscription
from apps.events.registry import Subscriber, load_subscribers


class Command(BaseCommand):
    help = "Stellt Ereignisse an registrierte Abonnements zu (Leader-Lease je Abonnement)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Einmal alles Fällige zustellen, dann beenden.")
        parser.add_argument(
            "--subscription",
            action="append",
            dest="subscriptions",
            default=[],
            metavar="NAME",
            help="Nur dieses Abonnement (mehrfach möglich).",
        )
        parser.add_argument("--queues", default="", help="Nur Abonnements dieser Warteschlangen (kommagetrennt).")
        parser.add_argument(
            "--interval", type=float, default=POLL_INTERVAL, help="Sekunden zwischen zwei Abfragen (Dauerbetrieb)."
        )
        parser.add_argument("--list", action="store_true", help="Abonnements mit Zustand und Rückstand anzeigen.")
        parser.add_argument(
            "--retry-parked", type=int, metavar="ID", help="Geparktes Ereignis sofort erneut zustellen."
        )
        parser.add_argument("--discard-parked", type=int, metavar="ID", help="Geparktes Ereignis verwerfen.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._liste()
            return
        if options["retry_parked"] is not None:
            if not retry_parked(options["retry_parked"]):
                raise CommandError(
                    "Nicht möglich: unbekannt oder nicht das erste geparkte Ereignis seines Objekts (Reihenfolge)."
                )
            self.stdout.write("Wird beim nächsten Lauf erneut zugestellt.")
            return
        if options["discard_parked"] is not None:
            if not discard_parked(options["discard_parked"]):
                raise CommandError("Unbekanntes geparktes Ereignis.")
            self.stdout.write("Verworfen; das nächste Ereignis desselben Objekts rückt nach.")
            return

        auswahl = self._auswahl(options["subscriptions"], options["queues"])
        if not auswahl:
            self.stdout.write("Keine Abonnements registriert bzw. ausgewählt.")
            return
        dispatcher = Dispatcher(auswahl)
        if options["once"]:
            self._once(dispatcher)
            return

        stop = threading.Event()

        def _anhalten(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _anhalten)
        signal.signal(signal.SIGINT, _anhalten)
        namen = ", ".join(spec.name for spec in auswahl)
        self.stdout.write(f"Zustellung gestartet für {namen}.")
        dispatcher.run(stop, interval=max(0.05, float(options["interval"])))
        self.stdout.write("Zustellung beendet.")

    def _auswahl(self, namen: list[str], warteschlangen: str) -> list[Subscriber]:
        alle = load_subscribers()
        bekannt = {spec.name for spec in alle}
        unbekannt = sorted(set(namen) - bekannt)
        if unbekannt:
            raise CommandError(f"Nicht registriert: {', '.join(unbekannt)}")
        queues = {queue.strip() for queue in warteschlangen.split(",") if queue.strip()}
        return [spec for spec in alle if (not namen or spec.name in namen) and (not queues or spec.queue in queues)]

    def _once(self, dispatcher: Dispatcher) -> None:
        try:
            anzahl = dispatcher.drain()
            uebersprungen = [loop.spec.name for loop in dispatcher.loops if not loop.is_leader]
        finally:
            dispatcher.release()
        if uebersprungen:
            self.stdout.write(f"Übersprungen (Lease hält ein anderer Prozess): {', '.join(uebersprungen)}")
        self.stdout.write(f"{anzahl} Ereignisse zugestellt.")

    def _liste(self) -> None:
        registriert = {spec.name: spec for spec in load_subscribers()}
        zeilen = {
            name: (cursor, zustand)
            for name, cursor, zustand in Subscription.objects.values_list("name", "cursor_seq", "state")
        }
        geparkt: dict[tuple[str, str], int] = {
            (abonnement, zustand): anzahl
            for abonnement, zustand, anzahl in ParkedEvent.objects.values_list("subscription", "state")
            .annotate(anzahl=Count("id"))
            .order_by()
        }
        self.stdout.write(f"Höchste Folgenummer: {head_seq()}")
        self.stdout.write(
            f"{'Abonnement':32} {'Zustand':10} {'Warteschlange':13} {'Cursor':>10}  wiederholen/blockiert/tot"
        )
        for name in sorted(set(registriert) | set(zeilen)):
            cursor, zustand = zeilen.get(name, (None, "neu"))
            spec = registriert.get(name)
            queue = spec.queue if spec else "(nicht registriert)"
            parken = "/".join(str(geparkt.get((name, z), 0)) for z in ParkedState.values)
            self.stdout.write(
                f"{name:32} {zustand:10} {queue:13} {cursor if cursor is not None else '-':>10}  {parken}"
            )
        tote = ParkedEvent.objects.filter(state=ParkedState.TOT).order_by("subscription", "event_seq")[:50]
        for eintrag in tote:
            self.stdout.write(
                f"  tot: ID {eintrag.pk}, {eintrag.subscription}, Folgenummer {eintrag.event_seq}, "
                f"Objekt {eintrag.aggregate_id}, {eintrag.error_code or 'unbekannt'}"
            )
