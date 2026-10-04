# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustand der Texterkennung im OCR-Worker des Ingestors (Issue #817).

Stirbt der Worker mitten in einer Datei (meist durch den Speicherwächter des Kernels), bleibt sie in
``processing``. Der Worker zählt je Datei die begonnenen, nie beendeten Bearbeitungen
(``text_extraction_attempts``), stellt solche Dateien nach ``TEXT_EXTRACTION_STALE_MINUTES`` zurück und gibt sie
nach mehreren Abbrüchen als gescheitert auf (Grund „Speichergrenze“). Gemessen in der Datenbank:

- ``haengend``: Dateien länger als die Zeitgrenze (plus Spielraum) in ``processing`` – der Worker löst sie
  nicht auf, läuft also nicht oder hängt selbst.
- ``abgebrochen``: Dateien mit mindestens einem Abbruch, die noch einmal laufen.
- ``aufgegeben``: in den letzten 24 Stunden nach wiederholten Abbrüchen aufgegebene Dateien.

Die Prüfung ``texterkennung`` in ``/health/worker/`` (Statusseite) scheitert bei hängenden oder aufgegebenen
Dateien; wie ``gescheitert`` bleibt sie nach einer aufgegebenen Datei 24 Stunden rot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.events.status import Check

#: Spielraum über der Zeitgrenze des Workers: Er löst hängende Dateien höchstens einmal je Minute auf
STALE_MARGIN: Final = timedelta(minutes=15)
#: Zeitfenster für aufgegebene Dateien (wie ``gescheitert`` im Worker-Status)
GIVE_UP_WINDOW: Final = timedelta(hours=24)


@dataclass(frozen=True)
class ExtractionHealth:
    haengend: int
    abgebrochen: int
    aufgegeben: int


def stale_after() -> timedelta:
    return timedelta(minutes=int(getattr(settings, "TEXT_EXTRACTION_STALE_MINUTES", 60)))


def extraction_health(now: datetime | None = None) -> ExtractionHealth:
    """Zahlen aus einer Abfrage über den Index des Status (nur ``pending``, ``processing``, ``failed``)."""
    from ..models import OParlFile

    now = now or timezone.now()
    grenze = now - stale_after() - STALE_MARGIN
    zahlen = (
        OParlFile.objects.filter(text_extraction_status__in=["pending", "processing", "failed"])
        .annotate(beginn=Coalesce("text_extraction_started_at", "updated_at"))
        .aggregate(
            haengend=Count("id", filter=Q(text_extraction_status="processing", beginn__lt=grenze)),
            abgebrochen=Count(
                "id",
                filter=Q(text_extraction_status__in=["pending", "processing"], text_extraction_attempts__gt=0),
            ),
            aufgegeben=Count(
                "id",
                filter=Q(
                    text_extraction_status="failed",
                    text_extraction_attempts__gt=0,
                    updated_at__gte=now - GIVE_UP_WINDOW,
                ),
            ),
        )
    )
    return ExtractionHealth(
        haengend=int(zahlen["haengend"] or 0),
        abgebrochen=int(zahlen["abgebrochen"] or 0),
        aufgegeben=int(zahlen["aufgegeben"] or 0),
    )


def check_text_extraction() -> Check:
    """Prüfung ``texterkennung`` für ``/health/worker/``; Text ohne Inhalte, nur Zahlen."""
    stand = extraction_health()
    return Check(
        stand.haengend == 0 and stand.aufgegeben == 0,
        (
            f"{stand.haengend} Dateien hängen, {stand.abgebrochen} nach Abbruch erneut eingeplant, "
            f"{stand.aufgegeben} nach wiederholtem Abbruch aufgegeben (24 h)"
        ),
    )
