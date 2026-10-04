# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufträge über die Tasks-Schnittstelle von Django in der Tabelle ``events_task``
(``docs/adr/20260929-auftraege-und-zeitplaene.md``).

``JournalBackend`` ist ein Backend für ``TASKS``: ``@task``-Funktionen und ``.enqueue()`` bleiben
unverändert, der Auftrag landet aber als Zeile in derselben Datenbank. In einer Transaktion
entsteht er genau dann, wenn auch die fachliche Änderung festgeschrieben wird. Abgearbeitet wird
er vom Runner (``manage.py events_tasks``, ``apps.events.task_runner``).

Umschalten per Einstellung (``TASKS_BACKEND=journal``); ohne Umschalten bleibt das sofort
ausführende Backend von Django aktiv, und dieselben Aufrufe laufen wie bisher in der Anfrage.

Einstellungen in ``TASKS["default"]["OPTIONS"]`` (alle optional)::

    "concurrency": {"ocr": 1, …}         # parallele Aufträge je Warteschlange im Runner
    "timeouts": {"mail": 120, …}         # Zeitgrenze in Sekunden je Warteschlange
    "tasks": {"<pfad>": {"timeout": 900, "max_attempts": 3}}   # je Auftragstyp
    "max_attempts": 8                    # Versuche, danach Status „tot“
    "max_tasks_per_process": 1000        # Runner startet sich danach neu (0 = nie)
    "max_memory_mb": 400                 # Runner startet sich oberhalb neu (0 = keine Grenze)

