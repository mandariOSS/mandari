# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Ausnahme einer Quelle von der robots.txt setzen oder entfernen.

Nur mit Freigabe der Stelle und mit Vermerk (wer hat wann was freigegeben, Stand der Anfrage). Die Ausnahme
steht in ``OParlSource.sync_config["robots_override"]`` und gilt für Ingestor und Django. Nach dem Setzen
werden Dateien, die wegen der robots.txt übersprungen wurden, neu eingereiht.

    python manage.py robots_override <quelle> --scope files --note "Freigabe per E-Mail vom …, Anfrage läuft"
    python manage.py robots_override <quelle> --entfernen

``<quelle>``: ID, Adresse oder genauer Name der Quelle.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from mandari_oparl.robots import (
    KIND_FILES,
    OVERRIDE_SCOPES,
    ROBOTS_OVERRIDE_KEY,
    robots_override,
    robots_override_problem,
)


class Command(BaseCommand):
    help = "Ausnahme einer Quelle von der robots.txt setzen (mit Pflicht-Vermerk) oder entfernen"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("quelle", help="ID, Adresse oder genauer Name der Quelle")
        parser.add_argument("--scope", choices=OVERRIDE_SCOPES, default=KIND_FILES, help="Geltungsbereich")
        parser.add_argument("--note", default="", help="Vermerk: wer hat wann was freigegeben (Pflicht)")
        parser.add_argument("--entfernen", action="store_true", help="Ausnahme entfernen")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlSource
        from insight_core.services import robots

        source = self._source(OParlSource, options["quelle"])
        with transaction.atomic():
            source = OParlSource.objects.select_for_update().get(pk=source.pk)
            config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
            if options["entfernen"]:
                if config.pop(ROBOTS_OVERRIDE_KEY, None) is None:
                    self.stdout.write(f"{source.name}: keine Ausnahme eingetragen.")
                    return
                source.sync_config = config
                source.save(update_fields=["sync_config"])
                self.stdout.write(self.style.SUCCESS(f"{source.name}: Ausnahme entfernt, die robots.txt gilt."))
                return

            config[ROBOTS_OVERRIDE_KEY] = {"scope": options["scope"], "note": options["note"].strip()}
            problem = robots_override_problem(config)
            if problem:
                raise CommandError(problem)
            source.sync_config = config
            source.save(update_fields=["sync_config"])

        override = robots_override(config)
        assert override is not None
        self.stdout.write(self.style.SUCCESS(f"{source.name}: Ausnahme ({override.scope}) gesetzt – {override.note}"))
        if override.covers(KIND_FILES):
            counts = robots.requeue_blocked_files(source)
            self.stdout.write(
                f"Neu eingereiht: {counts['extraction']} Textextraktion, {counts['file_cache']} Dokument-Cache"
            )

    @staticmethod
    def _source(model: Any, key: str) -> Any:
        candidates = model.objects.filter(url=key) | model.objects.filter(name=key)
        try:
            from uuid import UUID

            candidates = candidates | model.objects.filter(pk=UUID(key))
        except ValueError:
            pass
        found = list(candidates.distinct()[:2])
        if len(found) != 1:
            raise CommandError(f"Quelle „{key}“ nicht eindeutig gefunden (ID, Adresse oder genauer Name)")
        return found[0]
