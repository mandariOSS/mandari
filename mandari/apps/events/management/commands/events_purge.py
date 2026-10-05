# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Journal und beendete Aufträge nach ihren Fristen löschen (Issue #511, Spezifikation 4.9).

- **Journal** (``apps.events.aufbewahrung``): Zeilen, die vor ``EVENTS_JOURNAL_RETENTION_DAYS`` Tagen
  (Standard 90, mindestens so lange wie Cursor des Änderungsfeeds gelten) erfasst wurden – aber nie über dem
  kleinsten Cursor eines Abonnements, nie das neueste Ereignis, nie geparkte Ereignisse. In Stapeln, je
  Stapel eine kurze Transaktion; festgehalten in ``events_pruning``.
- **Aufträge** (``apps.events.tasks_backend.purge_finished``): erledigte nach
  ``EVENTS_TASKS_DONE_RETENTION_DAYS`` (14), tote und endgültig fehlgeschlagene nach
  ``EVENTS_TASKS_DEAD_RETENTION_DAYS`` (90) Tagen. Das macht täglich auch der Zeitplan ``auftraege_aufraeumen``.

Im Betrieb läuft das Journal als Zeitplan ``befehl:events_purge`` im Worker, sobald
``EVENTS_JOURNAL_PURGE_ENABLED`` eingeschaltet ist (Standard aus).

    manage.py events_purge --dry-run          # Probelauf: Grenze, Grund und Anzahl, nichts löschen
    manage.py events_purge                    # Journal und Aufträge
    manage.py events_purge --nur journal --max-seconds 3000 --pause 0.2
    manage.py events_purge --nur auftraege
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.common.einmalig import EinmaligMixin
from apps.events import aufbewahrung
from apps.events.tasks_backend import count_finished, purge_finished, task_retention

_GRUND = {
    aufbewahrung.FRIST: "Frist",
    aufbewahrung.ENDE: "neuestes Ereignis bleibt",
    aufbewahrung.LEER: "Journal leer",
}


class Command(EinmaligMixin, BaseCommand):
    help = "Löscht Journal und beendete Aufträge nach ihren Fristen (mit --dry-run nur zählen)."
    sperre = "events_purge"
    sperre_ttl = 3700

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen und die Grenze zeigen.")
        parser.add_argument(
            "--nur", choices=("journal", "auftraege"), help="Nur das Journal bzw. nur die Aufträge aufräumen."
        )
        parser.add_argument(
            "--batch", type=int, default=aufbewahrung.PURGE_BATCH, help="Zeilen je Löschschritt (Transaktion)."
        )
        parser.add_argument(
            "--max-seconds", type=float, default=0, help="Nach so vielen Sekunden aufhören (0 = ohne Grenze)."
        )
        parser.add_argument("--pause", type=float, default=0.0, help="Sekunden Pause zwischen zwei Löschschritten.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["batch"] < 1 or options["max_seconds"] < 0 or options["pause"] < 0:
            raise CommandError("--batch muss mindestens 1 sein, --max-seconds und --pause dürfen nicht negativ sein.")
        try:
            if options["nur"] != "auftraege":
                self._journal(options)
            if options["nur"] != "journal":
                self._auftraege(options["dry_run"])
        except ImproperlyConfigured:
            raise CommandError(
                "Fristen ungültig: EVENTS_JOURNAL_RETENTION_DAYS, EVENTS_TASKS_DONE_RETENTION_DAYS und "
                "EVENTS_TASKS_DEAD_RETENTION_DAYS müssen mindestens 1 sein."
            ) from None

    def _journal(self, options: dict[str, Any]) -> None:
        plan = aufbewahrung.plan()
        grund = (
            f"Abonnement {plan.subscription}"
            if plan.limited_by == aufbewahrung.ABONNEMENT
            else _GRUND.get(plan.limited_by, plan.limited_by)
        )
        self.stdout.write(
            f"Journal: Frist {aufbewahrung.retention_days()} Tage, erfasst vor {plan.cutoff:%Y-%m-%d %H:%M} UTC; "
            f"löschbar bis Folgenummer {plan.through_seq} (Grenze: {grund})."
        )
        if options["dry_run"]:
            self.stdout.write(f"Probelauf: {aufbewahrung.count(plan)} Zeilen des Journals würden gelöscht.")
            return
        ergebnis = aufbewahrung.purge(
            plan, batch=options["batch"], max_seconds=options["max_seconds"] or None, pause=options["pause"]
        )
        rest = " Zeitgrenze erreicht, der nächste Lauf setzt fort." if ergebnis.stopped_early else ""
        self.stdout.write(f"{ergebnis.deleted} Zeilen des Journals gelöscht.{rest}")

    def _auftraege(self, dry_run: bool) -> None:
        erledigt, tot = task_retention()
        frist = f"erledigt nach {erledigt.days}, tot und fehlgeschlagen nach {tot.days} Tagen"
        if dry_run:
            self.stdout.write(f"Probelauf: {count_finished()} beendete Aufträge würden gelöscht ({frist}).")
            return
        self.stdout.write(f"{purge_finished()} beendete Aufträge gelöscht ({frist}).")
