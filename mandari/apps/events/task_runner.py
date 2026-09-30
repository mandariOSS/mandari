# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Runner für Aufträge aus ``events_task`` (Befehl ``events_tasks``; später Rolle ``tasks`` in
``events_worker``). Grundlage: ``docs/adr/20260929-auftraege-und-zeitplaene.md``.

Aufbau: ein Koordinator (der aufrufende Thread) und je Warteschlange so viele Ausführungs-Threads,
wie ihre Parallelität erlaubt. Nur der Koordinator schreibt in ``events_task``: Er holt Aufträge,
verlängert Sperren, schreibt Ergebnisse und räumt auf. Die Ausführungs-Threads führen nur den
Auftrag aus und melden das Ergebnis zurück.

- **Holen:** ``SELECT … FOR UPDATE SKIP LOCKED``. Der Auftrag bekommt den Status „läuft“, einen
  weiteren Versuch und die Sperrfrist ``locked_until`` (Datenbankzeit). Solange er läuft, verlängert
  der Koordinator die Sperre. Stirbt der Prozess, läuft sie ab, und jeder Runner gibt den Auftrag
  wieder frei (mit Wartezeit) oder erklärt ihn nach dem letzten Versuch für tot.
- **Abgrenzung:** Ein Ergebnis wird nur geschrieben, solange der Auftrag „läuft“ und die Zahl der
  Versuche noch die des eigenen Versuchs ist. Hat ein anderer Runner übernommen, wird nichts
  überschrieben.
- **Fehler:** Wiederholung mit wachsender Wartezeit bis ``max_attempts``, danach „tot“ (Meldung im
  Protokoll und in ``mandari_tasks_dead``). ``PermanentTaskError``: sofort „fehlgeschlagen“.
- **Zeitgrenze je Auftragstyp:** Läuft ein Auftrag zu lange, gilt sein Versuch als gescheitert
  (Ergebniscode ``zeitgrenze``). Einen Thread kann Python nicht abbrechen; der Runner nimmt deshalb
  nichts Neues mehr an, lässt die übrigen Aufträge zu Ende laufen und startet den Prozess neu. Bis
  dahin bleibt der Auftrag gesperrt, damit kein anderer Runner ihn parallel zum hängenden Thread
  ausführt; nach dem Neustart läuft die Sperre ab, und er wird wiederholt. Wird der Thread vor dem
  Neustart doch fertig, gilt sein Ergebnis.
- **Neustart** auch nach ``max_tasks_per_process`` Aufträgen und oberhalb der Speichergrenze (RSS).
  Jeder Neustart wartet auf den längsten laufenden Auftrag des Prozesses und hält bis dahin alle seine
  Warteschlangen an. Warteschlangen mit langen Aufträgen (``ocr``, ``ai``) gehören deshalb in einen
  eigenen Runner (``--queues``), damit ``mail`` und ``default`` nicht bis zu deren Zeitgrenze stehen.
- **Aufbewahrung:** erledigte Aufträge 14 Tage, fehlgeschlagene und tote 90 Tage.
- **Metriken** dieses Prozesses: ``mandari_tasks_duration_seconds``, ``mandari_tasks_failed_total``,
  ``mandari_worker_rss_bytes`` (``apps.events.task_metrics``).

Zustellung mindestens einmal: Stirbt der Prozess nach dem Auftrag, aber vor dem Festschreiben des
Ergebnisses, läuft der Auftrag noch einmal. Aufträge müssen deshalb idempotent sein.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import random
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any, Final

from django.db import DatabaseError, close_old_connections, connection, models, transaction
from django.db.models.functions import Now
from django.tasks import Task, TaskContext, TaskResult, TaskResultStatus
from django.tasks.base import TaskError
from django.tasks.signals import task_finished, task_started
from django.utils import timezone
from django.utils.module_loading import import_string

from . import leases
from .models import Task as TaskRow
from .models import TaskStatus
from .task_metrics import TASK_DURATION, TASKS_FAILED, WORKER_RSS
from .tasks_backend import RESULT_OK, JournalBackend, JournalOptions, PermanentTaskError, journal_options

logger = logging.getLogger(__name__)

