# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Leader-Rolle ``scheduler``: legt für fällige Zeitpläne Aufträge an (``apps.events.schedule``).

Genau ein Prozess plant (Lease ``scheduler`` in ``events_lease``, ohne sitzungsgebundene Sperren).
Je Zeitplan steht der zuletzt geplante Termin in ``events_schedule``. Ein Durchlauf je Zeitplan:

1. jüngsten Termin bis jetzt berechnen; liegt er nicht hinter dem gespeicherten, ist nichts zu tun;
2. in **einer** Transaktion: Lease prüfen und sperren (``leases.fence``), Stand sperren, Auftrag
   anlegen (Idempotenzschlüssel aus Name und Termin) und den Termin festschreiben.

Übernimmt ein anderer Prozess die Lease, wartet er, bis diese Transaktion abgeschlossen ist, und
sieht danach den neuen Stand; ein Prozess, der die Lease verloren hat, legt nichts mehr an. So
entsteht je Termin genau ein Auftrag, auch mit mehreren Workern. Ausgeführt wird er vom Runner.

Aufträge landen immer in ``events_task`` (``JournalBackend``), auch wenn die Webprozesse noch das
sofort ausführende Backend nutzen: Der Scheduler läuft nur zusammen mit dem Runner.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.tasks import TaskResult
from django.utils import timezone

from . import leases
from .models import ScheduleState
from .schedule import Schedule, ScheduleRegistry
from .schedule import registry as standard_register
from .tasks_backend import JournalBackend, journal_backend

logger = logging.getLogger(__name__)

#: Name der Leader-Lease
LEASE_NAME = "scheduler"
#: Wartezeit zwischen zwei Durchläufen im Dauerbetrieb (Sekunden)
POLL_INTERVAL = 5.0


@dataclass
class Scheduler:
    """Leader-Rolle Zeitpläne für einen Prozess (Befehle ``events_scheduler`` und ``events_worker``)."""

    registry: ScheduleRegistry = field(default_factory=lambda: standard_register)
    holder: str = field(default_factory=leases.new_holder_id)
    backend_alias: str = "default"
    is_leader: bool = False
    _renewed_at: float = field(default=0.0, init=False, repr=False)
    _backend: JournalBackend | None = field(default=None, init=False, repr=False)

    @property
    def backend(self) -> JournalBackend:
        if self._backend is None:
            self._backend = journal_backend(self.backend_alias)
        return self._backend

    def ensure_lease(self) -> bool:
        """Übernimmt die Lease oder erneuert sie, wenn die Erneuerung fällig ist."""
        faellig = time.monotonic() - self._renewed_at >= leases.RENEW_INTERVAL.total_seconds()
        if self.is_leader and not faellig:
            return True
        war_leader = self.is_leader
        self.is_leader = leases.acquire(LEASE_NAME, self.holder)
        if self.is_leader:
            self._renewed_at = time.monotonic()
            if not war_leader:
                logger.info("Zeitpläne: Lease übernommen (%s)", self.holder)
        elif war_leader:
            logger.warning("Zeitpläne: Lease an einen anderen Prozess verloren (%s)", self.holder)
        return self.is_leader

    def release(self) -> None:
        if self.is_leader:
            leases.release(LEASE_NAME, self.holder)
            self.is_leader = False

    def tick(self, now: datetime | None = None) -> list[str]:
        """Ein Durchlauf; gibt die Namen der Zeitpläne zurück, für die ein Auftrag entstand."""
        if not self.ensure_lease():
            return []
        jetzt = now or timezone.now()
        # Meist ist nichts fällig: alle Stände in einer Abfrage ohne Sperren lesen, im Zweifel in
        # ``_plan`` unter Sperre genau prüfen
        staende: dict[str, datetime] = dict(ScheduleState.objects.values_list("name", "last_slot"))
        angelegt: list[str] = []
        for eintrag in self.registry:
            try:
                if self._plan(eintrag, jetzt, staende.get(eintrag.name)):
                    angelegt.append(eintrag.name)
            except leases.LeaseLostError:
                self.is_leader = False
                logger.warning("Zeitpläne: Lease während des Durchlaufs verloren (%s)", self.holder)
                break
            except DatabaseError:
                raise  # Verbindung neu aufbauen (``run``); betrifft alle Zeitpläne
            except Exception:  # noqa: BLE001 – ein fehlerhafter Zeitplan darf die übrigen nicht aufhalten
                logger.exception("Zeitplan %s: Planung gescheitert, die übrigen laufen weiter", eintrag.name)
        return angelegt

    def _plan(self, eintrag: Schedule, jetzt: datetime, zuletzt: datetime | None) -> bool:
        termin = eintrag.trigger.latest(jetzt)
        if zuletzt is not None and termin <= zuletzt:
            return False
        with transaction.atomic():
            leases.fence(LEASE_NAME, self.holder)
            stand = ScheduleState.objects.select_for_update().filter(name=eintrag.name).first()
            if stand is None:
                # Neu registriert: erst ab dem nächsten Termin, kein Lauf beim Deploy
                ScheduleState.objects.create(name=eintrag.name, last_slot=termin)
                logger.info("Zeitplan %s registriert (%s)", eintrag.name, eintrag.trigger.describe())
                return False
            if termin <= stand.last_slot:
                return False

            faellig = eintrag.is_due(termin, jetzt)
            if faellig:
                ergebnis: TaskResult[Any, Any] = self.backend.enqueue_once(
                    eintrag.task,
                    f"zeitplan:{eintrag.name}:{termin.isoformat()}",
                    eintrag.args,
                    dict(eintrag.kwargs),
                )
                stand.last_task_id = ergebnis.id
                logger.info("Zeitplan %s: Auftrag %s für %s", eintrag.name, ergebnis.id, termin.isoformat())
            else:
                logger.info("Zeitplan %s: Termin %s verpasst und ausgelassen", eintrag.name, termin.isoformat())
            stand.last_slot = termin
            stand.save(update_fields=["last_slot", "last_task_id", "updated_at"])
        return faellig

    def run(
        self, stop: threading.Event, interval: float = POLL_INTERVAL, beat: Callable[[], None] | None = None
    ) -> None:
        """Dauerbetrieb bis ``stop`` gesetzt ist; gibt am Ende die Lease frei.

        Fällt die Datenbank kurz aus, wird der Fehler protokolliert und nach ``interval`` erneut
        versucht. ``beat`` meldet jeden Durchlauf als Lebenszeichen (``events_worker``).
        """
        try:
            while not stop.is_set():
                if beat is not None:
                    beat()
                close_old_connections()
                try:
                    self.tick()
                except DatabaseError:
                    logger.warning("Zeitpläne: Datenbankfehler, neuer Versuch folgt", exc_info=True)
                    self.is_leader = False
                    connection.close()
                stop.wait(interval)
        finally:
            try:
                self.release()
            except DatabaseError:
                logger.warning("Zeitpläne: Lease konnte nicht freigegeben werden", exc_info=True)
