# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Bauzeit und Größe des Snapshots messen, bevor der Änderungsfeed eingeschaltet wird
(Issue #707, ``docs/OPARL_API.md``, Abschnitt „Betrieb“).

Ein Snapshot (``…/body/<uuid>/snapshot``) entsteht vollständig in einer temporären Datei, bevor das erste Byte
zum Abnehmer fließt. Für die größte Kommune einer Installation muss das in die Zeitlimits vorgeschalteter
Proxys und in den Platz des temporären Verzeichnisses passen. Dieser Befehl baut den Snapshot auf demselben
Weg (``hub.api.snapshot.write``) in eine temporäre Datei, misst und verwirft sie. Er braucht und ändert
``OPARL_CHANGES_ENABLED`` nicht und gibt nichts an Abnehmer; er liest nur.

Die Messung liest den ganzen öffentlichen Bestand der Kommune (Datenbank und CPU wie ein echter Abruf): nicht
zur Hauptlast laufen lassen, bei großen Kommunen im Hintergrund.

Verwendung:
    python manage.py oparl_snapshot_messen                  # die größte gelistete Kommune
    python manage.py oparl_snapshot_messen --anzahl 3       # die drei größten
    python manage.py oparl_snapshot_messen --body <uuid>    # eine bestimmte Kommune (auch nicht gelistet)
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hub.api import aggregator, snapshot
from insight_core.models import OParlBody, OParlMeeting, OParlPaper


@dataclass(frozen=True)
class Messung:
    """Ergebnis für eine Kommune: Zeilen des Snapshots (ohne Kopfzeile), Größe in Bytes, Bauzeit in Sekunden."""

    zeilen: int
    groesse: int
    sekunden: float


def messen(body_id: uuid.UUID) -> Messung:
    """Snapshot der Kommune bauen wie unter ``…/snapshot`` und verwerfen."""
    start = time.monotonic()
    with tempfile.TemporaryFile() as spool:
        _, zeilen = snapshot.write(aggregator.snapshot_for_measurement(body_id), spool)
        groesse = spool.tell()
    return Messung(zeilen=zeilen, groesse=groesse, sekunden=time.monotonic() - start)


def groesste(anzahl: int) -> list[OParlBody]:
    """Die gelisteten Kommunen mit den meisten Vorlagen und Sitzungen (sie bestimmen die Größe)."""
    umfang = {
        body.pk: OParlPaper.objects.filter(body=body, deleted=False).count()
        + OParlMeeting.objects.filter(body=body, deleted=False).count()
        for body in OParlBody.objects.listed().only("pk")
    }
    reihenfolge = sorted(umfang, key=lambda pk: umfang[pk], reverse=True)[:anzahl]
    kommunen = {body.pk: body for body in OParlBody.objects.filter(pk__in=reihenfolge)}
    return [kommunen[pk] for pk in reihenfolge]


def _spitze_mb() -> float | None:
    """Höchster Speicherbedarf dieses Prozesses in MB (unter Linux; ``ru_maxrss`` zählt dort in KB)."""
    if sys.platform != "linux":
        return None
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Command(BaseCommand):
    help = "Misst Bauzeit und Größe des Snapshots der größten Kommunen, ohne den Änderungsfeed einzuschalten."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", action="append", default=[], help="Nur diese Kommune (UUID, mehrfach möglich)")
        parser.add_argument("--anzahl", type=int, default=1, help="Die N größten gelisteten Kommunen (Standard 1)")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["body"]:
            try:
                kennungen = [uuid.UUID(str(wert)) for wert in options["body"]]
            except ValueError as exc:
                raise CommandError("--body erwartet die UUID einer Kommune.") from exc
            kommunen = list(OParlBody.objects.filter(pk__in=kennungen))
            if len(kommunen) != len(set(kennungen)):
                raise CommandError("Kommune nicht gefunden.")
        else:
            kommunen = groesste(max(1, options["anzahl"]))
        if not kommunen:
            self.stdout.write("Keine gelistete Kommune vorhanden.")
            return

        self.stdout.write("Snapshot-Messung (nur lesend; der Snapshot wird verworfen)")
        for kommune in kommunen:
            ergebnis = messen(kommune.pk)
            mb = ergebnis.groesse / 1024 / 1024
            je_sekunde = ergebnis.zeilen / ergebnis.sekunden if ergebnis.sekunden > 0 else 0.0
            self.stdout.write(
                f"  {kommune.name} ({kommune.pk}): {ergebnis.zeilen} Zeilen, {mb:.1f} MB, "
                f"{ergebnis.sekunden:.1f} s Bauzeit ({je_sekunde:.0f} Zeilen/s)"
            )
        spitze = _spitze_mb()
        if spitze is not None:
            self.stdout.write(f"Höchster Speicherbedarf des Prozesses: {spitze:.0f} MB")
