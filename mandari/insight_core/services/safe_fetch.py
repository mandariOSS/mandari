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
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import IO, Any
from urllib.parse import urlsplit

import httpcore
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


#: So viele geprüfte Adressen eines Namens versucht eine Verbindung höchstens nacheinander
MAX_PINNED_ADDRESSES = 4


def _pinned_addresses(host: str) -> list[str] | None:
    """
    Adressen, an die die Verbindung zu ``host`` gehen darf, in der Reihenfolge der Versuche; ``None`` heißt: nicht
    binden (eigener Host oder schon eine Adresse). Wirft ``BlockedDestinationError``, wenn der Name nicht auflösbar
    ist oder eine seiner Adressen nicht zulässig wäre.
    """
    name = host.lower().rstrip(".")
    if name in _own_hosts():
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return None
    addresses = _resolve(name)
    if not addresses:
        raise BlockedDestinationError("Hostname nicht auflösbar")
    if not all(_allowed_address(address) for address in addresses):
        raise BlockedDestinationError("Ziel liegt nicht im öffentlichen Netz")
    # IPv4 zuerst: überall erreichbar, auch wo IPv6 nur halb eingerichtet ist
    return sorted(addresses, key=lambda address: (":" in address, address))[:MAX_PINNED_ADDRESSES]


class _PinnedBackend(httpcore.SyncBackend):
    """Netz-Backend, das vor jeder Verbindung auflöst, prüft und nur mit der geprüften Adresse verbindet."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        addresses = _pinned_addresses(host)
        if addresses is None:
            return super().connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        # Alle Adressen stammen aus derselben Auflösung und sind geprüft: Ist die erste nicht erreichbar, darf es
        # die nächste sein, ohne die Bindung aufzugeben (wie ein gewöhnlicher Verbindungsaufbau über mehrere Adressen)
        # Die Zeitgrenze des Aufrufers gilt für alle Versuche zusammen: Jeder Versuch bekommt seinen Anteil an der
        # verbleibenden Zeit, mit einer einzigen Adresse also die ganze wie bisher.
        deadline = None if timeout is None else _now() + timeout
        failure: Exception | None = None
        for index, address in enumerate(addresses):
            attempt = timeout
            if deadline is not None:
                remaining = deadline - _now()
                if remaining <= 0:
                    break
                attempt = remaining / (len(addresses) - index)
            try:
                return super().connect_tcp(
                    address, port, timeout=attempt, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure or httpcore.ConnectTimeout("Zeitgrenze für den Verbindungsaufbau erreicht")


class PinnedTransport(httpx.HTTPTransport):
    """
    Verbindet nur mit Adressen, die die Zielprüfung bestanden haben.

    Die Prüfung allein (``check_url``) löst den Namen getrennt von der Verbindung auf; antwortet der Nameserver beim
    zweiten Mal mit einer internen Adresse, ginge die Verbindung dorthin. Hier löst der Verbindungsaufbau selbst
    einmal auf, prüft und verbindet mit genau dieser Adresse. Die Anfrage bleibt unverändert: Adresse,
    ``Host``-Kopfzeile und der Name für TLS (Zertifikat, SNI) sind weiter der Hostname.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Der Verbindungspool des Transports baut seine Verbindungen über dieses Backend auf
        self._pool._network_backend = _PinnedBackend()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return super().handle_request(request)
        except BlockedDestinationError as exc:
            raise BlockedDestinationError(str(exc), request=request) from None


