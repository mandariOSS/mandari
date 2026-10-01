# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Worker: Sequenzierer, Zustellung, Aufträge und Zeitpläne in einem Prozess (Issue #508).

Befehl ``manage.py events_worker``; Grundlagen: ``docs/adr/20260929-ereignistechnik-postgres.md``
(Punkt 9) und ``docs/adr/20260929-auftraege-und-zeitplaene.md``. Jede Rolle läuft mit derselben
Schleife wie im Einzelbefehl in einem eigenen Faden:

- ``sequencer``: ``Sequencer.run`` (Leader-Lease ``sequencer``)
- ``dispatch``: ``Dispatcher.run``, je Abonnement ein Faden (Lease ``dispatch:<name>``)
- ``tasks``: ``TaskRunner.run`` (ohne Lease, Aufträge per ``SKIP LOCKED``)
- ``scheduler``: ``Scheduler.run`` (Lease ``scheduler``)

Leader-Leases laufen nach 30 s ab, jede Rolle erneuert ihre eigene alle 10 s
(``apps.events.leases``). Ein Listener je Prozess weckt Sequenzierer und Zustellung
(``apps.events.wakeup``).

**Lebenszeichen:** Jede Schleife meldet jeden Durchlauf (``Heartbeat``). Der Hauptfaden prüft, ob
jeder Rollenfaden lebt und sich innerhalb von ``stale_after`` gemeldet hat. Nur dann erneuert er
alle 5 s die Heartbeat-Datei (Healthcheck des Containers) und seinen Eintrag in ``events_worker``
(``apps.events.presence``, für Health und Admin). Hängt eine Rolle, veralten beide, und die
Überwachung startet den Container neu. ``/health`` und ``/metrics`` (``apps.common.metrics_server``,
``mandari_worker_role_up``) zeigen denselben Zustand.

**Beenden:** SIGTERM oder SIGINT setzen ``stop``. Sequenzierer und Zustellung schreiben ihren
laufenden Batch fest, die Zustellung samt Cursor; der Runner nimmt nichts Neues mehr an und lässt
laufende Aufträge zu Ende laufen; alle geben ihre Leases frei. Was nach ``shutdown_timeout`` noch
läuft, gibt der Runner sofort frei (wie nach einem zweiten Signal), ohne den Versuch zu zählen; ein
anderer Runner holt diese Aufträge gleich wieder.

**Neustart:** Will der Runner neu starten (Zahl der Aufträge, Speichergrenze, Zeitgrenze), nimmt er
nichts Neues mehr an und wartet auf seine laufenden Aufträge. Die übrigen Rollen arbeiten
währenddessen in ihren Fäden weiter und erneuern ihre Leases selbst; der Neustart hält also weder
Zeitpläne noch Zustellung bis zum Ende des längsten Auftrags an. Erst danach beendet der Worker alle
Rollen wie bei SIGTERM und ersetzt den Prozess (``replace_process``); die Leases sind dann frei, der
neue Prozess übernimmt sie sofort.

**Ausfall:** Endet ein Rollenfaden unerwartet, beendet der Worker alle Rollen und sich selbst mit
Fehler (``ExitReason.FAILURE``); der Container startet neu.

