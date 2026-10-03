# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Dokument-Cache ausgeblendeter Kommunen leeren.

Zwischengespeichert werden nur gelistete Kommunen (siehe ``services/file_cache.py``). Dieser
Befehl räumt den Bestand ab, der vorher für ausgeblendete Kommunen (Piloten, Tests) entstanden
ist: Er löscht deren Verzeichnisse im Cache und setzt die Dateien in der Datenbank auf „nicht
zwischengespeichert“ zurück. Extrahierte Texte bleiben erhalten – Suche und Verortung sind nicht
betroffen. Wird eine Kommune später gelistet, lädt ``cache_files`` ihre Dokumente neu. Kommunen synthetischer
Quellen (Domäne ``.invalid``, etwa die Demo) bleiben unangetastet – ihre Dateien lassen sich nie neu abrufen.

Ein Verzeichnis bleibt stehen, sobald es auch eine gelistete Kommune nutzt – nach ihrem Verzeichnisnamen
oder weil dort tatsächlich Dateien von ihr liegen (``OParlFile.local_path``). So löscht der Befehl nie
Kopien gelisteter Kommunen, auch wenn Verzeichnisname und Ablage einmal auseinanderlaufen (Issue #373).

Verwendung:
    python manage.py prune_file_cache --unlisted --dry-run   # nur anzeigen
    python manage.py prune_file_cache --unlisted
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
    help = "Leert den Dokument-Cache ausgeblendeter Kommunen (Dateien und Datenbank-Status)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--unlisted", action="store_true", help="Alle ausgeblendeten Kommunen (Pflicht)")
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts löschen")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlBody, OParlFile
        from insight_core.services.file_cache import body_dir_name, cache_root
        from insight_core.services.file_store import release_queryset

        if not options["unlisted"]:
            raise CommandError("Bitte --unlisted angeben (bewusst kein Standard, der etwas löscht).")
        dry_run = options["dry_run"]

        # Ein Verzeichnis, das auch eine gelistete Kommune nutzt, bleibt unangetastet.
        gelistete_verzeichnisse = _verzeichnisse_gelisteter(cache_root())
        gesamt_bytes = gesamt_dateien = 0
        for body in OParlBody.objects.filter(is_listed=False).select_related("source").order_by("name"):
            if _nicht_abrufbar(body):
                self.stdout.write(f"{body.name[:45]:<45} übersprungen – Quelle nicht abrufbar, Kopie wäre verloren")
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
