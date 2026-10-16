# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Pflege der Dokumentablage nach SHA-256 (Issue #788, docs/FILE_CACHE.md).

Stündlich um :50 als Zeitplan ``befehl:dokumentablage`` im Worker (``apps/common/schedules.py``, Issue #516):
``--aufraeumen``, mit Objektspeicher ``--hochladen --aufraeumen``. Die Kennzahlen laufen immer; die übrigen
Schritte von Hand, während der Worker den Zeitplan bedient, mit ``--trotz-zeitplan``:

    python manage.py dokumentablage                    # Kennzahlen
    python manage.py dokumentablage --umstellen        # Kopien im Layout je Kommune verschieben (wiederaufnehmbar)
    python manage.py dokumentablage --aufraeumen       # Inhalte ohne Referenz löschen, Zwischenspeicher und
                                                       # Obergrenze (FILE_CACHE_MAX_TOTAL_GB, #961) einhalten
    python manage.py dokumentablage --hochladen        # Inhalte in den Objektspeicher (nur mit OBJ_ENABLED)
    python manage.py dokumentablage --referenzen       # Referenzzähler aus den Verweisen neu berechnen
    python manage.py dokumentablage --objektspeicher-aufgeben [--ausfuehren]
                                                       # Objektspeicher bewusst aufgeben (#990, nur ohne OBJ_*)
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
        """
        Nur Kennzahlen (kein Schritt gewählt) oder Probelauf von ``--objektspeicher-aufgeben``: läuft auch, während der
        Worker den Zeitplan bedient.
        """
        if options.get("objektspeicher_aufgeben") and options.get("ausfuehren"):
            return False
        return not any(options.get(name) for name in SCHRITTE)

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--umstellen", action="store_true", help="Kopien im alten Layout in die Ablage verschieben")
        parser.add_argument(
            "--aufraeumen", action="store_true", help="Verwaiste Inhalte löschen, Zwischenspeicher begrenzen"
        )
        parser.add_argument("--hochladen", action="store_true", help="Inhalte in den Objektspeicher hochladen")
        parser.add_argument("--referenzen", action="store_true", help="Referenzzähler neu berechnen")
        parser.add_argument("--limit", type=int, default=1000, help="Höchstens so viele Dateien je Schritt")
        parser.add_argument(
            "--objektspeicher-aufgeben",
            action="store_true",
            help="Objektspeicher bewusst aufgeben: Vermerke remote_at löschen (nur ohne OBJ_*; ohne --ausfuehren Probelauf)",
        )
        parser.add_argument(
            "--ausfuehren", action="store_true", help="Mit --objektspeicher-aufgeben: Vermerke wirklich löschen"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.services import file_cache_limit, file_store, object_storage

        if (options["umstellen"] or options["hochladen"]) and not file_store.uses_blobs():
            raise CommandError("FILE_STORE_LAYOUT=kommune: keine Ablage nach SHA-256")
        if options["hochladen"] and not object_storage.enabled():
            raise CommandError("Objektspeicher ist nicht eingeschaltet (OBJ_ENABLED und Zugangsdaten)")
        if options["objektspeicher_aufgeben"]:
            if any(options.get(name) for name in SCHRITTE):
                raise CommandError("--objektspeicher-aufgeben nur allein")
            self._objektspeicher_aufgeben(ausfuehren=options["ausfuehren"])
            return
        if options["ausfuehren"]:
            raise CommandError("--ausfuehren nur mit --objektspeicher-aufgeben")

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
            self._report_limit(file_cache_limit.enforce())
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

    def _objektspeicher_aufgeben(self, *, ausfuehren: bool) -> None:
        """
        Ausdrücklicher Schritt zum Ausschalten (Issue #990): Ohne ihn gelten Inhalte, die laut Datenbank im
        Objektspeicher liegen, ohne konfigurierten Objektspeicher als gestört, nie als fehlend.
        """
        from insight_core.services import file_store, object_storage

        if object_storage.enabled():
            raise CommandError(
                "Objektspeicher ist hier konfiguriert (OBJ_ENABLED, OBJ_*): erst in Anwendung und Worker ausschalten "
                "und neu starten"
            )
        remote, missing = file_store.object_storage_dependence()
        self.stdout.write(
            f"Laut Datenbank im Objektspeicher: {remote} Inhalte, davon lokal nicht vorhanden: {missing}. Diese gelten "
            "danach als fehlend und werden bei der Quelle neu abgerufen (Vorschau, cache_files). Was im Objektspeicher "
            "liegt, bleibt dort und wird nicht mehr gelöscht."
        )
        if not ausfuehren:
            self.stdout.write("Probelauf: nichts geändert. Übernehmen mit --ausfuehren.")
            return
        self.stdout.write(f"Vermerke gelöscht: {file_store.release_object_storage()}")

    def _report(self, title: str, results: Counter[str]) -> None:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(results.items())) or "nichts zu tun"
        self.stdout.write(f"{title}: {summary}")

    def _report_limit(self, result: Any) -> None:
        """Obergrenze (#961): nur mit gesetzter Grenze eine Zeile."""
        if result.disabled:
            return
        if result.aborted:
            # Stiller Moduswechsel (#961): ohne Objektspeicher würden Inhalte, die dort liegen, verwaisen
            self.stderr.write(
                self.style.ERROR(
                    f"Obergrenze ausgesetzt: {result.remote_conflict} Inhalte liegen laut Datenbank im Objektspeicher, "
                    "der hier nicht konfiguriert ist (OBJ_ENABLED, OBJ_* im Worker prüfen). Es wird nichts verdrängt."
                )
            )
            return
        if result.locked:
            self.stdout.write("Obergrenze: ein anderer Lauf verdrängt gerade, übersprungen")
            return
        gb = 1024**3
        line = (
            f"Obergrenze {result.limit / gb:.2f} GB ({result.mode}): belegt {result.before / gb:.2f} GB"
            f" → {result.after / gb:.2f} GB, verdrängt {result.units} Inhalte für {result.files} Dokumente"
            f" ({result.freed / gb:.2f} GB)"
        )
        if result.before > result.limit and not result.reached:
            geschuetzt = ", ".join(f"{key} {value / gb:.2f} GB" for key, value in sorted(result.protected.items()))
            line += f"; Ziel nicht erreicht (geschützt: {geschuetzt or '-'})"
        verified = result.verified
        if verified.get("fehlt") or verified.get("groesse_abweichend"):
            # Mit Objektspeicher prüft jeder Lauf per HEAD (#919): solche Kopien bleiben und werden neu hochgeladen
            line += (
                f"; im Objektspeicher fehlen {verified.get('fehlt', 0)}, Größe abweichend "
                f"{verified.get('groesse_abweichend', 0)} (Kopien bleiben, --hochladen überträgt sie erneut)"
            )
        if verified.get("fehler"):
            line += "; Objektspeicher gestört, Verdrängen abgebrochen"
        self.stdout.write(line)
