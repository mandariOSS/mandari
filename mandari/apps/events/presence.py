# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lebenszeichen der Worker-Prozesse in ``events_worker`` (Issue #508).

Ein Worker (``manage.py events_worker``, ``apps.events.worker``) meldet sich alle paar Sekunden
mit seinen Rollen und Warteschlangen, solange alle seine Rollen arbeiten, und meldet sich beim
Beenden ab. Wer wissen will, ob ein Worker läuft (Health, Admin), fragt ``live_workers()``: Als
lebend gilt, wer sich innerhalb von ``PRESENCE_TTL`` gemeldet hat. Ein abgestürzter Worker fällt
so nach spätestens einer Minute heraus; seine Zeile löscht der nächste startende Worker nach einem
Tag.

Zeitpunkte setzt und vergleicht die Datenbank (``Now()``), nicht die Uhr des Prozesses: Worker
auf mehreren Rechnern mit Uhrenversatz würden sonst fälschlich als ausgefallen gelten.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from django.db import IntegrityError, models, transaction
from django.db.models.functions import Now

from .models import WorkerProcess

#: So lange gilt ein Worker nach seiner letzten Meldung als lebend
PRESENCE_TTL: Final = timedelta(seconds=60)
#: Zeilen abgestürzter Worker werden nach dieser Zeit gelöscht
KEEP_STALE: Final = timedelta(days=1)


def _vor(dauer: timedelta) -> models.Expression:
    return models.ExpressionWrapper(Now() - dauer, output_field=models.DateTimeField())


def announce(holder: str, roles: Sequence[str], queues: Sequence[str]) -> None:
    """Meldet den Worker ``holder`` als lebend (legt seine Zeile an oder erneuert sie)."""
    werte = {"roles": list(roles), "queues": list(queues)}
    if WorkerProcess.objects.filter(holder=holder).update(seen_at=Now(), **werte):
        return
    try:
        with transaction.atomic():
            WorkerProcess.objects.create(holder=holder, **werte)
    except IntegrityError:
        # Gleichzeitig angelegt (dieselbe Kennung kommt nur nach einem Neustart per exec vor)
        WorkerProcess.objects.filter(holder=holder).update(seen_at=Now(), **werte)


def withdraw(holder: str) -> None:
    """Meldet den Worker beim Beenden ab."""
    WorkerProcess.objects.filter(holder=holder).delete()


def purge_stale(keep: timedelta = KEEP_STALE) -> int:
    """Löscht Zeilen von Workern, die sich seit ``keep`` nicht gemeldet haben; gibt deren Zahl zurück."""
    geloescht, _ = WorkerProcess.objects.filter(seen_at__lt=_vor(keep)).delete()
    return geloescht


def live_workers(ttl: timedelta = PRESENCE_TTL) -> list[WorkerProcess]:
    """Worker, die sich innerhalb von ``ttl`` gemeldet haben, älteste zuerst."""
    return list(WorkerProcess.objects.filter(seen_at__gte=_vor(ttl)).order_by("started_at", "holder"))


def live_roles(ttl: timedelta = PRESENCE_TTL) -> set[str]:
    """Rollen, die mindestens ein lebender Worker bedient."""
    return {rolle for worker in live_workers(ttl) for rolle in worker.roles}
