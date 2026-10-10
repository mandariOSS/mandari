# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``dokumentkette``: Pflege der Zustände des Abrufs (Issue #919, ``docs/adr/20261007-dokumentkette.md``).

    python manage.py dokumentkette zuruecksetzen [--probelauf]
        Vor einem Rückfall auf ein älteres Image (ohne Rückbau der Migration): ``retry`` und ``fetching`` werden
        ``none``, ``refused`` wird ``error`` mit dem Fehlertext, an dem ein älteres Image die Sperre erkennt.
        Idempotent; es ändern sich nur Zustandsspalten.

    python manage.py dokumentkette freigeben <quelle> [--code robots|html] [--probelauf]
        Verweigerte Abrufe einer Quelle neu einreihen (``refused`` → ``none``), etwa nachdem die Quelle den
        Bot-Schutz für uns geöffnet hat. Für eine Freigabe der robots.txt gibt es ``robots_override``.

    python manage.py dokumentkette umschalten [--probelauf]
        Stichtage der Ablage setzen (``sync_config["document_since"]``), vor dem Umschalten auf
        ``TEXT_EXTRACTION_RUNNER=worker``: Quellen ohne gelistete Kommune und ohne Stichtag bekommen den Zeitpunkt
        jetzt (vor dem ersten vollständigen Sync ``ausstehend``), ``ausstehend`` wird nach dem ersten vollständigen
        Sync zu dessen Zeitpunkt. Idempotent; mit ``TEXT_EXTRACTION_RUNNER=worker`` erledigt das außerdem jeder Lauf
        von ``cache_files``.

``<quelle>``: Kennung (UUID) oder eindeutiger Teil des Namens.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from hub.ris import abruf


class Command(BaseCommand):
    help = "Zustände des Abrufs der RIS-Dateien pflegen (zurücksetzen, freigeben, umschalten)"

    def add_arguments(self, parser: CommandParser) -> None:
        schritte = parser.add_subparsers(dest="schritt", required=True)
        zurueck = schritte.add_parser("zuruecksetzen", help="Zustände für ein älteres Image zurücksetzen")
        zurueck.add_argument("--probelauf", action="store_true", help="Nur zählen, nichts ändern")
        frei = schritte.add_parser("freigeben", help="Verweigerte Abrufe einer Quelle neu einreihen")
        frei.add_argument("quelle", help="Kennung (UUID) oder eindeutiger Teil des Namens")
        frei.add_argument("--code", choices=sorted(abruf.REFUSED_CODES), help="Nur diesen Fehlercode")
        frei.add_argument("--probelauf", action="store_true", help="Nur zählen, nichts ändern")
        umschalten = schritte.add_parser("umschalten", help="Stichtage der Ablage setzen (vor dem Umschalten)")
        umschalten.add_argument("--probelauf", action="store_true", help="Nur anzeigen, nichts schreiben")

    def handle(self, *args: Any, **options: Any) -> None:
        schritt = options["schritt"]
        if schritt == "zuruecksetzen":
            self._zuruecksetzen(probelauf=options["probelauf"])
        elif schritt == "freigeben":
            self._freigeben(options["quelle"], code=options.get("code"), probelauf=options["probelauf"])
        else:
            self._umschalten(probelauf=options["probelauf"])

    def _zuruecksetzen(self, *, probelauf: bool) -> None:
        with transaction.atomic():
            zahlen = abruf.zuruecksetzen()
            if probelauf:
                transaction.set_rollback(True)
        teile = ", ".join(f"{name}={anzahl}" for name, anzahl in zahlen.items())
        verb = "würden zurückgesetzt" if probelauf else "zurückgesetzt"
        self.stdout.write(self.style.SUCCESS(f"Zustände des Abrufs {verb}: {teile}"))

    def _freigeben(self, schluessel: str, *, code: str | None, probelauf: bool) -> None:
        quelle = _quelle(schluessel)
        with transaction.atomic():
            anzahl = abruf.freigeben(quelle, code=code)
            if probelauf:
                transaction.set_rollback(True)
        verb = "würden neu eingereiht" if probelauf else "neu eingereiht"
        self.stdout.write(self.style.SUCCESS(f"{quelle.name}: {anzahl} verweigerte Abrufe {verb}"))

    def _umschalten(self, *, probelauf: bool) -> None:
        from insight_core.models import OParlSource

        geaendert = abruf.stichtage_setzen(ausfuehren=not probelauf)
        namen = dict(OParlSource.objects.filter(pk__in=list(geaendert)).values_list("pk", "name"))
        for kennung, wert in sorted(geaendert.items()):
            name = namen.get(uuid.UUID(kennung), kennung)
            self.stdout.write(f"  {name}: document_since = {wert}")
        verb = "würden gesetzt" if probelauf else "gesetzt"
        self.stdout.write(self.style.SUCCESS(f"{len(geaendert)} Stichtage {verb}"))


def _quelle(schluessel: str) -> Any:
    from insight_core.models import OParlSource

    try:
        kennung = uuid.UUID(schluessel)
    except ValueError:
        treffer = list(OParlSource.objects.filter(name__icontains=schluessel)[:2])
    else:
        treffer = list(OParlSource.objects.filter(pk=kennung))
    if len(treffer) != 1:
        raise CommandError("Quelle nicht eindeutig gefunden (Kennung oder eindeutigen Teil des Namens angeben)")
    return treffer[0]
