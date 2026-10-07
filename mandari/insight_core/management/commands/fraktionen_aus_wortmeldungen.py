# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionen nachträglich aus vorhandenen Wortmeldungen der Live-Übertragungen ableiten (Issue #915, #916).

    python manage.py fraktionen_aus_wortmeldungen --meeting <uuid>      # eine Sitzung (etwa ein eingespieltes Protokoll)
    python manage.py fraktionen_aus_wortmeldungen --body <uuid>         # alle Wortmeldungen einer Kommune
    python manage.py fraktionen_aus_wortmeldungen --probelauf           # nur zählen, nichts speichern

Dieselben Regeln wie das Abonnement ``insight.fraktionen_live`` (``insight_core.services.fraktionen_live``):
Wortmeldungen mit Person und gelesener Fraktion, älteste zuerst; jede zählt höchstens einmal, ein erneuter Aufruf
ändert nichts.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from insight_core.services.fraktionen_live import Ergebnis, nachholen


def _uuid(wert: str | None, option: str) -> uuid.UUID | None:
    if wert is None:
        return None
    try:
        return uuid.UUID(wert)
    except ValueError as fehler:
        raise CommandError(f"{option}: UUID erwartet") from fehler


class Command(BaseCommand):
    help = "Fraktionen aus vorhandenen Wortmeldungen der Live-Übertragungen ableiten (Issue #915, #916)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--meeting", help="nur Wortmeldungen dieser Sitzung (UUID)")
        parser.add_argument("--body", help="nur Wortmeldungen dieser Kommune (UUID der Körperschaft)")
        parser.add_argument("--probelauf", action="store_true", help="nur zählen, nichts speichern")

    def handle(self, *args: Any, **optionen: Any) -> None:
        meeting_id = _uuid(optionen.get("meeting"), "--meeting")
        body_id = _uuid(optionen.get("body"), "--body")
        if optionen["probelauf"]:
            with transaction.atomic():
                ergebnisse = nachholen(meeting_id=meeting_id, body_id=body_id)
                transaction.set_rollback(True)
        else:
            # Jede Wortmeldung in einer eigenen Transaktion: Ein Abbruch verliert nichts, ein neuer Aufruf macht weiter
            ergebnisse = nachholen(meeting_id=meeting_id, body_id=body_id)
        self.stdout.write(
            f"{sum(ergebnisse.values())} Wortmeldungen mit Person und Fraktion: "
            f"{ergebnisse[Ergebnis.VERBUCHT]} verbucht, {ergebnisse[Ergebnis.SCHON_VERBUCHT]} schon verbucht, "
            f"{ergebnisse[Ergebnis.OHNE_FRAKTION]} ohne Fraktion, {ergebnisse[Ergebnis.FEHLT]} ohne Person oder Kommune"
            + (" (Probelauf, nichts gespeichert)" if optionen["probelauf"] else "")
        )
