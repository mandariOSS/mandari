# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Altbestand der Protokolle verketten und die Hash-Kette scharf schalten (Issue #221).

Einträge aus der Zeit vor der Hash-Kette haben noch keine laufende Nummer. Solange ein Bereich
solche Einträge hat, schreibt mandari auch neue Einträge unverkettet. Dieser Befehl hängt den
Bestand in Zeitreihenfolge an die Kette – stapelweise, ID-basiert, je Stapel eine kurze
Transaktion – und schaltet die Verkettung danach ein. Er ist wiederholbar: Bereiche mit aktiver
Kette werden übersprungen.

    python manage.py audit_chain_backfill                       # alle Bereiche
    python manage.py audit_chain_backfill --tenant stadt-musterstadt
    python manage.py audit_chain_backfill --chain faction --batch-size 500

Einmal nach dem Update ausführen. Der DSGVO-Löschlauf (session_privacy_purge) ruft die
Verkettung für seinen Mandanten bei Bedarf selbst auf.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.common import audit_chain
from apps.common.einmalig import EinmaligMixin
from apps.common.management.commands.verify_audit_chain import CHAIN_CHOICES, targets_from_options


class Command(EinmaligMixin, BaseCommand):
    sperre = "audit_chain_backfill"
    sperre_ttl = 6 * 3600
    help = "Altbestand der Protokolle verketten und die Hash-Kette scharf schalten."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", help="Slug eines Session-Mandanten")
        parser.add_argument("--organization", help="Slug einer Organisation (Fraktions-Änderungshistorie)")
        parser.add_argument("--chain", choices=CHAIN_CHOICES, default="all", help="Nur diese Kette")
        parser.add_argument("--batch-size", type=int, default=1000, help="Einträge je Transaktion (Standard 1000)")

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = max(10, min(int(options.get("batch_size") or 1000), 20000))
        total = 0
        for spec, scope_id, label in targets_from_options(options):
            chained = audit_chain.backfill(spec, scope_id, batch_size=batch_size)
            total += chained
            if chained:
                self.stdout.write(f"{label}: {chained} Einträge verkettet, Kette aktiv.")
        self.stdout.write(f"Fertig: {total} Einträge verkettet.")
