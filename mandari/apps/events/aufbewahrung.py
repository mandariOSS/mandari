# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufbewahrung des Journals (Issue #511; ``docs/adr/20260929-ereignistechnik-postgres.md``, Punkt 10;
Spezifikation 4.9). ``manage.py events_purge`` ruft das auf, im Betrieb als Zeitplan im Worker
(``apps/events/schedules.py``, Schalter ``EVENTS_JOURNAL_PURGE_ENABLED``, Standard aus).

Gelöscht wird immer ein **Anfang** des Journals: nummerierte Zeilen unter einer Grenze, in Stapeln mit
kurzen Transaktionen. Die Grenze ist die kleinste von vier Schranken:

- **Frist:** die erste Folgenummer, die seit dem Stichtag erfasst wurde. Stichtag ist der Beginn des Tages
  (UTC), der ``EVENTS_JOURNAL_RETENTION_DAYS`` (Standard und Minimum 90) zurückliegt, mindestens aber so weit, wie Cursor
  des Änderungsfeeds gelten (``OPARL_CHANGES_RETENTION_DAYS``). Ein Cursor trägt seinen Ausgabetag; was nach
  der Ausgabe eines gültigen Cursors erfasst wurde, bleibt also vorhanden, und das Sicherheitsnetz des Feeds
  (``hub.api.changes``, ``410``) spricht für gültige Cursor nie an. Ein Snapshot gibt einen frischen Cursor aus
  und braucht keine alten Zeilen.
- **Abonnements:** nichts über dem kleinsten Cursor, auch nicht von pausierten Abonnements und solchen im
  Schattenbetrieb. Ein Abonnement verliert nie ein Ereignis, das es noch nicht verarbeitet hat. Vor jedem
  Stapel wird der kleinste Cursor neu gelesen (Nachspielen, neue Abonnements), unter der geteilten Sperre
  ``pruning.lock``, die ein Nachspielen exklusiv nimmt.
- **Ende des Journals:** Das neueste nummerierte Ereignis bleibt. Die Prüfung nach einer Wiederherstellung
  (``apps.events.wiederherstellung``) vergleicht Cursor mit dem Ende des Journals, neue Abonnements beginnen
  dort (``dispatch.ensure_subscription``).
- Zeilen **geparkter Ereignisse** bleiben einzeln stehen (die Wiederholung liest sie aus dem Journal), ebenso
  alle Zeilen ohne Folgenummer (Sequenzierer).

Festgehalten wird das Aufräumen in ``events_pruning`` (``apps.events.pruning``), im ersten Stapel in derselben
Transaktion wie dessen Löschen und für die ganze geplante Grenze. Bricht der Lauf ab, hält der Eintrag also eher
zu viel als zu wenig fest; für Leser mit eigenem Stand (Änderungsfeed) ist das die sichere Seite.

Laufen Aufräumen und Nachspielen gleichzeitig, stimmen sie sich über ``pruning.lock`` ab: Ein Löschschritt
sieht entweder den zurückgesetzten Cursor und lässt dessen Ereignisse stehen, oder er ist schon
festgeschrieben, bevor das Nachspielen den Cursor zurücksetzt. Dann fehlt, was er gelöscht hat, so wie bei
jedem Nachspielen in den aufgeräumten Teil; nachgespielt wird, was das Journal noch enthält
(``events_dispatch --replay`` weist darauf hin).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Max, Min, QuerySet
from django.utils import timezone

from . import pruning
from .dispatch import first_seq_since
from .models import Event, ParkedEvent, Subscription

logger = logging.getLogger(__name__)

#: Aufbewahrung des Journals in Tagen; kürzer lässt die Spezifikation (N7) nicht zu
DEFAULT_RETENTION_DAYS: Final = 90
MIN_RETENTION_DAYS: Final = 90
#: Zeilen je Löschschritt: kurze Transaktionen, keine lange Sperre
PURGE_BATCH: Final = 5000

#: Was die Grenze setzt
FRIST: Final = "frist"
ABONNEMENT: Final = "abonnement"
ENDE: Final = "ende"
LEER: Final = "leer"


