# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt-Prüfung des Ingestors (RFC 9309, Auswertung in :mod:`mandari_oparl.robots`).

Jeder Abruf bei einer Quelle – OParl-JSON, HTML-Seiten der Scraper, Dateien für die Textextraktion –
fragt vorher hier an. Die robots.txt eines Hosts wird einmal geladen und 24 Stunden im Prozess gehalten;
alle Clients und Quellen eines Prozesses teilen sich den Zwischenspeicher. Der Schlüssel ist Host und
User-Agent: Manche Server filtern Wörter im User-Agent und antworten dann auch auf ``/robots.txt`` mit 403
(gilt als „nicht vorhanden“). Eine Quelle mit eigenem User-Agent bekommt deshalb ihre eigene Antwort.

Ist die robots.txt nicht erreichbar (5xx, 408, 429, Netzfehler) und gibt es keine letzte gültige Fassung,
lautet die Entscheidung „nicht erreichbar“ (:attr:`mandari_oparl.robots.Decision.unreachable`): Aufrufer
stellen den Abruf zurück, statt ihn als gesperrt zu werten. Neuer Versuch nach 15 Minuten.

Abruf mit unserem User-Agent und ``Accept: text/plain``: Mit dem JSON-Standard des OParl-Clients
antworten manche Server mit 406, was als „nicht vorhanden“ (alles erlaubt) gälte.

Ausnahmen je Quelle stehen in ``sync_config["robots_override"]`` und brauchen einen Vermerk
(:func:`mandari_oparl.robots.robots_override`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from mandari_oparl.crawler import PRODUCT_TOKEN, product_token
from mandari_oparl.robots import (
    CACHE_SECONDS,
    RETRY_UNREACHABLE_SECONDS,
    STATE_UNREACHABLE,
    Decision,
    RobotsOverride,
    RobotsTxt,
    host_key,
    robots_url,
)

logger = logging.getLogger(__name__)

#: Zeitlimit für den Abruf einer robots.txt
ROBOTS_TIMEOUT = 15.0


@dataclass
class _Entry:
    robots: RobotsTxt
    fetched_at: float
    #: Letztes ausgewertetes Ergebnis; gilt weiter, solange die Datei danach nicht erreichbar ist
    last_good: RobotsTxt | None = None

    def fresh(self, now: float) -> bool:
        ttl = RETRY_UNREACHABLE_SECONDS if self.robots.state == STATE_UNREACHABLE else CACHE_SECONDS
        return now - self.fetched_at < ttl

    @property
    def effective(self) -> RobotsTxt:
        if self.robots.state == STATE_UNREACHABLE and self.last_good is not None:
            return self.last_good
        return self.robots


class RobotsGate:
    """Prozessweiter robots.txt-Zwischenspeicher je Host."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Vorgaben je Host für alle User-Agents (Tests, Werkzeuge), siehe seed()
        self._seeded: dict[str, RobotsTxt] = {}
        self._clock = clock

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._seeded.clear()

    @staticmethod
    def _key(url: str, user_agent: str) -> str:
        return f"{host_key(url)} {user_agent}"

    async def _load(
        self,
        client: httpx.AsyncClient | None,
        url: str,
        user_agent: str,
        pace: Callable[[str], Awaitable[None]] | None,
    ) -> RobotsTxt:
        seeded = self._seeded.get(host_key(url))
        if seeded is not None:
            return seeded
        key = self._key(url, user_agent)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            now = self._clock()
            if entry is not None and entry.fresh(now):
                return entry.effective
            target = robots_url(url)
            status: int | None = None
            body = b""
            try:
                if pace is not None:
                    await pace(target)
                response = await self._get(client, target, user_agent)
                status = response.status_code
                body = response.content
            except httpx.HTTPError as exc:
                logger.warning(
                    "robots.txt %s nicht abrufbar (%s), Abrufe gelten als gesperrt", target, type(exc).__name__
                )
            robots = RobotsTxt.from_response(status, body)
            if robots.state == STATE_UNREACHABLE:
                logger.warning("robots.txt %s nicht erreichbar (HTTP %s)", target, status)
            last_good = robots if robots.state != STATE_UNREACHABLE else (entry.effective if entry else None)
            if last_good is not None and last_good.state == STATE_UNREACHABLE:
                last_good = None
            new_entry = _Entry(robots=robots, fetched_at=now, last_good=last_good)
            self._entries[key] = new_entry
            return new_entry.effective

    @staticmethod
    async def _get(client: httpx.AsyncClient | None, target: str, user_agent: str) -> httpx.Response:
        headers = {"User-Agent": user_agent, "Accept": "text/plain"}
        if client is not None:
            return await client.get(target, headers=headers, timeout=ROBOTS_TIMEOUT, follow_redirects=True)
        async with httpx.AsyncClient(timeout=ROBOTS_TIMEOUT, follow_redirects=True) as own:
            return await own.get(target, headers=headers)

    def seed(self, url: str, robots: RobotsTxt, *, user_agent: str | None = None) -> None:
        """Ergebnis für den Host einer URL vorgeben (Tests, Werkzeuge); ohne ``user_agent`` für alle."""
        if user_agent is None:
            self._seeded[host_key(url)] = robots
            return
        self._entries[self._key(url, user_agent)] = _Entry(robots=robots, fetched_at=self._clock())

    async def decide(
        self,
        client: httpx.AsyncClient | None,
        url: str,
        *,
        user_agent: str,
        kind: str,
        override: RobotsOverride | None = None,
        pace: Callable[[str], Awaitable[None]] | None = None,
    ) -> Decision:
        """
        Darf ``url`` (Art ``kind``: ``api`` oder ``files``) mit ``user_agent`` abgerufen werden?

        Eine Ausnahme der Quelle für diese Art erlaubt den Abruf, ohne die robots.txt zu laden. Ohne
        ``client`` wird für den Abruf der robots.txt ein eigener geöffnet. ``pace`` wird vor dem Abruf der
        robots.txt aufgerufen (Drossel je Host). Ausgewertet werden das Produkt-Token von ``user_agent`` und
        unser eigenes (eine Regel für ``mandari-ingestor`` gilt immer).
        """
        if override is not None and override.covers(kind):
            return override.decision()
        robots = await self._load(client, url, user_agent, pace)
        return robots.decide(url, tokens=(product_token(user_agent), PRODUCT_TOKEN))


#: Gemeinsamer Zwischenspeicher aller Clients eines Prozesses
robots_gate = RobotsGate()
