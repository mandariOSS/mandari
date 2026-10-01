# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Session-Quellen nach einem Domainwechsel an die aktuelle Adresse umziehen (Issue #733).

Ändert sich ``SITE_URL``, gibt die Session-OParl-Schnittstelle ihre Objekte unter der neuen Domain aus. Die
kanonischen Kennungen bleiben auf der festgeschriebenen Basis der Installation; der RIS-Bestand führt die
Objekte aber noch unter den alten Adressen. Der Befehl zieht jede Session-Quelle an ihre Adresse auf
``SITE_URL`` um (``insight_core.services.ris_addresses``): URL der Quelle, Adressen, Verweise, Links und
Rohdaten ihrer Objekte. Kennungen ändern sich nicht.

Ablauf (docs/SESSION_OPARL_API.md, Abschnitt „Domainwechsel“): Ingestor anhalten, ``SITE_URL`` ändern und
neu starten, Vorschau prüfen, umziehen, Ingestor starten, danach ``check_ris_ids --dry-run``.

Verwendung:
    python manage.py move_session_sources              # Vorschau (Standard), ändert nichts
    python manage.py move_session_sources --yes        # umziehen
    python manage.py move_session_sources --tenant musterstadt --yes
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_core.models import OParlSource
from insight_core.services.ris_addresses import Move, MoveError, move_source
from insight_core.services.ris_ids import session_oparl_base


class Command(BaseCommand):
    help = "Zieht Session-Quellen nach einem Domainwechsel an die Adresse auf SITE_URL um; Kennungen bleiben."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", help="Nur die Quelle dieses Session-Mandanten (Slug)")
        parser.add_argument("--yes", action="store_true", help="Wirklich umziehen (sonst nur Vorschau)")

    def handle(self, *args: Any, **options: Any) -> None:
        apply = bool(options["yes"])
        only = options.get("tenant")
        sources = [
            (source, str(source.sync_config.get("session_tenant")))
            for source in OParlSource.objects.order_by("name")
            if isinstance(source.sync_config, dict) and source.sync_config.get("session_tenant")
        ]
        if only:
            sources = [(source, tenant) for source, tenant in sources if tenant == only]
        if not sources:
            raise CommandError("Keine Session-Quelle gefunden.")

        self.stdout.write("Umzug der Session-Quellen" if apply else "Vorschau (nichts geändert, --yes zum Umziehen)")
        failed = False
        replaced: set[Any] = set()
        for source, tenant in sources:
            if source.pk in replaced:
                continue
            target = session_oparl_base(tenant)
            if target is None:
                self.stdout.write(self.style.WARNING(f"{tenant}: Adresse nicht bestimmbar – übersprungen."))
                continue
            if source.url.rstrip("/") == target.rstrip("/"):
                self.stdout.write(f"{tenant}: bereits unter {source.url}")
                continue
            try:
                move = move_source(source, target, apply=apply)
            except MoveError as exc:
                failed = True
                self.stdout.write(self.style.ERROR(f"{tenant}: {exc}"))
                continue
            if apply and move.replaced_source_id is not None:
                replaced.add(move.replaced_source_id)
            self._write_move(tenant, move, apply)
        if failed:
            raise CommandError("Mindestens eine Quelle ließ sich nicht umziehen (siehe oben).")

    def _write_move(self, tenant: str, move: Move, apply: bool) -> None:
        verb = "umgezogen" if apply else "würde umziehen"
        self.stdout.write(self.style.SUCCESS(f"{tenant}: {move.old} -> {move.new} ({verb}: {move.total} Objekte)"))
        for entity, count in move.objects.items():
            if count:
                self.stdout.write(f"  {entity:<16} {count:>8}")
        self.stdout.write(f"  Basis der Kennungen bleibt: {move.id_base}")
        if move.replaced_source_id is not None:
            self.stdout.write(f"  Leere Quelle unter der neuen Adresse geht auf: {move.replaced_source_id}")
