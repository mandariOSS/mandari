# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaiste Konten löschen (Issue #238).

Konten, die nie bestätigt wurden, deren Registrierungsanfrage abgelehnt wurde oder die
seit Langem ohne jede Zuordnung sind, werden nach Fristablauf entfernt. Die Kriterien
und Fristen stehen in ``apps/accounts/orphaned_accounts.py``; die Ausgabe nennt nur
Zahlen, keine E-Mail-Adressen (das Log soll keine personenbezogenen Daten sammeln).

    python manage.py cleanup_orphaned_accounts            # löschen
    python manage.py cleanup_orphaned_accounts --dry-run  # nur zählen

Gedacht für einen täglichen Cron-Lauf, siehe DEPLOYMENT.md („Geplante Aufgaben“).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.accounts.orphaned_accounts import (
    ABGELEHNT_TAGE,
    ALTBESTAND_TAGE,
    UNBESTAETIGT_TAGE,
    loesche_verwaiste_konten,
)
from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    sperre = "cleanup_orphaned_accounts"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    help = "Verwaiste Konten (unbestätigt, abgelehnt, ohne Zuordnung) nach Fristablauf löschen."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts löschen")

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options.get("dry_run"))
        fund, geloescht = loesche_verwaiste_konten(dry_run=dry_run)
        prefix = "[DRY-RUN] " if dry_run else ""
        self.stdout.write(
            f"{prefix}Verwaiste Konten: {fund.gesamt} "
            f"(unbestätigt > {UNBESTAETIGT_TAGE} Tage: {len(fund.unbestaetigt)}, "
            f"abgelehnt > {ABGELEHNT_TAGE} Tage: {len(fund.abgelehnt)}, "
            f"Altbestand > {ALTBESTAND_TAGE} Tage ohne Zuordnung: {len(fund.altbestand)})"
        )
        if not dry_run:
            self.stdout.write(f"Gelöscht: {geloescht}")
