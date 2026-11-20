# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Finanzcheck von Hand (Issue #1513): einzelne Vorgänge oder ein Lauf wie der stündliche Zeitplan.

    python manage.py finanzcheck --vorgang V/0452/2026 --kommune nster      # ein Vorgang, Ergebnis anzeigen
    python manage.py finanzcheck --vorgang <uuid> --neu                      # vorhandenen Finanzcheck ersetzen
    python manage.py finanzcheck --vorgang <uuid> --trocken                  # nur prüfen und anzeigen, nichts speichern
    python manage.py finanzcheck --nachziehen [--hoechstens 20]              # wie der Zeitplan, mehr Aufrufe

Kosten trägt das Monatsbudget der Zusammenfassungen; ohne eingerichtete Claude API passiert nichts.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Finanzcheck für einzelne Vorgänge erstellen oder nachziehen (Issue #1513)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--vorgang", help="Kennung (UUID) oder Vorlagen-Nr. des Vorgangs")
        parser.add_argument("--kommune", help="Kommune bei einer Vorlagen-Nr. (Kennung oder eindeutiger Namensteil)")
        parser.add_argument("--neu", action="store_true", help="Vorhandenen Finanzcheck ersetzen")
        parser.add_argument("--trocken", action="store_true", help="Nur erstellen und anzeigen, nicht speichern")
        parser.add_argument("--nachziehen", action="store_true", help="Lauf wie der stündliche Zeitplan")
        parser.add_argument("--hoechstens", type=int, default=None, help="KI-Aufrufe beim Nachziehen")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_ai.services import finanzcheck

        if options["nachziehen"]:
            anzahl = finanzcheck.nachziehen(hoechstens=options["hoechstens"] or finanzcheck.JE_LAUF)
            self.stdout.write(f"{anzahl} KI-Aufrufe")
            return
        if not options["vorgang"]:
            raise CommandError("--vorgang oder --nachziehen angeben")
        paper = self._vorgang(options["vorgang"], options["kommune"])
        if paper.finanzcheck and not (options["neu"] or options["trocken"]):
            self.stdout.write(f"Vorhanden ({paper.finanzcheck.get('status')}); ersetzen mit --neu")
        else:
            ergebnis = finanzcheck.erstellen(paper, speichern=not options["trocken"])
            if ergebnis is None:
                raise CommandError("Kein Text oder Claude API nicht eingerichtet")
            self.stdout.write(f"Status: {ergebnis.status} {ergebnis.grund}".rstrip())
            if options["trocken"]:
                paper.finanzcheck = {"status": ergebnis.status, "daten": ergebnis.daten}
            else:
                paper.refresh_from_db(fields=["finanzcheck"])
        anzeige = finanzcheck.anzeige(paper)
        if anzeige is None:
            return
        for kennzahl in anzeige.kennzahlen:
            self.stdout.write(f"  {kennzahl.titel}: {kennzahl.wert} {kennzahl.zusatz}".rstrip())
        for satz in anzeige.saetze:
            self.stdout.write(f"- {satz}")
        for hinweis in anzeige.hinweise:
            self.stdout.write(f"! {hinweis}")
        self.stdout.write(f"SEO: {anzeige.seo}")

    @staticmethod
    def _vorgang(schluessel: str, kommune: str | None) -> Any:
        import uuid

        from insight_core.models import OParlPaper
        from insight_core.services import summary_store

        try:
            return OParlPaper.objects.select_related("body").get(pk=uuid.UUID(schluessel))
        except (ValueError, OParlPaper.DoesNotExist):
            pass
        auswahl = OParlPaper.objects.select_related("body").filter(reference=schluessel, deleted=False)
        if kommune:
            try:
                auswahl = auswahl.filter(body=summary_store.kommune_finden(kommune))
            except LookupError as fehler:
                raise CommandError(str(fehler)) from fehler
        treffer = list(auswahl[:2])
        if len(treffer) != 1:
            raise CommandError(f"„{schluessel}“ passt auf {len(treffer)} Vorgänge; --kommune angeben")
        return treffer[0]
