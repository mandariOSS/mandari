# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt-Prüfung für Abrufe aus Django (RFC 9309, Auswertung in :mod:`mandari_oparl.robots`).

Gilt für jeden automatischen Abruf bei einer Quelle: Dokument-Cache, Textextraktion, Dateivorschau und
Personenfotos. Der Ingestor prüft seine Abrufe selbst (``ingestor/src/client/robots.py``) mit denselben
Regeln.

Die robots.txt eines Hosts liegt 24 Stunden im gemeinsamen Django-Cache (alle Prozesse). Ist sie danach
nicht erreichbar (5xx, Netzfehler), gilt die letzte gültige Fassung weiter; gab es keine, gilt der Host als
gesperrt und wird nach 15 Minuten erneut gefragt. Abruf mit unserem User-Agent und ``Accept: text/plain``.

Ausnahmen je Quelle: ``OParlSource.sync_config["robots_override"]`` mit Pflicht-Vermerk
(:func:`mandari_oparl.robots.robots_override`); setzen mit ``manage.py robots_override``, Überblick mit
``manage.py robots_report``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import httpx
from django.core.cache import cache
from mandari_oparl.crawler import PRODUCT_TOKEN, user_agent
from mandari_oparl.robots import (
    CACHE_SECONDS,
    KIND_FILES,
    MAX_BYTES,
    RETRY_UNREACHABLE_SECONDS,
    SKIP_ERROR_PREFIX,
    STATE_UNREACHABLE,
    Decision,
    RobotsTxt,
    host_key,
    robots_override,
    robots_url,
)

logger = logging.getLogger(__name__)

#: Kennung aller automatischen Abrufe aus Django (gleiches Produkt-Token wie der Ingestor)
USER_AGENT = user_agent()

_CACHE_PREFIX = "robots-txt:v1:"
#: Eintrag samt letzter gültiger Fassung bleibt so lange im Cache (Frische regelt ``fetched_at``)
_CACHE_TIMEOUT = 30 * 24 * 3600
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

__all__ = [
    "KIND_FILES",
    "SKIP_ERROR_PREFIX",
    "USER_AGENT",
    "check",
    "load",
    "requeue_blocked_files",
    "sync_config_of",
]


def _now() -> float:
    return time.time()


def _cache_key(url: str) -> str:
    return _CACHE_PREFIX + hashlib.sha256(host_key(url).encode()).hexdigest()[:32]


def _fetch(url: str) -> tuple[int | None, bytes]:
    from .safe_fetch import guarded_client

    target = robots_url(url)
    try:
        with guarded_client(timeout=_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            response = client.get(target, headers={"User-Agent": USER_AGENT, "Accept": "text/plain"})
            return response.status_code, response.content[:MAX_BYTES]
    except httpx.HTTPError as exc:
        logger.warning("robots.txt %s nicht abrufbar (%s), Abrufe gelten als gesperrt", target, type(exc).__name__)
        return None, b""


def _from_entry(data: dict[str, Any]) -> RobotsTxt:
    return RobotsTxt.from_response(data.get("status"), (data.get("text") or "").encode())


def load(url: str, *, refresh: bool = False) -> RobotsTxt:
    """Ausgewertete robots.txt zum Host von ``url`` (aus dem Cache, sonst neu abgerufen)."""
    key = _cache_key(url)
    entry: dict[str, Any] | None = cache.get(key)
    now = _now()
    if entry and not refresh:
        current = _from_entry(entry)
        ttl = RETRY_UNREACHABLE_SECONDS if current.state == STATE_UNREACHABLE else CACHE_SECONDS
        if now - float(entry.get("fetched_at") or 0) < ttl:
            last_good = entry.get("last_good")
            return _from_entry(last_good) if current.state == STATE_UNREACHABLE and last_good else current

    status, body = _fetch(url)
    fresh = {"status": status, "text": body.decode("utf-8", errors="replace"), "fetched_at": now}
    robots = RobotsTxt.from_response(status, body)
    previous_good = None
    if entry:
        previous_good = entry.get("last_good") if _from_entry(entry).state == STATE_UNREACHABLE else entry
    last_good = previous_good if robots.state == STATE_UNREACHABLE else None
    cache.set(key, {**fresh, "last_good": last_good}, timeout=_CACHE_TIMEOUT)
    if robots.state == STATE_UNREACHABLE:
        logger.warning("robots.txt %s nicht erreichbar (HTTP %s)", robots_url(url), status)
        if last_good:
            return _from_entry(last_good)
    return robots


def sync_config_of(obj: Any) -> Any:
    """``sync_config`` der Quelle zu einer Datei, einem Body, einer Person oder einer Quelle (sonst ``None``)."""
    for path in (("body", "source"), ("source",), ()):
        target = obj
        for attr in path:
            target = getattr(target, attr, None)
            if target is None:
                break
        config = getattr(target, "sync_config", None) if target is not None else None
        if isinstance(config, dict):
            return config
    return None


def check(url: str, kind: str = KIND_FILES, *, sync_config: Any = None) -> Decision:
    """
    Darf ``url`` (Art ``api`` oder ``files``) abgerufen werden? Eine gültige Ausnahme der Quelle für diese
    Art erlaubt den Abruf ohne Blick in die robots.txt.
    """
    override = robots_override(sync_config)
    if override is not None and override.covers(kind):
        return Decision(allowed=True, state="override", rule=f"Ausnahme der Quelle ({override.scope})")
    return load(url).decide(url, tokens=(PRODUCT_TOKEN,))


def requeue_blocked_files(source: Any) -> dict[str, int]:
    """
    Wegen der robots.txt übersprungene Dateien einer Quelle neu einreihen (nach Freigabe oder geänderter
    robots.txt): Textextraktion wieder ``pending``, Dokument-Cache wieder ``none``. Rückgabe: Anzahl je Weg.
    """
    from ..models import OParlFile

    files = OParlFile.objects.filter(body__source=source, deleted=False)
    extraction = files.filter(
        text_extraction_status="skipped", text_extraction_error__startswith=SKIP_ERROR_PREFIX
    ).update(text_extraction_status="pending", text_extraction_error=None)
    file_cache = files.filter(local_status="error", local_error__startswith=SKIP_ERROR_PREFIX).update(
        local_status="none", local_error=""
    )
    return {"extraction": extraction, "file_cache": file_cache}