def retention_days() -> int:
    """Aufbewahrung in Tagen: ``EVENTS_JOURNAL_RETENTION_DAYS``, mindestens die Gültigkeit der Feed-Cursor.

    Unter 90 Tagen (Spezifikation N7) gilt die Einstellung als Fehler: Aufgeräumt wird dann nicht, statt
    still mit einer kürzeren Frist zu löschen.
    """
    journal = int(getattr(settings, "EVENTS_JOURNAL_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    if journal < MIN_RETENTION_DAYS:
        raise ImproperlyConfigured(f"EVENTS_JOURNAL_RETENTION_DAYS muss mindestens {MIN_RETENTION_DAYS} sein.")
    feed = int(getattr(settings, "OPARL_CHANGES_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    return max(journal, feed)


def cutoff(now: datetime | None = None) -> datetime:
    """Stichtag: Zeilen, die davor erfasst wurden, dürfen gehen (Beginn eines Tages in UTC)."""
    jetzt = (now or timezone.now()).astimezone(UTC)
    tag = (jetzt - timedelta(days=retention_days())).date()
    return datetime(tag.year, tag.month, tag.day, tzinfo=UTC)


@dataclass(frozen=True)
class JournalPlan:
    """Wie weit das Journal aufgeräumt werden darf: Folgenummern unter ``boundary``."""

    cutoff: datetime
    boundary: int
    #: Was die Grenze setzt: ``frist``, ``abonnement``, ``ende`` (neuestes Ereignis) oder ``leer``
    limited_by: str
    #: Abonnement mit dem kleinsten Cursor, wenn es die Grenze setzt
    subscription: str | None = None

    @property
    def through_seq(self) -> int:
        """Höchste Folgenummer, die gelöscht werden darf (0: keine)."""
        return max(self.boundary - 1, 0)


def _kleinster_cursor() -> tuple[str, int] | None:
    zeile = Subscription.objects.order_by("cursor_seq", "name").values_list("name", "cursor_seq").first()
    return None if zeile is None else (str(zeile[0]), int(zeile[1]))


def plan(now: datetime | None = None) -> JournalPlan:
    """Berechnet die Grenze; ändert nichts."""
    stichtag = cutoff(now)
    ende = Event.objects.aggregate(h=Max("seq"))["h"] or 0
    if ende == 0:
        return JournalPlan(cutoff=stichtag, boundary=0, limited_by=LEER)
    schranken: list[tuple[int, str, str | None]] = []
    erste_junge = first_seq_since(stichtag)
    if erste_junge is not None:
        schranken.append((erste_junge, FRIST, None))
    kleinster = _kleinster_cursor()
    if kleinster is not None:
        schranken.append((kleinster[1] + 1, ABONNEMENT, kleinster[0]))
    schranken.append((ende, ENDE, None))
    grenze, grund, abonnement = min(schranken, key=lambda schranke: schranke[0])
    return JournalPlan(cutoff=stichtag, boundary=max(grenze, 0), limited_by=grund, subscription=abonnement)


def _loeschbar(grenze: int, ab: int = 0) -> QuerySet[Event]:
    """Nummerierte Zeilen in ``(ab, grenze)`` ohne geparkte Ereignisse."""
    geparkt = ParkedEvent.objects.filter(event_seq__lt=grenze).values("event_seq")
    return Event.objects.filter(seq__isnull=False, seq__gt=ab, seq__lt=grenze).exclude(seq__in=geparkt)


def count(journal_plan: JournalPlan) -> int:
    """Wie viele Zeilen ``purge`` nach diesem Plan löschen würde (Probelauf)."""
    if journal_plan.boundary <= 1:
        return 0
    return _loeschbar(journal_plan.boundary).count()


@dataclass(frozen=True)
class PurgeResult:
    deleted: int
    #: Der Lauf endete an der Zeitgrenze; der nächste setzt fort
    stopped_early: bool = False


def purge(
    journal_plan: JournalPlan,
    *,
    batch: int = PURGE_BATCH,
    max_seconds: float | None = None,
    pause: float = 0.0,
) -> PurgeResult:
    """Löscht nach ``journal_plan`` in Stapeln zu ``batch`` Zeilen, je Stapel eine Transaktion."""
    if batch < 1:
        raise ValueError("batch muss mindestens 1 sein")
    if journal_plan.boundary <= 1:
        return PurgeResult(deleted=0)
    ende = None if not max_seconds else time.monotonic() + max_seconds
    geloescht = 0
    letzte = 0
    festgehalten = False
    while True:
        if ende is not None and time.monotonic() >= ende:
            logger.info("Journal aufräumen: Zeitgrenze erreicht nach %s Zeilen; der nächste Lauf setzt fort", geloescht)
            return PurgeResult(deleted=geloescht, stopped_early=True)
        with transaction.atomic():
            # Erst die Abstimmung mit dem Nachspielen, dann den Cursor lesen (pruning.lock)
            pruning.lock(shared=True)
            kleinster = Subscription.objects.aggregate(m=Min("cursor_seq"))["m"]
            grenze = journal_plan.boundary if kleinster is None else min(journal_plan.boundary, kleinster + 1)
            schritt = list(_loeschbar(grenze, letzte).order_by("seq").values_list("pk", "seq")[:batch])
            if not schritt:
                break
            if not festgehalten:
                pruning.record(grenze - 1, journal_plan.cutoff)
                festgehalten = True
            Event.objects.filter(pk__in=[pk for pk, _ in schritt]).delete()
        geloescht += len(schritt)
        letzte = int(schritt[-1][1] or 0)
        if pause > 0:
            time.sleep(pause)
    if geloescht:
        logger.info(
            "Journal aufgeräumt: %s Zeilen bis Folgenummer %s (erfasst vor %s)",
            geloescht,
            letzte,
            journal_plan.cutoff.isoformat(),
        )
    return PurgeResult(deleted=geloescht)