#: Sperrfrist eines laufenden Auftrags ohne Verlängerung (Wiederanlauf nach Absturz)
LOCK_TTL: Final = timedelta(seconds=60)
#: So oft verlängert der Koordinator die Sperren laufender Aufträge (Sekunden)
RENEW_INTERVAL: Final = 15.0
#: Wartezeit zwischen zwei Abfragen im Leerlauf (Sekunden); ein fertiger Auftrag weckt sofort
POLL_INTERVAL: Final = 2.0
#: So oft werden abgelaufene Sperren anderer Runner freigegeben (Sekunden)
MAINTENANCE_INTERVAL: Final = 30.0
#: So oft werden alte Aufträge gelöscht (Sekunden)
PURGE_INTERVAL: Final = 3600.0
#: Wartezeit vor dem zweiten Versuch; verdoppelt sich je Versuch bis ``RETRY_MAX``
RETRY_BASE: Final = timedelta(seconds=10)
RETRY_MAX: Final = timedelta(hours=1)
KEEP_DONE: Final = timedelta(days=14)
KEEP_FAILED: Final = timedelta(days=90)
PURGE_BATCH: Final = 5000

#: Ergebniscodes ohne Ausnahmeklasse
CODE_TIMEOUT: Final = "zeitgrenze"
CODE_LOCK_EXPIRED: Final = "sperre_abgelaufen"
CODE_UNKNOWN_TASK: Final = "auftrag_unbekannt"
CODE_ABORTED: Final = "abgebrochen"


class StopReason(enum.StrEnum):
    """Warum ``TaskRunner.run`` zurückkehrt."""

    STOP = "stop"
    LEER = "leer"
    ANZAHL = "anzahl"
    SPEICHER = "speicher"
    ZEITGRENZE = "zeitgrenze"

    @property
    def restart(self) -> bool:
        """Der Prozess soll sich neu starten (statt sich zu beenden)."""
        return self in {StopReason.ANZAHL, StopReason.SPEICHER, StopReason.ZEITGRENZE}


@dataclass(frozen=True)
class ClaimedTask:
    """Ein geholter Auftrag mit der Nummer des eigenen Versuchs."""

    id: uuid.UUID
    queue: str
    task_path: str
    args: list[Any]
    kwargs: dict[str, Any]
    attempt: int
    max_attempts: int
    enqueued_at: datetime | None
    timeout: float


@dataclass(frozen=True)
class Outcome:
    """Ergebnis eines Versuchs: ``code`` ist ``ok``, eine Ausnahmeklasse oder ein fester Code."""

    code: str
    retry: bool = True

    @property
    def ok(self) -> bool:
        return self.code == RESULT_OK


SUCCESS: Final = Outcome(RESULT_OK)


def _ablauf(dauer: timedelta) -> models.Expression:
    return models.ExpressionWrapper(Now() + dauer, output_field=models.DateTimeField())


def _klassenpfad(exc: BaseException) -> str:
    art = type(exc)
    return f"{art.__module__}.{art.__qualname__}"


def retry_delay(attempt: int) -> timedelta:
    """Wartezeit nach dem Versuch ``attempt``: 10 s, 20 s, 40 s … höchstens eine Stunde, leicht gestreut."""
    grund = min(RETRY_MAX.total_seconds(), RETRY_BASE.total_seconds() * 2 ** max(0, attempt - 1))
    return timedelta(seconds=grund * random.uniform(1.0, 1.2))


# ---------------------------------------------------------------------------
# Einzelschritte (auch ohne Koordinator nutzbar, z. B. in Tests)
# ---------------------------------------------------------------------------


def queue_ready(queue_name: str) -> bool:
    """Gibt es in der Warteschlange einen fälligen Auftrag? (Teilindex ``events_task_ready``)"""
    return TaskRow.objects.filter(queue=queue_name, status=TaskStatus.WARTEND, run_after__lte=Now()).exists()


