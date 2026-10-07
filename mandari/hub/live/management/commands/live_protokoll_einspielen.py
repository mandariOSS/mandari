# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokoll des lokalen Live-Wächters (JSONL) einspielen (Issue #915, ``hub.live.einspielen``).

    python manage.py live_protokoll_einspielen <datei.jsonl> --meeting <uuid>

Die Sitzung muss zu einem Gremium mit Übertragungsquelle gehören (``live_quelle_einrichten``). Idempotent.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_core.models import OParlMeeting

from ...einspielen import einspielen
from ...selectors import quelle_fuer_sitzung


class Command(BaseCommand):
    help = "Protokoll des lokalen Live-Wächters (JSONL) als Übertragung einspielen (Issue #915)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("datei", help="JSONL-Datei des Live-Wächters")
        parser.add_argument("--meeting", required=True, help="UUID der Sitzung")

    def handle(self, *args: Any, **optionen: Any) -> None:
        try:
            meeting_id = uuid.UUID(optionen["meeting"])
        except ValueError as fehler:
            raise CommandError("--meeting: UUID erwartet") from fehler
        meeting = OParlMeeting.objects.filter(pk=meeting_id).first()
        if meeting is None:
            raise CommandError("Sitzung nicht gefunden")
        quelle = quelle_fuer_sitzung(meeting)
        if quelle is None:
            raise CommandError("Kein Gremium der Sitzung hat eine Übertragungsquelle (live_quelle_einrichten)")
        pfad = Path(optionen["datei"])
        try:
            with pfad.open(encoding="utf-8") as datei:
                bilanz = einspielen(datei, meeting=meeting, quelle=quelle)
        except OSError as fehler:
            raise CommandError("Datei nicht lesbar") from fehler
        self.stdout.write(
            f"{bilanz.zeilen} Zeilen: {bilanz.abschnitte} neue TOP-Abschnitte, {bilanz.wortmeldungen} neue "
            f"Wortmeldungen, {bilanz.neu_protokoll} neue Protokolleinträge, {bilanz.uebersprungen} übersprungen, "
            f"{bilanz.fehlerhaft} fehlerhaft; Status {bilanz.status}"
        )
        for hinweis in bilanz.hinweise:
            self.stdout.write(f"Hinweis: {hinweis}")
