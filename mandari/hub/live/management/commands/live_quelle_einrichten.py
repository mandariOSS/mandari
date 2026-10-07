# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Übertragungsquelle eines Gremiums anlegen oder ändern (Issue #915, docs/LIVE_UEBERTRAGUNG.md).

    python manage.py live_quelle_einrichten --gremium <uuid|external_id> --anbieter 3q --kennung <embed-id> \\
        [--profil balken_unten_dreizeilig | --profil-datei profil.json] [--seite https://…] [--takt 10] \\
        [--aktiv | --inaktiv]

Ohne ``--aktiv`` bleibt eine neue Quelle inaktiv; der globale Schalter ``LIVE_UEBERTRAGUNG_AKTIV`` gilt zusätzlich.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_core.models import OParlOrganization

from ...models import BroadcastSource
from ...profil import VORLAGEN, vorlage


def _gremium(angabe: str) -> OParlOrganization:
    try:
        kennung = uuid.UUID(angabe)
    except ValueError:
        gefunden = OParlOrganization.objects.filter(external_id=angabe, deleted=False).first()
    else:
        gefunden = OParlOrganization.objects.filter(pk=kennung, deleted=False).first()
    if gefunden is None:
        raise CommandError("Gremium nicht gefunden (UUID oder OParl-Adresse)")
    return gefunden


class Command(BaseCommand):
    help = "Übertragungsquelle eines Gremiums anlegen oder ändern (Live-Übertragungen, Issue #915)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--gremium", required=True, help="UUID oder OParl-Adresse des Gremiums")
        parser.add_argument("--anbieter", help="Code des Anbieters, z. B. 3q oder hls")
        parser.add_argument("--kennung", help="Kennung beim Anbieter (Embed-ID, HLS-Adresse)")
        profil = parser.add_mutually_exclusive_group()
        profil.add_argument("--profil", choices=sorted(VORLAGEN), help="Profilvorlage")
        profil.add_argument("--profil-datei", help="Profil als JSON-Datei")
        parser.add_argument("--seite", help="Seite der Übertragung bei der Kommune")
        parser.add_argument("--takt", type=int, help="Sekunden zwischen zwei Einzelbildern (5 bis 60)")
        schalter = parser.add_mutually_exclusive_group()
        schalter.add_argument("--aktiv", action="store_true", help="Quelle einschalten")
        schalter.add_argument("--inaktiv", action="store_true", help="Quelle ausschalten")

    def handle(self, *args: Any, **optionen: Any) -> None:
        gremium = _gremium(optionen["gremium"])
        quelle = BroadcastSource.objects.filter(organization=gremium).first()
        neu = quelle is None
        if quelle is None:
            if not optionen["anbieter"] or not optionen["kennung"]:
                raise CommandError("Neue Quelle: --anbieter und --kennung angeben")
            quelle = BroadcastSource(
                body_id=gremium.body_id, organization=gremium, overlay_profile=vorlage("balken_unten_dreizeilig")
            )
        if optionen["anbieter"]:
            quelle.provider = optionen["anbieter"]
        if optionen["kennung"]:
            quelle.identifier = optionen["kennung"]
        if optionen["profil"]:
            quelle.overlay_profile = vorlage(optionen["profil"])
        if optionen["profil_datei"]:
            try:
                quelle.overlay_profile = json.loads(Path(optionen["profil_datei"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as fehler:
                raise CommandError("Profildatei nicht lesbar oder kein JSON") from fehler
        if optionen["seite"] is not None:
            quelle.page_url = optionen["seite"]
        if optionen["takt"] is not None:
            quelle.interval_seconds = optionen["takt"]
        if optionen["aktiv"]:
            quelle.active = True
        if optionen["inaktiv"]:
            quelle.active = False
        try:
            quelle.full_clean()
        except ValidationError as fehler:
            raise CommandError(f"Quelle ungültig: {fehler.message_dict}") from fehler
        quelle.save()
        zustand = "aktiv" if quelle.active else "inaktiv"
        self.stdout.write(f"Quelle {'angelegt' if neu else 'geändert'}: {quelle.pk} ({quelle.provider}, {zustand})")
