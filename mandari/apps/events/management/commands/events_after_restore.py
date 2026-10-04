# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Journal und Aufträge nach einer Wiederherstellung prüfen und die Folgenummer anheben (Issue #573).

    manage.py events_after_restore                 # Stand zeigen, nichts ändern
    manage.py events_after_restore --apply         # Folgenummer anheben (vor dem Start des Workers)
    manage.py events_after_restore --apply --gap 1000000

Aufruf nach dem Einspielen der Datenbank und **bevor** der Worker startet; Ablauf in
``docs/BACKUP.md`` (Abschnitt „Journal und Aufträge“). Ohne Ereignistechnik in der Datenbank (Sicherung
von vor ihrer Einführung) gibt es nichts zu tun. Ein Cursor hinter dem Ende des Journals passt nicht
zur Sicherung (etwa nur einzelne Tabellen eingespielt) und beendet den Befehl mit Exit-Code 1.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.events import wiederherstellung
from apps.events.eingriffe import record_command
from apps.events.registry import load_subscribers


class Command(BaseCommand):
    help = "Journal, Abonnements und Aufträge nach einer Wiederherstellung prüfen; --apply hebt die Folgenummer an."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--apply", action="store_true", help="Folgenummer anheben (Worker vorher stoppen).")
        parser.add_argument(
            "--gap",
            type=int,
            default=wiederherstellung.DEFAULT_GAP,
            help=f"Abstand der Anhebung (Standard {wiederherstellung.DEFAULT_GAP}).",
        )
        parser.add_argument(
            "--force", action="store_true", help="Auch anheben, wenn ein Sequenzierer eine gültige Lease hält."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["gap"] < 1:
            raise CommandError("--gap muss mindestens 1 sein.")
        load_subscribers()
        stand = wiederherstellung.state()
        if not stand.available:
            self.stdout.write("Keine Ereignistechnik in dieser Datenbank (oder kein PostgreSQL): nichts zu tun.")
            return
        self._bericht(stand)
        if options["apply"]:
            self._anheben(options["gap"], options["force"])
        if stand.cursor_ahead:
            raise CommandError(
                "Cursor hinter dem Ende des Journals: "
                + ", ".join(stand.cursor_ahead)
                + ". Journal und Abonnements stammen nicht aus derselben Sicherung; ganze Datenbank einspielen."
            )

    def _bericht(self, stand: wiederherstellung.RestoreState) -> None:
        zuletzt = stand.last_recorded_at.isoformat(timespec="seconds") if stand.last_recorded_at else "–"
        self.stdout.write(
            f"Journal: höchste Folgenummer {stand.head_seq}, Sequenz {stand.sequence_value}, "
            f"ohne Nummer {stand.unsequenced}, letztes Ereignis {zuletzt}"
        )
        for abo in stand.subscriptions:
            art = {True: "extern", False: "Sicht", None: "nicht registriert"}[abo.external]
            self.stdout.write(f"  Abonnement {abo.name}: {abo.state}, Cursor {abo.cursor} ({art})")
        self.stdout.write(f"Geparkt: {stand.parked} (tot: {stand.dead})")
        auftraege = ", ".join(f"{status} {anzahl}" for status, anzahl in sorted(stand.tasks.items())) or "keine"
        self.stdout.write(f"Aufträge: {auftraege} (laufende werden nach Ablauf ihrer Sperre wiederholt)")
        if stand.external:
            self.stdout.write(
                "Externe Ziele sind nicht mitgesichert und neu aufzubauen bzw. nachzuspielen: "
                + ", ".join(stand.external)
                + " (docs/BACKUP.md, Abschnitt „Journal und Aufträge“)."
            )

    def _anheben(self, gap: int, force: bool) -> None:
        try:
            with transaction.atomic():
                ergebnis = wiederherstellung.raise_sequence(gap, force=force)
                if ergebnis is not None:
                    record_command(
                        "events_after_restore", "folgenummer_angehoben", vorher=ergebnis[0], nachher=ergebnis[1]
                    )
        except wiederherstellung.SequencerRunningError:
            raise CommandError(
                "Ein Sequenzierer hält eine gültige Lease. Worker stoppen, höchstens 30 s warten und erneut "
                "aufrufen (oder --force)."
            ) from None
        if ergebnis is None:
            self.stdout.write("Folgenummer bereits angehoben; nichts geändert.")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Folgenummer von {ergebnis[0]} auf {ergebnis[1]} angehoben; Worker kann starten.")
            )
