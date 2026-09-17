# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: monatlicher Verfügbarkeitsbericht aus der Statusseite (Issue #231).

    python manage.py availability_report --month 2026-08 [--gatus-url https://status.example] [--out bericht.md]

Ohne ``--month`` wird der Vormonat berichtet; ohne ``--gatus-url`` gilt ``GATUS_URL``.
Ohne erreichbare Statusseite endet der Lauf mit Exit-Code 1.
Auswertung und Grenzen: apps/common/availability.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.common.einmalig import EinmaligMixin


def vormonat(jetzt: datetime | None = None) -> str:
    jetzt = jetzt or datetime.now(tz=UTC)
    erster = jetzt.replace(day=1)
    return f"{erster.year - 1}-12" if erster.month == 1 else f"{erster.year}-{erster.month - 1:02d}"


class Command(EinmaligMixin, BaseCommand):
    sperre = "availability_report"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    help = "Erstellt den Verfügbarkeitsbericht eines Monats je überwachtem Dienst als Markdown"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--month", help="Monat im Format YYYY-MM (Standard: Vormonat)")
        parser.add_argument("--gatus-url", help="Basis-URL der Statusseite (Standard: GATUS_URL)")
        parser.add_argument("--out", help="Zieldatei (Markdown); ohne Angabe Ausgabe auf stdout")
        parser.add_argument("--target", type=float, default=99.5, help="Zielverfügbarkeit in Prozent (Standard 99,5)")

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.common.availability import GatusError, bericht_markdown, monatsgrenzen, sammle_dienste

        monat = options["month"] or vormonat()
        try:
            monatsgrenzen(monat)
        except ValueError as exc:
            raise CommandError(f"--month erwartet YYYY-MM, nicht {monat!r}") from exc
        basis = options["gatus_url"] or str(getattr(settings, "GATUS_URL", "") or "")
        if not basis:
            raise CommandError("Keine Statusseite bekannt: --gatus-url angeben oder GATUS_URL setzen")

        try:
            dienste = sammle_dienste(basis, monat)
        except GatusError as exc:
            raise CommandError(str(exc)) from exc
        if not dienste:
            raise CommandError(f"Die Statusseite {basis} meldet keine überwachten Endpunkte")

        bericht = bericht_markdown(monat, dienste, ziel=options["target"], quelle=basis)
        if options["out"]:
            ziel = Path(options["out"])
            ziel.write_text(bericht, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Bericht für {monat} geschrieben: {ziel} ({len(dienste)} Dienste)"))
        else:
            self.stdout.write(bericht)