def claim(queue_name: str, config: JournalOptions, lock_ttl: timedelta = LOCK_TTL) -> ClaimedTask | None:
    """Holt den nächsten fälligen Auftrag der Warteschlange oder ``None``.

    Reihenfolge: höhere Priorität zuerst, dann frühestes ``run_after``. Gesperrte Zeilen, die gerade
    ein anderer Runner holt, werden übersprungen.
    """
    with transaction.atomic():
        zeile = (
            TaskRow.objects.select_for_update(skip_locked=True)
            .filter(queue=queue_name, status=TaskStatus.WARTEND, run_after__lte=Now())
            .order_by("-priority", "run_after", "created_at")
            .only("id", "queue", "task_path", "args", "attempts", "max_attempts", "created_at")
            .first()
        )
        if zeile is None:
            return None
        versuch = zeile.attempts + 1
        # Bedingung auf Status und Versuche: ohne Zeilensperren (SQLite) holt so nur einer den Auftrag
        # Ergebniscode leeren: Ab jetzt gilt er für diesen Versuch (z. B. ``zeitgrenze``)
        geholt = TaskRow.objects.filter(pk=zeile.pk, status=TaskStatus.WARTEND, attempts=zeile.attempts).update(
            status=TaskStatus.LAEUFT, attempts=versuch, locked_until=_ablauf(lock_ttl), result_code=None
        )
        if not geholt:
            return None
    gespeichert = zeile.args if isinstance(zeile.args, dict) else {}
    return ClaimedTask(
        id=zeile.pk,
        queue=zeile.queue,
        task_path=zeile.task_path,
        args=list(gespeichert.get("args", [])),
        kwargs=dict(gespeichert.get("kwargs", {})),
        attempt=versuch,
        max_attempts=zeile.max_attempts,
        enqueued_at=zeile.created_at,
        timeout=config.timeout_for(zeile.task_path, zeile.queue),
    )


def execute(claimed: ClaimedTask, *, backend_alias: str = "default", worker_id: str = "direkt") -> Outcome:
    """Führt den Auftrag aus und meldet das Ergebnis; schreibt nichts in ``events_task``."""
    try:
        ziel = import_string(claimed.task_path)
    except ImportError:
        # Wiederholbar: Während eines Updates kann ein älterer Worker einen neuen Auftragstyp holen
        logger.error("Auftrag %s: Auftragstyp %s nicht gefunden", claimed.id, claimed.task_path)
        return Outcome(CODE_UNKNOWN_TASK)
    if not isinstance(ziel, Task):
        logger.error("Auftrag %s: %s ist keine @task-Funktion", claimed.id, claimed.task_path)
        return Outcome(CODE_UNKNOWN_TASK, retry=False)

    beginn = timezone.now()
    ergebnis: TaskResult[Any, Any] = TaskResult(
        task=ziel,
        id=str(claimed.id),
        status=TaskResultStatus.RUNNING,
        enqueued_at=claimed.enqueued_at,
        started_at=beginn,
        last_attempted_at=beginn,
        finished_at=None,
        args=claimed.args,
        kwargs=claimed.kwargs,
        backend=backend_alias,
        errors=[],
        worker_ids=[worker_id] * claimed.attempt,
    )
    task_started.send(sender=JournalBackend, task_result=ergebnis)
    start = time.monotonic()
    try:
        if ziel.takes_context:
            ziel.call(TaskContext(task_result=ergebnis), *claimed.args, **claimed.kwargs)
        else:
            ziel.call(*claimed.args, **claimed.kwargs)
    except PermanentTaskError as exc:
        logger.warning("Auftrag %s (%s) endgültig gescheitert", claimed.id, claimed.task_path, exc_info=True)
        ausgang = Outcome(_klassenpfad(exc), retry=False)
        TASKS_FAILED.labels(queue=claimed.queue, grund="endgueltig").inc()
    except Exception as exc:  # noqa: BLE001 – jeder Fehler eines Auftrags wird protokolliert und wiederholt
        logger.exception("Auftrag %s (%s) Versuch %d gescheitert", claimed.id, claimed.task_path, claimed.attempt)
        ausgang = Outcome(_klassenpfad(exc))
        TASKS_FAILED.labels(queue=claimed.queue, grund="fehler").inc()
    else:
        ausgang = SUCCESS
        logger.info("Auftrag %s (%s) erledigt in %.1f s", claimed.id, claimed.task_path, time.monotonic() - start)
    TASK_DURATION.labels(queue=claimed.queue).observe(time.monotonic() - start)

    object.__setattr__(ergebnis, "finished_at", timezone.now())
    object.__setattr__(ergebnis, "status", TaskResultStatus.SUCCESSFUL if ausgang.ok else TaskResultStatus.FAILED)
    if not ausgang.ok:
        ergebnis.errors.append(TaskError(exception_class_path=ausgang.code, traceback=""))
    task_finished.send(sender=JournalBackend, task_result=ergebnis)
    return ausgang


