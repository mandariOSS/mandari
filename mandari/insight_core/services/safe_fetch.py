# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe von Adressen aus fremden Ratsinformationssystemen.

Download-Adressen von Anlagen stammen aus den Quellen. Zeigt eine davon (oder eine Weiterleitung)
auf interne Dienste – Metadaten des Hosters, Loopback, das interne Netz –, darf der Server sie nicht
abrufen, denn die Dateivorschau reicht die Antwort an Besucher:innen weiter. Geprüft wird jeder
Abruf, auch jede Weiterleitung: Schema http(s), und alle Adressen, zu denen der Hostname auflöst,
müssen öffentlich sein. Ausnahmen: der eigene Host (``SITE_URL``, gespiegelte Session-Daten) und,
per ``INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS``, private Netze für Selbstbetrieb im Intranet.
Loopback und Link-Local bleiben immer gesperrt.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import IO, Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings


def _now() -> float:
    return time.monotonic()


class BlockedDestinationError(httpx.RequestError):
    """Ziel ist nicht öffentlich erreichbar oder kein http(s)."""


class TooLargeError(Exception):
    """Die Antwort überschreitet die zulässige Größe."""


class DeadlineExceededError(Exception):
    """Der Abruf dauert insgesamt zu lange."""


def _own_hosts() -> set[str]:
    host = urlsplit(str(getattr(settings, "SITE_URL", "") or "")).hostname
    return {host.lower()} if host else set()


def _resolve(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        # Nicht auflösbar: Die Verbindung scheitert ohnehin, es gibt kein internes Ziel
        return []


def _allowed_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_global:
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    return bool(getattr(settings, "INSIGHT_FETCH_ALLOW_PRIVATE_NETWORKS", False)) and ip.is_private


def check_url(url: str) -> None:
    """Wirft ``BlockedDestinationError``, wenn ``url`` kein zulässiges Ziel ist."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedDestinationError("Nur http(s)-Adressen sind zulässig")
    host = parts.hostname.lower().rstrip(".")
    if host in _own_hosts():
        return
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = _resolve(host)
    if not all(_allowed_address(address) for address in addresses):
        raise BlockedDestinationError("Ziel liegt nicht im öffentlichen Netz")


def _check_request(request: httpx.Request) -> None:
    try:
        check_url(str(request.url))
    except BlockedDestinationError as exc:
        raise BlockedDestinationError(str(exc), request=request) from None


def guarded_client(**kwargs: Any) -> httpx.Client:
    """``httpx.Client``, der jedes Ziel (auch nach Weiterleitungen) vor dem Abruf prüft."""
    return httpx.Client(event_hooks={"request": [_check_request]}, **kwargs)


async def _check_request_async(request: httpx.Request) -> None:
    import asyncio

    await asyncio.to_thread(_check_request, request)


def guarded_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Asynchrones Gegenstück zu ``guarded_client``."""
    return httpx.AsyncClient(event_hooks={"request": [_check_request_async]}, **kwargs)


@dataclass
class Download:
    content_type: str
    size: int


def download_to(
    target: IO[bytes],
    url: str,
    *,
    max_bytes: int,
    total_seconds: float,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
    user_agent: str | None = None,
) -> Download:
    """
    ``url`` gestreamt nach ``target`` laden – mit Größen- und Gesamtzeitgrenze.

    Wirft ``httpx.HTTPStatusError`` (Antwort ≠ 2xx), ``httpx.RequestError`` (auch
    ``BlockedDestinationError``), ``TooLargeError`` und ``DeadlineExceededError``.
    """
    deadline = _now() + total_seconds
    client_headers = {"User-Agent": user_agent} if user_agent else {}
    client = guarded_client(timeout=timeout, follow_redirects=True, max_redirects=5, headers=client_headers)
    with client, client.stream("GET", url, headers=headers or {}) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > max_bytes:
            raise TooLargeError
        size = 0
        for chunk in response.iter_bytes(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise TooLargeError
            if _now() > deadline:
                raise DeadlineExceededError
            target.write(chunk)
        return Download(content_type=response.headers.get("content-type", ""), size=size)
