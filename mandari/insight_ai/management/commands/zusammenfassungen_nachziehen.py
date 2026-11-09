# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``zusammenfassungen_nachziehen``: veraltete KI-Zusammenfassungen im Bestand neu erzeugen (Issue #1257).

Veraltet ist eine Zusammenfassung, wenn sie einen anderen Beratungsstand kennt als den aktuellen, etwa weil sie vor
dem Beschluss entstand (``insight_core.services.summary_store.veraltet``). Zusammenfassungen von vor #1257 kennen
keinen Stand; sie gelten als veraltet, sobald der Vorgang ein Ergebnis hat. Für die Kommunen im Tagesbatch zieht der
Tagesbatch sie ab dem Stichtag selbst nach; dieser Befehl ist für den übrigen Bestand.

    python manage.py zusammenfassungen_nachziehen [--kommune "<Kommune>" …] [--ab JJJJ-MM-TT]
        [--limit 100] [--hoechstens-usd 5] [--ausfuehren]

Ohne ``--ausfuehren`` wird nur gezählt, geplant und geschätzt; nichts geht an die Claude API. Mit ``--ausfuehren``
reicht ein Lauf genau einen Message Batch ein (halber Preis):

- höchstens ``--limit`` Vorgänge (Standard 100, nie mehr als ``MAX_JE_BATCH``), neueste zuerst,
- nur so viele, wie Monatsbudget und ``--hoechstens-usd`` nach der Schätzung tragen,
- nie, solange ein anderer Batch offen ist: Der nächste Lauf reicht erst nach dessen Übernahme ein (alle 10 Minuten
  durch ``zusammenfassungen_abholen``). So bleibt die Last gedrosselt.

Voraussetzung: Claude ist für die Zusammenfassungen eingerichtet und der Batch eingeschaltet
(``ki_anbieter zusammenfassung --claude … --batch``). ``<Kommune>``: Kennung (UUID) oder eindeutiger Teil des Namens.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

#: So viele Vorgänge eines Laufs nennt die Ausgabe einzeln
AUSGABE_VORGAENGE = 20


def _betrag(wert: str) -> Decimal:
    try:
        betrag = Decimal(wert)
    except InvalidOperation as fehler:
        raise ValueError(wert) from fehler
    if betrag <= 0:
        raise ValueError(wert)
    return betrag


class Command(BaseCommand):
    help = "Veraltete KI-Zusammenfassungen (älter als das letzte Beratungsergebnis) gedrosselt per Batch nachziehen"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--kommune",
            action="append",
            default=[],
            help="Nur diese Kommune (Kennung oder eindeutiger Teil des Namens); mehrfach möglich",
        )
        parser.add_argument("--ab", type=date.fromisoformat, help="Nur Vorgänge ab diesem Datum (JJJJ-MM-TT)")
        parser.add_argument(
            "--limit", type=int, default=100, help="Höchstens so viele Vorgänge in diesem Lauf (Standard 100)"
        )
        parser.add_argument(
            "--hoechstens-usd",
            type=_betrag,
            help="Geschätzte Kosten dieses Laufs höchstens (USD, zusätzlich zum Budget)",
        )
        parser.add_argument(
            "--ausfuehren", action="store_true", help="Batch wirklich einreichen (sonst nur zählen und schätzen)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_ai.services.zusammenfassung_batch import MAX_JE_BATCH, nachziehen
        from insight_core.services import summary_store

        if options["limit"] < 1:
            raise CommandError("--limit muss mindestens 1 sein.")
        kommunen: dict[Any, None] | None = None
        if options["kommune"]:
            kommunen = {}
            for schluessel in options["kommune"]:
                try:
                    kommunen[summary_store.kommune_finden(schluessel).pk] = None
                except LookupError as fehler:
                    raise CommandError(str(fehler)) from fehler
        limit = min(options["limit"], MAX_JE_BATCH)
        if options["limit"] > MAX_JE_BATCH:
            self.stdout.write(f"Obergrenze je Lauf: {MAX_JE_BATCH} Vorgänge.")
        ausfuehren = bool(options["ausfuehren"])
        ergebnis = nachziehen(
            limit=limit,
            kommunen=kommunen,
            ab=options["ab"],
            hoechstens_usd=options["hoechstens_usd"],
            ausfuehren=ausfuehren,
        )
        if ergebnis.gefunden == 0 and ergebnis.grund:
            self.stdout.write(f"Nichts eingereicht: {ergebnis.grund}")
            return
        self.stdout.write(
            f"Veraltete Zusammenfassungen (höchstens {limit}): {ergebnis.gefunden} gefunden, "
            f"{ergebnis.anfragen} im Batch, geschätzt höchstens {ergebnis.geschaetzt_usd.quantize(Decimal('0.0001'))} USD"
        )
        if ergebnis.wartend:
            self.stdout.write(f"Warten noch auf Text: {ergebnis.wartend}")
        for _kennung, nummer, titel in ergebnis.vorgaenge[:AUSGABE_VORGAENGE]:
            self.stdout.write(f"  {nummer or '(ohne Nummer)'}: {titel[:100]}")
        if len(ergebnis.vorgaenge) > AUSGABE_VORGAENGE:
            self.stdout.write(f"  … und {len(ergebnis.vorgaenge) - AUSGABE_VORGAENGE} weitere")
        if ergebnis.eingereicht:
            self.stdout.write(self.style.SUCCESS(f"Eingereicht: {ergebnis.anfragen} Anfragen in einem Batch."))
        elif ergebnis.grund:
            self.stdout.write(f"Nichts eingereicht: {ergebnis.grund}")
        elif not ausfuehren:
            self.stdout.write("Nur geplant. Einreichen mit --ausfuehren.")
