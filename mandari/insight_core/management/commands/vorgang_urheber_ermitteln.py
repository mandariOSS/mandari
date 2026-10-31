# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antragstellende Fraktionen für den Bestand ermitteln (Issue #1138, ``services/vorgang_urheber.py``).

Bearbeitet Vorgänge ohne Stand der aktuellen Erkennung in Stapeln und setzt nach einem Abbruch dort fort. Nach dem
Abruf hält das Abonnement ``insight.urheber`` den Stand aktuell; dieser Befehl ist für den Bestand, nach einer neuen
Version der Erkennung und nach Änderungen an der gepflegten Liste (``--alle`` mit ``--kommune``).

Beispiele::

    python manage.py vorgang_urheber_ermitteln --probe --kommune muenster   # nur zählen, nichts speichern
    python manage.py vorgang_urheber_ermitteln --stapel 500 --pause 1        # ganzer Bestand, gedrosselt
    python manage.py vorgang_urheber_ermitteln --kommune muenster --alle     # Kommune neu, etwa nach Listenimport
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from insight_core.models import OParlBody
from insight_core.services import vorgang_urheber
from insight_core.services.urheber_erkennung import HERKUNFT, Kommune


class Command(BaseCommand):
    help = "Antragstellende Fraktionen der Vorgänge ermitteln (Bestandslauf, in Stapeln, drosselbar)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--kommune", help="Nur diese Kommune (Kürzel, Kennung oder genauer Name)")
        parser.add_argument("--stapel", type=int, default=500, help="Vorgänge je Stapel (Standard 500)")
        parser.add_argument("--pause", type=float, default=0.5, help="Sekunden Pause nach jedem Stapel (Standard 0,5)")
        parser.add_argument("--limit", type=int, default=0, help="Höchstens so viele Vorgänge (0 = alle)")
        parser.add_argument("--alle", action="store_true", help="Auch Vorgänge mit aktuellem Stand neu ermitteln")
        parser.add_argument("--probe", action="store_true", help="Nur ermitteln und zählen, nichts speichern")

    def handle(self, *args: Any, **options: Any) -> None:
        stapel = max(1, options["stapel"])
        body_ids = self._koerperschaften(options.get("kommune"))
        zaehler: Counter[str] = Counter()
        bearbeitet = 0
        limit = options["limit"] or None
        for body_id in body_ids:
            kommunen: dict[Any, Kommune] = {}
            letzte = None
            while limit is None or bearbeitet < limit:
                kennungen = vorgang_urheber.ausstehend(body_id, alle=options["alle"] or options["probe"])
                if letzte is not None:
                    kennungen = kennungen.filter(pk__gt=letzte)
                anzahl = stapel if limit is None else min(stapel, limit - bearbeitet)
                ids = list(kennungen[:anzahl])
                if not ids:
                    break
                ergebnisse = vorgang_urheber.ermitteln(ids, speichern=not options["probe"], kommunen=kommunen)
                for ergebnis in ergebnisse.values():
                    zaehler[ergebnis.herkunft or "ohne"] += 1
                bearbeitet += len(ids)
                letzte = ids[-1]
                if options["pause"] > 0:
                    time.sleep(options["pause"])
            if limit is not None and bearbeitet >= limit:
                break
        zeilen = ", ".join(f"{weg}: {zaehler[weg]}" for weg in (*HERKUNFT, "ohne"))
        art = "Probe (nichts gespeichert)" if options["probe"] else "Gespeichert"
        self.stdout.write(self.style.SUCCESS(f"{art}: {bearbeitet} Vorgänge ({zeilen})"))

    def _koerperschaften(self, wert: str | None) -> list[Any]:
        if not wert:
            return list(OParlBody.objects.order_by("name").values_list("pk", flat=True))
        body = vorgang_urheber.koerperschaft_finden(wert)
        if body is None:
            raise CommandError(f"Kommune „{wert}“ nicht gefunden oder nicht eindeutig")
        return [body.pk]
