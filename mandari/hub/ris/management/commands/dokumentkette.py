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

    python manage.py dokumentkette nacharbeiten [--ausfuehren]
        Dateien, deren Texterkennung an einem Abruffehler gescheitert ist (``failed``, Fehlertext beginnt mit
        „Download“), zurück in die Kette (``hub/ris/erkennung.py``): mit abgelegtem Inhalt zurück in die Erkennung,
        ohne Inhalt zusätzlich in den Abruf; nicht abzulegende Dateien (nicht freigegebener Altbestand) bleiben
        unverändert. Standard ist der Probelauf mit Zahlen je Quelle; ``--ausfuehren`` erst, wenn die Erkennung im
        Worker läuft (``TEXT_EXTRACTION_RUNNER=worker``), sonst lüde der Ingestor erneut bei der Quelle – und nur nach
        Freigabe. Idempotent; es ändern sich nur Zustandsspalten.

``<quelle>``: Kennung (UUID) oder eindeutiger Teil des Namens.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from hub.ris import abruf, erkennung


class Command(BaseCommand):
    help = "Zustände der Dokumentkette pflegen (zurücksetzen, freigeben, umschalten, nacharbeiten)"

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
        nach = schritte.add_parser(
            "nacharbeiten",
            help="An Abruffehlern gescheiterte Texterkennungen zurück in die Kette (Standard: Probelauf)",
        )
        nach.add_argument(
            "--ausfuehren",
            action="store_true",
            help="Wirklich zurücksetzen (nur mit TEXT_EXTRACTION_RUNNER=worker und nach Freigabe)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        schritt = options["schritt"]
        if schritt == "zuruecksetzen":
            self._zuruecksetzen(probelauf=options["probelauf"])
        elif schritt == "freigeben":
            self._freigeben(options["quelle"], code=options.get("code"), probelauf=options["probelauf"])
        elif schritt == "nacharbeiten":
            self._nacharbeiten(ausfuehren=options["ausfuehren"])
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

    def _nacharbeiten(self, *, ausfuehren: bool) -> None:
        from insight_core.models import OParlSource
        from insight_core.services.text_extraction_job import runner_is_worker

        if ausfuehren and not runner_is_worker():
            raise CommandError(
                "Nacharbeit erst ausführen, wenn die Erkennung im Worker läuft (TEXT_EXTRACTION_RUNNER=worker); "
                "sonst lädt der Ingestor die Dateien erneut bei der Quelle."
            )
        stand = erkennung.nacharbeiten(ausfuehren=ausfuehren)
        namen = dict(OParlSource.objects.filter(pk__in=[k for k in stand.je_quelle if k]).values_list("pk", "name"))
        spalten = (
            (erkennung.NACH_ERKENNUNG, "Erkennung"),
            (erkennung.NACH_ABRUF, "Abruf und Erkennung"),
            (erkennung.NACH_WARTET, "Erkennung, Abruf läuft bzw. wartet auf Freigabe"),
            (erkennung.NACH_UNVERAENDERT, "unverändert (nicht abzulegen)"),
        )
        for kennung, zahlen in sorted(stand.je_quelle.items(), key=lambda e: str(namen.get(e[0], e[0]))):
            teile = ", ".join(f"{text} {zahlen[schluessel]}" for schluessel, text in spalten if zahlen[schluessel])
            self.stdout.write(f"  {namen.get(kennung, kennung or 'ohne Quelle')}: {teile}")
        gesamt = stand.gesamt()
        teile = ", ".join(f"{text} {gesamt[schluessel]}" for schluessel, text in spalten)
        if ausfuehren:
            self.stdout.write(self.style.SUCCESS(f"Nachgearbeitet: {teile}"))
            return
        self.stdout.write(self.style.WARNING(f"Probelauf, nichts geändert: {teile}"))
        if not runner_is_worker():
            self.stdout.write(
                "Hinweis: TEXT_EXTRACTION_RUNNER ist nicht worker. Ausführen erst nach dem Umschalten; dann gilt die "
                "Ablage für alle Quellen ab ihrem Stichtag, und die Zahlen können sich verschieben."
            )
        self.stdout.write("Ausführen mit --ausfuehren (nur nach Freigabe).")


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
