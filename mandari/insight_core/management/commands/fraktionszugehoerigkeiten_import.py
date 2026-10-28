# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gepflegte Fraktionszugehörigkeiten aus einer CSV-Datei übernehmen (Issue #1138, ``FraktionsZugehoerigkeit``).

Spalten (Kopfzeile, UTF-8): ``kommune``, ``name``, ``vorname``, ``fraktion``, ``gueltig_ab``, ``gueltig_bis``,
``quelle``; wahlweise ``kurzform`` („Grüne“), ``art`` (``fraktion``, ``gruppe``, ``einzelmandat``; ohne Angabe aus dem
Namen) und ``notiz``. ``kommune`` ist Kürzel, Kennung oder genauer Name der Körperschaft, Daten im Format
``JJJJ-MM-TT``. Eine leere Fraktion heißt „offen“ und wird übernommen, aber nie verwendet.

Eine Zeile gilt als vorhanden, wenn Kommune, Name, Vorname, Fraktion und ``gueltig_ab`` gleich sind; dann werden ihre
übrigen Angaben aktualisiert. Zeilen, die in der Datei fehlen, bleiben unverändert (gelöscht wird nur im Admin).

**Umbenennungen** einer Gruppe bei gleicher Besetzung stehen in einer zweiten Datei mit den Spalten ``kommune``,
``fraktion`` (Name wie in den Zugehörigkeiten), ``bezeichnung``, ``kurzform``, ``gueltig_ab``, ``gueltig_bis``,
``quelle`` (``FraktionsBezeichnung``); der Befehl erkennt sie an der Spalte ``bezeichnung``.

Startdaten für Münster: ``insight_core/fraktionslisten/fraktionszugehoerigkeiten_muenster.csv`` und
``insight_core/fraktionslisten/fraktionsbezeichnungen_muenster.csv``. Danach die Vorgänge der Kommune neu ermitteln:
``manage.py vorgang_urheber_ermitteln --kommune <kommune> --alle``.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from insight_core.models import FraktionsBezeichnung, FraktionsZugehoerigkeit
from insight_core.services import vorgang_urheber
from insight_core.services.urheber_erkennung import ist_gruppe

PFLICHT = ("kommune", "name", "vorname", "fraktion", "gueltig_ab", "gueltig_bis", "quelle")
PFLICHT_NAMEN = ("kommune", "fraktion", "bezeichnung", "gueltig_ab", "gueltig_bis", "quelle")
ARTEN = {wert for wert, _ in FraktionsZugehoerigkeit.ART_CHOICES}


def _datum(wert: str, zeile: int, spalte: str) -> date | None:
    wert = (wert or "").strip()
    if not wert:
        return None
    try:
        return date.fromisoformat(wert)
    except ValueError:
        raise CommandError(f"Zeile {zeile}: {spalte} „{wert}“ ist kein Datum (JJJJ-MM-TT)") from None


