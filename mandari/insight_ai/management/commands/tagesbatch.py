# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``tagesbatch``: Kommunen im täglichen Batch der Zusammenfassungen (Issues #1220, #1222).

    python manage.py tagesbatch liste
        Ausgewählte Kommunen mit Stichtag und Prüfung der Verortung, dazu je Kommune: Vorgänge ab Stichtag, mit
        Zusammenfassung, bereit für den nächsten Batch und wartend (Texterkennung, Abruf, Verortung).

    python manage.py tagesbatch kommune "<Kommune>" --ab 2026-11-01 [--ohne-verortung]
        Kommune aufnehmen oder ändern. Vorgänge ab dem Datum bekommen ihre Zusammenfassung im Tagesbatch, sobald der
        Text aller Anlagen erkannt ist; mit Prüfung der Verortung (Standard) prüft Claude dabei die Orte.

    python manage.py tagesbatch entfernen "<Kommune>"
        Kommune herausnehmen; vorhandene Zusammenfassungen bleiben.

    python manage.py tagesbatch nacharbeiten
        Am Abruf gescheiterte Anlagen der Kommunen jetzt neu einreihen (sonst täglich um 20:00 Uhr).

    python manage.py tagesbatch einreichen
        Den Tagesbatch jetzt einreichen (sonst täglich um 06:30 Uhr); gleiche Regeln wie der Zeitplan.

``<Kommune>``: Kennung (UUID) oder eindeutiger Teil des Namens. Der Batch läuft nur, wenn Claude für die
Zusammenfassungen eingerichtet und der Batch eingeschaltet ist (``ki_anbieter zusammenfassung --claude … --batch``).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_ai.models import TagesbatchKommune


class Command(BaseCommand):
    help = "Kommunen im täglichen Batch der Zusammenfassungen (Claude) verwalten"

    def add_arguments(self, parser: CommandParser) -> None:
        aktionen = parser.add_subparsers(dest="aktion", metavar="{liste,kommune,entfernen,nacharbeiten,einreichen}")
        aktionen.required = True
        aktionen.add_parser("liste", help="Ausgewählte Kommunen und Stand")
        kommune = aktionen.add_parser("kommune", help="Kommune aufnehmen oder ändern")
        kommune.add_argument("kommune", help="Kennung oder eindeutiger Teil des Namens")
        kommune.add_argument(
            "--ab", required=True, type=date.fromisoformat, help="Vorgänge ab diesem Datum (JJJJ-MM-TT)"
        )
        kommune.add_argument("--ohne-verortung", action="store_true", help="Verortung nicht von Claude prüfen lassen")
        entfernen = aktionen.add_parser("entfernen", help="Kommune herausnehmen")
        entfernen.add_argument("kommune", help="Kennung oder eindeutiger Teil des Namens")
        aktionen.add_parser("nacharbeiten", help="Gescheiterte Abrufe jetzt neu einreihen")
        aktionen.add_parser("einreichen", help="Tagesbatch jetzt einreichen")

    def handle(self, *args: Any, **options: Any) -> None:
        aktion = options["aktion"]
        if aktion == "liste":
            self._liste()
        elif aktion == "kommune":
            body = self._kommune(options["kommune"])
            eintrag, neu = TagesbatchKommune.objects.update_or_create(
                body=body, defaults={"ab": options["ab"], "verortung_pruefen": not options["ohne_verortung"]}
            )
            self.stdout.write(f"{'Aufgenommen' if neu else 'Geändert'}: {eintrag}")
            self._liste()
        elif aktion == "entfernen":
            body = self._kommune(options["kommune"])
            geloescht, _ = TagesbatchKommune.objects.filter(body=body).delete()
            self.stdout.write(f"Entfernt: {body}" if geloescht else f"Nicht ausgewählt: {body}")
        elif aktion == "nacharbeiten":
            from insight_ai.services.zusammenfassung_batch import nacharbeiten

            self.stdout.write(f"Neu eingereiht: {nacharbeiten()} Anlagen")
        elif aktion == "einreichen":
            from insight_ai.services.zusammenfassung_batch import einreichen

            self.stdout.write(f"Eingereicht: {einreichen()} Anfragen")

    @staticmethod
    def _kommune(schluessel: str) -> Any:
        from insight_core.services import summary_store

        try:
            return summary_store.kommune_finden(schluessel)
        except LookupError as fehler:
            raise CommandError(str(fehler)) from fehler

    def _liste(self) -> None:
        from insight_core.services import summary_store

        eintraege = list(TagesbatchKommune.objects.select_related("body"))
        if not eintraege:
            self.stdout.write("Keine Kommune im Tagesbatch: Der Batch reicht nichts ein.")
            return
        for eintrag in eintraege:
            offen = summary_store.fuer_tagesbatch({eintrag.body_id: eintrag.ab}, limit=10_000)
            wartend: Counter[str] = Counter(
                summary_store.tagesbatch_wartet(vorgang, verortung=False) or "bereit" for vorgang in offen
            )
            verortung = "mit Prüfung der Verortung" if eintrag.verortung_pruefen else "ohne Prüfung der Verortung"
            self.stdout.write(
                f"{eintrag.body} ab {eintrag.ab:%d.%m.%Y}, {verortung}: {len(offen)} ohne Zusammenfassung "
                f"(mit Text), davon {dict(wartend)}"
            )
