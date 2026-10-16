# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anmeldeversuche (``LoginAttempt``) nach Frist löschen (Issue #366).

``LoginAttempt`` dient der Drosselung von Anmeldung und zweitem Faktor (Fenster 15 Minuten) und hält
E-Mail-Adresse, IP-Adresse und Browserkennung fest. Länger als die Frist braucht es die Zeilen nicht:
Anmeldungen und Fehlversuche stehen zusätzlich im Sicherheits- bzw. Mandantenprotokoll mit eigener Frist
(docs/PROTOKOLLIERUNG.md). Frist ``LOGIN_ATTEMPT_RETENTION_DAYS`` (Standard 90 Tage wie die Anwendungslogs,
docs/PROTOKOLLE.md; 0 = keine Löschung). Die Ausgabe nennt nur Zahlen.

    python manage.py purge_login_attempts             # löschen
    python manage.py purge_login_attempts --dry-run   # nur zählen
    python manage.py purge_login_attempts --days 30   # abweichende Frist

Läuft täglich als Zeitplan im Worker (``befehl:purge_login_attempts``, apps/common/schedules.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from apps.accounts.models import LoginAttempt
from apps.common.einmalig import EinmaligMixin

#: Zeilen je Löschschritt: kurze Transaktionen auch nach einem großen Angriff mit vielen Fehlversuchen
BATCH_SIZE = 5000


def purge(cutoff: datetime, *, batch_size: int = BATCH_SIZE) -> int:
    """Anmeldeversuche vor ``cutoff`` löschen, schrittweise; liefert die Zahl der gelöschten Zeilen."""
    deleted = 0
    while True:
        ids = list(LoginAttempt.objects.filter(timestamp__lt=cutoff).values_list("pk", flat=True)[:batch_size])
        if not ids:
            return deleted
        count, _ = LoginAttempt.objects.filter(pk__in=ids).delete()
        deleted += count


class Command(EinmaligMixin, BaseCommand):
    sperre = "purge_login_attempts"
    sperre_ttl = 3600
    help = "Anmeldeversuche (LoginAttempt) nach Fristablauf löschen."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--days", type=int, help="Aufbewahrung in Tagen (Standard: LOGIN_ATTEMPT_RETENTION_DAYS)")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts löschen")

    def handle(self, *args: Any, **options: Any) -> None:
        days = options.get("days")
        if days is None:
            days = int(getattr(settings, "LOGIN_ATTEMPT_RETENTION_DAYS", 90))
        if days <= 0:
            self.stdout.write("Frist deaktiviert – nichts gelöscht.")
            return
        cutoff = timezone.now() - timedelta(days=days)
        if options.get("dry_run"):
            count = LoginAttempt.objects.filter(timestamp__lt=cutoff).count()
            self.stdout.write(f"[DRY-RUN] Anmeldeversuche: {count} älter als {days} Tage.")
            return
        deleted = purge(cutoff)
        self.stdout.write(f"Anmeldeversuche: {deleted} älter als {days} Tage gelöscht.")