class Command(BaseCommand):
    help = "Gepflegte Fraktionszugehörigkeiten aus einer CSV-Datei übernehmen"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("datei", help="Pfad der CSV-Datei")
        parser.add_argument("--probe", action="store_true", help="Nur prüfen und zählen, nichts speichern")

    def handle(self, *args: Any, **options: Any) -> None:
        pfad = Path(options["datei"])
        if not pfad.is_file():
            raise CommandError(f"Datei nicht gefunden: {pfad}")
        with pfad.open(encoding="utf-8-sig", newline="") as datei:
            leser = csv.DictReader(datei)
            namen = "bezeichnung" in (leser.fieldnames or [])
            fehlend = [s for s in (PFLICHT_NAMEN if namen else PFLICHT) if s not in (leser.fieldnames or [])]
            if fehlend:
                raise CommandError(f"Spalten fehlen: {', '.join(fehlend)}")
            zeilen = list(leser)

        koerperschaften: dict[str, object] = {}
        neu = geaendert = 0
        with transaction.atomic():
            for nummer, zeile in enumerate(zeilen, start=2):
                kommune = (zeile["kommune"] or "").strip()
                if kommune not in koerperschaften:
                    body = vorgang_urheber.koerperschaft_finden(kommune)
                    if body is None:
                        raise CommandError(f"Zeile {nummer}: Kommune „{kommune}“ nicht gefunden oder nicht eindeutig")
                    koerperschaften[kommune] = body
                fraktion = (zeile["fraktion"] or "").strip()
                modell: type[FraktionsBezeichnung] | type[FraktionsZugehoerigkeit]
                if namen:
                    modell = FraktionsBezeichnung
                    schluessel = {
                        "body": koerperschaften[kommune],
                        "fraktion": fraktion,
                        "gueltig_ab": _datum(zeile["gueltig_ab"], nummer, "gueltig_ab"),
                    }
                    werte = {
                        "bezeichnung": (zeile["bezeichnung"] or "").strip(),
                        "kurzform": (zeile.get("kurzform") or "").strip(),
                        "gueltig_bis": _datum(zeile["gueltig_bis"], nummer, "gueltig_bis"),
                        "quelle": (zeile["quelle"] or "").strip(),
                    }
                    if not fraktion or not werte["bezeichnung"]:
                        raise CommandError(f"Zeile {nummer}: Fraktion und Bezeichnung sind Pflicht")
                else:
                    modell, schluessel, werte = (
                        FraktionsZugehoerigkeit,
                        *self._zugehoerigkeit(zeile, nummer, koerperschaften[kommune], fraktion),
                    )
                eintrag = modell.objects.filter(**schluessel).first()
                if eintrag is None:
                    eintrag = modell(**schluessel, **werte)
                    neu += 1
                elif any(getattr(eintrag, feld) != wert for feld, wert in werte.items()):
                    for feld, wert in werte.items():
                        setattr(eintrag, feld, wert)
                    geaendert += 1
                else:
                    continue
                try:
                    eintrag.full_clean(validate_unique=False, validate_constraints=False)
                except ValidationError as fehler:
                    raise CommandError(f"Zeile {nummer}: {'; '.join(fehler.messages)}") from None
                if not options["probe"]:
                    eintrag.save()
            if options["probe"]:
                transaction.set_rollback(True)

        art = "Probe (nichts gespeichert)" if options["probe"] else "Übernommen"
        self.stdout.write(self.style.SUCCESS(f"{art}: {len(zeilen)} Zeilen, {neu} neu, {geaendert} geändert"))

    def _zugehoerigkeit(
        self, zeile: dict[str, str], nummer: int, body: object, fraktion: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Schlüssel und Werte einer Zeile der Zugehörigkeiten."""
        art = (zeile.get("art") or "").strip() or (
            FraktionsZugehoerigkeit.ART_GRUPPE if ist_gruppe(fraktion) else FraktionsZugehoerigkeit.ART_FRAKTION
        )
        if art not in ARTEN:
            raise CommandError(f"Zeile {nummer}: unbekannte Art „{art}“")
        schluessel = {
            "body": body,
            "nachname": (zeile["name"] or "").strip(),
            "vorname": (zeile["vorname"] or "").strip(),
            "fraktion": fraktion,
            "gueltig_ab": _datum(zeile["gueltig_ab"], nummer, "gueltig_ab"),
        }
        if not schluessel["nachname"] or not schluessel["vorname"]:
            raise CommandError(f"Zeile {nummer}: Name und Vorname sind Pflicht")
        werte = {
            "kurzform": (zeile.get("kurzform") or "").strip(),
            "art": art,
            "gueltig_bis": _datum(zeile["gueltig_bis"], nummer, "gueltig_bis"),
            "quelle": (zeile["quelle"] or "").strip(),
            "notiz": (zeile.get("notiz") or "").strip(),
        }
        return schluessel, werte