def finish(claimed: ClaimedTask, outcome: Outcome) -> str | None:
    """Schreibt das Ergebnis des eigenen Versuchs; gibt den neuen Status zurück.

    ``None``: Der Versuch gilt nicht mehr (Sperre abgelaufen, freigegeben oder von einem anderen Runner
    übernommen); es wird nichts geändert.
    """
    werte: dict[str, Any] = {"locked_until": None, "result_code": outcome.code}
    if outcome.ok:
        status = TaskStatus.ERLEDIGT
    elif outcome.retry and claimed.attempt < claimed.max_attempts:
        status = TaskStatus.WARTEND
        werte["run_after"] = _ablauf(retry_delay(claimed.attempt))
    elif outcome.retry:
        status = TaskStatus.TOT
    else:
        status = TaskStatus.FEHLGESCHLAGEN
    if status != TaskStatus.WARTEND:
        werte["finished_at"] = Now()

    geschrieben = TaskRow.objects.filter(pk=claimed.id, status=TaskStatus.LAEUFT, attempts=claimed.attempt).update(
        status=status, **werte
    )
    if not geschrieben:
        logger.warning("Auftrag %s: Versuch %d gilt nicht mehr, Ergebnis verworfen", claimed.id, claimed.attempt)
        return None
    if status == TaskStatus.TOT:
        logger.error(
            "Auftrag %s (%s) ist nach %d Versuchen tot (%s)",
            claimed.id,
            claimed.task_path,
            claimed.attempt,
            outcome.code,
        )
    elif status == TaskStatus.FEHLGESCHLAGEN:
        logger.error("Auftrag %s (%s) fehlgeschlagen (%s)", claimed.id, claimed.task_path, outcome.code)
    return status


def release(claimed: ClaimedTask) -> bool:
    """Gibt einen unterbrochenen Auftrag sofort wieder frei; der Versuch zählt nicht (Beenden erzwungen)."""
    return bool(
        TaskRow.objects.filter(pk=claimed.id, status=TaskStatus.LAEUFT, attempts=claimed.attempt).update(
            status=TaskStatus.WARTEND,
            attempts=models.F("attempts") - 1,
            run_after=Now(),
            locked_until=None,
            result_code=CODE_ABORTED,
        )
    )


def mark_timed_out(claimed: ClaimedTask) -> bool:
    """Vermerkt die Zeitüberschreitung des eigenen Versuchs (Ergebniscode ``zeitgrenze``).

    Der Auftrag bleibt „läuft“ und gesperrt, solange der Prozess mit dem hängenden Thread lebt; sonst
    könnte ein anderer Runner ihn nach der Wartezeit parallel ein zweites Mal ausführen. Nach dem
    Neustart läuft die Sperre ab, und ``release_expired`` gibt ihn zur Wiederholung frei.
    """
    return bool(
        TaskRow.objects.filter(pk=claimed.id, status=TaskStatus.LAEUFT, attempts=claimed.attempt).update(
            result_code=CODE_TIMEOUT
        )
    )


def renew_locks(claimed: Sequence[ClaimedTask], lock_ttl: timedelta = LOCK_TTL) -> int:
    """Verlängert die Sperren laufender eigener Versuche; gibt die Zahl der noch eigenen zurück."""
    if not claimed:
        return 0
    bedingung = models.Q()
    for auftrag in claimed:
        bedingung |= models.Q(pk=auftrag.id, attempts=auftrag.attempt)
    return TaskRow.objects.filter(bedingung, status=TaskStatus.LAEUFT).update(locked_until=_ablauf(lock_ttl))


