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

**Braucht die Installation einen Worker?** (``required_roles``, Issues #509, #515) Ja: Seit die
wiederkehrende Arbeit (Erinnerungen und Einladungen zu Fraktionssitzungen, Verortung, Aufräumen)
als Zeitpläne im Worker läuft und nicht mehr in einem Faden im Webprozess, braucht jede Installation
die Rollen ``tasks`` und ``scheduler``; ohne sie fiele diese Arbeit still aus. Schreibt der Ingestor
Ereignisse (``INGESTOR_EVENTS_ENABLED``), zusätzlich ``sequencer``, sonst bekämen sie keine
Folgenummer. ``EVENTS_WORKER_REQUIRED=true`` verlangt alle Rollen, ``false`` keine (etwa eine
Vorführinstanz ohne Worker).

**Abdeckung je Warteschlange:** Ist ``tasks`` nötig, müssen die lebenden Worker mit dieser Rolle
zusammen jede Warteschlange bedienen, in der Aufträge entstehen (``required_queues``; ausgenommen
Warteschlangen mit Parallelität 0): mit ``TASKS_BACKEND=journal`` jede des Backends, sonst die der
Zeitpläne und der Aufträge, die immer im Journal landen (``ALWAYS_JOURNAL_QUEUES``). Laufen
Texterkennung und KI in einem eigenen Worker (``--queues ocr,ai``) und fällt der aus, meldet die
Prüfung genau diese Warteschlangen.
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

#: Warteschlangen der Aufträge, die unabhängig von ``TASKS_BACKEND`` im Journal landen (Admin: Sync einer
#: Quelle, Löschen einer Kommune); die der Zeitpläne kommen aus dem Register hinzu
ALWAYS_JOURNAL_QUEUES: Final = frozenset({"default"})

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
    # Zeitpläne für die wiederkehrende Arbeit, Runner für ihre Aufträge (und alle übrigen mit Journal)
    rollen: set[str] = {"tasks", "scheduler"}
    if getattr(settings, "INGESTOR_EVENTS_ENABLED", False):
        rollen.add("sequencer")
    return frozenset(rollen)


def _journal_backend_active() -> bool:
    return str(dict(getattr(settings, "TASKS", {}).get("default", {})).get("BACKEND", "")) == _JOURNAL_BACKEND


def schedule_queues() -> frozenset[str]:
    """Warteschlangen der registrierten Zeitpläne (lädt ``schedules.py`` aller Apps)."""
    from .schedule import autodiscover, registry

    autodiscover()
    return frozenset(eintrag.task.queue_name for eintrag in registry)


def required_queues() -> frozenset[str]:
    """Warteschlangen, die die Worker mit der Rolle ``tasks`` zusammen bedienen müssen.

    Mit ``TASKS_BACKEND=journal`` alle des Backends, sonst die der Zeitpläne und
    ``ALWAYS_JOURNAL_QUEUES``; jeweils ohne die mit Parallelität 0 (bewusst abgeschaltet).
    """
    from .tasks_backend import journal_options

    optionen, queues = journal_options()
    kandidaten = frozenset(queues) if _journal_backend_active() else ALWAYS_JOURNAL_QUEUES | schedule_queues()
    return frozenset(queue for queue in kandidaten if optionen.concurrency.get(queue, 1) > 0)


@dataclass(frozen=True)
class WorkerStatus:
    """Was die Installation braucht und was die lebenden Worker bedienen."""

    required: frozenset[str]
    workers: tuple[WorkerProcess, ...]
    #: Warteschlangen, die die Rolle ``tasks`` abdecken muss (nur, wenn ``tasks`` nötig ist)
    queues: frozenset[str] = frozenset()

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(rolle for worker in self.workers for rolle in worker.roles)

    @property
    def missing(self) -> frozenset[str]:
        """Nötige Rollen, die kein lebender Worker bedient."""
        return self.required - self.roles

    @property
    def missing_queues(self) -> frozenset[str]:
        """Nötige Warteschlangen, die kein lebender Worker mit der Rolle ``tasks`` bedient.

        Leer, wenn ``tasks`` nicht nötig ist oder ganz fehlt (das meldet schon ``missing``). Ein
        Worker ohne ``--queues`` bedient alle.
        """
        if "tasks" not in self.required or "tasks" in self.missing:
            return frozenset()
        bedient: set[str] = set()
        for worker in self.workers:
            if "tasks" not in worker.roles:
                continue
            if not worker.queues:
                return frozenset()
            bedient.update(worker.queues)
        return self.queues - bedient

    @property
    def degraded(self) -> bool:
        return bool(self.missing or self.missing_queues)

    def missing_summary(self) -> str:
        """Was fehlt, als fester Text für Health und Admin, z. B. ``tasks (Warteschlangen ai, ocr)``."""
        teile = sorted(self.missing)
        if self.missing_queues:
            teile.append(f"tasks (Warteschlangen {', '.join(sorted(self.missing_queues))})")
        return ", ".join(teile)


def worker_status(ttl: timedelta = PRESENCE_TTL, required: frozenset[str] | None = None) -> WorkerStatus:
    """Stand für Health und Admin: nötige Rollen und Warteschlangen, lebende Worker.

    ``required`` übernimmt einen schon ermittelten Bedarf (``required_roles``).
    """
    rollen = required_roles() if required is None else required
    queues = required_queues() if "tasks" in rollen else frozenset()
    return WorkerStatus(required=rollen, workers=tuple(live_workers(ttl)), queues=queues)