def guarded_client(**kwargs: Any) -> httpx.Client:
    """
    ``httpx.Client``, der jedes Ziel (auch nach Weiterleitungen) vor dem Abruf prüft und nur mit der geprüften
    Adresse verbindet (``PinnedTransport``). Wer einen eigenen ``transport`` mitgibt (Tests), behält die Prüfung
    der Adresse, aber nicht die Bindung.
    """
    kwargs.setdefault("transport", PinnedTransport())
    return httpx.Client(event_hooks={"request": [_check_request]}, **kwargs)


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
    before_redirect: Callable[[httpx.Request], None] | None = None,
) -> Download:
    """
    ``url`` gestreamt nach ``target`` laden – mit Größen- und Gesamtzeitgrenze.

    Die Gesamtzeit gilt über Weiterleitungen hinweg: Nach ihrem Ablauf beginnt keine Anfrage mehr, und Verbindungsaufbau
    und Lesen jeder Anfrage bekommen höchstens die verbleibende Zeit.

    Wirft ``httpx.HTTPStatusError`` (Antwort ≠ 2xx), ``httpx.RequestError`` (auch
    ``BlockedDestinationError``), ``TooLargeError`` und ``DeadlineExceededError``.

    ``before_redirect``: Abrufe bei Ratsinformationssystemen folgen Weiterleitungen selbst und prüfen vorher
    robots.txt und Takt des Ziels (:func:`insight_core.services.redirects.guard`, Issue #1027). Ohne Angabe (andere
    Dienste, etwa Geodaten) folgt ``httpx`` wie bisher höchstens fünf Weiterleitungen.
    """
    deadline = _now() + total_seconds
    client_headers = {"User-Agent": user_agent} if user_agent else {}
    # Dateien unkomprimiert: Die Größengrenze gilt dann für das, was tatsächlich im Speicher ankommt. Das gilt auch,
    # wenn die mitgegebenen Kopfzeilen (etwa die einer Quelle) etwas anderes anfordern, in jeder Schreibweise.
    headers = {name: value for name, value in (headers or {}).items() if name.lower() != "accept-encoding"}
    headers["Accept-Encoding"] = "identity"
    client = guarded_client(timeout=timeout, follow_redirects=True, max_redirects=5, headers=client_headers)

    def within_deadline(request: httpx.Request) -> None:
        """Vor jeder Anfrage, auch nach einer Weiterleitung: keine neue nach der Frist, Zeitgrenzen höchstens der Rest."""
        remaining = deadline - _now()
        if remaining <= 0:
            raise DeadlineExceededError
        limits = request.extensions.get("timeout")
        if isinstance(limits, dict):
            request.extensions["timeout"] = {
                name: remaining if value is None else min(value, remaining) for name, value in limits.items()
            }

    client.event_hooks = {**client.event_hooks, "request": [*client.event_hooks.get("request", []), within_deadline]}
    response_cm: AbstractContextManager[httpx.Response]
    if before_redirect is None:
        response_cm = client.stream("GET", url, headers=headers or {})
    else:
        from .redirects import stream

        response_cm = stream(client, "GET", url, headers=headers, before_redirect=before_redirect)
    with client, response_cm as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > max_bytes:
            raise TooLargeError
        # Angefordert wird unkomprimiert (siehe oben). Kommt die Antwort trotzdem kodiert, weisen wir sie ab: Die
        # Grenze zählte sonst erst nach dem Entpacken, und ein kleiner Rohblock könnte den Speicher füllen. Als
        # Abruffehler (``httpx.RequestError``), nicht als „zu groß“: Der Zustand soll sich nicht festschreiben, die
        # Quelle kann beim nächsten Versuch wieder unkodiert antworten.
        if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            raise httpx.DecodingError("Antwort ist kodiert, angefordert war unkodiert", request=response.request)
        size = 0
        # Schon vollständig geladene Antworten (Ersatz-Transport im Test) haben keinen rohen Strom mehr
        # Ohne Stückgröße: Die Gesamtfrist unten wird nach jedem Stück geprüft, wie es aus dem Netz kommt; mit
        # fester Größe sammelte httpx bei einer tröpfelnden Antwort erst lange
        chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
        for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise TooLargeError
            if _now() > deadline:
                raise DeadlineExceededError
            target.write(chunk)
        return Download(content_type=response.headers.get("content-type", ""), size=size)
