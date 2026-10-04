# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebP-Vorschaubilder der Kommunen-Logos für den Bestand erzeugen (insight_core/logo_vorschau.py).

    python manage.py build_logo_thumbnails            # fehlende oder veraltete Fassungen erzeugen
    python manage.py build_logo_thumbnails --force    # alle Originale erneut lesen

Idempotent: Kommunen, deren Fassungen zum aktuellen Logo passen, bleiben unberührt; Dateien mit
gleichem Inhalts-Hash werden auch mit ``--force`` nicht neu geschrieben. Das Original bleibt immer
erhalten. Neue oder geänderte Logos bekommen ihre Fassungen schon beim Speichern.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from insight_core.models import OParlBody


class Command(BaseCommand):
    help = "Erzeugt WebP-Vorschaubilder der Kommunen-Logos (Bestand, idempotent)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--force", action="store_true", help="Alle Originale erneut lesen")

    def handle(self, *args: Any, **options: Any) -> None:
        geaendert = unveraendert = ohne = 0
        for body in OParlBody.objects.exclude(logo="").exclude(logo__isnull=True).order_by("name"):
            if body.update_logo_thumbnails(force=options["force"]):
                geaendert += 1
                daten = body.logo_thumbnails or {}
                groessen = ", ".join(f"{g['width']}×{g['height']}" for g in daten.get("sizes", []))
                hinweis = groessen or ("nicht lesbar" if daten.get("error") else "SVG, bleibt Original")
                self.stdout.write(f"{body.get_display_name()}: {hinweis}")
            else:
                unveraendert += 1
            if not (body.logo_thumbnails or {}).get("sizes"):
                ohne += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"{geaendert} Kommunen aktualisiert, {unveraendert} unverändert, {ohne} ohne Vorschau (SVG oder Fehler)"
            )
        )