Argumente werden als JSON gespeichert und dürfen nur Kennungen enthalten, nie verschlüsselte oder
entschlüsselte Inhalte. Rückgabewerte und Stacktraces werden nicht gespeichert: Ein Auftrag meldet
fachliche Ergebnisse beim Eigentümer, Fehler stehen im Protokoll; in der Tabelle steht nur der
Ergebniscode (``ok``, Ausnahmeklasse oder ``zeitgrenze``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, ParamSpec, TypeVar

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models.expressions import DatabaseDefault
from django.tasks import DEFAULT_TASK_QUEUE_NAME, Task, TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued
from django.utils import timezone
from django.utils.json import normalize_json
from django.utils.module_loading import import_string

from .models import Task as TaskRow
from .models import TaskStatus

P = ParamSpec("P")
R = TypeVar("R")

#: Warteschlangen mit Standard-Parallelität im Runner (ADR A4)
DEFAULT_CONCURRENCY: Final[Mapping[str, int]] = {
    "default": 4,
    "mail": 2,
    "index": 2,
    "ocr": 1,
    "ai": 1,
    "adapter": 2,
}
QUEUES: Final[tuple[str, ...]] = tuple(DEFAULT_CONCURRENCY)
#: Zeitgrenze je Warteschlange in Sekunden, wenn für den Auftragstyp nichts eingestellt ist
DEFAULT_TIMEOUTS: Final[Mapping[str, float]] = {
    "default": 300,
    "mail": 120,
    "index": 600,
    "ocr": 1800,
    "ai": 900,
    "adapter": 900,
}
#: Zeitgrenze für Warteschlangen ohne eigenen Wert
FALLBACK_TIMEOUT: Final = 300.0
DEFAULT_MAX_ATTEMPTS: Final = 8
DEFAULT_MAX_TASKS_PER_PROCESS: Final = 1000
DEFAULT_MAX_MEMORY_MB: Final = 400

#: Ergebniscode erfolgreicher Aufträge
RESULT_OK: Final = "ok"

_STATUS: Final[Mapping[str, TaskResultStatus]] = {
    TaskStatus.WARTEND: TaskResultStatus.READY,
    TaskStatus.LAEUFT: TaskResultStatus.RUNNING,
    TaskStatus.ERLEDIGT: TaskResultStatus.SUCCESSFUL,
    TaskStatus.FEHLGESCHLAGEN: TaskResultStatus.FAILED,
    TaskStatus.TOT: TaskResultStatus.FAILED,
}


class PermanentTaskError(Exception):
    """Ein Auftrag ist endgültig gescheitert; der Runner wiederholt ihn nicht (Status „fehlgeschlagen“)."""


@dataclass(frozen=True)
class TaskTypeOptions:
    """Zeitgrenze und Versuche eines Auftragstyps."""

    timeout: float | None = None
    max_attempts: int | None = None


@dataclass(frozen=True)
class JournalOptions:
    """Geprüfte Einstellungen des Backends (``OPTIONS``)."""

    concurrency: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_CONCURRENCY))
    timeouts: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    tasks: Mapping[str, TaskTypeOptions] = field(default_factory=dict)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_tasks_per_process: int = DEFAULT_MAX_TASKS_PER_PROCESS
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB

    @classmethod
    def from_settings(cls, options: Mapping[str, Any], queues: set[str] | frozenset[str]) -> JournalOptions:
        """Liest ``OPTIONS``; wirft ``ImproperlyConfigured`` bei unbekannten Warteschlangen oder Werten."""
        unbekannt = set(options) - {
            "concurrency",
            "timeouts",
            "tasks",
            "max_attempts",
            "max_tasks_per_process",
            "max_memory_mb",
        }
        if unbekannt:
            raise ImproperlyConfigured(f"TASKS OPTIONS: unbekannte Schlüssel {sorted(unbekannt)}")

        concurrency = {q: DEFAULT_CONCURRENCY.get(q, 1) for q in queues}
        concurrency.update(_je_warteschlange(options.get("concurrency", {}), queues, "concurrency", _ganzzahl(0)))
        timeouts = {q: float(DEFAULT_TIMEOUTS.get(q, FALLBACK_TIMEOUT)) for q in queues}
        timeouts.update(_je_warteschlange(options.get("timeouts", {}), queues, "timeouts", _sekunden))

        typen: dict[str, TaskTypeOptions] = {}
        for pfad, werte in dict(options.get("tasks", {})).items():
            if not isinstance(werte, Mapping) or set(werte) - {"timeout", "max_attempts"}:
                raise ImproperlyConfigured(f"TASKS OPTIONS tasks[{pfad!r}]: erlaubt sind timeout und max_attempts")
            typen[str(pfad)] = TaskTypeOptions(
                timeout=_sekunden(werte["timeout"], f"tasks[{pfad!r}].timeout") if "timeout" in werte else None,
                max_attempts=(
                    _ganzzahl(1)(werte["max_attempts"], f"tasks[{pfad!r}].max_attempts")
                    if "max_attempts" in werte
                    else None
                ),
            )

        return cls(
            concurrency=concurrency,
            timeouts=timeouts,
            tasks=typen,
            max_attempts=_ganzzahl(1)(options.get("max_attempts", DEFAULT_MAX_ATTEMPTS), "max_attempts"),
            max_tasks_per_process=_ganzzahl(0)(
                options.get("max_tasks_per_process", DEFAULT_MAX_TASKS_PER_PROCESS), "max_tasks_per_process"
            ),
            max_memory_mb=_ganzzahl(0)(options.get("max_memory_mb", DEFAULT_MAX_MEMORY_MB), "max_memory_mb"),
        )

    def timeout_for(self, task_path: str, queue: str) -> float:
        eigene = self.tasks.get(task_path)
        if eigene is not None and eigene.timeout is not None:
            return eigene.timeout
        return float(self.timeouts.get(queue, FALLBACK_TIMEOUT))

    def max_attempts_for(self, task_path: str) -> int:
        eigene = self.tasks.get(task_path)
        if eigene is not None and eigene.max_attempts is not None:
            return eigene.max_attempts
        return self.max_attempts


def _ganzzahl(minimum: int) -> Callable[[object, str], int]:
    def pruefen(wert: object, name: str) -> int:
        if isinstance(wert, bool) or not isinstance(wert, int) or wert < minimum:
            raise ImproperlyConfigured(f"TASKS OPTIONS {name}: ganze Zahl ≥ {minimum} erwartet")
        return wert

    return pruefen


def _sekunden(wert: object, name: str) -> float:
    if isinstance(wert, bool) or not isinstance(wert, int | float) or wert <= 0:
        raise ImproperlyConfigured(f"TASKS OPTIONS {name}: Sekunden > 0 erwartet")
    return float(wert)


