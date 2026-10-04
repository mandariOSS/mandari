# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host über alle Quellen und Prozesse: gemeinsamer Zeitstempel in Redis.

Ingestor (OParl-Abruf, Textextraktion, Scraper) und Django (Dokument-Cache, Textextraktion, Vorschau,
Personenfotos, robots.txt) reservieren vor jeder Anfrage an einen Host einen Zeitpunkt. Der Schlüssel je Host
hält den frühesten Beginn der nächsten Anfrage; jede Reservierung schiebt ihn um den Abstand weiter. So stellen
alle Prozesse zusammen höchstens eine Anfrage je Abstand an denselben Host, gleich wie viele Quellen dort liegen.

Die Reservierung läuft atomar als Lua-Skript mit der Uhr des Redis-Servers (gleiche Zeit für alle Prozesse).
Ist Redis nicht erreichbar, drosselt jeder Prozess für sich (Rückfall in :mod:`ingestor` und Django).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: Standardabstand zwischen zwei Anfragen an denselben Host (Sekunden): eine Anfrage je Sekunde
DEFAULT_INTERVAL = 1.0

#: Obergrenze für den Abstand je Quelle (Tippfehler wie 600 statt 0.6 bremsen sonst alles aus)
MAX_REQUEST_INTERVAL = 30.0

#: Schlüssel in ``OParlSource.sync_config`` für den Abstand je Quelle
REQUEST_INTERVAL_KEY = "request_interval"

KEY_PREFIX = "mandari:abruf-takt:"

#: Reserviert den nächsten Zeitpunkt für einen Host.
#: KEYS[1] Schlüssel des Hosts; ARGV[1] Abstand in ms; ARGV[2] höchste Wartezeit in ms (negativ: unbegrenzt).
#: Rückgabe: Wartezeit in ms bis zum reservierten Beginn, oder -1, wenn sie die Höchstwartezeit überschreitet
#: (dann wird nichts reserviert).
RESERVE_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local max_wait = tonumber(ARGV[2])
local next_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = math.max(now, next_at)
local wait = start - now
if max_wait >= 0 and wait > max_wait then
  return -1
end
redis.call('SET', KEYS[1], tostring(start + interval), 'PX', wait + interval + 60000)
return wait
"""


def host_of(url_or_host: str) -> str:
    """Host (mit Port) einer URL, kleingeschrieben; ein bloßer Hostname bleibt, wie er ist."""
    value = (url_or_host or "").strip()
    netloc = urlsplit(value).netloc if "://" in value else value
    return netloc.lower()


def key(url_or_host: str) -> str:
    """Redis-Schlüssel des Hosts."""
    return KEY_PREFIX + host_of(url_or_host)


def request_interval(value: Any) -> float | None:
    """Abstand aus der Konfiguration (Sekunden, 0 bis 30); ungültige Werte gelten als nicht gesetzt."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or value > MAX_REQUEST_INTERVAL:
        return None
    return float(value)


def interval_for(sync_config: Any, default: float = DEFAULT_INTERVAL) -> float:
    """Abstand einer Quelle: ``sync_config["request_interval"]``, sonst ``default``."""
    if isinstance(sync_config, dict):
        configured = request_interval(sync_config.get(REQUEST_INTERVAL_KEY))
        if configured is not None:
            return configured
    return default


class LocalSchedule:
    """
    Rückfall ohne Redis: dieselbe Reservierung im Prozess (nur dieser Prozess zählt).

    Nicht threadsicher; Aufrufer mit Threads sichern den Aufruf mit einer Sperre.
    """

    def __init__(self) -> None:
        self._next_at: dict[str, float] = {}

    def reserve(self, host: str, now: float, interval: float, max_wait: float | None) -> float | None:
        """Wartezeit in Sekunden bis zum reservierten Beginn; ``None``, wenn sie ``max_wait`` überschreitet."""
        start = max(now, self._next_at.get(host, 0.0))
        wait = start - now
        if max_wait is not None and wait > max_wait:
            return None
        self._next_at[host] = start + interval
        return wait

    def clear(self) -> None:
        self._next_at.clear()
