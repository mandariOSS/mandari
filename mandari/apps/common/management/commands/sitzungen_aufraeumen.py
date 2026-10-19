# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgelaufene Sitzungen löschen (Issue #1091), täglich als Zeitplan im Worker (``apps/common/schedules.py``).

Django räumt ``django_session`` nicht selbst auf; ``clearsessions`` löscht alles in einer Anweisung. Hier geschieht
das in Blöcken, damit der erste Lauf (Altbestand von Hunderttausenden Zeilen) die Tabelle nicht lange sperrt und
der Arbeitsspeicher klein bleibt.
"""

from __future__ import annotations

from typing import Any

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from apps.common.einmalig import EinmaligMixin

#: Zeilen je Löschblock
BLOCK = 5_000


class Command(EinmaligMixin, BaseCommand):
    sperre = "sitzungen_aufraeumen"
    sperre_ttl = 1800
    help = "Abgelaufene Sitzungen in Blöcken löschen (Issue #1091)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts löschen")

    def handle(self, *args: Any, **options: Any) -> None:
        jetzt = timezone.now()
        abgelaufen = Session.objects.filter(expire_date__lt=jetzt)
        if options.get("dry_run"):
            self.stdout.write(f"[DRY-RUN] Abgelaufene Sitzungen: {abgelaufen.count()}")
            return
        geloescht = 0
        while True:
            schluessel = list(abgelaufen.values_list("session_key", flat=True)[:BLOCK])
            if not schluessel:
                break
            anzahl, _ = Session.objects.filter(session_key__in=schluessel).delete()
            geloescht += anzahl
        self.stdout.write(f"Abgelaufene Sitzungen gelöscht: {geloescht}")
