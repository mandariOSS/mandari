# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt-Prüfung für Abrufe aus Django (RFC 9309, Auswertung in :mod:`mandari_oparl.robots`).

Gilt für jeden automatischen Abruf bei einer Quelle: Dokument-Cache, Textextraktion, Dateivorschau,
Personenfotos und das Anlegen einer Quelle. Der Ingestor prüft seine Abrufe selbst
(``ingestor/src/client/robots.py``) mit denselben Regeln.

Die robots.txt eines Hosts liegt 24 Stunden im gemeinsamen Django-Cache (alle Prozesse), je Host und
User-Agent: Manche Server filtern Wörter im User-Agent und antworten dann auch auf ``/robots.txt`` mit 403.
Abruf mit dem User-Agent, mit dem auch die Datei geladen wird (:func:`user_agent_for`), und
``Accept: text/plain``.

Ist die robots.txt nicht erreichbar (5xx, 408, 429, Netzfehler), gilt die letzte gültige Fassung weiter. Gab
es keine, lautet die Entscheidung „nicht erreichbar“ (:attr:`~mandari_oparl.robots.Decision.unreachable`):
Aufrufer stellen den Abruf zurück, statt die Datei als gesperrt zu überspringen. Neuer Versuch nach 15 Minuten.

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
from django.db.models import Q
from mandari_oparl.crawler import PRODUCT_TOKEN, product_token, user_agent
from mandari_oparl.robots import (
    CACHE_SECONDS,
    KIND_API,
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

_CACHE_PREFIX = "robots-txt:v2:"
#: Eintrag samt letzter gültiger Fassung bleibt so lange im Cache (Frische regelt ``fetched_at``)
_CACHE_TIMEOUT = 30 * 24 * 3600
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

__all__ = [
    "KIND_API",
    "KIND_FILES",
    "SKIP_ERROR_PREFIX",
    "USER_AGENT",
    "cached_check",
    "check",
    "decide",
    "load",
    "peek",
    "requeue_blocked_files",
    "sync_config_of",
    "user_agent_for",
]


def _now() -> float:
    return time.time()


def _cache_key(url: str, agent: str) -> str:
    return _CACHE_PREFIX + hashlib.sha256(f"{host_key(url)} {agent}".encode()).hexdigest()[:32]


def _fetch(
    url: str, agent: str = USER_AGENT, *, sync_config: Any = None, max_wait: float | None = None
) -> tuple[int | None, bytes]:
    """
    robots.txt abrufen. Der Abruf zählt wie jede Anfrage an den Host (Drossel je Host); mit ``max_wait`` wird
    höchstens so lange gewartet, sonst folgt :class:`~insight_core.services.host_pacing.PacingBusyError`.
    """
    from . import host_pacing
    from .safe_fetch import guarded_client

    target = robots_url(url)
    if not host_pacing.wait(target, sync_config=sync_config, max_wait=max_wait):
        raise host_pacing.PacingBusyError(target)
    try:
        with guarded_client(timeout=_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            response = client.get(target, headers={"User-Agent": agent, "Accept": "text/plain"})
            return response.status_code, response.content[:MAX_BYTES]
    except httpx.HTTPError as exc:
        logger.warning("robots.txt %s nicht abrufbar (%s), Abrufe werden zurückgestellt", target, type(exc).__name__)
        return None, b""


def _from_entry(data: dict[str, Any]) -> RobotsTxt:
    return RobotsTxt.from_response(data.get("status"), (data.get("text") or "").encode())


def load(
    url: str,
    *,
    refresh: bool = False,
    agent: str | None = None,
    sync_config: Any = None,
    max_wait: float | None = None,
) -> RobotsTxt:
    """
    Ausgewertete robots.txt zum Host von ``url``, abgerufen mit ``agent`` (Standard: unser User-Agent);
    aus dem Cache, sonst neu abgerufen. ``sync_config`` (Abstand der Quelle) und ``max_wait`` gelten für die
    Drossel je Host beim Abruf; in Web-Anfragen nur mit ``max_wait`` aufrufen.
    """
    agent = agent or USER_AGENT
    key = _cache_key(url, agent)
    entry: dict[str, Any] | None = cache.get(key)
    now = _now()
    if entry and not refresh:
        current = _from_entry(entry)
        ttl = RETRY_UNREACHABLE_SECONDS if current.state == STATE_UNREACHABLE else CACHE_SECONDS
        if now - float(entry.get("fetched_at") or 0) < ttl:
            last_good = entry.get("last_good")
            return _from_entry(last_good) if current.state == STATE_UNREACHABLE and last_good else current

    status, body = _fetch(url, agent, sync_config=sync_config, max_wait=max_wait)
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


def user_agent_for(obj: Any) -> str:
    """
    User-Agent für Datei-Abrufe einer Quelle (Datei, Body, Person oder Quelle): ``User-Agent`` aus
    ``sync_config["download_headers"]``, sonst unser Standard. Mit demselben Wert wird die robots.txt geprüft.
    """
    config = sync_config_of(obj)
    headers = config.get("download_headers") if isinstance(config, dict) else None
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "user-agent" and isinstance(value, str) and value.strip():
                return value.strip()
    return USER_AGENT


def decide(
    url: str,
    *,
    agent: str | None = None,
    refresh: bool = False,
    sync_config: Any = None,
    max_wait: float | None = None,
) -> Decision:
    """Was sagt die robots.txt allein (ohne Ausnahme der Quelle) zu ``url`` für ``agent``?"""
    agent = agent or USER_AGENT
    robots_txt = load(url, refresh=refresh, agent=agent, sync_config=sync_config, max_wait=max_wait)
    return robots_txt.decide(url, tokens=(product_token(agent), PRODUCT_TOKEN))


def check(
    url: str,
    kind: str = KIND_FILES,
    *,
    sync_config: Any = None,
    agent: str | None = None,
    max_wait: float | None = None,
) -> Decision:
    """
    Darf ``url`` (Art ``api`` oder ``files``) mit ``agent`` abgerufen werden? Eine gültige Ausnahme der Quelle
    für diese Art erlaubt den Abruf ohne Blick in die robots.txt.

    ``max_wait``: Muss die robots.txt erst geladen werden, wartet die Drossel je Host höchstens so lange (für
    Web-Anfragen); sonst folgt :class:`~insight_core.services.host_pacing.PacingBusyError`.
    """
    override = robots_override(sync_config)
    if override is not None and override.covers(kind):
        return override.decision()
    return decide(url, agent=agent, sync_config=sync_config, max_wait=max_wait)


def peek(url: str, *, agent: str | None = None) -> RobotsTxt | None:
    """
    Ausgewertete robots.txt zum Host von ``url`` nur aus dem Cache, ohne Abruf und ohne Blick auf die Frische;
    ``None``, wenn keine vorliegt. War sie zuletzt nicht erreichbar, gilt die letzte gültige Fassung.

    Für Hintergrundläufe, die keine Anfrage an die Quelle stellen dürfen (Obergrenze des Dokument-Caches, #961).
    """
    entry: dict[str, Any] | None = cache.get(_cache_key(url, agent or USER_AGENT))
    if not entry:
        return None
    current = _from_entry(entry)
    last_good = entry.get("last_good")
    if current.state == STATE_UNREACHABLE and last_good:
        return _from_entry(last_good)
    return current


def cached_check(
    url: str, kind: str = KIND_FILES, *, sync_config: Any = None, agent: str | None = None
) -> Decision | None:
    """Wie :func:`check`, aber nur mit der robots.txt aus dem Cache (``None``: keine bekannt). Fragt nie an."""
    override = robots_override(sync_config)
    if override is not None and override.covers(kind):
        return override.decision()
    agent = agent or USER_AGENT
    robots_txt = peek(url, agent=agent)
    if robots_txt is None:
        return None
    return robots_txt.decide(url, tokens=(product_token(agent), PRODUCT_TOKEN))


def requeue_blocked_files(source: Any) -> dict[str, int]:
    """
    Wegen der robots.txt übersprungene Dateien einer Quelle neu einreihen (nach Freigabe oder geänderter
    robots.txt): Textextraktion wieder ``pending``, Dokument-Cache wieder ``none`` – verweigerte Abrufe
    (``refused`` mit Fehlercode ``robots``, ``hub.ris.abruf``) und ältere Einträge mit ``error``. Rückgabe:
    Anzahl je Weg.
    """
    from ..models import OParlFile

    files = OParlFile.objects.filter(body__source=source, deleted=False)
    extraction = files.filter(
        text_extraction_status="skipped", text_extraction_error__startswith=SKIP_ERROR_PREFIX
    ).update(text_extraction_status="pending", text_extraction_error=None)
    gesperrt = Q(local_status="refused", fetch_error="robots") | Q(
        local_status="error", local_error__startswith=SKIP_ERROR_PREFIX
    )
    file_cache = files.filter(gesperrt).update(
        local_status="none", local_error="", fetch_error="", fetch_attempts=0, fetch_next_at=None
    )
    return {"extraction": extraction, "file_cache": file_cache}