def release_expired(limit: int = 100) -> int:
    """Gibt Aufträge mit abgelaufener Sperre frei (Runner abgestürzt oder hängt); gibt deren Zahl zurück.

    Der Versuch zählt: Ein Auftrag, der den Prozess jedes Mal abstürzen lässt, ist nach
    ``max_attempts`` tot. Hatte der Versuch die Zeitgrenze überschritten, bleibt der Ergebniscode
    ``zeitgrenze`` (sein Runner hat ihn bis zum eigenen Neustart gesperrt gehalten).
    """
    with transaction.atomic():
        abgelaufen = list(
            TaskRow.objects.select_for_update(skip_locked=True)
            .filter(status=TaskStatus.LAEUFT, locked_until__lt=Now())
            .values_list("pk", "attempts", "max_attempts", "task_path", "queue", "result_code")[:limit]
        )
        for pk, versuche, hoechstens, pfad, warteschlange, code in abgelaufen:
            zeile = TaskRow.objects.filter(pk=pk, status=TaskStatus.LAEUFT, attempts=versuche)
            if code != CODE_TIMEOUT:  # Zeitüberschreitungen hat ihr Runner schon gezählt
                code = CODE_LOCK_EXPIRED
                TASKS_FAILED.labels(queue=warteschlange, grund="sperre_abgelaufen").inc()
            if versuche >= hoechstens:
                zeile.update(status=TaskStatus.TOT, finished_at=Now(), locked_until=None, result_code=code)
                logger.error("Auftrag %s (%s): Sperre abgelaufen, nach %d Versuchen tot", pk, pfad, versuche)
            else:
                zeile.update(
                    status=TaskStatus.WARTEND,
                    run_after=_ablauf(retry_delay(versuche)),
                    locked_until=None,
                    result_code=code,
                )
                logger.warning("Auftrag %s (%s): Sperre abgelaufen, wird wiederholt", pk, pfad)
    return len(abgelaufen)


def purge_finished(batch: int = PURGE_BATCH) -> int:
    """Löscht erledigte Aufträge nach 14 Tagen, fehlgeschlagene und tote nach 90 Tagen."""
    jetzt = timezone.now()
    bedingungen = [
        models.Q(status=TaskStatus.ERLEDIGT, finished_at__lt=jetzt - KEEP_DONE),
        models.Q(status__in=[TaskStatus.FEHLGESCHLAGEN, TaskStatus.TOT], finished_at__lt=jetzt - KEEP_FAILED),
    ]
    gesamt = 0
    for bedingung in bedingungen:
        while True:
            ids = list(TaskRow.objects.filter(bedingung).values_list("pk", flat=True)[:batch])
            if not ids:
                break
            geloescht, _ = TaskRow.objects.filter(pk__in=ids).delete()
            gesamt += geloescht
            if len(ids) < batch:
                break
    return gesamt


def run_pending(queues: Sequence[str] | None = None, *, alias: str = "default", limit: int | None = None) -> int:
    """Arbeitet fällige Aufträge nacheinander im aufrufenden Thread ab; gibt deren Zahl zurück.

    Für Tests und Fehlersuche: ohne Zeitgrenzen, ohne Neustart.
    """
    config, alle = journal_options(alias)
    anzahl = 0
    for warteschlange in queues or alle:
        while limit is None or anzahl < limit:
            auftrag = claim(warteschlange, config)
            if auftrag is None:
                break
            finish(auftrag, execute(auftrag, backend_alias=alias))
            anzahl += 1
    return anzahl


def current_rss_bytes() -> int | None:
    """Belegter Arbeitsspeicher dieses Prozesses (RSS) oder ``None``, wo er nicht messbar ist."""
    if sys.platform == "win32":
        return None
    try:
        with open("/proc/self/statm", encoding="ascii") as datei:
            seiten = int(datei.read().split()[1])
        return seiten * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    import resource

    # Ohne /proc (z. B. macOS) nur der Höchststand; für „Grenze überschritten“ genügt das
    hoechst = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return hoechst if sys.platform == "darwin" else hoechst * 1024