**Verbindungsbudget** (``connection_budget``): je ein Faden für Sequenzierer und Zeitpläne, je
Abonnement einer, der Koordinator des Runners und je Ausführungsplatz einer, die Selbstprüfung des
Listeners, ein Abruf von ``/metrics`` und der Hauptfaden, dazu eine Reserve. Der Befehl vergrößert
den Pool des Prozesses darauf. Die Lauschverbindung des Listeners geht am Pool vorbei und kommt in
der Datenbank noch dazu.
"""

from __future__ import annotations

import enum
import functools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NoReturn

from django.db import DatabaseError, connections

from apps.common.db_connections import close_thread_connections, releases_db_connections
from apps.common.metrics_server import MetricsServer, start_metrics_server

from . import leases, presence
from .dispatch import POLL_INTERVAL as DISPATCH_INTERVAL
from .dispatch import Dispatcher
from .metrics import WORKER_ROLES, RoleState
from .models import NOTIFY_CHANNEL
from .scheduler import POLL_INTERVAL as SCHEDULER_INTERVAL
from .scheduler import Scheduler
from .sequencer import POLL_INTERVAL as SEQUENCER_INTERVAL
from .sequencer import SEQUENCED_CHANNEL, Sequencer
from .task_runner import StopReason, TaskRunner
from .wakeup import error_summary, start_listener

logger = logging.getLogger(__name__)

ROLE_SEQUENCER: Final = "sequencer"
ROLE_DISPATCH: Final = "dispatch"
ROLE_TASKS: Final = "tasks"
ROLE_SCHEDULER: Final = "scheduler"
#: Alle Rollen in Startreihenfolge
ROLES: Final = (ROLE_SEQUENCER, ROLE_DISPATCH, ROLE_TASKS, ROLE_SCHEDULER)

#: So oft prüft der Hauptfaden die Rollen (Sekunden)
SUPERVISE_INTERVAL: Final = 1.0
#: So oft erneuert er Heartbeat-Datei und Eintrag in ``events_worker`` (Sekunden)
HEARTBEAT_INTERVAL: Final = 5.0
#: Ohne Lebenszeichen seit so vielen Sekunden gilt eine Rolle als hängend. Großzügig, weil ein
#: großer Batch der Zustellung oder ein Sequenzierer, der auf eine Zeilensperre wartet, eine Weile
#: brauchen darf; ein Neustart würde ihn nur wiederholen.
STALE_AFTER: Final = 300.0
#: So lange warten laufende Aufträge beim Beenden, bevor sie freigegeben werden (Sekunden). Unter
#: der Frist von Docker bzw. Kubernetes bis SIGKILL (``stop_grace_period``, 30 s).
SHUTDOWN_TIMEOUT: Final = 20.0
#: Danach noch so lange auf die Fäden warten (Sekunden)
FORCE_GRACE: Final = 5.0
#: Verbindungen über das Budget hinaus (Kurzzeitiges, Aufräumen)
POOL_RESERVE: Final = 2
#: Exit-Code „bitte neu starten“ (EX_TEMPFAIL), wenn der Prozess sich nicht selbst ersetzt
EXIT_RESTART: Final = 75


class ExitReason(enum.StrEnum):
    """Warum ``Worker.run`` zurückkehrt."""

    #: Beenden per Signal bzw. ``stop``
    STOP = "stop"
    #: Der Runner will neu starten (``StopReason.restart``): Prozess ersetzen
    RESTART = "neustart"
    #: Eine Rolle ist ausgefallen: mit Fehler beenden, die Überwachung startet neu
    FAILURE = "ausfall"


def connection_budget(
    roles: Collection[str],
    *,
    subscriptions: int = 0,
    task_slots: int = 0,
    listener: bool = False,
    metrics: bool = False,
) -> int:
    """Verbindungen aus dem Pool, die der Worker höchstens gleichzeitig braucht (siehe Moduldokumentation)."""
    budget = 1 + POOL_RESERVE  # Hauptfaden: Eintrag in events_worker
    if ROLE_SEQUENCER in roles:
        budget += 1
    if ROLE_DISPATCH in roles:
        budget += subscriptions
    if ROLE_TASKS in roles:
        budget += 1 + task_slots
    if ROLE_SCHEDULER in roles:
        budget += 1
    if listener:
        budget += 1  # Selbstprüfung über die Standardverbindung
    if metrics:
        budget += 1  # Sammler fragen beim Abruf die Datenbank
    return budget


class Heartbeat:
    """Letztes Lebenszeichen je Schlüssel (Rolle bzw. ``dispatch:<abonnement>``), threadsicher."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._zuletzt: dict[str, float] = {}

    def beat(self, key: str) -> None:
        jetzt = self._clock()
        with self._lock:
            self._zuletzt[key] = jetzt

    def beater(self, key: str) -> Callable[[], None]:
        return functools.partial(self.beat, key)

    def age(self, keys: Iterable[str]) -> float | None:
        """Sekunden seit dem ältesten der letzten Lebenszeichen; ``None``, wenn einer noch keins hat."""
        jetzt = self._clock()
        with self._lock:
            zuletzt = [self._zuletzt.get(key) for key in keys]
        if not zuletzt or any(wert is None for wert in zuletzt):
            return None
        return max(0.0, max(jetzt - wert for wert in zuletzt if wert is not None))


@dataclass(eq=False)
class _Rolle:
    name: str
    ziel: Callable[[], object]
    #: Schlüssel der Lebenszeichen (Rolle bzw. je Abonnement)
    schluessel: tuple[str, ...]
    faden: threading.Thread | None = None
    ergebnis: object = None
    fehler: bool = False
    #: ohne ``stop`` zurückgekehrt
    unerwartet: bool = False
    beendet: threading.Event = field(default_factory=threading.Event)


