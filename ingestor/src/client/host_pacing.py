# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host für den Ingestor (gemeinsamer Zeitstempel in Redis, siehe :mod:`mandari_oparl.pacing`).

Vor jeder Anfrage an eine Quelle reserviert der Ingestor einen Zeitpunkt für deren Host und wartet bis dahin.
Der Zeitstempel liegt in Redis und gilt damit über alle Quellen, Abrufplätze und Prozesse hinweg, auch für
Django (Dokument-Cache, Vorschau). Ist Redis nicht erreichbar, drosselt der Prozess für sich und versucht es
nach einer Minute erneut.

Zusätzlich laufen je Prozess und Host höchstens ``settings.host_max_concurrent`` Anfragen gleichzeitig
(:meth:`HostPacer.limit`, Reservierung und Anfrage zusammen). Sonst reservierte jeder freie Abrufplatz einen
eigenen Zeitpunkt, und der Takt je Host reichte so viele Sekunden in die Zukunft, wie es Plätze gibt. Folgt ein
Abruf einer Weiterleitung auf einen anderen Host, zieht sein Platz mit (:meth:`HostSlot.move_to`): Die Grenze gilt
auch für den Ziel-Host, und ein Abruf belegt nie Plätze bei zwei Hosts zugleich (keine gegenseitige Blockade).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mandari_oparl.pacing import RESERVE_SCRIPT, LocalSchedule, host_of, key

from src.config import settings
from src.redaction import mask_credentials

logger = logging.getLogger(__name__)

#: Nach einem Redis-Fehler so lange nur im Prozess drosseln
REDIS_RETRY_SECONDS = 60.0


class HostPacer:
    """Reserviert Zeitpunkte je Host; Redis, sonst im Prozess."""

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.redis_url = redis_url
        self.redis_enabled = True
        self._clock = clock
        self._sleep = sleep
        self._local = LocalSchedule()
        self._script: Any = None
        self._redis_down_until = 0.0
        # Grenze gleichzeitiger Anfragen je Host; Semaphoren gehören zur laufenden Ereignisschleife
        self._limits: dict[str, asyncio.Semaphore] = {}
        self._limits_loop: asyncio.AbstractEventLoop | None = None

    def reset(self) -> None:
        """Zustand im Prozess verwerfen (Tests)."""
        self._local.clear()
        self._script = None
        self._redis_down_until = 0.0
        self._limits = {}
        self._limits_loop = None

    def limit(self, url: str, interval: float) -> HostSlot:
        """
        Höchstens ``settings.host_max_concurrent`` laufende Anfragen je Host in diesem Prozess. Um Reservierung
        (:meth:`wait`) und Anfrage legen: ``async with host_pacer.limit(url) as slot: await wait(...); await
        get(...)``. So bleibt der reservierte Takt je Host kurz, und andere Prozesse (Vorschau) finden einen freien
        Zeitpunkt. Ohne Drossel (``interval`` 0) gibt es keine Reservierung und damit auch keine Grenze.

        Weiterleitungen auf einen anderen Host: ``slot`` an :func:`src.client.redirects.check_and_pace` geben, dann
        zieht der Platz vor der Anfrage an das Ziel dorthin um (:meth:`HostSlot.move_to`).
        """
        return HostSlot(self, url, interval)

    def _semaphore(self, host: str, interval: float) -> asyncio.Semaphore | None:
        """Grenze des Hosts in der laufenden Ereignisschleife; ``None`` ohne Grenze (abgeschaltet, ohne Drossel)."""
        size = settings.host_max_concurrent
        if size <= 0 or interval <= 0 or not host:
            return None
        loop = asyncio.get_running_loop()
        if self._limits_loop is not loop:
            self._limits = {}
            self._limits_loop = loop
        semaphore = self._limits.get(host)
        if semaphore is None:
            semaphore = self._limits[host] = asyncio.Semaphore(size)
        return semaphore

    def _get_script(self) -> Any:
        if self._script is None:
            import redis.asyncio as aioredis

            url = self.redis_url or settings.redis_url
            client = aioredis.from_url(url, socket_connect_timeout=1.0, socket_timeout=2.0)
            self._script = client.register_script(RESERVE_SCRIPT)
        return self._script

    async def _reserve_redis(self, host: str, interval: float, max_wait: float | None) -> float | None | bool:
        """Wartezeit in Sekunden, ``None`` (Höchstwartezeit überschritten) oder ``False`` (Redis nicht nutzbar)."""
        if not self.redis_enabled or self._clock() < self._redis_down_until:
            return False
        try:
            script = self._get_script()
            waited = await script(
                keys=[key(host)],
                args=[int(interval * 1000), int(max_wait * 1000) if max_wait is not None else -1],
            )
        except Exception as exc:  # noqa: BLE001 - jede Redis-Störung: im Prozess weiter drosseln
            logger.warning(
                "Drossel je Host: Redis %s nicht nutzbar (%s), drossle %d s nur im Prozess",
                mask_credentials(self.redis_url or settings.redis_url),
                type(exc).__name__,
                int(REDIS_RETRY_SECONDS),
            )
            self._script = None
            self._redis_down_until = self._clock() + REDIS_RETRY_SECONDS
            return False
        waited = int(waited)
        return None if waited < 0 else waited / 1000.0

    async def wait(self, url: str, interval: float, *, max_wait: float | None = None) -> bool:
        """
        Bis zum reservierten Zeitpunkt für den Host von ``url`` warten.

        ``interval``: Abstand zur nächsten Anfrage an diesen Host (Sekunden, 0 = keine Drossel).
        ``max_wait``: höchstens so lange warten; sonst nichts reservieren und ``False`` zurückgeben.
        """
        if interval <= 0:
            return True
        host = host_of(url)
        if not host:
            return True
        result = await self._reserve_redis(host, interval, max_wait)
        if result is False:
            result = self._local.reserve(host, self._clock(), interval, max_wait)
        if result is None:
            return False
        if result > 0:
            await self._sleep(result)
        return True


class HostSlot:
    """
    Platz eines Abrufs in der Grenze gleichzeitiger Anfragen je Host (:meth:`HostPacer.limit`).

    Als ``async with`` belegt er einen Platz beim Host der Adresse und gibt ihn am Ende zurück. Folgt der Abruf
    einer Weiterleitung auf einen anderen Host, zieht der Platz mit (:meth:`move_to`).
    """

    def __init__(self, pacer: HostPacer, url: str, interval: float) -> None:
        self._pacer = pacer
        self._url = url
        self._interval = interval
        self._host = ""
        self._held: asyncio.Semaphore | None = None

    async def __aenter__(self) -> HostSlot:
        await self._take(host_of(self._url))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._give_back()

    async def move_to(self, url: str) -> None:
        """
        Vor der Anfrage an ``url`` (Ziel einer Weiterleitung): Platz beim Host von ``url`` statt beim bisherigen;
        derselbe Host behält seinen Platz. Den bisherigen Platz gibt der Abruf vorher zurück: So hält er nie zwei
        Plätze, und Abrufe mit Weiterleitungen in Gegenrichtung blockieren einander nicht.
        """
        host = host_of(url)
        if host == self._host:
            return
        self._give_back()
        await self._take(host)

    async def _take(self, host: str) -> None:
        semaphore = self._pacer._semaphore(host, self._interval)
        if semaphore is not None:
            await semaphore.acquire()
        self._host = host
        self._held = semaphore

    def _give_back(self) -> None:
        held, self._held = self._held, None
        self._host = ""
        if held is not None:
            held.release()


#: Gemeinsamer Taktgeber aller Clients eines Prozesses
host_pacer = HostPacer()