def ensure_pool_capacity(needed: int) -> None:
    """Vergrößert den Verbindungspool dieses Prozesses, damit jeder Ausführungs-Thread eine Verbindung bekommt."""
    pool = getattr(connection, "pool", None)
    if pool is None or pool.max_size >= needed:
        return
    logger.info("Aufträge: Verbindungspool von %d auf %d Verbindungen vergrößert", pool.max_size, needed)
    pool.resize(min_size=min(pool.min_size, needed), max_size=needed)


# ---------------------------------------------------------------------------
# Koordinator
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Slot:
    """Ein Ausführungs-Thread einer Warteschlange."""

    queue: str
    number: int
    inbox: SimpleQueue[ClaimedTask | None] = field(default_factory=SimpleQueue)
    thread: threading.Thread | None = None
    current: ClaimedTask | None = None
    deadline: float = 0.0
    #: hing über die Zeitgrenze hinaus; bekommt nichts mehr, bis der Prozess neu startet (sein Auftrag
    #: bleibt bis dahin gesperrt, siehe ``mark_timed_out``)
    abandoned: bool = False

    @property
    def free(self) -> bool:
        return self.current is None and not self.abandoned

    @property
    def busy(self) -> bool:
        return self.current is not None and not self.abandoned


class TaskRunner:
    """Arbeitet Aufträge der gewählten Warteschlangen mit eigener Parallelität ab."""

    def __init__(
        self,
        config: JournalOptions,
        queues: Sequence[str],
        *,
        concurrency: Mapping[str, int] | None = None,
        backend_alias: str = "default",
        poll_interval: float = POLL_INTERVAL,
        max_tasks: int | None = None,
        max_memory_mb: int | None = None,
        burst: bool = False,
        heartbeat_file: Path | None = None,
        lock_ttl: timedelta = LOCK_TTL,
        renew_interval: float = RENEW_INTERVAL,
        maintenance_interval: float = MAINTENANCE_INTERVAL,
        purge_interval: float = PURGE_INTERVAL,
        rss: Callable[[], int | None] = current_rss_bytes,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.queues = tuple(queues)
        self.backend_alias = backend_alias
        self.worker_id = leases.new_holder_id()
        parallel = {**config.concurrency, **(concurrency or {})}
        self.concurrency = {q: max(0, int(parallel.get(q, 1))) for q in self.queues}
        self.poll_interval = poll_interval
        self.max_tasks = config.max_tasks_per_process if max_tasks is None else max_tasks
        grenze_mb = config.max_memory_mb if max_memory_mb is None else max_memory_mb
        self.max_memory_bytes = max(0, grenze_mb) * 1024 * 1024
        self.burst = burst
        self.heartbeat_file = heartbeat_file
        self.lock_ttl = lock_ttl
        self.renew_interval = renew_interval
        self.maintenance_interval = maintenance_interval
        self.purge_interval = purge_interval
        self._rss = rss
        self._clock = clock
        #: in diesem Prozess abgeschlossene Aufträge (für den Neustart nach N Aufträgen)
        self.completed = 0
        self._slots = [_Slot(q, n) for q in self.queues for n in range(self.concurrency[q])]
        self._results: SimpleQueue[tuple[_Slot, ClaimedTask, Outcome]] = SimpleQueue()
        #: Ergebnisse, die wegen eines Datenbankfehlers noch nicht geschrieben sind
        self._unwritten: list[tuple[ClaimedTask, Outcome]] = []
        self._wake = threading.Event()
        self._reason: StopReason | None = None

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    # -- Lebenszyklus ---------------------------------------------------------

    def run(self, stop: threading.Event, force: threading.Event | None = None) -> StopReason:
        """Arbeitet ab, bis ``stop`` gesetzt ist oder ein Neustart fällig wird; gibt den Grund zurück.

        Nach ``stop`` werden keine Aufträge mehr geholt, laufende noch beendet (Zeitgrenzen gelten
        weiter). ``force`` gibt laufende Aufträge sofort frei und kehrt zurück.
        """
        self._check_memory_baseline()
        self._start_threads()
        jetzt = self._clock()
        naechste_verlaengerung = jetzt + self.renew_interval
        naechste_wartung = jetzt  # sofort: Sperren eines abgestürzten Vorgängers freigeben
        naechstes_aufraeumen = jetzt
        try:
            while True:
                self._wake.clear()
                if force is not None and force.is_set():
                    self._release_running()
                    return StopReason.STOP
                if stop.is_set():
                    self._request(StopReason.STOP)
                geholt = 0
                try:
                    self._collect_results()
                    self._enforce_deadlines()
                    jetzt = self._clock()
                    if jetzt >= naechste_verlaengerung:
                        self._renew()
                        naechste_verlaengerung = jetzt + self.renew_interval
                    if jetzt >= naechste_wartung:
                        release_expired()
                        naechste_wartung = jetzt + self.maintenance_interval
                    if jetzt >= naechstes_aufraeumen:
                        if geloescht := purge_finished():
                            logger.info("Aufträge: %d alte Aufträge gelöscht", geloescht)
                        naechstes_aufraeumen = jetzt + self.purge_interval
                    self._check_limits()
                    if self._reason is None:
                        geholt = self._claim_free_slots()
                except DatabaseError:
                    logger.warning("Aufträge: Datenbankfehler, neuer Versuch folgt", exc_info=True)
                    with contextlib.suppress(DatabaseError):
                        connection.close()
                self._heartbeat()

                beschaeftigt = any(slot.busy for slot in self._slots)
                if self._reason is not None and not beschaeftigt and not self._unwritten:
                    return self._reason
                if self.burst and self._reason is None and not beschaeftigt and not geholt and not self._unwritten:
                    return StopReason.LEER
                if not geholt:
                    self._wake.wait(self.poll_interval)
        finally:
            self._stop_threads()

    def _start_threads(self) -> None:
        for slot in self._slots:
            slot.thread = threading.Thread(
                target=self._slot_loop, args=(slot,), name=f"auftrag-{slot.queue}-{slot.number}", daemon=True
            )
            slot.thread.start()

    def _stop_threads(self) -> None:
        # Threads mit laufendem Auftrag (erzwungenes Ende, Zeitgrenze) bleiben als Daemon zurück
        for slot in self._slots:
            slot.inbox.put(None)
        for slot in self._slots:
            if slot.thread is not None and slot.current is None:
                slot.thread.join(timeout=5)

    def wake(self) -> None:
        """Weckt den Koordinator vorzeitig (z. B. aus einem Signal-Handler)."""
        self._wake.set()

    def _request(self, reason: StopReason) -> None:
        """Nimmt nichts Neues mehr an. Beenden hat Vorrang vor einem Neustart."""
        if self._reason is None or (reason == StopReason.STOP and self._reason != StopReason.STOP):
            if self._reason is None:
                logger.info("Aufträge: nehme keine neuen an (%s)", reason)
            self._reason = reason

    # -- Ausführungs-Thread ---------------------------------------------------

    def _slot_loop(self, slot: _Slot) -> None:
        try:
            while (auftrag := slot.inbox.get()) is not None:
                _verbindungen_pflegen()
                try:
                    ausgang = execute(auftrag, backend_alias=self.backend_alias, worker_id=self.worker_id)
                except BaseException as exc:  # noqa: BLE001 – auch SystemExit eines Auftrags darf den Thread nicht beenden
                    logger.exception("Auftrag %s: Ausführung abgebrochen", auftrag.id)
                    ausgang = Outcome(_klassenpfad(exc))
                finally:
                    _verbindungen_pflegen()
                self._results.put((slot, auftrag, ausgang))
                self._wake.set()
        finally:
            with contextlib.suppress(DatabaseError):
                connection.close()

    # -- Schritte des Koordinators ---------------------------------------------

    def _collect_results(self) -> None:
        offen, self._unwritten = self._unwritten, []
        while True:
            try:
                slot, auftrag, ausgang = self._results.get_nowait()
            except Empty:
                break
            if slot.current is auftrag:
                slot.current = None
            self.completed += 1
            if slot.abandoned:
                # Noch gesperrt (``mark_timed_out``) und kein anderer Versuch läuft: Das Ergebnis gilt
                logger.info("Auftrag %s wurde nach der Zeitgrenze doch fertig (%s)", auftrag.id, ausgang.code)
            offen.append((auftrag, ausgang))
        for index, (auftrag, ausgang) in enumerate(offen):
            try:
                finish(auftrag, ausgang)
            except DatabaseError:
                # Nicht verlieren: beim nächsten Durchlauf erneut schreiben, Sperre bis dahin verlängern
                self._unwritten = offen[index:]
                raise

    def _enforce_deadlines(self) -> None:
        jetzt = self._clock()
        for slot in self._slots:
            auftrag = slot.current
            if auftrag is None or slot.abandoned or jetzt < slot.deadline:
                continue
            self._request(StopReason.ZEITGRENZE)
            # Bei einem Datenbankfehler im nächsten Durchlauf erneut: Der Slot gilt erst danach als aufgegeben
            mark_timed_out(auftrag)
            slot.abandoned = True
            TASKS_FAILED.labels(queue=auftrag.queue, grund="zeitgrenze").inc()
            logger.error(
                "Auftrag %s (%s) hat die Zeitgrenze von %.0f s überschritten; der Runner startet danach neu",
                auftrag.id,
                auftrag.task_path,
                auftrag.timeout,
            )

    def _renew(self) -> None:
        # auch aufgegebene (Zeitgrenze): gesperrt, bis dieser Prozess endet
        laufend = [slot.current for slot in self._slots if slot.current is not None]
        laufend += [auftrag for auftrag, _ in self._unwritten]
        eigene = renew_locks(laufend, self.lock_ttl)
        if eigene < len(laufend):
            logger.warning("Aufträge: %d Sperren gehören nicht mehr diesem Runner", len(laufend) - eigene)

    def _check_limits(self) -> None:
        if self.max_tasks and self.completed >= self.max_tasks:
            self._request(StopReason.ANZAHL)
        if self.max_memory_bytes and self.completed:
            belegt = self._rss()
            if belegt is not None:
                WORKER_RSS.set(belegt)
            if belegt is not None and belegt > self.max_memory_bytes:
                logger.info("Aufträge: Speichergrenze überschritten (%d MB)", belegt // (1024 * 1024))
                self._request(StopReason.SPEICHER)

    def _check_memory_baseline(self) -> None:
        belegt = self._rss() if self.max_memory_bytes else None
        if belegt is not None:
            WORKER_RSS.set(belegt)
        if belegt is not None and belegt >= self.max_memory_bytes:
            # Sonst startete der Runner nach jedem Auftrag neu
            logger.error(
                "Aufträge: Speichergrenze %d MB liegt unter dem Grundbedarf (%d MB); Grenze ausgeschaltet",
                self.max_memory_bytes // (1024 * 1024),
                belegt // (1024 * 1024),
            )
            self.max_memory_bytes = 0

    def _claim_free_slots(self) -> int:
        geholt = 0
        for warteschlange in self.queues:
            freie = [slot for slot in self._slots if slot.queue == warteschlange and slot.free]
            if not freie or not queue_ready(warteschlange):
                continue
            for slot in freie:
                if self.max_tasks and self.completed + self._running() >= self.max_tasks:
                    return geholt
                auftrag = claim(warteschlange, self.config, self.lock_ttl)
                if auftrag is None:
                    break
                slot.current = auftrag
                slot.deadline = self._clock() + auftrag.timeout
                slot.inbox.put(auftrag)
                geholt += 1
        return geholt

    def _running(self) -> int:
        return sum(1 for slot in self._slots if slot.busy)

    def _release_running(self) -> None:
        for slot in self._slots:
            if slot.busy and slot.current is not None:
                with contextlib.suppress(DatabaseError):
                    release(slot.current)

    def _heartbeat(self) -> None:
        if self.heartbeat_file is None:
            return
        try:
            self.heartbeat_file.touch()
        except OSError:
            logger.debug("Heartbeat-Datei nicht beschreibbar", exc_info=True)


def _verbindungen_pflegen() -> None:
    """Gibt unbrauchbare oder abgelaufene Verbindungen des Threads zurück (mit Pool: jede)."""
    try:
        close_old_connections()
    except DatabaseError:
        logger.debug("Verbindung ließ sich nicht sauber schließen", exc_info=True)
