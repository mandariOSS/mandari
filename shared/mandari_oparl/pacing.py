# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host über alle Quellen und Prozesse: gemeinsamer Zeitstempel in Redis.

Ingestor (OParl-Abruf, Textextraktion, Scraper, Sondierung) und Django (Dokument-Cache, Textextraktion,
Vorschau, Personenfotos, Löschabgleich, Quellenanlage, robots.txt) reservieren vor jeder Anfrage an einen Host
einen Zeitpunkt, auch vor jeder Weiterleitung. Der Schlüssel je Host (:func:`host_of`: Hostname ohne Port,
kleingeschrieben) hält den frühesten Beginn der nächsten Anfrage; jede Reservierung schiebt ihn um den Abstand
weiter. So stellen alle Prozesse zusammen höchstens eine Anfrage je Abstand an denselben Host, gleich wie viele
Quellen dort liegen.

Die Reservierung läuft atomar als Lua-Skript mit der Uhr des Redis-Servers (gleiche Zeit für alle Prozesse).
Ist Redis nicht erreichbar, drosselt jeder Prozess für sich (Rückfall in :mod:`ingestor` und Django).

Takt (Issue #1025): Standard ist eine Anfrage je Sekunde und Server (``DEFAULT_INTERVAL``). Eine Quelle kann
davon über ``sync_config["request_interval"]`` abweichen, aber nur nach oben: Werte unter
``MIN_REQUEST_INTERVAL`` (auch 0) hebt :func:`request_interval` auf diese Untergrenze an und meldet das im Log.
Darunter gehen nur die Betriebsvariablen ``INGESTOR_REQUEST_INTERVAL`` bzw. ``RIS_REQUEST_INTERVAL``
(0 schaltet die Drossel ab, nur für Tests und Notfälle).
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Standardabstand zwischen zwei Anfragen an denselben Host (Sekunden): eine Anfrage je Sekunde
DEFAULT_INTERVAL = 1.0

#: Untergrenze für den Abstand aus der Konfiguration einer Quelle (Sekunden): nie schneller als eine Anfrage je
#: Sekunde und Server, wie den Kommunen zugesagt. Kleinere Werte werden angehoben (:func:`at_least_minimum`).
MIN_REQUEST_INTERVAL = 1.0

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
    """
    Schlüssel des Servers für die Drossel: der Hostname einer URL (oder ein bloßer Hostname), vereinheitlicht.

    Ein Server bekommt genau einen Takt (Issue #1027): Groß-/Kleinschreibung, ein Punkt am Ende, Zugangsdaten vor
    ``@`` und der Port spielen keine Rolle. ``https://RIS.example.de:443/`` und ``http://ris.example.de/`` teilen
    sich also den Takt, auch ein anderer Port desselben Hostnamens (gleicher Server, nur langsamer). Internationale
    Namen werden in ihre ASCII-Form (IDNA) gebracht, wie sie in Weiterleitungen und in ``httpx`` erscheinen.
    """
    value = (url_or_host or "").strip()
    if not value:
        return ""
    try:
        host = urlsplit(value if "://" in value else f"//{value}").hostname
    except ValueError:
        host = None
    host = (host or value).rstrip(".").lower()
    if host and not host.isascii():
        with contextlib.suppress(UnicodeError):  # nicht kodierbar: der Name bleibt, wie er ist
            host = host.encode("idna").decode("ascii")
    return host


def key(url_or_host: str) -> str:
    """Redis-Schlüssel des Hosts."""
    return KEY_PREFIX + host_of(url_or_host)


#: Bereits gemeldete Unterschreitungen (Einstellung, Wert): je Prozess eine Warnung statt einer je Abruf
_reported: set[tuple[str, float]] = set()


def at_least_minimum(seconds: float, setting: str = REQUEST_INTERVAL_KEY) -> float:
    """
    Abstand aus der Konfiguration einer Quelle, mindestens ``MIN_REQUEST_INTERVAL``.

    Ein kleinerer Wert (auch 0) würde uns schneller als eine Anfrage je Sekunde machen; er wird angehoben und
    einmal je Prozess und Wert als Warnung geloggt. ``setting`` nennt die Einstellung in der Meldung.
    """
    minimum = MIN_REQUEST_INTERVAL
    if seconds >= minimum:
        return seconds
    if (setting, seconds) not in _reported:
        _reported.add((setting, seconds))
        logger.warning(
            "Abruftakt: %s=%s s liegt unter der Untergrenze von %s s je Server, es gilt %s s",
            setting,
            seconds,
            minimum,
            minimum,
        )
    return minimum


def request_interval(value: Any) -> float | None:
    """
    Abstand aus der Konfiguration einer Quelle (Sekunden, höchstens ``MAX_REQUEST_INTERVAL``); ungültige Werte
    (keine Zahl, negativ, zu groß) gelten als nicht gesetzt. Werte unter ``MIN_REQUEST_INTERVAL``, auch 0, werden
    auf die Untergrenze angehoben (:func:`at_least_minimum`): Eine Quelle darf uns nur verlangsamen.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0 or value > MAX_REQUEST_INTERVAL:
        return None
    return at_least_minimum(float(value))


def interval_for(sync_config: Any, default: float = DEFAULT_INTERVAL) -> float:
    """Abstand einer Quelle: ``sync_config["request_interval"]``, sonst ``default``."""
    if isinstance(sync_config, dict):
        configured = request_interval(sync_config.get(REQUEST_INTERVAL_KEY))
        if configured is not None:
            return configured
    return default


def host_intervals(sources: Iterable[tuple[str | None, Any]]) -> dict[str, float]:
    """
    Größter eingestellter Abstand je Host aus Paaren (Adresse der Quelle, ``sync_config``); Quellen ohne gültigen
    ``request_interval`` fehlen. Für Werkzeuge ohne eigene Quelle (Sondierung, Quellenanlage, Issue #1027): Hat eine
    bekannte Quelle auf dem Host einen größeren Abstand, gilt dieser auch für sie (:func:`interval_for_host`).
    """
    result: dict[str, float] = {}
    for url, sync_config in sources:
        host = host_of(url or "")
        if not host or not isinstance(sync_config, dict):
            continue
        configured = request_interval(sync_config.get(REQUEST_INTERVAL_KEY))
        if configured is not None and configured > result.get(host, 0.0):
            result[host] = configured
    return result


def interval_for_host(url_or_host: str, intervals: Mapping[str, float] | None, default: float) -> float:
    """Abstand für eine Anfrage an den Host von ``url_or_host``: ``default`` oder der größere Wert aus ``intervals``."""
    known = (intervals or {}).get(host_of(url_or_host))
    return default if known is None else max(default, known)


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
