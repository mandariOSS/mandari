# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Metriken zu Aufträgen aus ``events_task`` (Registrierung in ``EventsConfig.ready``).

Gemessen wird beim Abruf von ``/metrics`` in der Datenbank, deshalb sehen die Webprozesse auch die
Arbeit des Runners:

- ``mandari_tasks_queued{queue}``: fällige wartende Aufträge (Rückstand).
- ``mandari_tasks_oldest_queued_seconds{queue}``: wie lange der älteste fällige Auftrag schon wartet.
- ``mandari_tasks_running{queue}``: laufende Aufträge.
- ``mandari_tasks_dead{queue}``: in den letzten 24 Stunden tot oder endgültig fehlgeschlagen beendete
  Aufträge. Alarm bei mehr als null; er erlischt nach einem Tag von selbst, die Ursache steht im
  Protokoll des Runners.

Nur im Prozess des Runners (``manage.py events_tasks``) und ab dem Start bei null:

- ``mandari_tasks_duration_seconds{queue}``: Laufzeit je Versuch.
- ``mandari_tasks_failed_total{queue,grund}``: gescheiterte Versuche (``fehler``, ``endgueltig``,
  ``zeitgrenze``, ``sperre_abgelaufen``).
- ``mandari_worker_rss_bytes{role}``: belegter Arbeitsspeicher (RSS) des Runners.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Final

from django.db.models import Count, Min, Q
from django.db.models.functions import Now
from django.utils import timezone
from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric

from apps.common.metrics import MisstErstBeimAbruf

from .models import Task, TaskStatus

logger = logging.getLogger(__name__)

#: Zeitfenster für ``mandari_tasks_dead``
DEAD_WINDOW: Final = timedelta(hours=24)

TASK_DURATION = Histogram(
    "mandari_tasks_duration_seconds",
    "Laufzeit eines Auftragsversuchs (Sekunden)",
    ["queue"],
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0),
)
TASKS_FAILED = Counter("mandari_tasks_failed_total", "Gescheiterte Auftragsversuche nach Grund", ["queue", "grund"])
# Mit Label, damit Prozesse ohne Runner (Web) keinen Wert 0 melden
WORKER_RSS = Gauge("mandari_worker_rss_bytes", "Belegter Arbeitsspeicher des Runners (RSS, Bytes)", ["role"]).labels(
    role="tasks"
)


class TaskCollector(MisstErstBeimAbruf):
    def collect(self) -> Iterator[Metric]:
        tot_seit = timezone.now() - DEAD_WINDOW
        try:
            je_status = list(
                Task.objects.filter(
                    Q(status=TaskStatus.LAEUFT)
                    | Q(status__in=[TaskStatus.FEHLGESCHLAGEN, TaskStatus.TOT], finished_at__gte=tot_seit)
                )
                .values("queue")
                .annotate(
                    laufend=Count("id", filter=Q(status=TaskStatus.LAEUFT)),
                    tot=Count("id", filter=Q(status__in=[TaskStatus.FEHLGESCHLAGEN, TaskStatus.TOT])),
                )
                .order_by()
            )
            faellig = list(
                Task.objects.filter(status=TaskStatus.WARTEND, run_after__lte=Now())
                .values("queue")
                .annotate(anzahl=Count("id"), seit=Min("run_after"))
                .order_by()
            )
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie abbrechen (z. B. Migration ausstehend)
            logger.debug("Aufträge nicht messbar", exc_info=True)
            return

        jetzt = timezone.now()
        wartend = GaugeMetricFamily("mandari_tasks_queued", "Fällige wartende Aufträge", labels=["queue"])
        alter = GaugeMetricFamily(
            "mandari_tasks_oldest_queued_seconds", "Wartezeit des ältesten fälligen Auftrags", labels=["queue"]
        )
        for rueckstand in faellig:
            wartend.add_metric([rueckstand["queue"]], rueckstand["anzahl"])
            alter.add_metric([rueckstand["queue"]], max((jetzt - rueckstand["seit"]).total_seconds(), 0.0))
        laufend = GaugeMetricFamily("mandari_tasks_running", "Laufende Aufträge", labels=["queue"])
        tot = GaugeMetricFamily(
            "mandari_tasks_dead", "Tote und fehlgeschlagene Aufträge der letzten 24 Stunden", labels=["queue"]
        )
        for stand in je_status:
            laufend.add_metric([stand["queue"]], stand["laufend"])
            tot.add_metric([stand["queue"]], stand["tot"])
        yield from (wartend, alter, laufend, tot)


def register() -> None:
    # ValueError: bereits registriert (z. B. erneutes ready() in Tests)
    with contextlib.suppress(ValueError):
        REGISTRY.register(_COLLECTOR)


_COLLECTOR = TaskCollector()
