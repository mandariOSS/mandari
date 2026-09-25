# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Fassung für bereits veröffentlichte Niederschriften erzeugen (Issue #318).

Seit Issue #318 entsteht beim Veröffentlichen einer Niederschrift eine Datei mit dem öffentlichen
Teil (OParl ``resultsProtocol``, Bürgerportal). Niederschriften, die vorher veröffentlicht wurden,
haben diese Datei noch nicht – der Befehl erzeugt sie einmalig nach dem Deploy. Er ist
idempotent: Ohne ``--force`` bleiben vorhandene Fassungen unberührt.

    python manage.py session_publish_protocols [--tenant SLUG] [--dry-run] [--force]
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.session.models import SessionProtocol
from apps.session.services import protocol_publication

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Öffentliche Fassung (PDF und Text) für veröffentlichte Niederschriften öffentlicher Sitzungen erzeugen."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", help="Nur dieser Mandant (Slug)")
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts erzeugen")
        parser.add_argument("--force", action="store_true", help="Auch vorhandene öffentliche Fassungen neu erzeugen")

    def handle(self, *args: Any, **options: Any) -> None:
        protocols = SessionProtocol.objects.filter(status="published", meeting__is_public=True).select_related(
            "meeting__tenant", "meeting__organization"
        )
        if options.get("tenant"):
            protocols = protocols.filter(meeting__tenant__slug=options["tenant"])
        if not options.get("force"):
            protocols = protocols.filter(public_file__isnull=True)
        created = 0
        failed = 0
        for protocol in protocols.order_by("meeting__start"):
            label = f"{protocol.meeting.tenant.slug}: {protocol.meeting.name} ({protocol.meeting.start:%d.%m.%Y})"
            if options.get("dry_run"):
                self.stdout.write(f"würde erzeugen: {label}")
                continue
            try:
                protocol_publication.publish(protocol, force=bool(options.get("force")))
            except Exception:  # noqa: BLE001 – eine fehlerhafte Niederschrift hält die übrigen nicht auf
                failed += 1
                logger.exception("Öffentliche Fassung der Niederschrift %s konnte nicht erzeugt werden", protocol.pk)
                self.stderr.write(f"Fehler bei {label} – Einzelheiten im Log.")
                continue
            created += 1
            self.stdout.write(f"erzeugt: {label}")
        summary = f"{created} öffentliche Fassung(en) erzeugt"
        if failed:
            summary += f", {failed} fehlgeschlagen"
        self.stdout.write(self.style.SUCCESS(summary + "."))
