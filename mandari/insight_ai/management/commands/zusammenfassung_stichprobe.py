# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``zusammenfassung_stichprobe``: eine Prompt-Fassung der KI-Zusammenfassung an einer festen Stichprobe bewerten
(Issue #1271, ``insight_ai.services.zusammenfassung_stichprobe``).

    python manage.py zusammenfassung_stichprobe --kommune "<Kommune>" [--anzahl 30] [--bis JJJJ-MM-TT]
        [--ausgabe pruefbogen.tsv] [--ausfuehren] [--hoechstens-usd 5]
    python manage.py zusammenfassung_stichprobe --auswerten pruefbogen.tsv

Ohne ``--ausfuehren`` schreibt der Befehl den Prüfbogen mit den vorhandenen Zusammenfassungen der Stichprobe und
ihrer mechanischen Prüfung; nichts geht an einen KI-Anbieter, nichts kostet. Mit ``--ausfuehren`` erzeugt er für jeden
Vorgang eine neue Zusammenfassung mit der aktuellen Prompt-Fassung (Einzelaufrufe zum vollen Preis, nur so viel, wie
die Schätzung unter ``--hoechstens-usd`` und dem Monatsbudget bleibt). Gespeichert, angezeigt oder protokolliert wird
dabei nichts; das Ergebnis steht nur im Prüfbogen.

Den Prüfbogen füllt ein Mensch aus (Spalte ``urteil``: zutreffend, teilweise oder falsch); ``--auswerten`` zählt
die Urteile gegen die vorgeschlagene Schwelle. ``<Kommune>``: Kennung (UUID) oder eindeutiger Teil des Namens.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

#: Obergrenze der geschätzten Kosten eines Laufs mit ``--ausfuehren`` (USD), wenn nichts angegeben ist
HOECHSTENS_USD = Decimal("5")


def _betrag(wert: str) -> Decimal:
    try:
        betrag = Decimal(wert)
    except InvalidOperation as fehler:
        raise ValueError(wert) from fehler
    if betrag <= 0:
        raise ValueError(wert)
    return betrag


class Command(BaseCommand):
    help = "Prompt-Fassung der KI-Zusammenfassung an einer festen Stichprobe bewerten (Prüfbogen als TSV)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--kommune", help="Kommune der Stichprobe (Kennung oder eindeutiger Teil des Namens)")
        parser.add_argument("--anzahl", type=int, default=None, help="Größe der Stichprobe (Standard 30)")
        parser.add_argument(
            "--bis",
            type=date.fromisoformat,
            help="Nur Vorgänge bis zu diesem Datum (JJJJ-MM-TT): Stichprobe bleibt fest",
        )
        parser.add_argument("--ausgabe", help="Datei des Prüfbogens (Standard: stichprobe_f<Fassung>_<Kommune>.tsv)")
        parser.add_argument(
            "--ausfuehren",
            action="store_true",
            help="Neue Zusammenfassungen mit der aktuellen Fassung erzeugen (KI-Aufrufe, kostet); sonst nur Bestand",
        )
        parser.add_argument(
            "--hoechstens-usd",
            type=_betrag,
            default=HOECHSTENS_USD,
            help=f"Mit --ausfuehren: höchstens so viel geschätzte Kosten (Standard {HOECHSTENS_USD} USD)",
        )
        parser.add_argument("--auswerten", help="Ausgefüllten Prüfbogen auswerten (statt eine Stichprobe zu ziehen)")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_ai.services import zusammenfassung_stichprobe as stichprobe

        if options["auswerten"]:
            self._auswerten(Path(options["auswerten"]))
            return
        if not options["kommune"]:
            raise CommandError("--kommune fehlt (oder --auswerten mit einem Prüfbogen).")

        from insight_ai.services.prompts import PROMPT_FASSUNG
        from insight_core.services import summary_store

        try:
            kommune = summary_store.kommune_finden(options["kommune"])
        except LookupError as fehler:
            raise CommandError(str(fehler)) from fehler
        anzahl = options["anzahl"] or stichprobe.STICHPROBE_ANZAHL
        if anzahl <= 0:
            raise CommandError("--anzahl muss größer als 0 sein.")
        vorgaenge = stichprobe.auswaehlen(kommune.pk, anzahl=anzahl, bis=options["bis"])
        if not vorgaenge:
            raise CommandError(f"{kommune.name}: keine Vorgänge mit Text für die Stichprobe.")
        ausgabe = Path(options["ausgabe"] or f"stichprobe_f{PROMPT_FASSUNG}_{kommune.slug or kommune.pk}.tsv")
        self.stdout.write(
            f"Stichprobe Fassung {PROMPT_FASSUNG}, {kommune.name}: {len(vorgaenge)} Vorgänge"
            + (f" bis {options['bis']:%d.%m.%Y}" if options["bis"] else "")
        )

        if options["ausfuehren"]:
            zeilen = self._erzeugen(vorgaenge, options["hoechstens_usd"])
        else:
            zeilen = stichprobe.bestand(vorgaenge)
            self.stdout.write("Ohne --ausfuehren: vorhandene Zusammenfassungen, kein KI-Aufruf.")
        stichprobe.schreiben(zeilen, ausgabe)
        bestanden = sum(1 for zeile in zeilen if zeile.pruefung == stichprobe.BESTANDEN)
        verworfen = sum(1 for zeile in zeilen if zeile.pruefung.startswith("verworfen"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Prüfbogen {ausgabe}: {bestanden} bestanden, {verworfen} von der Prüfung verworfen, "
                f"{len(zeilen) - bestanden - verworfen} ohne Zusammenfassung oder mit Hinweis."
            )
        )
        self.stdout.write("Spalte „urteil“ ausfüllen (zutreffend, teilweise, falsch), dann --auswerten.")

    def _erzeugen(self, vorgaenge: list[Any], hoechstens: Decimal) -> list[Any]:
        from apps.common.ki_anbieter import claude_fuer_zusammenfassung
        from insight_ai import kosten
        from insight_ai.providers import get_summary_provider
        from insight_ai.services import zusammenfassung_stichprobe as stichprobe

        provider = get_summary_provider()
        if not provider.is_available():
            raise CommandError("Kein KI-Anbieter für die Zusammenfassungen eingerichtet (Admin → KI-Einstellungen).")
        geschaetzt = stichprobe.schaetzung(vorgaenge, provider)
        if geschaetzt is not None:
            endpunkt = claude_fuer_zusammenfassung()
            rest = kosten.budget_rest(endpunkt) if endpunkt is not None else hoechstens
            grenze = min(hoechstens, rest)
            self.stdout.write(f"Geschätzt höchstens {geschaetzt.quantize(Decimal('0.01'))} USD (Grenze {grenze} USD).")
            if geschaetzt > grenze:
                raise CommandError(
                    "Die Schätzung liegt über der Grenze; nichts erzeugt. --anzahl verkleinern oder --hoechstens-usd "
                    "erhöhen (im Monatsbudget)."
                )
        else:
            self.stdout.write("Kosten nicht schätzbar (kein Anbieter mit bekannten Preisen).")
        return stichprobe.erzeugen(vorgaenge, provider)

    def _auswerten(self, pfad: Path) -> None:
        from insight_ai.services import zusammenfassung_stichprobe as stichprobe

        if not pfad.is_file():
            raise CommandError(f"Prüfbogen {pfad} nicht gefunden.")
        auswertung = stichprobe.auswerten(pfad)
        urteile = ", ".join(f"{anzahl} {urteil}" for urteil, anzahl in auswertung.urteile.items())
        self.stdout.write(
            f"{pfad}: {auswertung.zeilen} Vorgänge, {urteile}, {auswertung.offen} ohne Urteil; "
            f"{auswertung.verworfen} von der Prüfung verworfen (im Betrieb nicht angezeigt)."
        )
        schwelle = (
            f"mindestens {stichprobe.FREIGABE_ZUTREFFEND} zutreffend, höchstens {stichprobe.FREIGABE_FALSCH} falsch, "
            "alle beurteilt"
        )
        if auswertung.freigabe_erreicht:
            self.stdout.write(
                self.style.SUCCESS(f"Schwelle erreicht ({schwelle}). Über die Freigabe entscheidet ein Mensch.")
            )
        else:
            self.stdout.write(self.style.WARNING(f"Schwelle nicht erreicht ({schwelle})."))
