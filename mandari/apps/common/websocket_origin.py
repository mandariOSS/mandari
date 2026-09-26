# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Herkunftsprüfung für WebSocket-Verbindungen.

Browser senden beim Aufbau einer WebSocket-Verbindung die Cookies des Zielhosts mit – auch
wenn die Seite, die sie öffnet, auf einer anderen Subdomain derselben Domain liegt. Ein
Platzhalter wie ``.mandari.de`` in ``ALLOWED_HOSTS`` (nötig für Organisations-Subdomains) ist
deshalb kein geeigneter Maßstab für die Herkunft.

Zugelassen wird hier nur, was Django auch für Formulare zulässt:

- eine Verbindung vom eigenen Host (``Origin`` gleich dem ``Host`` der Verbindung, und dieser
  Host steht in ``ALLOWED_HOSTS``) – so funktionieren Organisations-Subdomains und eigene
  Bürgerportal-Hosts weiter, jeweils nur für sich selbst;
- ein ausdrücklich vertrauter Ursprung aus ``CSRF_TRUSTED_ORIGINS`` (genaue Übereinstimmung).

Ohne ``Origin`` (kein Browser) wird die Verbindung abgelehnt, wie bei Channels.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from channels.security.websocket import WebsocketDenier
from django.conf import settings
from django.http.request import split_domain_port, validate_host


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return str(value.decode("latin1")).strip()
            except UnicodeDecodeError:
                return ""
    return ""


def origin_allowed(origin: str, host: str) -> bool:
    """Darf eine Seite des Ursprungs ``origin`` eine Verbindung zu ``host`` aufbauen?"""
    if not origin or origin == "null":
        return False
    origin = origin.rstrip("/").lower()
    trusted = {str(entry).rstrip("/").lower() for entry in getattr(settings, "CSRF_TRUSTED_ORIGINS", [])}
    if origin in trusted:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or not host:
        return False
    if parts.netloc != host.lower():
        return False
    domain, _port = split_domain_port(host.lower())
    return bool(domain) and validate_host(domain, settings.ALLOWED_HOSTS)


class SameOriginWebSocketValidator:
    """ASGI-Hülle: nur WebSocket-Verbindungen vom eigenen Host oder aus ``CSRF_TRUSTED_ORIGINS``."""

    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] != "websocket":
            raise ValueError("SameOriginWebSocketValidator gilt nur für WebSocket-Verbindungen.")
        if origin_allowed(_header(scope, b"origin"), _header(scope, b"host")):
            return await self.application(scope, receive, send)
        denier = WebsocketDenier()
        return await denier(scope, receive, send)
