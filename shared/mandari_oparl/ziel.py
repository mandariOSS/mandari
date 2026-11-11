# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zielprüfung für Abrufe von Adressen, die eine fremde Quelle nennt.

Download-Adressen von Dateien stehen in den Daten der Ratsinformationssysteme. Zeigt eine davon (oder eine
Weiterleitung) auf interne Dienste – Loopback, das interne Netz, Metadaten des Hosters –, darf kein Abruf dorthin
gehen: Die Antwort landete als Text der Datei im öffentlichen Bestand.

Geprüft wird beim Verbindungsaufbau (``PinnedAsyncTransport``): Der Hostname wird genau einmal aufgelöst, alle
Adressen müssen zulässig sein, und die Verbindung geht an die geprüfte Adresse. Eine Prüfung vor der Anfrage mit
eigener Namensauflösung genügte nicht, weil die Verbindung den Namen danach ein zweites Mal auflöste. Die Anfrage
selbst bleibt unverändert (Adresse, ``Host``-Kopfzeile, Name für TLS).

Zulässig sind öffentliche Adressen; private Netze nur mit ``allow_private`` (Selbstbetrieb im Intranet), Loopback
und Link-Local nie. Hostnamen in ``allowed_hosts`` sind ausgenommen (der eigene Dienst im internen Netz).
Dieselben Regeln wie ``insight_core.services.safe_fetch`` der Anwendung.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx


class BlockedDestinationError(httpx.RequestError):
    """Das Ziel ist nicht öffentlich erreichbar oder nicht auflösbar."""


class UnresolvableHostError(BlockedDestinationError, httpx.ConnectError):
    """
    Der Hostname ließ sich nicht auflösen. Es geht keine Verbindung hinaus (es gibt keine geprüfte Adresse), der
    Fehler ist aber wie ein Verbindungsfehler vorübergehend: Aufrufer versuchen es später erneut, statt die Datei
    dauerhaft zu überspringen.
    """


def allowed_address(address: str, *, allow_private: bool = False) -> bool:
    """Darf eine Verbindung an diese Adresse gehen?"""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_global:
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    return allow_private and ip.is_private


def _resolve(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def checked_address(host: str, *, allow_private: bool = False, allowed_hosts: Iterable[str] = ()) -> str | None:
    """
    Adresse, an die die Verbindung zu ``host`` gehen darf; ``None`` heißt: mit dem Namen verbinden (ausgenommener
    Host). Wirft ``BlockedDestinationError``, wenn der Name nicht auflösbar ist oder eine seiner Adressen nicht
    zulässig wäre. Steht in ``host`` schon eine Adresse, wird sie selbst geprüft.
    """
    name = host.lower().rstrip(".")
    if name in {eintrag.strip().lower().rstrip(".") for eintrag in allowed_hosts if eintrag.strip()}:
        return None
    try:
        addresses = [str(ipaddress.ip_address(name.strip("[]")))]
    except ValueError:
        addresses = _resolve(name)
        if not addresses:
            raise UnresolvableHostError("Hostname nicht auflösbar") from None
    if not all(allowed_address(address, allow_private=allow_private) for address in addresses):
        raise BlockedDestinationError("Ziel liegt nicht im öffentlichen Netz")
    # IPv4 zuerst: überall erreichbar, auch wo IPv6 nur halb eingerichtet ist
    return sorted(addresses, key=lambda address: (":" in address, address))[0]


class _PinnedAsyncBackend(httpcore.AnyIOBackend):
    def __init__(self, allow_private: bool, allowed_hosts: tuple[str, ...]) -> None:
        self._allow_private = allow_private
        self._allowed_hosts = allowed_hosts

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # Die Namensauflösung blockiert; sie läuft in einem Thread, damit andere Abrufe weiterlaufen
        address = await asyncio.to_thread(
            checked_address, host, allow_private=self._allow_private, allowed_hosts=self._allowed_hosts
        )
        return await super().connect_tcp(
            address or host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )


class PinnedAsyncTransport(httpx.AsyncHTTPTransport):
    """``httpx``-Transport, der nur mit geprüften Adressen verbindet (siehe Modulbeschreibung)."""

    def __init__(self, *, allow_private: bool = False, allowed_hosts: Iterable[str] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Der Verbindungspool des Transports baut seine Verbindungen über dieses Backend auf
        self._pool._network_backend = _PinnedAsyncBackend(allow_private, tuple(allowed_hosts))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except BlockedDestinationError as exc:
            raise BlockedDestinationError(str(exc), request=request) from None