def _je_warteschlange[W](
    werte: object, queues: set[str] | frozenset[str], name: str, pruefen: Callable[[object, str], W]
) -> dict[str, W]:
    if not isinstance(werte, Mapping):
        raise ImproperlyConfigured(f"TASKS OPTIONS {name}: Zuordnung Warteschlange → Wert erwartet")
    fremd = set(werte) - set(queues)
    if fremd:
        raise ImproperlyConfigured(f"TASKS OPTIONS {name}: unbekannte Warteschlangen {sorted(fremd)}")
    return {str(q): pruefen(w, f"{name}[{q!r}]") for q, w in werte.items()}


class JournalBackend(BaseTaskBackend):
    """Tasks-Backend auf ``events_task``; ausgeführt wird im Runner, nicht in der Anfrage."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        # Ohne eigene Angabe gelten die Warteschlangen der Plattform, nicht nur "default"
        super().__init__(alias, {"QUEUES": list(QUEUES), **params})
        self.config = JournalOptions.from_settings(self.options, frozenset(self.queues))

    def enqueue(self, task: Task[P, R], args: Any, kwargs: dict[str, Any]) -> TaskResult[P, R]:
        return self._insert(task, list(args), kwargs, idempotency_key=None)

    def enqueue_once(
        self, task: Task[P, R], idempotency_key: str, args: Any, kwargs: dict[str, Any]
    ) -> TaskResult[P, R]:
        """Wie ``enqueue``; gibt es den Schlüssel für diesen Auftragstyp schon, bleibt es bei dem vorhandenen."""
        if not idempotency_key:
            raise ValueError("Idempotenzschlüssel darf nicht leer sein")
        return self._insert(task, list(args), kwargs, idempotency_key=f"{task.module_path}:{idempotency_key}")

    def _insert(
        self, task: Task[P, R], args: list[Any], kwargs: dict[str, Any], *, idempotency_key: str | None
    ) -> TaskResult[P, R]:
        self.validate_task(task)
        zeile = TaskRow(
            queue=task.queue_name,
            task_path=task.module_path,
            args={"args": normalize_json(args), "kwargs": normalize_json(kwargs)},
            priority=task.priority,
            max_attempts=self.config.max_attempts_for(task.module_path),
            idempotency_key=idempotency_key,
        )
        if task.run_after is not None:
            zeile.run_after = task.run_after
        if idempotency_key is None:
            zeile.save(force_insert=True)
        else:
            try:
                with transaction.atomic():
                    zeile.save(force_insert=True)
            except IntegrityError:
                vorhanden = TaskRow.objects.filter(idempotency_key=idempotency_key).first()
                if vorhanden is None:  # anderer Integritätsfehler als der doppelte Schlüssel
                    raise
                return self._result(vorhanden, task)

        ergebnis = self._result(zeile, task)
        # robust: Ein fehlerhafter Empfänger darf den Commit der fachlichen Änderung nicht stören
        transaction.on_commit(lambda: task_enqueued.send(type(self), task_result=ergebnis), robust=True)
        return ergebnis

    def get_result(self, result_id: str) -> TaskResult[Any, Any]:
        try:
            pk = uuid.UUID(str(result_id))
        except ValueError:
            raise TaskResultDoesNotExist(result_id) from None
        zeile = TaskRow.objects.filter(pk=pk).first()
        if zeile is None:
            raise TaskResultDoesNotExist(result_id)
        try:
            task = import_string(zeile.task_path)
        except ImportError:
            raise TaskResultDoesNotExist(result_id) from None
        if not isinstance(task, Task):
            raise TaskResultDoesNotExist(result_id)
        return self._result(zeile, task)

    def _result(self, zeile: TaskRow, task: Task[P, R]) -> TaskResult[P, R]:
        status = _STATUS.get(zeile.status, TaskResultStatus.READY)
        gespeichert = zeile.args if isinstance(zeile.args, dict) else {}
        fehler = []
        if status == TaskResultStatus.FAILED and zeile.result_code:
            fehler.append(TaskError(exception_class_path=zeile.result_code, traceback=""))
        return TaskResult(
            task=task.using(queue_name=zeile.queue, priority=zeile.priority, backend=self.alias),
            id=str(zeile.pk),
            status=status,
            enqueued_at=_zeitpunkt(zeile.created_at),
            started_at=None,
            last_attempted_at=None,
            finished_at=_zeitpunkt(zeile.finished_at),
            args=list(gespeichert.get("args", [])),
            kwargs=dict(gespeichert.get("kwargs", {})),
            backend=self.alias,
            errors=fehler,
            # Kennungen früherer Versuche werden nicht gespeichert, nur ihre Zahl
            worker_ids=["journal"] * int(zeile.attempts or 0),
        )


def _zeitpunkt(wert: object) -> datetime | None:
    """Zeitpunkt aus der Zeile; ein nicht zurückgelesener Datenbank-Standard gilt als „jetzt“."""
    if isinstance(wert, DatabaseDefault):
        return timezone.now()
    return wert if isinstance(wert, datetime) else None


def enqueue_once(task: Task[P, R], idempotency_key: str, /, *args: P.args, **kwargs: P.kwargs) -> TaskResult[P, R]:
    """Reiht ``task`` höchstens einmal je Idempotenzschlüssel ein.

    Der Schlüssel gilt je Auftragstyp und so lange, wie die Zeile aufbewahrt wird (erledigt 14 Tage,
    tot 90 Tage). Ein doppelter Aufruf liefert das Ergebnis des vorhandenen Auftrags, auch wenn
    dieser schon gelaufen oder gescheitert ist. Mit einem anderen Backend (z. B. dem sofort
    ausführenden) gibt es keine Aufzeichnung; dann wird wie bei ``enqueue`` jedes Mal ausgeführt.
    """
    backend = task.get_backend()
    if isinstance(backend, JournalBackend):
        return backend.enqueue_once(task, idempotency_key, args, kwargs)
    return task.enqueue(*args, **kwargs)


#: Aufbewahrung beendeter Aufträge (ADR A4): erledigte 14 Tage, tote und endgültig fehlgeschlagene 90 Tage
KEEP_DONE: Final = timedelta(days=14)
KEEP_FAILED: Final = timedelta(days=90)
#: Zeilen je Löschschritt; kurze Transaktionen statt einer langen Sperre
PURGE_BATCH: Final = 5000


def purge_finished(now: datetime | None = None, batch: int = PURGE_BATCH) -> int:
    """Löscht beendete Aufträge nach ihrer Aufbewahrungsfrist; liefert ihre Anzahl.

    Mit den Zeilen verfallen auch ihre Idempotenzschlüssel (``enqueue_once``). Wartende und laufende
    Aufträge bleiben unberührt.
    """
    jetzt = now or timezone.now()
    regeln = (
        (TaskStatus.ERLEDIGT, KEEP_DONE),
        (TaskStatus.FEHLGESCHLAGEN, KEEP_FAILED),
        (TaskStatus.TOT, KEEP_FAILED),
    )
    geloescht = 0
    for status, frist in regeln:
        while True:
            schritt = list(
                TaskRow.objects.filter(status=status, finished_at__lt=jetzt - frist).values_list("pk", flat=True)[
                    :batch
                ]
            )
            if not schritt:
                break
            geloescht += TaskRow.objects.filter(pk__in=schritt).delete()[0]
    return geloescht


def journal_options(alias: str = "default") -> tuple[JournalOptions, tuple[str, ...]]:
    """Einstellungen und Warteschlangen für den Runner, auch wenn das Backend noch nicht umgestellt ist.

    So kann der Worker schon laufen, bevor die Webprozesse auf ``JournalBackend`` umschalten.
    """
    from django.conf import settings

    params = dict(getattr(settings, "TASKS", {}).get(alias, {}))
    queues = tuple(params.get("QUEUES") or QUEUES) or (DEFAULT_TASK_QUEUE_NAME,)
    return JournalOptions.from_settings(params.get("OPTIONS", {}), frozenset(queues)), queues


def journal_backend(alias: str = "default") -> JournalBackend:
    """Das ``JournalBackend`` für ``alias``, auch wenn ``TASKS`` dort noch ein anderes Backend nennt.

    Für Worker-Rollen (Zeitpläne), die Aufträge immer in ``events_task`` anlegen und nie selbst
    ausführen sollen.
    """
    from django.conf import settings
    from django.tasks import task_backends

    vorhanden = task_backends[alias]
    if isinstance(vorhanden, JournalBackend):
        return vorhanden
    params = {k: v for k, v in dict(getattr(settings, "TASKS", {}).get(alias, {})).items() if k != "BACKEND"}
    return JournalBackend(alias, params)
