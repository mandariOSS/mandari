# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Dokument-Cache ausgeblendeter Kommunen leeren bzw. auf eine Gesamtgröße verkleinern.

**Ausgeblendete Kommunen** (``--unlisted``):

Zwischengespeichert werden gelistete Kommunen, mit ``TEXT_EXTRACTION_RUNNER=worker`` auch ausgeblendete ab
ihrem Stichtag (``hub.ris.abruf.stores_file``); deren Kopien bleiben. Dieser Befehl räumt den Bestand ab, der
vorher für ausgeblendete Kommunen (Piloten, Tests) entstanden ist: Er löscht deren Verzeichnisse im Cache und setzt die Dateien in der Datenbank auf „nicht
zwischengespeichert“ zurück. Extrahierte Texte bleiben erhalten – Suche und Verortung sind nicht
betroffen. Wird eine Kommune später gelistet, lädt ``cache_files`` ihre Dokumente neu. Kommunen synthetischer
Quellen (Domäne ``.invalid``, etwa die Demo) bleiben unangetastet – ihre Dateien lassen sich nie neu abrufen.

Ein Verzeichnis bleibt stehen, sobald es auch eine gelistete Kommune nutzt – nach ihrem Verzeichnisnamen
oder weil dort tatsächlich Dateien von ihr liegen (``OParlFile.local_path``). So löscht der Befehl nie
Kopien gelisteter Kommunen, auch wenn Verzeichnisname und Ablage einmal auseinanderlaufen (Issue #373).

Verwendung:
    python manage.py prune_file_cache --unlisted --dry-run   # nur anzeigen
    python manage.py prune_file_cache --unlisted

**Gesamtgröße** (``--max-gb``, Issue #961): verdrängt wie das stündliche Aufräumen mit ``FILE_CACHE_MAX_TOTAL_GB``
die am wenigsten gebrauchten Dokumente, bis ``FILE_CACHE_EVICT_TARGET_PERCENT`` (Standard 90 %) der angegebenen
Grenze erreicht sind (``services/file_cache_limit.py``). Für den einmaligen Abbau eines großen Bestands; die Ausgabe
fasst Anzahl, Größe und Kommunen zusammen. Läuft neben dem Zeitplan (eigene Sperre, ein gleichzeitiger Lauf endet mit
Hinweis):

    python manage.py prune_file_cache --max-gb 10 --dry-run  # nur anzeigen
    python manage.py prune_file_cache --max-gb 10

Die erste Zeile nennt den Modus (mit bzw. ohne Objektspeicher). Liegen Inhalte laut Datenbank im Objektspeicher, ist er
in diesem Container aber nicht konfiguriert (fehlende ``OBJ_*``, ``OBJ_ENABLED=false``), bricht der Befehl ab:
Meist läuft er dann im falschen Container. ``--ohne-objektspeicher`` macht ausdrücklich ohne ihn weiter; Inhalte im
Objektspeicher bleiben auch dann unangetastet.

Mit Objektspeicher prüft ``--pruefe-objektspeicher`` vor dem Löschen jeder lokalen Kopie per ``HEAD``, ob der Inhalt
mit seiner Größe dort liegt; fehlt er, bleibt die Kopie und wird erneut hochgeladen. Im Probelauf prüft die Option alle
Inhalte, die gingen, oder mit ``--stichprobe N`` eine zufällige Auswahl:

    python manage.py prune_file_cache --max-gb 10 --dry-run --pruefe-objektspeicher --stichprobe 500
    python manage.py prune_file_cache --max-gb 10 --pruefe-objektspeicher
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError, CommandParser


def _nicht_abrufbar(body: Any) -> bool:
    """Synthetische Quelle (Domäne ``.invalid``, etwa die Demo): Die Kopie ist die einzige."""
    source = getattr(body, "source", None)
    return (urlparse(getattr(source, "url", "") or "").hostname or "").endswith(".invalid")


def _groesse(pfad: Path) -> int:
    return sum(p.stat().st_size for p in pfad.rglob("*") if p.is_file())


def _verzeichnisse_gelisteter(root: Path) -> set[str]:
    """Verzeichnisse im Cache, die gelistete Kommunen nutzen: festgeschriebene Namen und tatsächliche Ablage."""
    from insight_core.models import OParlBody, OParlFile
    from insight_core.services.file_cache import body_dir_name

    namen = {body_dir_name(b) for b in OParlBody.objects.filter(is_listed=True)}
    pfade = (
        OParlFile.objects.filter(body__is_listed=True)
        .exclude(local_path__isnull=True)
        .exclude(local_path="")
        .values_list("local_path", flat=True)
    )
    for pfad in pfade.iterator(chunk_size=5000):
        try:
            teile = Path(pfad or "").relative_to(root).parts
        except ValueError:
            continue  # außerhalb des Cache-Wurzelverzeichnisses
        if len(teile) > 1:
            namen.add(teile[0])
    return namen


class Command(BaseCommand):
    help = (
        "Leert den Dokument-Cache ausgeblendeter Kommunen (--unlisted) oder verkleinert ihn auf eine Gesamtgröße "
        "(--max-gb)"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--unlisted", action="store_true", help="Alle ausgeblendeten Kommunen")
        parser.add_argument(
            "--max-gb",
            type=float,
            default=None,
            help="Gesamtgröße in GB: verdrängt die am wenigsten gebrauchten Dokumente (wie FILE_CACHE_MAX_TOTAL_GB)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts löschen")
        parser.add_argument(
            "--ohne-objektspeicher",
            action="store_true",
            help="Weitermachen, obwohl Inhalte im Objektspeicher liegen, der hier nicht konfiguriert ist (die bleiben)",
        )
        parser.add_argument(
            "--pruefe-objektspeicher",
            action="store_true",
            help="Vor dem Löschen jeder lokalen Kopie per HEAD prüfen, ob der Inhalt im Objektspeicher liegt",
        )
        parser.add_argument(
            "--stichprobe",
            type=int,
            default=0,
            help="Mit --dry-run --pruefe-objektspeicher: nur so viele zufällig gewählte Inhalte prüfen",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        max_gb = options["max_gb"]
        if not options["unlisted"] and max_gb is None:
            raise CommandError("Bitte --unlisted oder --max-gb <GB> angeben (bewusst kein Standard, der etwas löscht).")
        if max_gb is not None and max_gb <= 0:
            raise CommandError("--max-gb muss größer als 0 sein.")
        if options["stichprobe"] < 0:
            raise CommandError("--stichprobe darf nicht negativ sein.")
        if options["stichprobe"] and not (options["dry_run"] and options["pruefe_objektspeicher"]):
            raise CommandError(
                "--stichprobe nur mit --dry-run --pruefe-objektspeicher; der echte Lauf prüft mit "
                "--pruefe-objektspeicher jeden Inhalt."
            )
        if options["unlisted"]:
            self._unlisted(options["dry_run"])
        if max_gb is not None:
            self._max_gb(max_gb, options)

    def _max_gb(self, max_gb: float, options: dict[str, Any]) -> None:
        from insight_core.services import file_cache_limit

        gb = 1024**3
        dry_run = options["dry_run"]
        remote = file_cache_limit.mode() == file_cache_limit.MODE_REMOTE
        self.stdout.write(
            "Modus: mit Objektspeicher (OBJ_ENABLED und Zugangsdaten gesetzt, Ablage nach SHA-256)"
            if remote
            else "Modus: ohne Objektspeicher (OBJ_ENABLED aus, OBJ_* unvollständig oder Ablage je Kommune)"
        )
        if options["pruefe_objektspeicher"] and not remote:
            raise CommandError("--pruefe-objektspeicher braucht einen konfigurierten Objektspeicher (OBJ_*).")
        result = file_cache_limit.enforce(
            max_bytes=round(max_gb * gb),
            dry_run=dry_run,
            without_object_storage=options["ohne_objektspeicher"],
            verify_remote=options["pruefe_objektspeicher"],
            sample=options["stichprobe"],
        )
        if result.aborted:
            raise CommandError(
                f"Abgebrochen: {result.remote_conflict} Inhalte liegen laut Datenbank im Objektspeicher, der in diesem "
                "Container nicht konfiguriert ist. Meist fehlen hier OBJ_* (falscher Container, OBJ_ENABLED=false). "
                "Ohne Objektspeicher würden freigegebene Inhalte verwaisen und beim nächsten Aufräumen mit "
                "Objektspeicher dort gelöscht. Ausdrücklich ohne Objektspeicher weitermachen (diese Inhalte bleiben): "
                "--ohne-objektspeicher"
            )
        if result.locked:
            raise CommandError("Ein anderer Lauf verdrängt gerade (Sperre „dokumentcache-grenze“); später erneut.")
        art = (
            "nur lokale Kopien, Objektspeicher behält alles"
            if result.mode == file_cache_limit.MODE_REMOTE
            else "Dokumente auf „Verdrängt“, die Vorschau holt sie bei Bedarf von der Quelle"
        )
        self.stdout.write(
            f"Grenze {result.limit / gb:.2f} GB, Ziel {result.target / gb:.2f} GB ({art}); "
            f"belegt lokal {result.before / gb:.2f} GB"
        )
        if result.remote_conflict:
            self.stdout.write(
                self.style.WARNING(
                    f"{result.remote_conflict} Inhalte liegen im Objektspeicher, der hier nicht konfiguriert ist: "
                    "Sie bleiben unangetastet (--ohne-objektspeicher)."
                )
            )
        if result.before <= result.limit:
            self.stdout.write(self.style.SUCCESS("Belegung liegt unter der Grenze, nichts zu tun."))
            return
        for name, (dateien, belegt) in sorted(result.per_body.items(), key=lambda item: (-item[1][1], item[0])):
            self.stdout.write(f"  {name[:45]:<45} {dateien:>7} Dokumente  {belegt / gb:6.2f} GB")
        for grund, belegt in sorted(result.protected.items()):
            self.stdout.write(f"  geschützt ({grund}): {belegt / gb:.2f} GB")
        for grund, anzahl in sorted(result.skipped.items()):
            self.stdout.write(f"  übersprungen ({grund}): {anzahl}")
        verb = "würden verdrängt" if dry_run else "verdrängt"
        self.stdout.write(
            self.style.SUCCESS(
                f"{result.units} Inhalte für {result.files} Dokumente {verb}, {result.freed / gb:.2f} GB; "
                f"danach belegt {result.after / gb:.2f} GB."
            )
        )
        if result.verified:
            self._report_verified(result, dry_run)
        if not result.reached:
            self.stdout.write(
                self.style.WARNING(
                    "Ziel nicht erreicht: Der Rest ist geschützt (Texterkennung, frisch abgelegt, nicht neu abrufbar, "
                    "im Objektspeicher ohne Konfiguration) oder gerade in Gebrauch."
                )
            )

    def _report_verified(self, result: Any, dry_run: bool) -> None:
        """Ergebnis der Prüfung im Objektspeicher (``--pruefe-objektspeicher``)."""
        verified = result.verified
        geprueft = verified.get("vorhanden", 0) + verified.get("fehlt", 0) + verified.get("groesse_abweichend", 0)
        zeile = (
            f"Objektspeicher geprüft (HEAD): {geprueft} Inhalte, vorhanden {verified.get('vorhanden', 0)}, "
            f"fehlen {verified.get('fehlt', 0)}, Größe abweichend {verified.get('groesse_abweichend', 0)}"
        )
        if verified.get("fehler"):
            zeile += "; Objektspeicher nicht erreichbar, abgebrochen"
        probleme = verified.get("fehlt", 0) + verified.get("groesse_abweichend", 0) + verified.get("fehler", 0)
        if not probleme:
            self.stdout.write(self.style.SUCCESS(zeile))
            return
        self.stdout.write(self.style.WARNING(zeile))
        self.stdout.write(
            self.style.WARNING(
                "  Vor dem echten Lauf klären (dokumentablage --hochladen, Objektspeicher prüfen)."
                if dry_run
                else "  Diese lokalen Kopien bleiben; fehlende lädt dokumentablage --hochladen erneut hoch."
            )
        )

    def _unlisted(self, dry_run: bool) -> None:
        from insight_core.models import OParlBody, OParlFile
        from insight_core.services.file_cache import body_dir_name, cache_root, caches_body
        from insight_core.services.file_store import release_queryset

        # Ein Verzeichnis, das auch eine gelistete Kommune nutzt, bleibt unangetastet.
        gelistete_verzeichnisse = _verzeichnisse_gelisteter(cache_root())
        gesamt_bytes = gesamt_dateien = 0
        for body in OParlBody.objects.filter(is_listed=False).select_related("source").order_by("name"):
            if _nicht_abrufbar(body):
                self.stdout.write(f"{body.name[:45]:<45} übersprungen – Quelle nicht abrufbar, Kopie wäre verloren")
                continue
            if caches_body(body):
                # Ablage für alle Quellen mit erlaubtem Abruf (TEXT_EXTRACTION_RUNNER=worker, Issue #919)
                self.stdout.write(f"{body.name[:45]:<45} übersprungen – legt Dokumente ab (Stichtag gesetzt)")
                continue
            verzeichnis = cache_root() / body_dir_name(body)
            dateien = OParlFile.objects.filter(body=body).exclude(local_status="none")
            anzahl = dateien.count()
            belegt = _groesse(verzeichnis) if verzeichnis.is_dir() else 0
            if not anzahl and not belegt:
                continue
            geteilt = body_dir_name(body) in gelistete_verzeichnisse
            self.stdout.write(
                f"{body.name[:45]:<45} {anzahl:>7} Einträge  {belegt / 1024**3:6.2f} GB  {verzeichnis}"
                + ("  (Verzeichnis geteilt – bleibt)" if geteilt else "")
            )
            gesamt_bytes += 0 if geteilt else belegt
            gesamt_dateien += anzahl
            if dry_run:
                continue
            # Ablage nach SHA-256: Referenzen freigeben (Inhalte anderer Kommunen bleiben), danach Status
            release_queryset(dateien.filter(blob__isnull=False))
            dateien.update(local_status="none", local_path=None, local_error="")
            if verzeichnis.is_dir() and not geteilt:
                shutil.rmtree(verzeichnis)

        verb = "würden frei" if dry_run else "freigegeben"
        self.stdout.write(
            self.style.SUCCESS(f"{gesamt_dateien} Einträge zurückgesetzt, {gesamt_bytes / 1024**3:.1f} GB {verb}.")
        )
