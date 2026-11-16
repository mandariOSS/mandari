# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt-Prüfung des Ingestors (RFC 9309, Auswertung in :mod:`mandari_oparl.robots`).

Jeder Abruf bei einer Quelle – OParl-JSON, HTML-Seiten der Scraper, Dateien für die Textextraktion –
fragt vorher hier an. Die robots.txt eines Hosts wird einmal geladen und 24 Stunden im Prozess gehalten;
alle Clients und Quellen eines Prozesses teilen sich den Zwischenspeicher. Er hat ein Budget
(``CACHE_BUDGET_BYTES``): Hostnamen kommen auch aus den Daten der Quellen (Adressen von Dateien, Ziele von
Weiterleitungen), ihre Zahl ist also nicht begrenzt. Ist das Budget erreicht, weichen die am längsten nicht
gebrauchten Einträge; ihre robots.txt wird beim nächsten Abruf neu geladen. Der Schlüssel ist der Hostname
(:func:`mandari_oparl.robots.host_key`, ohne Schema, Port und Unterschiede der Schreibweise, Issue #1036) und der
User-Agent: Manche Server filtern Wörter im User-Agent und antworten dann auch auf ``/robots.txt`` mit 403
(gilt als „nicht vorhanden“). Eine Quelle mit eigenem User-Agent bekommt deshalb ihre eigene Antwort.

Ist die robots.txt nicht erreichbar (5xx, 408, 429, Netzfehler) und gibt es keine letzte gültige Fassung,
lautet die Entscheidung „nicht erreichbar“ (:attr:`mandari_oparl.robots.Decision.unreachable`): Aufrufer
stellen den Abruf zurück, statt ihn als gesperrt zu werten. Neuer Versuch nach 15 Minuten.

Abruf mit unserem User-Agent und ``Accept: text/plain``: Mit dem JSON-Standard des OParl-Clients
antworten manche Server mit 406, was als „nicht vorhanden“ (alles erlaubt) gälte. Der Abruf zählt zum Takt je
Host (``pace``), auch jede Weiterleitung (:mod:`src.client.redirects`, höchstens fünf).

Ausnahmen je Quelle stehen in ``sync_config["robots_override"]`` und brauchen einen Vermerk
(:func:`mandari_oparl.robots.robots_override`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from mandari_oparl.crawler import PRODUCT_TOKEN, product_token
from mandari_oparl.robots import (
    CACHE_SECONDS,
    MAX_BYTES,
    RETRY_UNREACHABLE_SECONDS,
    STATE_UNREACHABLE,
    Decision,
    RobotsOverride,
    RobotsTxt,
    host_key,
    robots_url,
)
from mandari_oparl.ziel import BlockedDestinationError, PinnedAsyncTransport, UnresolvableHostError

from src.config import settings

logger = logging.getLogger(__name__)

#: Zeitlimit für den Abruf einer robots.txt, je Netzvorgang
ROBOTS_TIMEOUT = 15.0
#: Gesamtfrist für den Abruf einer robots.txt samt Weiterleitungen: Tropft die Antwort, greift das Zeitlimit je
#: Vorgang nie
ROBOTS_DEADLINE = 60.0
#: Budget des Zwischenspeichers: geschätzter Speicher aller Einträge zusammen. Eine gewöhnliche robots.txt braucht
#: wenige Kilobyte, eine voll ausgeschöpfte (``MAX_BYTES`` kurzer Regeln) rund 20 MiB
CACHE_BUDGET_BYTES = 64 * 1024 * 1024
#: Geschätzter Speicher eines Eintrags ohne Regeln und einer Regel ohne ihr Muster (Objekt, Teilstücke)
ENTRY_COST_BYTES = 1024
RULE_COST_BYTES = 512
#: Ab dieser Größe wird eine robots.txt in einem eigenen Thread ausgewertet: Eine große Datei hielte sonst alle
#: Abgleiche des Prozesses an, solange sie ausgewertet wird
PARSE_IN_THREAD_BYTES = 16 * 1024


def _rules_cost(robots: RobotsTxt | None) -> int:
    """Geschätzter Speicher der Regeln einer ausgewerteten robots.txt: je Regel ein Sockel und ihr Muster."""
    if robots is None:
        return 0
    return sum(RULE_COST_BYTES + 2 * len(rule.pattern) for _agents, rules in robots.groups for rule in rules)


@dataclass
class _Entry:
    robots: RobotsTxt
    fetched_at: float
    #: Letztes ausgewertetes Ergebnis; gilt weiter, solange die Datei danach nicht erreichbar ist
    last_good: RobotsTxt | None = None
    #: Geschätzter Speicher des Eintrags, für das Budget des Zwischenspeichers
    cost: int = field(init=False)

    def __post_init__(self) -> None:
        kept = self.last_good if self.last_good is not self.robots else None
        self.cost = ENTRY_COST_BYTES + _rules_cost(self.robots) + _rules_cost(kept)

    def fresh(self, now: float) -> bool:
        ttl = RETRY_UNREACHABLE_SECONDS if self.robots.state == STATE_UNREACHABLE else CACHE_SECONDS
        return now - self.fetched_at < ttl

    @property
    def effective(self) -> RobotsTxt:
        if self.robots.state == STATE_UNREACHABLE and self.last_good is not None:
            return self.last_good
        return self.robots


class RobotsGate:
    """Prozessweiter robots.txt-Zwischenspeicher je Host, mit Budget (siehe Modulbeschreibung)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, budget: int = CACHE_BUDGET_BYTES) -> None:
        #: In der Reihenfolge des letzten Gebrauchs, der am längsten nicht gebrauchte Eintrag zuerst
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        # Vorgaben je Host für alle User-Agents (Tests, Werkzeuge), siehe seed()
        self._seeded: dict[str, RobotsTxt] = {}
        self._clock = clock
        self._budget = budget
        #: Geschätzter Speicher aller Einträge
        self._cost = 0

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._seeded.clear()
        self._cost = 0

    def _store(self, key: str, entry: _Entry) -> None:
        """
        Eintrag ablegen und das Budget einhalten: Die am längsten nicht gebrauchten Einträge weichen, der neue
        bleibt. Die Sperre eines verdrängten Eintrags geht mit, außer sie ist gerade gehalten.
        """
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._cost -= previous.cost
        self._entries[key] = entry
        self._cost += entry.cost
        while self._cost > self._budget and len(self._entries) > 1:
            oldest, dropped = self._entries.popitem(last=False)
            self._cost -= dropped.cost
            lock = self._locks.get(oldest)
            if lock is not None and not lock.locked():
                del self._locks[oldest]

    @staticmethod
    async def _evaluate(status: int | None, body: bytes) -> RobotsTxt:
        """Antwort auswerten; eine große Datei in einem eigenen Thread, damit andere Abgleiche weiterlaufen."""
        if len(body) < PARSE_IN_THREAD_BYTES:
            return RobotsTxt.from_response(status, body)
        return await asyncio.to_thread(RobotsTxt.from_response, status, body)

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
        try:
            return await self._load_locked(client, url, user_agent, pace, key, lock)
        finally:
            # Endet das Laden ohne Eintrag (Abbruch), bliebe die Sperre sonst für immer liegen
            if key not in self._entries and not lock.locked():
                self._locks.pop(key, None)

    async def _load_locked(
        self,
        client: httpx.AsyncClient | None,
        url: str,
        user_agent: str,
        pace: Callable[[str], Awaitable[None]] | None,
        key: str,
        lock: asyncio.Lock,
    ) -> RobotsTxt:
        async with lock:
            entry = self._entries.get(key)
            now = self._clock()
            if entry is not None and entry.fresh(now):
                self._entries.move_to_end(key)
                return entry.effective
            target = robots_url(url)
            status: int | None = None
            body = b""
            try:
                if pace is not None:
                    await pace(target)
                status, body = await self._get(client, target, user_agent, pace)
            except httpx.HTTPError as exc:
                if isinstance(exc, BlockedDestinationError) and not isinstance(exc, UnresolvableHostError):
                    # Der Host liegt nicht im öffentlichen Netz: kein Abruf dorthin, auch keine Datei. Das ist ein
                    # Befund zum Ziel (überspringen), kein vorübergehender Ausfall (zurückstellen).
                    logger.warning(
                        "robots.txt %s nicht abgerufen: %s, Abrufe an den Host bleiben gesperrt", target, exc
                    )
                    gesperrt = RobotsTxt.parse("User-agent: *\nDisallow: /")
                    self._store(key, _Entry(robots=gesperrt, fetched_at=now, last_good=gesperrt))
                    return gesperrt
                logger.warning(
                    "robots.txt %s nicht abrufbar (%s), Abrufe gelten als gesperrt", target, type(exc).__name__
                )
            robots = await self._evaluate(status, body)
            if robots.state == STATE_UNREACHABLE:
                logger.warning("robots.txt %s nicht erreichbar (HTTP %s)", target, status)
            last_good = robots if robots.state != STATE_UNREACHABLE else (entry.effective if entry else None)
            if last_good is not None and last_good.state == STATE_UNREACHABLE:
                last_good = None
            new_entry = _Entry(robots=robots, fetched_at=now, last_good=last_good)
            self._store(key, new_entry)
            return new_entry.effective

    @staticmethod
    async def _get(
        client: httpx.AsyncClient | None,
        target: str,
        user_agent: str,
        pace: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[int, bytes]:
        """
        robots.txt abrufen, Rückgabe Status und Inhalt; Weiterleitungen (RFC 9309: mindestens fünf, auch auf andere
        Hosts) folgen wir selbst, jede mit eigenem Zeitpunkt in der Drossel je Host (``pace``). Mehr als fünf:
        ``httpx.TooManyRedirects``, also „nicht erreichbar“.

        Gelesen werden höchstens die ``MAX_BYTES``, die auch ausgewertet werden, und der Abruf endet nach
        ``ROBOTS_DEADLINE`` (``httpx.ReadTimeout``): Die Datei eines fremden Hosts belegt weder beliebig viel Speicher
        noch hält sie den Abruf beliebig lange offen.
        """
        from src.client.redirects import Deadline, pace_only, read_limited, send

        # Nur gzip anbieten: Das entpackt read_limited selbst, mit Obergrenze
        headers = {"User-Agent": user_agent, "Accept": "text/plain", "Accept-Encoding": "gzip"}
        before_redirect = pace_only(pace if pace is not None else _no_pace)
        deadline = Deadline(ROBOTS_DEADLINE)

        async def fetch(http: httpx.AsyncClient, request: httpx.Request) -> tuple[int, bytes]:
            response = await send(http, request, before_redirect=before_redirect, stream=True, deadline=deadline)
            if not 200 <= response.status_code < 300:
                # Ausgewertet wird nur der Inhalt einer erfolgreichen Antwort
                await response.aclose()
                return response.status_code, b""
            return response.status_code, await read_limited(response, MAX_BYTES, deadline=deadline)

        if client is not None:
            return await fetch(client, client.build_request("GET", target, headers=headers, timeout=ROBOTS_TIMEOUT))
        # Ohne Client der Quelle geht es um Dateien und Ziele von Weiterleitungen, deren Adressen die Quelle nennt:
        # Auch ihre robots.txt rufen wir nur bei öffentlichen Adressen ab (mandari_oparl.ziel)
        transport = PinnedAsyncTransport(
            allow_private=settings.fetch_allow_private_networks,
            allowed_hosts=[host for host in settings.fetch_internal_hosts.split(",") if host.strip()],
        )
        async with httpx.AsyncClient(timeout=ROBOTS_TIMEOUT, transport=transport) as own:
            return await fetch(own, own.build_request("GET", target, headers=headers))

    def seed(self, url: str, robots: RobotsTxt, *, user_agent: str | None = None) -> None:
        """Ergebnis für den Host einer URL vorgeben (Tests, Werkzeuge); ohne ``user_agent`` für alle."""
        if user_agent is None:
            self._seeded[host_key(url)] = robots
            return
        self._store(self._key(url, user_agent), _Entry(robots=robots, fetched_at=self._clock()))

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


async def _no_pace(_url: str) -> None:
    """Ohne Drossel (nur Werkzeuge und Tests, die selbst keinen Takt angeben)."""


#: Gemeinsamer Zwischenspeicher aller Clients eines Prozesses
robots_gate = RobotsGate()
