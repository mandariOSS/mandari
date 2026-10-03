# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host für Abrufe aus Django (gemeinsamer Zeitstempel in Redis, siehe :mod:`mandari_oparl.pacing`).

Dokument-Cache, Textextraktion, Vorschau, Personenfotos und der Abruf der robots.txt reservieren vor jeder
Anfrage an ein Ratsinformationssystem einen Zeitpunkt für dessen Host – im selben Takt wie der Ingestor.
So bleibt es bei höchstens einer Anfrage je Abstand an einen Host, gleich welcher Prozess sie stellt.

Abstand: ``sync_config["request_interval"]`` der Quelle, sonst ``RIS_REQUEST_INTERVAL`` (Standard eine Sekunde).
Ohne erreichbares Redis drosselt der Prozess für sich und versucht es nach einer Minute erneut.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from django.conf import settings
from mandari_oparl.pacing import DEFAULT_INTERVAL, RESERVE_SCRIPT, LocalSchedule, host_of, interval_for, key

logger = logging.getLogger(__name__)

#: Nach einem Redis-Fehler so lange nur im Prozess drosseln
REDIS_RETRY_SECONDS = 60.0

_lock = threading.Lock()
_local = LocalSchedule()
_state: dict[str, Any] = {"script": None, "down_until": 0.0}


def reset() -> None:
    """Zustand im Prozess verwerfen (Tests)."""
    with _lock:
        _local.clear()
        _state["script"] = None
        _state["down_until"] = 0.0


def interval(sync_config: Any = None) -> float:
    """Abstand für eine Quelle: ``request_interval`` aus ``sync_config``, sonst ``RIS_REQUEST_INTERVAL``."""
    default = float(getattr(settings, "RIS_REQUEST_INTERVAL", DEFAULT_INTERVAL))
    return interval_for(sync_config, default=default)


def _now() -> float:
    return time.monotonic()


def _script() -> Any:
    if _state["script"] is None:
        import redis

        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1.0, socket_timeout=2.0)
        _state["script"] = client.register_script(RESERVE_SCRIPT)
    return _state["script"]


def _reserve_redis(host: str, seconds: float, max_wait: float | None) -> float | None | bool:
    """Wartezeit in Sekunden, ``None`` (Höchstwartezeit überschritten) oder ``False`` (Redis nicht nutzbar)."""
    if not getattr(settings, "REDIS_URL", "") or _now() < _state["down_until"]:
        return False
    try:
        waited = int(
            _script()(
                keys=[key(host)],
                args=[int(seconds * 1000), int(max_wait * 1000) if max_wait is not None else -1],
            )
        )
    except Exception as exc:  # noqa: BLE001 - jede Redis-Störung: im Prozess weiter drosseln
        logger.warning(
            "Drossel je Host: Redis nicht nutzbar (%s), drossle %d s nur im Prozess",
            type(exc).__name__,
            int(REDIS_RETRY_SECONDS),
        )
        _state["script"] = None
        _state["down_until"] = _now() + REDIS_RETRY_SECONDS
        return False
    return None if waited < 0 else waited / 1000.0


def wait(url: str, *, sync_config: Any = None, max_wait: float | None = None) -> bool:
    """
    Bis zum reservierten Zeitpunkt für den Host von ``url`` warten.

    ``max_wait``: höchstens so lange warten (z. B. in einer Web-Anfrage); sonst nichts reservieren und
    ``False`` zurückgeben.
    """
    seconds = interval(sync_config)
    host = host_of(url)
    if seconds <= 0 or not host:
        return True
    result = _reserve_redis(host, seconds, max_wait)
    if result is False:
        with _lock:
            result = _local.reserve(host, _now(), seconds, max_wait)
    if result is None:
        return False
    if result > 0:
        time.sleep(result)
    return True
