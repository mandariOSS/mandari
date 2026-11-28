# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Ereignismenge und Einträge des Änderungsfeeds messen, bevor er eingeschaltet wird
(Issue #707, ``docs/OPARL_API.md``, Abschnitt „Betrieb“).

Gezählt werden je Kommune die öffentlichen Ereignisse ``ris.*``, die der Feed des Aggregators liest (nur die des
RIS-Bestands, ``aggregator.BESTAND_PRODUCERS``), und die Einträge, die er daraus nach dem Profil von kommParl macht
(mit den einbettenden Objekten), jeweils für einen Zeitraum, etwa den eines Vollabgleichs. Der Befehl liest das Journal
wie ein Abnehmer, braucht und ändert ``OPARL_CHANGES_ENABLED`` nicht und gibt nichts an Abnehmer.

Ereignisse gibt es erst, wenn die Erzeuger sie schreiben (``INGESTOR_EVENTS_ENABLED``). Für die Ereignismenge eines
Vollabgleichs also zuerst die Erzeuger einschalten, einen Vollabgleich abwarten und dann dessen Zeitraum messen.

Verwendung:
    python manage.py oparl_feed_messen                      # die fünf Kommunen mit den meisten Ereignissen, 24 Stunden
    python manage.py oparl_feed_messen --stunden 168        # eine Woche
    python manage.py oparl_feed_messen --body <uuid>        # eine bestimmte Kommune
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Count, Max, Q, QuerySet
from django.utils import timezone

from apps.events.models import Event, Visibility
from hub.api import aggregator, changes
from insight_core.models import OParlBody


@dataclass(frozen=True)
class Messung:
    """Ergebnis für eine Kommune: Ereignisse und Einträge je Typ, Lesezeit in Sekunden."""

    ereignisse: Counter[str]
    eintraege: Counter[str]
    sekunden: float


def beginn(seit: datetime) -> int:
    """Folgenummer vor dem Zeitraum: das letzte Ereignis, das vor ``seit`` erfasst wurde (0: keines)."""
    found = Event.objects.filter(seq__isnull=False, recorded_at__lt=seit).aggregate(hoechste=Max("seq"))
    return int(found["hoechste"] or 0)


def ereignisse(seq: int) -> QuerySet[Event]:
    """Ereignisse hinter ``seq``, die der Feed des Aggregators liest, aller Kommunen (``changes.events`` je Kommune)."""
    herkunft = Q()
    for prefix in aggregator.BESTAND_PRODUCERS:
        herkunft |= Q(tenant_ref__startswith=prefix)
    return Event.objects.filter(
        herkunft,
        seq__gt=seq,
        body_id__isnull=False,
        visibility=Visibility.OEFFENTLICH,
        type__startswith=changes.TYPE_PREFIX,
        aggregate_type__in=[*changes.KINDS, *changes.CARRIERS],
    )


def messen(body_id: uuid.UUID, seq: int) -> Messung:
    """Ereignisse und Einträge der Kommune hinter ``seq``, gelesen wie von einem Abnehmer."""
    start = time.monotonic()
    feed = aggregator.feed_for_measurement(body_id)
    gelesen = changes.events(body_id, feed.producers).filter(seq__gt=seq)
    je_typ = Counter({zeile["type"]: zeile["anzahl"] for zeile in gelesen.values("type").annotate(anzahl=Count("id"))})
    eintraege: Counter[str] = Counter(
        str(eintrag["type"]).rsplit("/", 1)[-1] for eintrag in changes.entries_since(feed, seq)
    )
    return Messung(ereignisse=je_typ, eintraege=eintraege, sekunden=time.monotonic() - start)


def _summe(zaehler: Counter[str]) -> str:
    teile = ", ".join(f"{name} {anzahl}" for name, anzahl in sorted(zaehler.items(), key=lambda paar: -paar[1]))
    return f"{sum(zaehler.values())}" + (f" ({teile})" if teile else "")


class Command(BaseCommand):
    help = "Misst Ereignisse und Einträge des Änderungsfeeds je Kommune in einem Zeitraum, ohne ihn einzuschalten."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", action="append", default=[], help="Nur diese Kommune (UUID, mehrfach möglich)")
        parser.add_argument("--anzahl", type=int, default=5, help="Die N Kommunen mit den meisten Ereignissen")
        parser.add_argument("--stunden", type=float, default=24.0, help="Zeitraum bis jetzt in Stunden (Standard 24)")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["stunden"] <= 0:
            raise CommandError("--stunden muss größer als 0 sein.")
        seit = timezone.now() - timedelta(hours=options["stunden"])
        seq = beginn(seit)
        if options["body"]:
            try:
                kennungen = [uuid.UUID(str(wert)) for wert in options["body"]]
            except ValueError as exc:
                raise CommandError("--body erwartet die UUID einer Kommune.") from exc
            if OParlBody.objects.filter(pk__in=kennungen).count() != len(set(kennungen)):
                raise CommandError("Kommune nicht gefunden.")
        else:
            haeufigste = (
                ereignisse(seq)
                .values("body_id")
                .annotate(anzahl=Count("id"))
                .order_by("-anzahl", "body_id")[: max(1, options["anzahl"])]
            )
            kennungen = [zeile["body_id"] for zeile in haeufigste]
        namen = dict(OParlBody.objects.filter(pk__in=kennungen).values_list("pk", "name"))

        self.stdout.write(f"Änderungsfeed-Messung seit {seit:%d.%m.%Y %H:%M} (nur lesend)")
        self.stdout.write(f"Öffentliche Ereignisse des Bestands, alle Kommunen: {ereignisse(seq).count()}")
        kennungen = [kennung for kennung in kennungen if kennung in namen]
        if not kennungen:
            self.stdout.write("Keine Kommune mit Ereignissen im Zeitraum.")
            return
        for kennung in kennungen:
            ergebnis = messen(kennung, seq)
            self.stdout.write(f"  {namen[kennung]} ({kennung}), {ergebnis.sekunden:.1f} s gelesen")
            self.stdout.write(f"    Ereignisse: {_summe(ergebnis.ereignisse)}")
            self.stdout.write(f"    Einträge:   {_summe(ergebnis.eintraege)}")
