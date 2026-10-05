# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO: personenbezogene Nutzlasten im Journal zu einer Person neutralisieren (``apps.events.datenschutz``).

Leert die Nutzlast der Journaleinträge mit Sichtbarkeit ``personenbezogen``, deren Objekt die Person ist oder
deren Personenfeld (``x-person`` im Vertrag) sie nennt. Die Kennung bleibt (Zeile, Ereignis-ID, Objekt,
Folgenummer). Wiederholbar. Der Eingriff steht im Sicherheitsprotokoll (Quelle ``kommandozeile``).

Nach einem ``redact`` geschieht dasselbe als Auftrag, wenn ``EVENTS_REDACT_NEUTRALIZE`` eingeschaltet ist; dieser
Befehl ist für Anfragen von Hand und zum Nachholen.

    manage.py events_neutralize --person 01234567-89ab-4cde-8f01-23456789abcd --dry-run
    manage.py events_neutralize --person 01234567-89ab-4cde-8f01-23456789abcd
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.events import datenschutz
from apps.events.eingriffe import record_command


class Command(BaseCommand):
    help = "Leert personenbezogene Nutzlasten im Journal zu einer Person (DSGVO); die Kennung bleibt."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--person", required=True, metavar="UUID", help="Kennung der Person (Konto oder Person).")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts ändern.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            person = uuid.UUID(str(options["person"]).strip())
        except ValueError:
            raise CommandError("--person: Kennung im Format einer UUID angeben.") from None
        if not datenschutz.person_fields():
            self.stdout.write(
                self.style.WARNING("Keine Personenfelder aus den Verträgen geladen; geprüft wird nur das Objekt.")
            )
        if options["dry_run"]:
            self.stdout.write(f"Probelauf: {datenschutz.count(person)} Journaleinträge würden neutralisiert.")
            return
        with transaction.atomic():
            anzahl = datenschutz.neutralize(person)
            record_command("events_neutralize", "journal_neutralisiert", person=str(person), anzahl=anzahl)
        self.stdout.write(f"{anzahl} Journaleinträge neutralisiert (Nutzlast geleert, Kennung bleibt).")
