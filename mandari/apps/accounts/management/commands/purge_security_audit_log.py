# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mandantenübergreifendes Sicherheitsprotokoll fristgerecht löschen – mit Archivpaket (Issue #221).

Einträge älter als ``SECURITY_AUDIT_RETENTION_DAYS`` (Standard 365 Tage) werden vorher geprüft
und als Archivpaket (JSON, CSV, Kettenanker, SHA256SUMS) im Archivspeicher abgelegt; danach
werden sie gelöscht und der Kettenanker gesetzt, damit die Kette prüfbar bleibt.

    python manage.py purge_security_audit_log             # löschen
    python manage.py purge_security_audit_log --dry-run   # nur zählen
    python manage.py purge_security_audit_log --days 180  # abweichende Frist

Gedacht für einen täglichen Cron-Lauf, siehe DEPLOYMENT.md („Geplante Aufgaben“).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.common import audit_archive, audit_chain
from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    sperre = "purge_security_audit_log"
    sperre_ttl = 3600
    help = "Sicherheitsprotokoll nach Fristablauf archivieren und löschen."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--days", type=int, help="Aufbewahrung in Tagen (Standard: SECURITY_AUDIT_RETENTION_DAYS)")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts löschen")

    def handle(self, *args: Any, **options: Any) -> None:
        days = options.get("days")
        if days is None:
            days = int(getattr(settings, "SECURITY_AUDIT_RETENTION_DAYS", 365))
        if days <= 0:
            self.stdout.write("Frist deaktiviert – nichts gelöscht.")
            return
        cutoff = timezone.now() - timedelta(days=days)
        spec = audit_chain.SECURITY
        if options.get("dry_run"):
            found = audit_archive.expired_range(spec, None, cutoff)
            count = (found[1] - found[0] + 1) if found else 0
            self.stdout.write(f"[DRY-RUN] Sicherheitsprotokoll: {count} Einträge älter als {days} Tage.")
            return
        audit_chain.backfill(spec, None)
        try:
            result = audit_archive.archive_and_delete(
                spec,
                None,
                cutoff,
                scope_label="sicherheitsprotokoll",
                meta={"bereich_name": "Sicherheitsprotokoll", "frist_tage": days},
            )
        except audit_chain.ChainError as exc:
            raise CommandError(f"Keine Löschung: {exc}", returncode=1) from exc
        if result is None:
            self.stdout.write("Sicherheitsprotokoll: nichts zu löschen.")
            return
        self.stdout.write(
            f"Sicherheitsprotokoll: {result.deleted} Einträge (Nr. {result.first_seq}–{result.last_seq}) archiviert "
            f"und gelöscht, Archivpaket {result.archive_name} (SHA-256 {result.archive_sha256})."
        )
