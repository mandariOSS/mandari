# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchqualität mit dem Bewertungssatz messen (Konzept Insight-Suche, 4.4), nur lesend.

    manage.py suchqualitaet messen                              # Abfrage aus SEARCH_RANKING
    manage.py suchqualitaet messen --ranking v1 --ranking v2    # beide Versionen nebeneinander
    manage.py suchqualitaet messen --kommune muenster --json

Gibt je Anfrage Treffer, Z1 (junge Treffer unter den ersten zehn), Z2 (Platz des neuesten erwarteten
Vorgangs), Z4 (Rauschen unter den ersten 20) und die Dauer aus, dazu Z1, Z2, Z4, Z8 und Z13 gesamt.
Elasticsearch bekommt nur Such- und Zählanfragen; ausgegeben werden Zahlen und Aktenzeichen, keine Inhalte.
Anfragen von Kommunen, die es in dieser Installation nicht gibt, entfallen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_search import bewertung


class Command(BaseCommand):
    help = "Suchqualität mit dem Bewertungssatz messen (nur lesend)."

    def add_arguments(self, parser: CommandParser) -> None:
        unter = parser.add_subparsers(dest="aktion", required=True)
        messen = unter.add_parser("messen", help="Bewertungssatz gegen den Live-Index messen.")
        messen.add_argument(
            "--ranking",
            action="append",
            choices=["v1", "v2"],
            help="Abfrageversion (mehrfach für einen Vergleich); Standard SEARCH_RANKING.",
        )
        messen.add_argument("--kommune", default="", help="Nur Anfragen dieser Kommune (Kurzname, z. B. muenster).")
        messen.add_argument("--datei", default="", help="Anderer Bewertungssatz (JSON wie anfragen.json).")
        messen.add_argument("--json", action="store_true", help="Ausgabe als JSON.")

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlBody
        from insight_core.services.search_service import get_search_service, ranking_version

        datei = Path(options["datei"]) if options["datei"] else bewertung.ANFRAGEN_DATEI
        if not datei.is_file():
            raise CommandError(f"Bewertungssatz nicht gefunden: {datei}")
        anfragen = bewertung.lade_anfragen(datei, kommune=options["kommune"])
        kurznamen = {a.kommune for a in anfragen}
        kommunen = {
            slug: str(pk) for slug, pk in OParlBody.objects.filter(slug__in=kurznamen).values_list("slug", "id")
        }
        if not kommunen:
            raise CommandError("Keine Kommune des Bewertungssatzes in dieser Installation.")

        dienst = get_search_service()
        versionen = options["ranking"] or [ranking_version()]
        berichte = [bewertung.messen(dienst, anfragen, kommunen, version) for version in versionen]

        if options["json"]:
            self.stdout.write(json.dumps([b.als_dict() for b in berichte], ensure_ascii=False, indent=1))
        else:
            self.stdout.write(bewertung.als_text(berichte))