class Worker:
    """Rollen in Fäden starten, überwachen, Lebenszeichen geben und sauber beenden."""

    def __init__(
        self,
        *,
        sequencer: Sequencer | None = None,
        dispatcher: Dispatcher | None = None,
        runner: TaskRunner | None = None,
        scheduler: Scheduler | None = None,
        roles: Sequence[str] | None = None,
        queues: Sequence[str] = (),
        holder: str | None = None,
        listen: bool = True,
        heartbeat_file: Path | None = None,
        metrics_addr: str = "0.0.0.0",  # noqa: S104 – im Container für Prometheus; Zugriff wie /metrics/
        metrics_port: int | None = None,
        stale_after: float = STALE_AFTER,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        supervise_interval: float = SUPERVISE_INTERVAL,
        sequencer_interval: float = SEQUENCER_INTERVAL,
        dispatch_interval: float = DISPATCH_INTERVAL,
        scheduler_interval: float = SCHEDULER_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sequencer = sequencer
        self.dispatcher = dispatcher
        self.runner = runner
        self.scheduler = scheduler
        vorhanden = {
            ROLE_SEQUENCER: sequencer is not None,
            ROLE_DISPATCH: dispatcher is not None,
            ROLE_TASKS: runner is not None,
            ROLE_SCHEDULER: scheduler is not None,
        }
        #: gemeldete Rollen; ``dispatch`` auch ohne Abonnement (es gibt dann nichts zuzustellen)
        self.roles = tuple(roles) if roles is not None else tuple(r for r in ROLES if vorhanden[r])
        self.queues = tuple(queues)
        self.holder = holder or leases.new_holder_id()
        self.listen = listen
        self.heartbeat_file = heartbeat_file
        self.metrics_addr = metrics_addr
        self.metrics_port = metrics_port
        self.stale_after = stale_after
        self.shutdown_timeout = shutdown_timeout
        self.heartbeat_interval = heartbeat_interval
        self.supervise_interval = supervise_interval
        self._clock = clock
        self.heartbeat = Heartbeat(clock)
        self._sequencer_wecken = threading.Event()
        self._intervalle = {
            ROLE_SEQUENCER: sequencer_interval,
            ROLE_DISPATCH: dispatch_interval,
            ROLE_SCHEDULER: scheduler_interval,
        }
        if runner is not None:
            runner.on_beat = self.heartbeat.beater(ROLE_TASKS)
        self._rollen: list[_Rolle] = []
        self._metrics: MetricsServer | None = None
        self._ohne_lebenszeichen: list[str] = []
        self._datei_warnung = False
        self._aufgeraeumt = False

    # -- Zustand ------------------------------------------------------------------------------

    @property
    def metrics_server_port(self) -> int | None:
        """Tatsächlicher Port von ``/metrics`` (bei Port 0 frei gewählt) oder ``None``."""
        return self._metrics.port if self._metrics is not None else None

    def role_states(self) -> dict[str, RoleState]:
        """Je laufender Rolle: arbeitet sie (Faden lebt, Lebenszeichen frisch) und wie alt ist ihr letztes."""
        zustaende: dict[str, RoleState] = {}
        for rolle in self._rollen:
            alter = self.heartbeat.age(rolle.schluessel)
            lebt = rolle.faden is not None and rolle.faden.is_alive() and not rolle.beendet.is_set()
            frisch = alter is not None and alter <= self.stale_after
            zustaende[rolle.name] = RoleState(up=lebt and frisch, beat_age=alter)
        return zustaende

    def health(self) -> tuple[bool, dict[str, Any]]:
        """Für ``/health``: gesund, wenn jede Rolle arbeitet; nennt nur Rollen und ihren Zustand."""
        zustaende = self.role_states()
        gesund = bool(zustaende) and all(zustand.up for zustand in zustaende.values())
        return gesund, {"roles": {name: {"ok": zustand.up} for name, zustand in zustaende.items()}}

    # -- Lebenszyklus -------------------------------------------------------------------------

    def run(self, stop: threading.Event, force: threading.Event | None = None) -> ExitReason:
        """Startet die Rollen und überwacht sie bis ``stop``, einen Neustartwunsch oder einen Ausfall.

        ``force`` gibt laufende Aufträge sofort frei (zweites Signal); nach ``shutdown_timeout``
        setzt der Worker es selbst.
        """
        force = force if force is not None else threading.Event()
        self._rollen = self._rollen_bilden(stop, force)
        WORKER_ROLES.provide(self.role_states)
        try:
            self._listener_starten(stop)
            for rolle in self._rollen:
                self._starten(rolle, stop)
            self._metrics_starten()
            self._ueberwachen(stop)
            self._beenden(stop, force)
            return self._grund()
        finally:
            WORKER_ROLES.provide(None)
            self._abmelden()
            if self._metrics is not None:
                self._metrics.close()

    def _rollen_bilden(self, stop: threading.Event, force: threading.Event) -> list[_Rolle]:
        rollen: list[_Rolle] = []
        if self.sequencer is not None:
            ziel = functools.partial(
                self.sequencer.run,
                stop,
                interval=self._intervalle[ROLE_SEQUENCER],
                wake=self._sequencer_wecken,
                beat=self.heartbeat.beater(ROLE_SEQUENCER),
            )
            rollen.append(_Rolle(ROLE_SEQUENCER, ziel, (ROLE_SEQUENCER,)))
        if self.dispatcher is not None and self.dispatcher.loops:
            ziel = functools.partial(
                self.dispatcher.run, stop, interval=self._intervalle[ROLE_DISPATCH], beat=self.heartbeat.beat
            )
            schluessel = tuple(loop.spec.lease_name for loop in self.dispatcher.loops)
            rollen.append(_Rolle(ROLE_DISPATCH, ziel, schluessel))
        if self.runner is not None:
            rollen.append(_Rolle(ROLE_TASKS, functools.partial(self.runner.run, stop, force), (ROLE_TASKS,)))
        if self.scheduler is not None:
            ziel = functools.partial(
                self.scheduler.run,
                stop,
                interval=self._intervalle[ROLE_SCHEDULER],
                beat=self.heartbeat.beater(ROLE_SCHEDULER),
            )
            rollen.append(_Rolle(ROLE_SCHEDULER, ziel, (ROLE_SCHEDULER,)))
        return rollen

    def _listener_starten(self, stop: threading.Event) -> None:
        """Ein Listener für beide Kanäle: Journalzeilen wecken den Sequenzierer, Folgenummern die Zustellung."""
        rueckrufe: dict[str, list[Callable[[], None]]] = {}
        if self.sequencer is not None:
            rueckrufe[NOTIFY_CHANNEL] = [self._sequencer_wecken.set]
        if self.dispatcher is not None and self.dispatcher.loops:
            rueckrufe[SEQUENCED_CHANNEL] = [self.dispatcher.wake]
        if self.listen and rueckrufe:
            start_listener(rueckrufe, stop)

    def _starten(self, rolle: _Rolle, stop: threading.Event) -> None:
        for schluessel in rolle.schluessel:
            self.heartbeat.beat(schluessel)  # Startzeit zählt als erstes Lebenszeichen
        rolle.faden = threading.Thread(
            target=self._ausfuehren, args=(rolle, stop), name=f"events-worker-{rolle.name}", daemon=True
        )
        rolle.faden.start()

    def _ausfuehren(self, rolle: _Rolle, stop: threading.Event) -> None:
        try:
            rolle.ergebnis = rolle.ziel()
            rolle.unerwartet = not stop.is_set()
        except BaseException:  # noqa: BLE001 – Ausfall melden; der Hauptfaden beendet den Worker
            rolle.fehler = True
            logger.exception("Worker: Rolle %s ist ausgefallen", rolle.name)
        finally:
            rolle.beendet.set()
            try:
                close_thread_connections()
            except DatabaseError as exc:
                logger.debug(
                    "Worker: Verbindung der Rolle %s nicht sauber geschlossen (%s)", rolle.name, error_summary(exc)
                )

    def _metrics_starten(self) -> None:
        if self.metrics_port is None:
            return
        try:
            self._metrics = start_metrics_server(self.metrics_addr, self.metrics_port, self.health)
        except OSError as exc:
            # Die Arbeit hängt nicht an den Metriken; das Fehlen fällt in Prometheus auf (absent)
            logger.error(
                "Worker: /metrics und /health auf Port %s nicht verfügbar (%s), weiter ohne",
                self.metrics_port,
                type(exc).__name__,
            )

    def _ueberwachen(self, stop: threading.Event) -> None:
        naechstes = self._clock()  # sofort ein erstes Lebenszeichen
        while not stop.is_set():
            if any(rolle.beendet.is_set() for rolle in self._rollen):
                return
            jetzt = self._clock()
            if jetzt >= naechstes:
                self._lebenszeichen()
                naechstes = jetzt + self.heartbeat_interval
            stop.wait(self.supervise_interval)

    def wake(self) -> None:
        """Weckt alle Schleifen vorzeitig (aus dem Signal-Handler nach ``stop`` bzw. ``force``)."""
        self._wecken()

    def _wecken(self) -> None:
        self._sequencer_wecken.set()
        if self.dispatcher is not None:
            self.dispatcher.wake()
        if self.runner is not None:
            self.runner.wake()

    def _beenden(self, stop: threading.Event, force: threading.Event) -> None:
        """Alle Rollen beenden: laufende Batches festschreiben, Aufträge nach der Frist freigeben."""
        stop.set()
        self._wecken()
        frist = self._clock() + self.shutdown_timeout
        for rolle in self._rollen:
            if rolle.faden is not None:
                rolle.faden.join(max(0.0, frist - self._clock()))
        if self.runner is not None and not force.is_set() and self._laeuft(ROLE_TASKS):
            logger.warning(
                "Worker: Aufträge laufen nach %.0f s noch; sie werden freigegeben und erneut ausgeführt",
                self.shutdown_timeout,
            )
            force.set()
            self.runner.wake()
        for rolle in self._rollen:
            if rolle.faden is None:
                continue
            rolle.faden.join(FORCE_GRACE)
            if rolle.faden.is_alive():
                logger.error("Worker: Rolle %s endet nicht; der Prozess wird trotzdem beendet", rolle.name)

    def _laeuft(self, name: str) -> bool:
        return any(r.name == name and r.faden is not None and r.faden.is_alive() for r in self._rollen)

    def _grund(self) -> ExitReason:
        for rolle in self._rollen:
            if rolle.fehler:
                return ExitReason.FAILURE
            neustart = isinstance(rolle.ergebnis, StopReason) and rolle.ergebnis.restart
            if rolle.unerwartet and not (rolle.name == ROLE_TASKS and neustart):
                logger.error("Worker: Rolle %s hat sich unerwartet beendet", rolle.name)
                return ExitReason.FAILURE
        for rolle in self._rollen:
            if rolle.name == ROLE_TASKS and isinstance(rolle.ergebnis, StopReason) and rolle.ergebnis.restart:
                logger.info("Worker: Neustart, weil der Runner neu starten will (%s)", rolle.ergebnis)
                return ExitReason.RESTART
        return ExitReason.STOP

    # -- Lebenszeichen ------------------------------------------------------------------------

    def _lebenszeichen(self) -> None:
        """Heartbeat-Datei und Eintrag in ``events_worker`` erneuern, aber nur, wenn jede Rolle arbeitet."""
        haengend = sorted(name for name, zustand in self.role_states().items() if not zustand.up)
        if haengend:
            if haengend != self._ohne_lebenszeichen:
                logger.warning(
                    "Worker: Rolle(n) %s ohne Lebenszeichen seit über %.0f s; Heartbeat wird nicht erneuert",
                    ", ".join(haengend),
                    self.stale_after,
                )
            self._ohne_lebenszeichen = haengend
            return
        if self._ohne_lebenszeichen:
            logger.info("Worker: alle Rollen arbeiten wieder")
            self._ohne_lebenszeichen = []
        self._datei_erneuern()
        self._anmelden()

    def _datei_erneuern(self) -> None:
        if self.heartbeat_file is None:
            return
        try:
            self.heartbeat_file.touch()
        except OSError as exc:
            if not self._datei_warnung:
                logger.warning("Worker: Heartbeat-Datei nicht beschreibbar (%s)", type(exc).__name__)
                self._datei_warnung = True

    @releases_db_connections
    def _anmelden(self) -> None:
        try:
            if not self._aufgeraeumt:
                presence.purge_stale()
                self._aufgeraeumt = True
            presence.announce(self.holder, self.roles, self.queues)
        except DatabaseError as exc:
            logger.warning("Worker: Lebenszeichen nicht in der Datenbank gespeichert (%s)", error_summary(exc))

    @releases_db_connections
    def _abmelden(self) -> None:
        try:
            presence.withdraw(self.holder)
        except DatabaseError as exc:
            logger.warning("Worker: Abmeldung nicht gespeichert (%s)", error_summary(exc))


def replace_process() -> NoReturn:
    """Ersetzt den Prozess durch denselben Befehl (``exec``): gibt Speicher frei und beendet hängende Fäden.

    Die Prozessnummer bleibt; im Container zählt das nicht als Neustart.
    """
    connections.close_all()
    for strom in (sys.stdout, sys.stderr):
        strom.flush()
    for handler in logging.getLogger().handlers:
        handler.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
