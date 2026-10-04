# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Pflege der Dokumentablage nach SHA-256 (Issue #788, docs/FILE_CACHE.md).

Stündlich um :50 als Zeitplan ``befehl:dokumentablage`` im Worker (``apps/common/schedules.py``, Issue #516):
``--aufraeumen``, mit Objektspeicher ``--hochladen --aufraeumen``. Die Kennzahlen laufen immer; die übrigen
Schritte von Hand, während der Worker den Zeitplan bedient, mit ``--trotz-zeitplan``:

    python manage.py dokumentablage                    # Kennzahlen
    python manage.py dokumentablage --umstellen        # Kopien im Layout je Kommune verschieben (wiederaufnehmbar)
    python manage.py dokumentablage --aufraeumen       # Inhalte ohne Referenz löschen, Zwischenspeicher begrenzen
    python manage.py dokumentablage --hochladen        # Inhalte in den Objektspeicher (nur mit OBJ_ENABLED)
    python manage.py dokumentablage --referenzen       # Referenzzähler aus den Verweisen neu berechnen
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.common.einmalig import EinmaligMixin

#: Optionen, die die Ablage ändern; ohne sie zeigt der Befehl nur Kennzahlen
SCHRITTE = ("umstellen", "aufraeumen", "hochladen", "referenzen")


class Command(EinmaligMixin, BaseCommand):
    help = "Pflegt die Dokumentablage nach SHA-256: umstellen, aufräumen, hochladen, Referenzen prüfen"

    # Singleton je Cache/Redis (#55); verfällt vor dem nächsten stündlichen Termin, falls ein Lauf an
    # seiner Zeitgrenze abgebrochen wird
    sperre = "dokumentablage"
    sperre_ttl = 3000

    def liest_nur(self, options: dict[str, Any]) -> bool:
        """Nur Kennzahlen (kein Schritt gewählt): läuft auch, während der Worker den Zeitplan bedient."""
        return not any(options.get(name) for name in SCHRITTE)

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--umstellen", action="store_true", help="Kopien im alten Layout in die Ablage verschieben")
        parser.add_argument(
            "--aufraeumen", action="store_true", help="Verwaiste Inhalte löschen, Zwischenspeicher begrenzen"
        )
        parser.add_argument("--hochladen", action="store_true", help="Inhalte in den Objektspeicher hochladen")
        parser.add_argument("--referenzen", action="store_true", help="Referenzzähler neu berechnen")
        parser.add_argument("--limit", type=int, default=1000, help="Höchstens so viele Dateien je Schritt")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.services import file_store, object_storage

        if (options["umstellen"] or options["hochladen"]) and not file_store.uses_blobs():
            raise CommandError("FILE_STORE_LAYOUT=kommune: keine Ablage nach SHA-256")
        if options["hochladen"] and not object_storage.enabled():
            raise CommandError("Objektspeicher ist nicht eingeschaltet (OBJ_ENABLED und Zugangsdaten)")

        if options["umstellen"]:
            self._report("Umgestellt", file_store.migrate_legacy(limit=options["limit"]))
        if options["referenzen"]:
            self._report("Referenzen", file_store.repair_refcounts())
        if options["hochladen"]:
            self._report("Hochgeladen", file_store.upload_pending(limit=options["limit"]))
        if options["aufraeumen"]:
            self._report("Verwaiste Inhalte", file_store.cleanup_orphans())
            if object_storage.enabled():
                self._report("Zwischenspeicher", file_store.evict_local())
            removed = file_store.cleanup_tmp()
            if removed:
                self.stdout.write(f"Liegengebliebene Teil-Downloads entfernt: {removed}")

        stats = file_store.stats()
        self.stdout.write(
            f"Ablage ({stats['layout']}): {stats['blobs']} Inhalte, {stats['blob_bytes'] / 1024**3:.2f} GB, "
            f"für {stats['files']} Dateien ({stats['file_bytes'] / 1024**3:.2f} GB), durch Deduplizierung "
            f"gespart {stats['saved_bytes'] / 1024**3:.2f} GB; verwaist {stats['orphaned']}, "
            f"im Objektspeicher {stats['remote']}"
            + ("" if stats["object_storage"] else " (aus)")
            + f"; noch im alten Layout {stats['legacy_files']}"
        )

    def _report(self, title: str, results: Counter[str]) -> None:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(results.items())) or "nichts zu tun"
        self.stdout.write(f"{title}: {summary}")
