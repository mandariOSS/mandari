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

Jeder Lauf schreibt sein eigenes Sync-Protokoll (``insight_sync_synclog`` mit ``source_id``) und Metriken je Quelle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any

from rich.panel import Panel

from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.metrics import metrics
from src.observability import trigger_context
from src.redaction import MaskingConsole
from src.scheduler.takt import (
    STATE_KEY,
    SyncSchedule,
    SyncState,
    default_full_sync_window,
    full_sync_due,
)
from src.sync.orchestrator import ERROR_KIND_LABELS, SyncOrchestrator, SyncResult, source_backoff_until

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
        self.default_window = default_full_sync_window(full_sync_hour)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._orchestrator = orchestrator
        self._owns_orchestrator = orchestrator is None
        self._heartbeat_enabled = heartbeat

        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._full_running: set[str] = set()
        self._triggers: dict[str, _Trigger] = {}
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._backoff_shown: dict[str, datetime] = {}
        self._last_cache_clear = self._clock()
        self._overview: dict[str, tuple[str, datetime | None]] = {}
        self._last_runs: dict[str, dict[str, Any]] = {}
        #: Zustände, die sich nicht speichern ließen; sie gelten, bis die Datenbank einen neueren Stand hat
        self._unsaved: dict[str, SyncState] = {}
        self._redis: Any = None
        self._redis_warned = False

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
                console.print(f"[yellow]Sync-Auslöser verworfen: keine aktive Quelle ({key})[/yellow]")
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
            f"\n[bold yellow]Sync-Trigger empfangen (full={full}, Quelle={source_id or source_url or 'alle'}, "
            f"request_id={request_id or '-'}, trace_id={trace_id or '-'})[/bold yellow]"
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
        dann).
        """
        now = now or self._clock()
        sources = list(await self.orchestrator.storage.get_all_sources())
        self._expand_triggers(sources)
        active = {str(source.id) for source in sources}
        for gone in [key for key in self._overview if key not in active]:
            self._overview.pop(gone, None)
        for gone in [key for key in self._unsaved if key not in active]:
            self._unsaved.pop(gone, None)

        candidates: list[_Candidate] = []
        for source in sources:
            key = str(source.id)
            state = self._state_of(source)
            until = source_backoff_until(source, now)
            self._publish_schedule(source, state, until)
            if until is not None:
                self._note_backoff(source, until)
                if self._triggers.pop(key, None) is not None:
                    console.print(
                        f"[yellow]Sync-Auslöser für {source.name} verworfen: Schonung bis {until:%d.%m. %H:%M} UTC"
                        "[/yellow]"
                    )
                continue
            if key in self._tasks:
                continue  # höchstens ein Lauf je Quelle; ein Auslöser wartet auf das Ende
            trigger = self._triggers.get(key)
            options = SourceFetchOptions.from_sync_config(source.sync_config)
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

        free_full = max(0, self.full_slots - len(self._full_running))
        granted = {c.key for c in sorted((c for c in candidates if c.want_full), key=_Candidate.full_rank)[:free_full]}
        # Auslöser zuerst, dann die Vollabgleiche mit Platz, dann nach Fälligkeit
        candidates.sort(key=lambda c: (c.trigger is None, c.key not in granted, c.due_rank()))
        started: list[asyncio.Task[None]] = []
        for candidate in candidates:
            if len(self._tasks) >= self.pool_size:
                break
            source, trigger, key = candidate.source, candidate.trigger, candidate.key
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
            if full:
                self._full_running.add(key)
            started.append(task)
        metrics.set_active_syncs(len(self._tasks))
        await self._heartbeat(now)
        return started

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
            f"[yellow]Quelle {source.name}: {reason} — Schonung, nächster Versuch nach {until:%d.%m. %H:%M} UTC"
            "[/yellow]"
        )

    def _publish_schedule(self, source: Any, state: SyncState, until: datetime | None) -> None:
        """Nächsten Lauf (nach Schonung), Abstand und Ruhe der Quelle als Metrik und für den Status festhalten."""
        due = state.next_due
        if until is not None and (due is None or until > due):
            due = until
        self._overview[str(source.id)] = (source.name, due)
        metrics.record_source_schedule(
            source.name,
            due.timestamp() if due is not None else self._clock().timestamp(),
            float((state.interval_minutes or 0) * 60),
            state.idle_runs,
        )

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
            self._full_running.discard(key)
            metrics.set_active_syncs(len(self._tasks))
            self._maybe_clear_caches()
            self._event().set()

    async def _sync_one(self, source: Any, full: bool, triggered_by: str) -> None:
        started = self._clock()
        state = self._state_of(source)
        if full:
            # Einmal je Fenster: Ein abgebrochener Vollabgleich wird im nächsten Fenster wiederholt
            state = replace(state, full_attempted=started)
            await self._save_state(source, state)
        art = "Vollabgleich" if full else "Änderungsabgleich"
        console.print(f"\n[blue][{started:%H:%M:%S} UTC] {source.name}: {art} beginnt ({triggered_by})[/blue]")

        try:
            result = await self.orchestrator.sync_body_url(source.url, full=full)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Sync der Quelle %s fehlgeschlagen", source.name)
            result = SyncResult(
                source_url=source.url,
                source_name=source.name,
                success=False,
                errors=[f"{type(e).__name__}: {e}"],
                full=full,
            )
        finished = self._clock()
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
        )
        await self._save_state(source, new_state)

        requests = result.http_stats.http_requests if result.http_stats is not None else 0
        duration = max(0.0, (finished - started).total_seconds())
        status = "success" if result.success else "failed"
        sync_type = "full" if result.full else "incremental"
        await self._write_log(source, result, started, finished, triggered_by, new_state, requests)
        metrics.record_source_run(source.name, sync_type, status, duration, requests, finished.timestamp())
        # Eine neue Schonung nach diesem Lauf steht erst in der Datenbank; der nächste Durchlauf zeigt sie
        self._publish_schedule(source, new_state, None)
        self._last_runs[str(source.id)] = {
            "source": source.name,
            "type": sync_type,
            "status": status,
            "started": started.isoformat(timespec="seconds"),
            "duration": round(duration, 1),
            "requests": requests,
            "changes": changes,
            "next_due": new_state.next_due.isoformat(timespec="seconds") if new_state.next_due else None,
        }

        self.orchestrator.print_result(result)
        next_local = new_state.next_due.astimezone(schedule.tz) if new_state.next_due else None
        console.print(
            f"[green]{source.name}: {art} fertig in {duration:.1f}s, {requests} Abrufe, {changes} Änderungen; "
            f"nächster Lauf {next_local:%d.%m. %H:%M} ({new_state.interval_minutes} min, {new_state.period}, "
            f"{new_state.idle_runs} Läufe ohne Änderung)[/green]"
            if next_local is not None
            else f"[green]{source.name}: {art} fertig in {duration:.1f}s[/green]"
        )

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
    ) -> None:
        """Ein Sync-Protokoll je Lauf und Quelle (``insight_sync_synclog``)."""
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
            "takt": {
                "period": state.period,
                "interval_minutes": state.interval_minutes,
                "idle_runs": state.idle_runs,
                "next_due": state.next_due.isoformat(timespec="seconds") if state.next_due else None,
            },
            **SyncOrchestrator._finding_details(result),
        }
        if result.unstored:
            details["unstored"] = dict(sorted(result.unstored.items()))
        try:
            await self.orchestrator.storage.write_sync_log(
                source_id=source.id,
                sync_type="full" if result.full else "incremental",
                status="success" if result.success else "failed",
                started_at=started,
                finished_at=finished,
                duration_seconds=max(0.0, (finished - started).total_seconds()),
                entities_synced=entities_of(result),
                errors=list(result.errors[:20]),
                details=details,
                triggered_by=triggered_by,
            )
        except Exception as e:  # noqa: BLE001 - das Protokoll darf den Takt nie stören
            console.print(f"[yellow]Warning: SyncLog für {source.name} nicht geschrieben: {type(e).__name__}[/yellow]")

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

    try:
        await metrics.start_server(port=metrics_port)

        await fuehrung.warten(
            lambda inhaber: console.print(f"[yellow]Andere Ingestor-Instanz aktiv ({inhaber}) – warte …[/yellow]")
        )

        await scheduler.start()

        # Alle zehn Minuten ein kurzer Überblick
        while True:
            await asyncio.sleep(600)
            status = scheduler.get_status()
            console.print(
                f"[dim]Takt: laufend {', '.join(status['running']) or '–'}; "
                f"nächste fällig {', '.join(f'{name} {due}' for name, due in status['next_due'][:3]) or '–'}[/dim]",
                highlight=False,
            )
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await scheduler.stop()
        await fuehrung.freigeben()
