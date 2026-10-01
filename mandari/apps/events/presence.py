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

**Braucht die Installation einen Worker?** (``required_roles``, Issue #509) Nur dann melden Health
und Admin sein Fehlen; sonst stünde jede bestehende Installation ohne Worker sofort auf
„degraded“. ``EVENTS_WORKER_REQUIRED=true`` verlangt alle Rollen, ``false`` keine. Ohne Angabe
gilt: Laufen Aufträge über das Journal (``TASKS_BACKEND=journal``), braucht es die Rolle ``tasks``,
sonst blieben sie liegen; schreibt der Ingestor Ereignisse (``INGESTOR_EVENTS_ENABLED``), die Rolle
``sequencer``, sonst bekämen sie keine Folgenummer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Now

from .models import WorkerProcess

#: Alle Rollen (wie ``apps.events.worker.ROLES``; hier ohne Import des Workers)
ALL_ROLES: Final = frozenset({"sequencer", "dispatch", "tasks", "scheduler"})
_JOURNAL_BACKEND: Final = "apps.events.tasks_backend.JournalBackend"

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


def required_roles() -> frozenset[str]:
    """Rollen, die ein Worker dieser Installation bedienen muss; leer = kein Worker nötig."""
    wert = str(getattr(settings, "EVENTS_WORKER_REQUIRED", "") or "").strip().lower()
    if wert in ("true", "1", "yes"):
        return ALL_ROLES
    if wert in ("false", "0", "no"):
        return frozenset()
    rollen: set[str] = set()
    backend = str(dict(getattr(settings, "TASKS", {}).get("default", {})).get("BACKEND", ""))
    if backend == _JOURNAL_BACKEND:
        rollen.add("tasks")
    if getattr(settings, "INGESTOR_EVENTS_ENABLED", False):
        rollen.add("sequencer")
    return frozenset(rollen)


@dataclass(frozen=True)
class WorkerStatus:
    """Was die Installation braucht und was die lebenden Worker bedienen."""

    required: frozenset[str]
    workers: tuple[WorkerProcess, ...]

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(rolle for worker in self.workers for rolle in worker.roles)

    @property
    def missing(self) -> frozenset[str]:
        """Nötige Rollen, die kein lebender Worker bedient."""
        return self.required - self.roles

    @property
    def degraded(self) -> bool:
        return bool(self.missing)


def worker_status(ttl: timedelta = PRESENCE_TTL) -> WorkerStatus:
    """Stand für Health und Admin: nötige Rollen und lebende Worker."""
    return WorkerStatus(required=required_roles(), workers=tuple(live_workers(ttl)))
