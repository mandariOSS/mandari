# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Adresspunkte von Privatanschriften im Bestand auf die Straße zurückstufen (Issue #1269).

Betrifft Eingaben aus der Einwohnerschaft und Bauanträge (``insight_core.services.ortsschutz``). Ein Adresspunkt rückt
auf den Punkt der Straße; ist die Straße unbekannt oder schon verortet, wird er ausgeblendet. Nichts wird gelöscht, die
alten Angaben stehen in ``PaperLocation.vorher`` (``insight_core.services.ortsschutz_bestand``).

Ohne ``--ausfuehren`` ist es ein Probelauf, der nur zählt. ``--rueckgaengig`` hebt frühere Rückstufungen auf.

Verwendung:
    python manage.py adresspunkte_zurueckstufen                      # Probelauf, alle Kommunen
    python manage.py adresspunkte_zurueckstufen --body muenster --ausfuehren
    python manage.py adresspunkte_zurueckstufen --body muenster --rueckgaengig --ausfuehren
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from insight_core.models import OParlBody
from insight_core.services import ortsschutz_bestand


class Command(BaseCommand):
    help = "Stuft Adresspunkte von Eingaben aus der Einwohnerschaft und Bauanträgen auf die Straße zurück (umkehrbar)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", type=str, default=None, help="UUID oder Slug der Kommune")
        parser.add_argument("--limit", type=int, default=0, help="Höchstens so viele Vorgänge (0 = alle)")
        parser.add_argument("--ausfuehren", action="store_true", help="Änderungen speichern (sonst Probelauf)")
        parser.add_argument("--rueckgaengig", action="store_true", help="Frühere Rückstufungen aufheben")

    def handle(self, *args: Any, **options: Any) -> None:
        body = self._resolve_body(options["body"]) if options["body"] else None
        rueckgaengig = options["rueckgaengig"]
        ausfuehren = options["ausfuehren"]
        vorgaenge = ortsschutz_bestand.zurueckgestufte(body) if rueckgaengig else ortsschutz_bestand.kandidaten(body)
        if options["limit"] > 0:
            vorgaenge = vorgaenge[: options["limit"]]
        schritt = ortsschutz_bestand.rueckgaengig if rueckgaengig else ortsschutz_bestand.zurueckstufen

        gesamt = ortsschutz_bestand.Bilanz()
        geprueft = 0
        for paper in vorgaenge.iterator(chunk_size=200):
            geprueft += 1
            ergebnis = schritt(paper, ausfuehren=ausfuehren)
            if ergebnis.vorgaenge and options["verbosity"] >= 2:
                self.stdout.write(f"  {paper.reference or paper.pk}: {ergebnis}")
            gesamt += ergebnis

        art = "Probelauf, nichts gespeichert" if not ausfuehren else "gespeichert"
        if rueckgaengig:
            text = (
                f"{gesamt.vorgaenge} von {geprueft} Vorgängen: {gesamt.wiederhergestellt} Orte wiederhergestellt, "
                f"{gesamt.uebersprungen} übersprungen ({art})."
            )
        else:
            text = (
                f"{gesamt.vorgaenge} von {geprueft} geprüften Vorgängen: {gesamt.verschoben} Adresspunkte auf die "
                f"Straße zurückgestuft, {gesamt.ausgeblendet} ausgeblendet ({art})."
            )
        self.stdout.write(self.style.SUCCESS(text) if ausfuehren else self.style.WARNING(text))

    @staticmethod
    def _resolve_body(identifier: str) -> OParlBody:
        try:
            body: OParlBody = OParlBody.objects.get(Q(id=identifier) | Q(slug=identifier))
            return body
        except (OParlBody.DoesNotExist, ValueError, ValidationError):
            fallback: OParlBody | None = OParlBody.objects.filter(slug=identifier).first()
            if fallback is None:
                raise CommandError(f"Kommune mit ID/Slug '{identifier}' nicht gefunden.") from None
            return fallback
