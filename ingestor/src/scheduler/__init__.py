"""
Scheduler: Takt je Quelle und Abruf nach Bedarf (Issue #1026).

Es gibt keinen gemeinsamen Zyklus über alle Quellen mehr. Jede Quelle hat ihren Zeitpunkt „nächster Abgleich fällig“
(``sync_config["sync_state"]``, ``src/scheduler/takt.py``). Eine Schleife prüft alle ``TICK_SECONDS`` Sekunden, sofort
nach einem Admin-Auslöser und nach jedem beendeten Lauf, welche Quellen fällig sind, und startet sie in einem Pool:
höchstens ``sync_pool_size`` Läufe zugleich (Standard 4), je Quelle höchstens einer, Vollabgleiche auf höchstens allen
Plätzen bis auf einen. Ein langer Vollabgleich hält so keine andere Quelle auf. Die Drossel je Server
(``src/client/host_pacing.py``) gilt unverändert für jeden Lauf.

Vollabgleiche plant der Daemon je Quelle, sobald sie fällig sind: im Fenster der Quelle (``full_sync_window``), sonst
im Standardfenster ab ``--full-sync-hour``, einmal je Fenster und im Abstand ``full_sync_every_days``. Einen freien
Platz für einen Vollabgleich bekommt die Quelle, deren letzter begonnener Vollabgleich am längsten zurückliegt.

Der Admin-Auslöser „Jetzt synchronisieren“ (Redis-Kanal ``mandari:sync:trigger``) macht alle Quellen oder eine
einzelne (``source_id`` bzw. ``source_url``) sofort fällig. Die Schonung bei Fehlern geht immer vor.

Abruffenster je Quelle (``sync_config["sync_window"]``, Issue #1186): Außerhalb startet die Quelle nicht, weder nach
Takt noch als erster Lauf, nach einer Schonung oder für einen Vollabgleich; ihr nächster Lauf ist zu Beginn des
nächsten Fensters fällig (``SyncState.after_run``). Ein Auslöser für eine Quelle außerhalb ihres Fensters verfällt mit
Meldung im Protokoll des Daemons, wie in der Schonung; eine Übersteuerung gibt es nicht. Ein laufender Abgleich endet
mit dem Fenster (``SyncOrchestrator.sync_body_url``). Status und Metrik (``mandari_ingestor_sync_window_open``) zeigen
das Fenster, ``sync_state["reason"]`` und das Sync-Protokoll (``details.takt``) den Grund für den späteren Lauf.

Jeder Lauf schreibt sein eigenes Sync-Protokoll (``insight_sync_synclog`` mit ``source_id``) und Metriken je Quelle.
Nach einem erfolgreichen Lauf gleicht der Daemon die Quelle höchstens einmal täglich mit ihrer Schnittstelle ab
(``totalElements`` der Hauptlisten gegen den Bestand, ``src/sync/datenabgleich.py``, Issue #1525; abschaltbar mit
``INGESTOR_DATENABGLEICH=false``).

Kein Lauf hält seinen Platz auf Dauer, und kein Lauf bleibt ohne Folge:

- Gesamtfrist je Lauf (``INGESTOR_SYNC_RUN_DEADLINE_MINUTES``, für Vollabgleiche
  ``INGESTOR_SYNC_FULL_DEADLINE_MINUTES``): Danach endet der Lauf, zählt als Fehlversuch der Quelle und gibt seinen
  Platz zurück; die Quelle ist frühestens nach derselben Dauer wieder fällig.
- Je Server laufen höchstens halb so viele Quellen zugleich, wie der Pool Plätze hat (mindestens eine): Die Quellen
  eines Sammelservers belegen den Pool nicht allein.
- Wird eine Quelle abgeschaltet, endet ihr laufender Abgleich mit dem nächsten Durchlauf.
- Der Beginn jedes Laufs steht in ``sync_state`` (``run_started``). Findet der Daemon ihn beim Start noch vor, endete
  der Prozess mitten im Lauf, etwa am Speicherlimit. Solche Quellen laufen danach zuletzt und nie zwei zugleich;
  endet der Prozess wieder mitten in ihrem Lauf, zählt das als Fehlversuch dieser Quelle, und die Schonung greift
  (``_recover_aborted``). Ein geordnetes Ende (SIGTERM, ``docker stop``) zählt nicht.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any

from mandari_oparl.pacing import host_of
from rich.markup import escape
from rich.panel import Panel

from src.client.source_options import FullSyncWindow, SourceFetchOptions
from src.config import settings
from src.metrics import metrics
from src.observability import trigger_context
from src.redaction import MaskingConsole
from src.scheduler.takt import (
    REASON_WINDOW,
    STATE_KEY,
    SyncSchedule,
    SyncState,
    default_full_sync_window,
    full_sync_due,
)
from src.sync import datenabgleich
from src.sync.orchestrator import (
    ERROR_KIND_LABELS,
    SyncOrchestrator,
    SyncResult,
    outside_window_note,
    source_backoff_until,
    sync_log_status,
)

logger = logging.getLogger(__name__)
console = MaskingConsole()

#: Abstand der Schleife in Sekunden (fällige Quellen starten spätestens so viel später)
TICK_SECONDS = 30
TRIGGER_CHANNEL = "mandari:sync:trigger"
#: Lebenszeichen des Daemons für die Anwendung (``insight_sync.daemon.is_ingestor_active``)
HEARTBEAT_KEY = "mandari:ingestor:heartbeat"
HEARTBEAT_TTL_SECONDS = 180
#: Zwischenspeicher des Orchestrators leeren, spätestens nach so vielen Sekunden (sonst, sobald kein Lauf aktiv ist)
CACHE_CLEAR_SECONDS = 30 * 60
#: Auslöser für alle aktiven Quellen
ALL_SOURCES = "*"
#: Wartezeit vor einem neuen Versuch, wenn der Empfang der Auslöser (Redis) scheitert
TRIGGER_RETRY_SECONDS = 60
_EARLIEST = datetime.min.replace(tzinfo=UTC)
#: So lange wartet eine Quelle nach einem abgebrochenen Lauf höchstens darauf, dass der Pool leer ist; danach
#: startet sie auch neben anderen Läufen
SUSPECT_MAX_WAIT = timedelta(minutes=30)
#: So lange wartet ein abgebrochener Lauf höchstens darauf, dass sein Ende gespeichert ist. Kürzer als die Frist,
#: die ``docker stop`` dem Prozess lässt (10 s)
CANCEL_SAVE_SECONDS = 5.0
#: Fehlertexte an der Quelle (Betriebsmonitor), ohne Fehlerklasse: Es gilt die gewöhnliche Schonung
DEADLINE_ERROR = "Gesamtfrist des Laufs überschritten"
ABORTED_RUN_ERROR = "Lauf nicht beendet: Der Daemon endete erneut mitten im Lauf dieser Quelle"


def _deadline_seconds(minutes: int) -> float | None:
    """Gesamtfrist in Sekunden; ``None`` (keine Frist) bei 0."""
    return float(minutes * 60) if minutes > 0 else None


@dataclass
class _Trigger:
    """Admin-Auslöser für eine Quelle (oder alle), mit den Kennungen der auslösenden Django-Anfrage."""

    full: bool = False
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def merge(self, other: _Trigger) -> _Trigger:
        return replace(self, full=self.full or other.full)


@dataclass
class _Candidate:
    """Eine in diesem Durchlauf fällige Quelle."""

    source: Any
    state: SyncState
    want_full: bool
    incremental_due: bool
    trigger: _Trigger | None

    @property
    def key(self) -> str:
        return str(self.source.id)

    @property
    def triggered_full(self) -> bool:
        return self.trigger is not None and self.trigger.full

    def due_rank(self) -> tuple[int, datetime]:
        """Admin-Auslöser zuerst, dann nach Fälligkeit."""
        return (0 if self.trigger is not None else 1, self.state.next_due or _EARLIEST)

    def full_rank(self) -> tuple[int, datetime, tuple[int, datetime]]:
        """Angeforderte Vollabgleiche zuerst, dann der am längsten zurückliegende (ohne zuerst)."""
        return (0 if self.triggered_full else 1, self.state.full_attempted or _EARLIEST, self.due_rank())


def entities_of(result: SyncResult) -> int:
    """Gespeicherte Objekte eines Laufs (neu oder geändert), wie im bisherigen Sync-Protokoll."""
    return (
        result.organizations_synced
        + result.persons_synced
        + result.memberships_synced
        + result.meetings_synced
        + result.papers_synced
        + result.files_synced
        + result.locations_synced
        + result.agenda_items_synced
        + result.consultations_synced
    )


class SyncScheduler:
    """Daemon mit eigenem Takt je Quelle (siehe Moduldokumentation)."""

    def __init__(
        self,
        sync_interval_minutes: int | None = None,
        full_sync_hour: int | None = 3,
        max_concurrent: int = 10,
        pool_size: int | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        orchestrator: Any = None,
        heartbeat: bool = True,
    ) -> None:
        """
        Args:
            sync_interval_minutes: Grundtakt werktags in der Arbeitszeit (``daemon --interval``), je Quelle über
                ``sync_config["sync_schedule"]["workday_minutes"]`` abweichend.
            full_sync_hour: Beginn des Standardfensters für Vollabgleiche (Ortszeit), außerhalb 0 bis 23 keine
                geplanten Vollabgleiche.
            max_concurrent: Abrufplätze je Lauf (je Server gilt ``INGESTOR_HOST_MAX_CONCURRENT``).
            pool_size: Läufe gleichzeitig (Standard ``INGESTOR_SYNC_POOL_SIZE``, 4).
            clock: Uhr (Tests), liefert UTC-Zeitpunkte mit Zeitzone.
            orchestrator: Orchestrator (Tests); ohne wird beim Start einer angelegt.
            heartbeat: Lebenszeichen in Redis schreiben.
        """
        self.sync_interval = sync_interval_minutes or settings.sync_interval_minutes
        self.full_sync_hour = full_sync_hour
        self.max_concurrent = max_concurrent
        self.pool_size = max(1, pool_size if pool_size is not None else settings.sync_pool_size)
        # Vollabgleiche lassen immer einen Platz für Änderungsabgleiche frei (bei einem Platz geht das nicht)
        self.full_slots = max(1, self.pool_size - 1)
        # Quellen eines Servers belegen höchstens den halben Pool: Hält ein Server seine Läufe fest, bleibt Platz für
        # die übrigen. Der Takt je Server begrenzt ihre Anfragen ohnehin gemeinsam
        self.host_slots = max(1, self.pool_size // 2)
        #: Gesamtfrist je Lauf in Sekunden (``None``: keine), getrennt für Änderungs- und Vollabgleich
        self.run_deadline = _deadline_seconds(settings.sync_run_deadline_minutes)
        self.full_deadline = _deadline_seconds(settings.sync_full_deadline_minutes)
        self.default_window = default_full_sync_window(full_sync_hour)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._orchestrator = orchestrator
        self._owns_orchestrator = orchestrator is None
        self._heartbeat_enabled = heartbeat

        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: Server der laufenden Quellen (Schlüssel wie ``_tasks``), für die Grenze je Server
        self._task_hosts: dict[str, str] = {}
        self._full_running: set[str] = set()
        #: Abgebrochene Läufe des vorigen Prozesses sind zugeordnet (einmal je Start, ``_recover_aborted``)
        self._recovered = False
        #: Quelle aus einem abgebrochenen Lauf, die gerade läuft (höchstens eine zugleich)
        self._suspect_running: str | None = None
        #: Seit wann eine solche Quelle fällig ist und auf einen leeren Pool wartet
        self._suspect_since: dict[str, datetime] = {}
        #: Zuletzt gespeicherter Zustand der laufenden Quellen: Ein Lauf, der vorzeitig endet, schreibt ihn fort
        self._current: dict[str, SyncState] = {}
        self._triggers: dict[str, _Trigger] = {}
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._backoff_shown: dict[str, datetime] = {}
        #: Je Quelle der Beginn des Abruffensters, auf das sie zuletzt gemeldet wartete (eine Meldung je Fenster)
        self._window_shown: dict[str, datetime] = {}
        self._last_cache_clear = self._clock()
        self._overview: dict[str, tuple[str, datetime | None]] = {}
        self._last_runs: dict[str, dict[str, Any]] = {}
        #: Zustände, die sich nicht speichern ließen; sie gelten, bis die Datenbank einen neueren Stand hat
        self._unsaved: dict[str, SyncState] = {}
        self._redis: Any = None
        self._redis_warned = False
        #: Letzter Versuch des täglichen Datenabgleichs je Quelle (``_datenabgleich``)
        self._datenabgleich_versuche: dict[str, datetime] = {}

    # ========== Lebenszyklus ==========

    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            raise RuntimeError("Scheduler nicht gestartet")
        return self._orchestrator

    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    async def start(self) -> None:
        """Orchestrator anlegen, Auslöser-Empfang und Schleife starten."""
        window = (
            f"{self.default_window.start:%H:%M}–{self.default_window.end:%H:%M}"
            if self.default_window is not None
            else "aus"
        )
        console.print(
            Panel.fit(
                "[bold green]Starting Sync Scheduler (Takt je Quelle)[/bold green]\n"
                f"[dim]Grundtakt werktags 07–19 Uhr: {self.sync_interval} min, sonst nach sync_schedule[/dim]\n"
                f"[dim]Vollabgleich: Standardfenster {window} (Europe/Berlin), je Quelle full_sync_window[/dim]\n"
                "[dim]Abruffenster je Quelle: sync_window (außerhalb kein Abruf)[/dim]\n"
                f"[dim]Pool: {self.pool_size} Läufe, davon höchstens {self.full_slots} Vollabgleiche[/dim]",
                border_style="green",
            )
        )
        if self._orchestrator is None:
            orchestrator = SyncOrchestrator(max_concurrent=self.max_concurrent)
            await orchestrator.__aenter__()
            self._orchestrator = orchestrator
        # Mehrere Quellen laufen zugleich: keine Fortschrittsbalken (Rich erlaubt nur eine Live-Anzeige)
        self._orchestrator._parallel_mode = True
        self._event()
        self._listener_task = asyncio.create_task(self._listen_for_triggers())
        self._loop_task = asyncio.create_task(self.run_forever())
        console.print("[green]Scheduler started successfully[/green]")

    async def stop(self) -> None:
        """Schleife, Empfang und laufende Abgleiche beenden."""
        console.print("[yellow]Stopping scheduler...[/yellow]")
        tasks = [t for t in (self._loop_task, self._listener_task, *self._tasks.values()) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = self._listener_task = None
        if self._owns_orchestrator and self._orchestrator is not None:
            await self._orchestrator.__aexit__(None, None, None)
            self._orchestrator = None
        if self._redis is not None:
            with contextlib.suppress(Exception):  # beim Beenden unerheblich
                await self._redis.aclose()
            self._redis = None
        console.print("[green]Scheduler stopped[/green]")

    async def run_forever(self) -> None:
        """Fällige Quellen starten, dann bis zum nächsten Durchlauf oder Weckruf warten."""
        wake = self._event()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Takt: Durchlauf fehlgeschlagen")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=TICK_SECONDS)
            wake.clear()

    # ========== Auslöser ==========

    def request_sync(
        self,
        full: bool = False,
        source_id: str | None = None,
        source_url: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> None:
        """Alle Quellen oder eine (``source_id`` bzw. ``source_url``) sofort fällig machen (Admin-Auslöser)."""
        if source_id:
            key = str(source_id)
        elif source_url:
            key = "url:" + str(source_url).rstrip("/")
        else:
            key = ALL_SOURCES
        trigger = _Trigger(full=bool(full), request_id=request_id, trace_id=trace_id, span_id=span_id)
        existing = self._triggers.get(key)
        self._triggers[key] = existing.merge(trigger) if existing is not None else trigger
        self._event().set()

    def _expand_triggers(self, sources: list[Any]) -> None:
        """Auslöser für alle Quellen oder per Adresse auf die einzelnen aktiven Quellen verteilen."""
        if not self._triggers:
            return
        active = {str(source.id) for source in sources}
        by_url = {str(source.url).rstrip("/"): str(source.id) for source in sources}
        for key in list(self._triggers):
            if key in active:
                continue
            trigger = self._triggers.pop(key)
            if key == ALL_SOURCES:
                targets = sorted(active)
            elif key.startswith("url:") and key[4:] in by_url:
                targets = [by_url[key[4:]]]
            else:
                console.print(f"[yellow]Sync-Auslöser verworfen: keine aktive Quelle ({escape(key)})[/yellow]")
                continue
            for target in targets:
                existing = self._triggers.get(target)
                self._triggers[target] = existing.merge(trigger) if existing is not None else trigger

    async def _listen_for_triggers(self) -> None:
        """Hört auf Redis-Events vom Django-Admin ('Jetzt synchronisieren'), für alle Quellen oder eine."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.warning("redis.asyncio nicht verfügbar, Trigger-Listener deaktiviert")
            return

        while True:
            client: Any = None
            pubsub: Any = None
            pause = 1.0
            try:
                client = aioredis.from_url(settings.redis_url)
                pubsub = client.pubsub()
                await pubsub.subscribe(TRIGGER_CHANNEL)
                console.print(f"[dim]Redis trigger listener aktiv ({TRIGGER_CHANNEL})[/dim]")
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    self._handle_trigger(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Redis trigger listener Fehler: %s, neuer Versuch in %s s", type(e).__name__, TRIGGER_RETRY_SECONDS
                )
                pause = TRIGGER_RETRY_SECONDS
            finally:
                # Vor dem nächsten Versuch schließen: Bei langem Redis-Ausfall sammelten sich sonst offene Clients an
                for closable in (pubsub, client):
                    if closable is not None:
                        with contextlib.suppress(Exception):
                            await closable.aclose()
            await asyncio.sleep(pause)

    def _handle_trigger(self, raw: Any) -> None:
        """Eine Nachricht des Kanals ``mandari:sync:trigger`` verarbeiten."""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Sync-Trigger ohne gültiges JSON verworfen")
            return
        if not isinstance(data, dict):
            logger.warning("Sync-Trigger ohne Objekt verworfen")
            return
        full = data.get("full") is True
        source_id = data.get("source_id") or None
        source_url = data.get("source_url") or None
        request_id = data.get("request_id") or None
        trace_id = data.get("trace_id") or None
        span_id = data.get("span_id") or None
        console.print(
            f"\n[bold yellow]Sync-Trigger empfangen (full={full}, "
            f"Quelle={escape(str(source_id or source_url or 'alle'))}, "
            f"request_id={escape(str(request_id or '-'))}, trace_id={escape(str(trace_id or '-'))})[/bold yellow]"
        )
        with trigger_context("sync.trigger", request_id, trace_id, span_id):
            logger.info("Sync-Trigger empfangen", extra={"full": full, "source": source_id or source_url or "*"})
        self.request_sync(
            full=full,
            source_id=str(source_id) if source_id else None,
            source_url=str(source_url) if source_url else None,
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
        )

    # ========== Takt ==========

    async def tick(self, now: datetime | None = None) -> list[asyncio.Task[None]]:
        """
        Ein Durchlauf: fällige Quellen in den Pool geben. Liefert die gestarteten Läufe (Tests warten darauf).

        Reihenfolge: Admin-Auslöser zuerst, dann nach Fälligkeit. Freie Plätze für Vollabgleiche bekommen die Quellen,
        deren letzter begonnener Vollabgleich am längsten zurückliegt (ohne zuerst, angeforderte vorweg); sie starten
        vor den übrigen ohne Auslöser. So verdrängen lange Vollabgleiche nicht Nacht für Nacht dieselben Quellen aus
        dem Fenster. Eine Quelle in Schonung startet nie, auch nicht auf einen Auslöser hin (der Auslöser entfällt
        dann). Ebenso eine Quelle außerhalb ihres Abruffensters (``sync_window``, Issue #1186).

        Je Server starten höchstens ``host_slots`` Quellen zugleich. Quellen aus einem abgebrochenen Lauf starten
        zuletzt und nie zwei zugleich (``_suspects_last``). Der Lauf einer inzwischen abgeschalteten Quelle wird
        beendet.
        """
        now = now or self._clock()
        if not self._recovered:
            await self._recover_aborted(list(await self.orchestrator.storage.get_all_sources()))
            self._recovered = True
        sources = list(await self.orchestrator.storage.get_all_sources())
        self._expand_triggers(sources)
        active = {str(source.id) for source in sources}
        for key, task in list(self._tasks.items()):
            if key not in active:
                # Abgeschaltet (im Admin oder mit der Rücknahme einer Veröffentlichung): Der Lauf schriebe sonst
                # weiter in den Bestand und hielte seinen Platz, bis er von selbst endet
                task.cancel()
        for gone in [key for key in self._overview if key not in active]:
            self._overview.pop(gone, None)
        for gone in [key for key in self._unsaved if key not in active]:
            self._unsaved.pop(gone, None)

        candidates: list[_Candidate] = []
        for source in sources:
            key = str(source.id)
            state = self._state_of(source)
            options = SourceFetchOptions.from_sync_config(source.sync_config)
            window = options.sync_window
            until = source_backoff_until(source, now)
            self._publish_schedule(source, state, until, window)
            if until is not None:
                self._note_backoff(source, until)
                if self._triggers.pop(key, None) is not None:
                    console.print(
                        f"[yellow]Sync-Auslöser für {escape(source.name)} verworfen: Schonung bis "
                        f"{until:%d.%m. %H:%M} UTC[/yellow]"
                    )
                continue
            if window is not None and window.closes_at(now) is None:
                # Außerhalb des Abruffensters: kein Start, weder nach Takt noch auf einen Auslöser hin
                self._note_window(source, window, now)
                if self._triggers.pop(key, None) is not None:
                    note = escape(outside_window_note(source.name, window, now))
                    console.print(f"[yellow]Sync-Auslöser verworfen: {note}[/yellow]")
                continue
            if key in self._tasks:
                continue  # höchstens ein Lauf je Quelle; ein Auslöser wartet auf das Ende
            trigger = self._triggers.get(key)
            full_due = full_sync_due(
                options,
                self.default_window,
                now,
                state.full_attempted,
                getattr(source, "last_successful_full_sync", None),
            )
            incremental_due = state.next_due is None or state.next_due <= now
            if trigger is None and not full_due and not incremental_due:
                continue
            want_full = full_due or (trigger is not None and trigger.full)
            candidates.append(_Candidate(source, state, want_full, incremental_due, trigger))

        candidates = self._suspects_last(candidates, now)
        free_full = max(0, self.full_slots - len(self._full_running))
        granted = {c.key for c in sorted((c for c in candidates if c.want_full), key=_Candidate.full_rank)[:free_full]}
        # Auslöser zuerst, dann die Vollabgleiche mit Platz, dann nach Fälligkeit
        candidates.sort(key=lambda c: (c.trigger is None, c.key not in granted, c.due_rank()))
        running_on = Counter(self._task_hosts.values())
        started: list[asyncio.Task[None]] = []
        for candidate in candidates:
            if len(self._tasks) >= self.pool_size:
                break
            source, trigger, key = candidate.source, candidate.trigger, candidate.key
            host = host_of(source.url)
            if running_on[host] >= self.host_slots:
                continue  # der Server hat seinen Anteil am Pool; die Quelle bleibt fällig, ein Auslöser wartet
            full = candidate.want_full
            if full and key not in granted:
                # Kein Platz für einen weiteren Vollabgleich: Ein ausdrücklich angeforderter wartet; sonst läuft der
                # fällige Änderungsabgleich, und der Vollabgleich folgt, sobald ein Platz frei ist
                if candidate.triggered_full or (trigger is None and not candidate.incremental_due):
                    continue
                full = False
            self._triggers.pop(key, None)
            task = asyncio.create_task(self._run_source(source, full, trigger), name=f"sync:{source.name}")
            self._tasks[key] = task
            self._task_hosts[key] = host
            running_on[host] += 1
            if full:
                self._full_running.add(key)
            if candidate.state.suspect:
                self._suspect_running = key
            started.append(task)
        metrics.set_active_syncs(len(self._tasks))
        await self._heartbeat(now)
        return started

    def _suspects_last(self, candidates: list[_Candidate], now: datetime) -> list[_Candidate]:
        """
        Fällige Quellen dieses Durchlaufs; die aus einem abgebrochenen Lauf (``SyncState.suspect``) nur, wenn sie an
        der Reihe sind.

        Endete der Daemon mitten in mehreren Läufen, ist offen, welche Quelle der Auslöser war. Die Beteiligten
        starten deshalb zuletzt: erst wenn nichts anderes fällig und der Pool leer ist, und nie zwei von ihnen
        zugleich. Die übrigen Quellen sind bis dahin gelaufen und kommen bei jedem Neustart wieder zuerst an die
        Reihe. Endet der Prozess wieder mitten im Lauf einer solchen Quelle, zählt das als ihr Fehlversuch.

        Andere Quellen hält das nie auf: Sie starten wie sonst, auch während eine solche Quelle läuft. Wird der Pool
        nicht leer (lange Vollabgleiche), startet die Quelle nach ``SUSPECT_MAX_WAIT`` neben den anderen.
        """
        suspects = sorted((c for c in candidates if c.state.suspect), key=_Candidate.due_rank)
        waiting = {c.key for c in suspects}
        self._suspect_since = {key: since for key, since in self._suspect_since.items() if key in waiting}
        for candidate in suspects:
            self._suspect_since.setdefault(candidate.key, now)
        regular = [c for c in candidates if not c.state.suspect]
        if not suspects or self._suspect_running is not None:
            return regular
        first = suspects[0]
        if not regular and not self._tasks:
            return [first]
        if now - self._suspect_since[first.key] >= SUSPECT_MAX_WAIT:
            return [*regular, first]
        return regular

    async def _recover_aborted(self, sources: list[Any]) -> None:
        """
        Einmal nach dem Start: Läufe zuordnen, die begonnen und nie beendet wurden.

        Der Zustand eines Laufs entsteht erst an seinem Ende. Endet der Prozess vorher hart (Speicherlimit), blieb
        die Quelle bisher unverändert fällig, ohne Fehlversuch und ohne Schonung, und war bei jedem Neustart wieder
        unter den ersten. Jetzt gilt: Wer in einem abgebrochenen Lauf steckte, läuft als Nächstes zuletzt
        (``suspect``, ``_suspects_last``), noch ohne Fehlversuch, denn andere Quellen liefen mit. Endet der Prozess
        auch mitten in diesem Lauf, zählt das als Fehlversuch dieser Quelle; nach den üblichen drei greift die
        Schonung, und der Betriebsmonitor nennt Quelle und Grund.
        """
        for source in sources:
            state = self._state_of(source)
            if state.run_started is None:
                continue
            name = escape(source.name)
            if state.suspect:
                await self._record_failure(source, ABORTED_RUN_ERROR)
                console.print(f"[red]Quelle {name}: Lauf endete erneut mit dem Daemon, Fehlversuch gezählt[/red]")
            else:
                console.print(
                    f"[yellow]Quelle {name}: Lauf vom {state.run_started:%d.%m. %H:%M} UTC nicht beendet, "
                    "nächster Lauf nach den übrigen Quellen[/yellow]"
                )
            await self._save_state(source, replace(state, run_started=None, suspect=True))

    async def _record_failure(self, source: Any, message: str) -> None:
        """Fehlversuch an der Quelle festhalten (Zähler für Schonung und Betriebsmonitor); stört den Takt nie."""
        try:
            await self.orchestrator.storage.record_source_failure(source.url, message)
        except Exception as e:  # noqa: BLE001 - ohne Zähler gilt weiter der Takt
            logger.warning("Fehlversuch der Quelle %s nicht gespeichert: %s", source.name, type(e).__name__)

    def _note_backoff(self, source: Any, until: datetime) -> None:
        """Schonung einmal je Frist melden, nicht in jedem Durchlauf."""
        key = str(source.id)
        if self._backoff_shown.get(key) == until:
            return
        self._backoff_shown[key] = until
        reason = ERROR_KIND_LABELS.get(
            getattr(source, "last_error_kind", None) or "",
            f"{getattr(source, 'consecutive_failures', 0)} Fehlversuche in Folge",
        )
        console.print(
            f"[yellow]Quelle {escape(source.name)}: {reason} — Schonung, nächster Versuch nach "
            f"{until:%d.%m. %H:%M} UTC[/yellow]"
        )

    def _note_window(self, source: Any, window: FullSyncWindow, now: datetime) -> None:
        """Wartet eine Quelle auf ihr Abruffenster, einmal je Fenster melden, nicht in jedem Durchlauf."""
        key = str(source.id)
        opens = window.next_open(now)
        if self._window_shown.get(key) == opens:
            return
        self._window_shown[key] = opens
        console.print(
            f"[dim]Quelle {escape(source.name)}: außerhalb des Abruffensters {window.label()}, nächster Lauf ab "
            f"{opens.astimezone(window.tz):%d.%m. %H:%M} Uhr[/dim]"
        )

    def _publish_schedule(
        self, source: Any, state: SyncState, until: datetime | None, window: FullSyncWindow | None = None
    ) -> None:
        """
        Nächsten Lauf (nach Schonung und Abruffenster), Abstand und Ruhe der Quelle als Metrik und für den Status
        festhalten. Liegt der fällige Lauf außerhalb des Abruffensters, gilt der Beginn des nächsten Fensters.
        """
        now = self._clock()
        due = state.next_due
        if until is not None and (due is None or until > due):
            due = until
        if window is not None:
            base = due if due is not None else now
            if window.closes_at(base) is None:
                due = window.next_open(max(base, now))
        self._overview[str(source.id)] = (source.name, due)
        metrics.record_source_schedule(
            source.name,
            due.timestamp() if due is not None else now.timestamp(),
            float((state.interval_minutes or 0) * 60),
            state.idle_runs,
        )
        metrics.record_source_window(source.name, None if window is None else window.closes_at(now) is not None)

    # ========== Lauf einer Quelle ==========

    async def _run_source(self, source: Any, full: bool, trigger: _Trigger | None) -> None:
        key = str(source.id)
        try:
            if trigger is not None:
                with trigger_context("sync.trigger", trigger.request_id, trigger.trace_id, trigger.span_id):
                    await self._sync_one(source, full, "admin")
            else:
                await self._sync_one(source, full, "daemon")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Takt: Lauf der Quelle %s fehlgeschlagen", getattr(source, "name", key))
        finally:
            self._tasks.pop(key, None)
            self._task_hosts.pop(key, None)
            self._current.pop(key, None)
            self._full_running.discard(key)
            if self._suspect_running == key:
                self._suspect_running = None
            metrics.set_active_syncs(len(self._tasks))
            self._maybe_clear_caches()
            self._event().set()

    async def _sync_one(self, source: Any, full: bool, triggered_by: str) -> None:
        """
        Ein Lauf mit Buchführung: Beginn festhalten, abgleichen, Zustand fortschreiben.

        Der Beginn steht in der Datenbank, bevor die erste Anfrage hinausgeht (``run_started``; beim Vollabgleich
        dazu ``full_attempted``, einmal je Fenster). Jedes Ende schreibt ihn fort: das gewöhnliche über
        ``SyncState.after_run``, ein geordneter Abbruch und ein Fehler neben dem Abgleich hier. Nur wenn der Prozess
        endet, bleibt er stehen, und der nächste Start ordnet ihn zu (``_recover_aborted``).
        """
        started = self._clock()
        state = self._state_of(source)
        state = replace(state, run_started=started, full_attempted=started if full else state.full_attempted)
        key = str(source.id)
        try:
            # Im geschützten Teil: Kommt der Abbruch während dieses Speicherns, kann der Beginn schon in der
            # Datenbank stehen, und der Abbruch unten nimmt ihn wieder heraus
            await self._save_state(source, state)
            await self._sync_and_record(source, full, triggered_by, started, state)
        except asyncio.CancelledError:
            # Geordnetes Ende (Daemon stoppt, Quelle abgeschaltet): kein abgebrochener Lauf. Das Speichern läuft zu
            # Ende, auch wenn der Abbruch noch einmal kommt, hält das Ende des Daemons aber nur begrenzt auf: Hängt
            # die Datenbank, ordnet der nächste Start den Lauf zu (``_recover_aborted``)
            ended = replace(self._current.get(key, state), run_started=None)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._save_state(source, ended)), CANCEL_SAVE_SECONDS)
            raise
        except Exception:
            # Fehler neben dem Abgleich (Zustand, Protokoll, Ausgabe): Ohne neuen Zeitpunkt bliebe die Quelle fällig
            # und begänne sofort wieder, ohne Pause und ohne Grenze
            await self._save_state(source, self._deferred(source, self._current.get(key, state)))
            raise

    def _deferred(self, source: Any, state: SyncState) -> SyncState:
        """Zustand nach einem Lauf, der mit einem Fehler neben dem Abgleich endete: nächster Lauf nach dem Takt."""
        if state.run_started is None:
            return state  # das Ende des Laufs ist schon fortgeschrieben
        schedule = SyncSchedule.from_sync_config(source.sync_config, self.sync_interval)
        window = SourceFetchOptions.from_sync_config(source.sync_config).sync_window
        return state.after_run(schedule, self._clock(), success=False, changes=0, full=False, window=window)

    async def _sync_and_record(
        self, source: Any, full: bool, triggered_by: str, started: datetime, state: SyncState
    ) -> None:
        art = "Vollabgleich" if full else "Änderungsabgleich"
        name = escape(source.name)
        console.print(f"\n[blue][{started:%H:%M:%S} UTC] {name}: {art} beginnt ({triggered_by})[/blue]")

        limit = self.full_deadline if full else self.run_deadline
        deadline = asyncio.timeout(limit)
        overrun = False
        try:
            async with deadline:
                # Das Standardfenster begrenzt den Vollabgleich in Zeitscheiben ohne eigenes Fenster (Issue #1033)
                result = await self.orchestrator.sync_body_url(
                    source.url, full=full, default_window=self.default_window
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            overrun = deadline.expired()
            if overrun and limit is not None:
                # Der Lauf hielt seinen Platz bis zur Gesamtfrist: beendet, Platz frei, Fehlversuch der Quelle. Der
                # Orchestrator hält ihn in diesem Fall nicht selbst fest
                message = f"{DEADLINE_ERROR} ({limit / 60:.0f} min)"
                logger.warning("Sync der Quelle %s beendet: %s", source.name, message)
                await self._record_failure(source, message)
            else:
                logger.exception("Sync der Quelle %s fehlgeschlagen", source.name)
                message = f"{type(e).__name__}: {e}"
            result = SyncResult(
                source_url=source.url,
                source_name=source.name,
                success=False,
                errors=[message],
                full=full,
            )
        finished = self._clock()
        window = SourceFetchOptions.from_sync_config(source.sync_config).sync_window
        if result.outside_window and window is not None:
            # Das Abruffenster schloss zwischen Durchlauf und Beginn des Laufs: nichts abgerufen, kein Protokoll und
            # kein Lauf in den Metriken; fällig zu Beginn des nächsten Fensters (Issue #1186). Zeigt die Uhr des
            # Daemons noch das alte Fenster, gilt das Fenster danach
            reopens = window.next_open(finished)
            closes = window.closes_at(finished)
            if reopens <= finished and closes is not None:
                reopens = window.next_open(closes)
            skipped = replace(state, next_due=reopens, reason=REASON_WINDOW, run_started=None)
            await self._save_state(source, skipped)
            self._publish_schedule(source, skipped, None, window)
            note = escape("; ".join(result.errors) or outside_window_note(source.name, window))
            console.print(f"[yellow]{note}[/yellow]")
            return
        changes = entities_of(result) if result.success else 0

        schedule = SyncSchedule.from_sync_config(source.sync_config, self.sync_interval)
        meeting_days = await self._meeting_days(source, schedule, finished)
        new_state = state.after_run(
            schedule,
            finished,
            success=result.success,
            changes=changes,
            full=result.full,
            meeting_days=meeting_days,
            window=window,
        )
        if overrun and limit is not None and new_state.next_due is not None:
            # Wer seinen Platz bis zur Frist gehalten hat, ist frühestens nach derselben Dauer wieder an der Reihe:
            # Eine Quelle, die jeden Lauf bis zur Frist hält, belegt so höchstens die halbe Zeit einen Platz
            new_state = replace(new_state, next_due=max(new_state.next_due, finished + timedelta(seconds=limit)))
        await self._save_state(source, new_state)

        requests = result.http_stats.http_requests if result.http_stats is not None else 0
        duration = max(0.0, (finished - started).total_seconds())
        status = sync_log_status(result)
        sync_type = "full" if result.full else "incremental"
        await self._write_log(source, result, started, finished, triggered_by, new_state, requests, window)
        metrics.record_source_run(source.name, sync_type, status, duration, requests, finished.timestamp())
        # Eine neue Schonung nach diesem Lauf steht erst in der Datenbank; der nächste Durchlauf zeigt sie
        self._publish_schedule(source, new_state, None, window)
        self._last_runs[str(source.id)] = {
            "source": source.name,
            "type": sync_type,
            "status": status,
            "started": started.isoformat(timespec="seconds"),
            "duration": round(duration, 1),
            "requests": requests,
            "changes": changes,
            "next_due": new_state.next_due.isoformat(timespec="seconds") if new_state.next_due else None,
            **({"reason": new_state.reason} if new_state.reason else {}),
        }

        self.orchestrator.print_result(result)
        next_local = new_state.next_due.astimezone(schedule.tz) if new_state.next_due else None
        held = f"; Abruffenster {window.label()}" if window is not None and new_state.reason == REASON_WINDOW else ""
        console.print(
            f"[green]{name}: {art} fertig in {duration:.1f}s, {requests} Abrufe, {changes} Änderungen; "
            f"nächster Lauf {next_local:%d.%m. %H:%M} ({new_state.interval_minutes} min, {new_state.period}, "
            f"{new_state.idle_runs} Läufe ohne Änderung{held})[/green]"
            if next_local is not None
            else f"[green]{name}: {art} fertig in {duration:.1f}s[/green]"
        )
        if result.success and settings.datenabgleich_enabled:
            await self._datenabgleich(source)

    async def _datenabgleich(self, source: Any) -> None:
        """
        Datenabgleich mit der Schnittstelle der Kommune (Issue #1525, ``src/sync/datenabgleich.py``) nach einem
        erfolgreichen Lauf, höchstens einmal täglich je Quelle. Der Versuch zählt auch, wenn er scheitert oder die Frist
        erreicht: Eine Quelle, deren Listen nicht antworten, hält ihren Platz so nicht nach jedem Lauf erneut fest.
        """
        key = str(source.id)
        now = self._clock()
        versucht = self._datenabgleich_versuche.get(key)
        if (versucht is not None and now - versucht < datenabgleich.ABSTAND) or not datenabgleich.faellig(source, now):
            return
        self._datenabgleich_versuche[key] = now
        await datenabgleich.im_daemon(self.orchestrator.storage, source, now)

    async def _meeting_days(self, source: Any, schedule: SyncSchedule, at: datetime) -> frozenset[Any]:
        """Tage mit kurzem Takt aus den Sitzungen der Quelle im Bestand (ab heute, Ortszeit)."""
        today = at.astimezone(schedule.tz).date()
        start = datetime.combine(today, time(0, 0), tzinfo=schedule.tz).astimezone(UTC)
        end = start + timedelta(days=schedule.meeting_days_before + 3)
        try:
            starts = await self.orchestrator.storage.get_meeting_starts(source.id, start, end)
        except Exception as e:  # noqa: BLE001 - ohne Sitzungstage gilt der übrige Takt
            logger.warning("Sitzungstage der Quelle %s nicht lesbar: %s", source.name, type(e).__name__)
            return frozenset()
        return schedule.meeting_days(starts)

    def _state_of(self, source: Any) -> SyncState:
        """
        Zustand des Takts: aus der Datenbank, solange dort der neueste Stand steht. Ließ sich der zuletzt berechnete
        nicht speichern, gilt er, bis die Datenbank einen neueren Lauf kennt (sonst begänne die Quelle sofort wieder).
        """
        state = SyncState.from_sync_config(source.sync_config)
        key = str(source.id)
        unsaved = self._unsaved.get(key)
        if unsaved is None:
            return state
        if state.last_run is not None and (unsaved.last_run is None or state.last_run > unsaved.last_run):
            self._unsaved.pop(key, None)
            return state
        return unsaved

    async def _save_state(self, source: Any, state: SyncState) -> None:
        """Zustand des Takts an der Quelle speichern (nur der eigene Schlüssel, unter Zeilensperre)."""
        key = str(source.id)
        if key in self._tasks:
            self._current[key] = state
        try:
            await self.orchestrator.storage.set_source_sync_config_value(source.id, STATE_KEY, state.to_dict())
        except Exception as e:  # noqa: BLE001 - bis zum nächsten Speichern gilt der Zustand im Speicher
            self._unsaved[key] = state
            logger.warning("Takt der Quelle %s nicht gespeichert: %s", source.name, type(e).__name__)
        else:
            self._unsaved.pop(key, None)

    async def _write_log(
        self,
        source: Any,
        result: SyncResult,
        started: datetime,
        finished: datetime,
        triggered_by: str,
        state: SyncState,
        requests: int,
        window: FullSyncWindow | None = None,
    ) -> None:
        """
        Ein Sync-Protokoll je Lauf und Quelle (``insight_sync_synclog``). Unter ``details.takt`` stehen mit Abruffenster
        auch das Fenster (``window``) und, wenn es den nächsten Lauf verschoben hat, der Grund (``reason``).
        """
        takt: dict[str, Any] = {
            "period": state.period,
            "interval_minutes": state.interval_minutes,
            "idle_runs": state.idle_runs,
            "next_due": state.next_due.isoformat(timespec="seconds") if state.next_due else None,
        }
        if state.reason:
            takt["reason"] = state.reason
        if window is not None:
            takt["window"] = window.label()
        details: dict[str, Any] = {
            "organizations": result.organizations_synced,
            "persons": result.persons_synced,
            "memberships": result.memberships_synced,
            "meetings": result.meetings_synced,
            "papers": result.papers_synced,
            "files": result.files_synced,
            "locations": result.locations_synced,
            "agenda_items": result.agenda_items_synced,
            "consultations": result.consultations_synced,
            "requests": requests,
            "takt": takt,
            **SyncOrchestrator._finding_details(result),
        }
        if result.unstored:
            details["unstored"] = dict(sorted(result.unstored.items()))
        # Davon zurückgestellt: halten das Änderungsfenster nicht mehr (``settle_unstored_holds``, Issue #1528)
        if result.unstored_deferred:
            details["unstored_deferred"] = dict(sorted(result.unstored_deferred.items()))
        # Kennungen anderer Quellen, die der Abgleich abgewiesen hat (nichts davon geschrieben)
        if result.foreign:
            details["foreign"] = dict(sorted(result.foreign.items()))
        # Verweise, die sich im Bestand nicht auflösen ließen (Issue #1441)
        if result.unresolved:
            details["unresolved"] = dict(sorted(result.unresolved.items()))
        # Zeitscheiben des Vollabgleichs und ausgesetzte langsame Listen (Issue #1033)
        details.update(result.slice_details)
        try:
            await self.orchestrator.storage.write_sync_log(
                source_id=source.id,
                sync_type="full" if result.full else "incremental",
                status=sync_log_status(result),
                started_at=started,
                finished_at=finished,
                duration_seconds=max(0.0, (finished - started).total_seconds()),
                entities_synced=entities_of(result),
                errors=list(result.errors[:20]),
                details=details,
                triggered_by=triggered_by,
            )
        except Exception as e:  # noqa: BLE001 - das Protokoll darf den Takt nie stören
            console.print(
                f"[yellow]Warning: SyncLog für {escape(source.name)} nicht geschrieben: {type(e).__name__}[/yellow]"
            )

    def _maybe_clear_caches(self) -> None:
        """
        Zwischenspeicher (Kennungen, FK-UUIDs) leeren, sobald kein Lauf mehr aktiv ist, spätestens alle
        ``CACHE_CLEAR_SECONDS``: Der Orchestrator lebt so lange wie der Daemon, die Caches wüchsen sonst (Issue #22).
        Leeren mitten in einem Lauf ist unschädlich, danach wird nachgeschlagen.
        """
        now = self._clock()
        if self._orchestrator is None:
            return
        if self._tasks and now - self._last_cache_clear < timedelta(seconds=CACHE_CLEAR_SECONDS):
            return
        self._last_cache_clear = now
        for owner, method in (("storage", "clear_uuid_caches"), ("processor", "clear_id_cache")):
            clear = getattr(getattr(self._orchestrator, owner, None), method, None)
            if callable(clear):
                clear()

    # ========== Lebenszeichen und Status ==========

    async def _heartbeat(self, now: datetime) -> None:
        """Lebenszeichen in Redis (läuft ab, wenn der Daemon steht); Fehler nur einmal melden."""
        if not self._heartbeat_enabled:
            return
        try:
            if self._redis is None:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
            names = sorted(task.get_name().removeprefix("sync:") for task in self._tasks.values())
            await self._redis.set(
                HEARTBEAT_KEY,
                json.dumps({"at": now.isoformat(timespec="seconds"), "running": names, "pool": self.pool_size}),
                ex=HEARTBEAT_TTL_SECONDS,
            )
            self._redis_warned = False
        except Exception as e:  # noqa: BLE001 - ohne Redis gilt das Sync-Protokoll als Lebenszeichen
            if not self._redis_warned:
                logger.warning("Lebenszeichen in Redis nicht geschrieben: %s", type(e).__name__)
                self._redis_warned = True

    def get_status(self) -> dict[str, Any]:
        """Laufende Abgleiche, nächste Fälligkeiten und letzte Läufe je Quelle."""
        upcoming = sorted(
            ((due, name) for name, due in self._overview.values() if due is not None),
            key=lambda item: item[0],
        )
        return {
            "running": sorted(task.get_name().removeprefix("sync:") for task in self._tasks.values()),
            "pool_size": self.pool_size,
            "next_due": [(name, due.isoformat(timespec="seconds")) for due, name in upcoming[:5]],
            "pending_triggers": sorted(self._triggers),
            "last_runs": dict(self._last_runs),
        }


def print_overview(scheduler: SyncScheduler) -> None:
    """
    Kurzer Überblick des Takts auf der Konsole: laufende Abgleiche und die nächsten fälligen Quellen.

    Die Namen stammen aus fremden Quellen und werden nicht als Markup ausgewertet. Ein Fehler hier bleibt ohne Folge
    für den Daemon: Die Ausgabe steht in der Hauptschleife, eine Ausnahme beendete sonst alle laufenden Abgleiche.
    """
    try:
        status = scheduler.get_status()
        running = escape(", ".join(status["running"]) or "–")
        upcoming = escape(", ".join(f"{name} {due}" for name, due in status["next_due"][:3]) or "–")
        console.print(f"[dim]Takt: laufend {running}; nächste fällig {upcoming}[/dim]", highlight=False)
    except Exception:  # noqa: BLE001 - die Übersicht darf den Daemon und seine Läufe nie beenden
        logger.exception("Takt: Übersicht nicht ausgegeben")


async def run_scheduler(
    sync_interval: int | None = None,
    full_sync_hour: int | None = 3,
    max_concurrent: int = 10,
    metrics_port: int = 9090,
    pool_size: int | None = None,
) -> None:
    """
    Run the sync scheduler continuously.

    Args:
        sync_interval: Grundtakt werktags in der Arbeitszeit (Minuten).
        full_sync_hour: Beginn des Standardfensters für Vollabgleiche.
        max_concurrent: Max concurrent HTTP requests je Lauf.
        metrics_port: Port for Prometheus metrics server.
        pool_size: Läufe gleichzeitig.
    """
    scheduler = SyncScheduler(
        sync_interval_minutes=sync_interval,
        full_sync_hour=full_sync_hour,
        max_concurrent=max_concurrent,
        pool_size=pool_size,
    )

    # Singleton (#55): Nur eine Daemon-Instanz je Datenbank synchronisiert; eine zweite
    # wartet auf die PostgreSQL-Advisory-Sperre und übernimmt, wenn die erste endet.
    from src.scheduler.singleton import Fuehrung

    fuehrung = Fuehrung(settings.database_url)

    # ``docker stop`` und jeder Deploy senden SIGTERM. Ohne eigene Behandlung endete der Prozess sofort: Laufende
    # Abgleiche gälten beim nächsten Start als abgebrochen. So endet der Daemon geordnet wie nach Strg+C
    main_task = asyncio.current_task()
    if main_task is not None:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):  # Windows kennt das nicht
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        await metrics.start_server(port=metrics_port)

        await fuehrung.warten(
            lambda inhaber: console.print(f"[yellow]Andere Ingestor-Instanz aktiv ({inhaber}) – warte …[/yellow]")
        )

        await scheduler.start()

        # Alle zehn Minuten ein kurzer Überblick
        while True:
            await asyncio.sleep(600)
            print_overview(scheduler)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await scheduler.stop()
        await fuehrung.freigeben()
