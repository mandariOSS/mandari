# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustellung als eigener Prozess: stellt Ereignisse an die registrierten Abonnements zu.

Abonnements registrieren die Apps per ``@subscriber`` in ihrem Modul ``subscribers``
(``apps.events.registry``). Je Abonnement arbeitet genau ein Prozess (Lease ``dispatch:<name>``);
weitere warten und übernehmen spätestens 30 s nach dem Ausfall des Inhabers. SIGTERM und SIGINT
beenden den Dauerbetrieb nach dem laufenden Batch, dessen Cursor noch festgeschrieben wird. Neue
Folgenummern wecken die Zustellung per ``LISTEN`` (``apps.events.wakeup``,
``EVENTS_DB_DIRECT_URL``); dazu fragt sie alle 2 s ab. Im Betrieb übernimmt ``events_worker`` diese
Rolle (``--roles dispatch``); der Befehl bleibt für Betrieb und Fehlersuche.

    manage.py events_dispatch                          # Dauerbetrieb, alle Abonnements
    manage.py events_dispatch --once                   # einmal alles Fällige zustellen, dann Ende
    manage.py events_dispatch --no-listen              # ohne Weckruf, nur Abfrage
    manage.py events_dispatch --subscription suchindex --queues index
    manage.py events_dispatch --list                   # Zustand, Cursor und geparkte Ereignisse
    manage.py events_dispatch --retry-parked 17        # geparktes Ereignis sofort erneut zustellen
    manage.py events_dispatch --discard-parked 17      # geparktes Ereignis verwerfen
    manage.py events_dispatch --replay suchindex --from-seq 1200        # ab Folgenummer erneut zustellen
    manage.py events_dispatch --replay suchindex --since 2026-10-01T00:00  # ab Erfassungszeitpunkt

Nachspielen (``--replay``) setzt nur den Cursor zurück; zugestellt wird im laufenden Worker bzw. mit
``--once``. Der Handler muss wiederholte Ereignisse vertragen (Idempotenz).

Nachspielen, erneut Zustellen und Verwerfen sind Eingriffe: Sie stehen wie im Admin im
Sicherheitsprotokoll (``apps.events.eingriffe``, Quelle ``kommandozeile``), in derselben Transaktion.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from datetime import datetime
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.events.dispatch import (
    POLL_INTERVAL,
    Dispatcher,
    discard_parked,
    first_seq_since,
    head_seq,
    retry_parked,
    rewind,
)
from apps.events.eingriffe import parked_identifiers, record_command
from apps.events.models import ParkedEvent, ParkedState, Subscription
from apps.events.registry import Subscriber, load_subscribers
from apps.events.sequencer import SEQUENCED_CHANNEL
from apps.events.wakeup import start_listener


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
        parser.add_argument("--no-listen", action="store_true", help="Ohne Weckruf per LISTEN, nur Abfrage.")
        parser.add_argument("--list", action="store_true", help="Abonnements mit Zustand und Rückstand anzeigen.")
        parser.add_argument(
            "--retry-parked", type=int, metavar="ID", help="Geparktes Ereignis sofort erneut zustellen."
        )
        parser.add_argument("--discard-parked", type=int, metavar="ID", help="Geparktes Ereignis verwerfen.")
        parser.add_argument(
            "--replay",
            metavar="NAME",
            help="Nachspielen: Cursor des Abonnements zurücksetzen (mit --from-seq/--since).",
        )
        parser.add_argument("--from-seq", type=int, metavar="N", help="Nachspielen ab dieser Folgenummer.")
        parser.add_argument(
            "--since", metavar="DATUM", help="Nachspielen ab diesem Erfassungszeitpunkt (ISO 8601, Ortszeit ohne Zone)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._liste()
            return
        if options["retry_parked"] is not None:
            if not self._geparkt(options["retry_parked"], retry_parked, "geparkt_wiederholen"):
                raise CommandError(
                    "Nicht möglich: unbekannt oder nicht das erste geparkte Ereignis seines Objekts (Reihenfolge)."
                )
            self.stdout.write("Wird beim nächsten Lauf erneut zugestellt.")
            return
        if options["replay"] is not None or options["from_seq"] is not None or options["since"] is not None:
            self._nachspielen(options["replay"], options["from_seq"], options["since"])
            return
        if options["discard_parked"] is not None:
            if not self._geparkt(options["discard_parked"], discard_parked, "geparkt_verworfen"):
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
        if not options["no_listen"]:
            start_listener({SEQUENCED_CHANNEL: [dispatcher.wake]}, stop)
        namen = ", ".join(spec.name for spec in auswahl)
        self.stdout.write(f"Zustellung gestartet für {namen}.")
        dispatcher.run(stop, interval=max(0.05, float(options["interval"])))
        self.stdout.write("Zustellung beendet.")

    def _nachspielen(self, name: str | None, ab_seq: int | None, seit: str | None) -> None:
        if not name or (ab_seq is None) == (seit is None):
            raise CommandError("Nachspielen: --replay NAME und genau eines von --from-seq oder --since angeben.")
        if ab_seq is None:
            zeitpunkt = self._zeitpunkt(seit or "")
            ab_seq = first_seq_since(zeitpunkt)
            if ab_seq is None:
                self.stdout.write("Seit diesem Zeitpunkt gibt es keine nummerierten Ereignisse; nichts zu tun.")
                return
        try:
            with transaction.atomic():
                ergebnis = rewind(name, ab_seq)
                if ergebnis is not None and ergebnis[0] != ergebnis[1]:
                    record_command(
                        "events_dispatch",
                        "abonnement_nachspielen",
                        abonnement=name,
                        vorher=ergebnis[0],
                        nachher=ergebnis[1],
                    )
        except ValueError as exc:
            raise CommandError(f"Nachspielen nicht möglich: {exc}") from None
        if ergebnis is None:
            raise CommandError(f"Abonnement {name} gibt es nicht (noch nie zugestellt).")
        vorher, neu = ergebnis
        self.stdout.write(
            f"Abonnement {name}: Cursor {vorher} -> {neu}; Ereignisse ab Folgenummer {neu + 1} werden erneut zugestellt."
        )

    @staticmethod
    def _geparkt(parked_id: int, eingriff: Callable[[int], bool], aktion: str) -> bool:
        """Erneut zustellen bzw. verwerfen samt Eintrag im Sicherheitsprotokoll (eine Transaktion)."""
        with transaction.atomic():
            geparkt = ParkedEvent.objects.filter(pk=parked_id).first()
            if geparkt is None or not eingriff(parked_id):
                return False
            record_command("events_dispatch", aktion, **parked_identifiers(geparkt))
        return True

    @staticmethod
    def _zeitpunkt(angabe: str) -> datetime:
        try:
            zeitpunkt = parse_datetime(angabe.strip())
        except ValueError:  # Format stimmt, Datum nicht (z. B. Monat 13)
            zeitpunkt = None
        if zeitpunkt is None:
            raise CommandError("--since: Zeitpunkt im Format 2026-10-01T00:00 (ISO 8601) angeben.")
        if timezone.is_naive(zeitpunkt):
            zeitpunkt = timezone.make_aware(zeitpunkt)
        return zeitpunkt

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
