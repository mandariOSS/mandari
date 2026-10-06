# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ankündigung in Work anlegen oder zurückziehen (Issue #857, ``apps.work.notifications.ankuendigung``).

Die Ankündigung erscheint in der Glocke und, solange ungelesen, im Hinweisband auf Start. Sie geht nie per E-Mail
hinaus und wird nicht an Vertretungen weitergeleitet. Ein zweiter Aufruf mit demselben Schlüssel legt nichts doppelt
an; wer später Mitglied wird, bekommt sie beim nächsten Aufruf.

    python manage.py work_ankuendigung --schluessel work-update-2026-11 \\
        --titel "Großes Update" --text "Neues Design, bessere Recherche …" \\
        --link https://docs.mandari.de/work/was-ist-neu/ --linktext "Was ist neu" --rueckmeldung --probelauf

    # nur bestimmte Organisationen, Gäste eingeschlossen
    python manage.py work_ankuendigung … --organisation <slug> --organisation <slug> --mit-gaesten

    # zurückziehen (Benachrichtigungen bleiben gespeichert, erscheinen aber nicht mehr)
    python manage.py work_ankuendigung --schluessel work-update-2026-11 --zurueckziehen

Die Ausgabe nennt nur Anzahlen je Organisation, keine Personen.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.tenants.models import Organization
from apps.work.notifications.ankuendigung import (
    Ankuendigung,
    Bericht,
    UngueltigeAnkuendigung,
    ankuendigen,
    zurueckziehen,
)


class Command(BaseCommand):
    help = "Legt eine Ankündigung für die aktiven Mitglieder in Work an (Glocke und Hinweisband auf Start, keine E-Mail)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--schluessel",
            required=True,
            help="Fester Schlüssel der Ankündigung (Kleinbuchstaben, Ziffern, Bindestriche), etwa work-update-2026-11.",
        )
        parser.add_argument("--titel", default="", help="Titel (höchstens 120 Zeichen).")
        parser.add_argument("--text", default="", help="Kurzer Text (höchstens 500 Zeichen).")
        parser.add_argument("--link", default="", help="https://… oder Pfad dieser Installation (/…).")
        parser.add_argument("--linktext", default="", help="Text des Links im Band (Vorgabe „Mehr dazu“).")
        parser.add_argument(
            "--rueckmeldung", action="store_true", help="Im Band zusätzlich „Rückmeldung geben“ (Support)."
        )
        parser.add_argument(
            "--organisation",
            action="append",
            default=[],
            metavar="SLUG",
            help="Nur diese Organisation (mehrfach möglich). Ohne Angabe: alle Organisationen.",
        )
        parser.add_argument("--mit-gaesten", action="store_true", help="Auch Gäste erhalten die Ankündigung.")
        parser.add_argument(
            "--zurueckziehen", action="store_true", help="Ankündigung mit diesem Schlüssel zurückziehen."
        )
        parser.add_argument(
            "--probelauf", "--dry-run", dest="probelauf", action="store_true", help="Nur zählen, nichts ändern."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        probelauf: bool = options["probelauf"]
        if options["zurueckziehen"]:
            self._zurueckziehen(options["schluessel"], probelauf)
            return

        organisationen = self._organisationen(options["organisation"])
        ankuendigung = Ankuendigung(
            schluessel=options["schluessel"],
            titel=options["titel"].strip(),
            text=options["text"].strip(),
            link=options["link"].strip(),
            linktext=options["linktext"].strip(),
            rueckmeldung=options["rueckmeldung"],
        )
        try:
            bericht = ankuendigen(
                ankuendigung,
                organisationen=organisationen,
                mit_gaesten=options["mit_gaesten"],
                probelauf=probelauf,
            )
        except UngueltigeAnkuendigung as fehler:
            raise CommandError(str(fehler)) from fehler
        self._ausgeben(bericht, probelauf)

    def _organisationen(self, slugs: list[str]) -> list[Organization] | None:
        if not slugs:
            return None
        gefunden = {org.slug: org for org in Organization.objects.filter(slug__in=slugs)}
        unbekannt = sorted(set(slugs) - set(gefunden))
        if unbekannt:
            raise CommandError(f"Unbekannte Organisation: {', '.join(unbekannt)}. Es wurde nichts angelegt.")
        return list(gefunden.values())

    def _zurueckziehen(self, schluessel: str, probelauf: bool) -> None:
        try:
            anzahl = zurueckziehen(schluessel, probelauf=probelauf)
        except UngueltigeAnkuendigung as fehler:
            raise CommandError(str(fehler)) from fehler
        if probelauf:
            self.stdout.write(f"Probelauf: würde {anzahl} Benachrichtigungen zurückziehen.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Zurückgezogen: {anzahl} Benachrichtigungen."))

    def _ausgeben(self, bericht: Bericht, probelauf: bool) -> None:
        if probelauf:
            self.stdout.write("Probelauf – es wird nichts angelegt.")
        for zeile in bericht.zeilen:
            self.stdout.write(
                f"  {zeile.organisation}: {zeile.neu} neu, {zeile.vorhanden} schon vorhanden, "
                f"{zeile.abgeschaltet} abgeschaltet"
            )
        summe = (
            f"{bericht.neu} neu, {bericht.vorhanden} schon vorhanden, {bericht.abgeschaltet} abgeschaltet "
            f"(Ankündigungen in den Benachrichtigungseinstellungen aus)"
        )
        if probelauf:
            self.stdout.write(f"Würde anlegen: {summe}")
        else:
            self.stdout.write(self.style.SUCCESS(f"Angelegt: {summe}"))
