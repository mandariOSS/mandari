# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Idempotenzschlüssel nach der Aufbewahrungsfrist löschen (``apps.events.idempotency``).

Die Frist steht in ``EVENTS_IDEMPOTENCY_RETENTION_DAYS`` (Standard 30 Tage). Bis dahin erhält eine
Wiederholung mit demselben Schlüssel dieselbe Quittung, danach gilt der Schlüssel als neu.
Im Betrieb räumt der tägliche Zeitplan auf (``apps/events/schedules.py``); dieser Befehl ist für
Handbetrieb und Fehlersuche.

    manage.py events_idempotency_purge            # Frist aus den Einstellungen
    manage.py events_idempotency_purge --days 7
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.events.idempotency import purge, retention_days


class Command(BaseCommand):
    help = "Löscht Idempotenzschlüssel nach der Aufbewahrungsfrist."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--days", type=int, default=None, help="Aufbewahrung in Tagen (Standard: Einstellung)")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            days = options["days"] if options["days"] is not None else retention_days()
        except ImproperlyConfigured:
            days = 0
        if days < 1:
            raise CommandError("Die Aufbewahrung muss mindestens einen Tag betragen.")
        deleted = purge(timezone.now() - timedelta(days=days))
        self.stdout.write(f"{deleted} Idempotenzschlüssel gelöscht (älter als {days} Tage).")
