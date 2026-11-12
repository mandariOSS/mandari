# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Pflicht zum zweiten Faktor für WebSocket-Verbindungen.

Für HTTP leitet ``TwoFactorEnforcementMiddleware`` Sitzungen mit offener Pflicht zur Einrichtung um. WebSockets
laufen nicht durch die Django-Middleware: Eine Sitzung aus der Zeit vor der Pflicht könnte sonst weiter lesen und
schreiben. Deshalb zwei Stellen:

- :class:`TwoFactorWebSocketMiddleware` (in ``mandari/asgi.py`` innerhalb von ``AuthMiddlewareStack``) lehnt den
  Aufbau jeder WebSocket-Verbindung ab, solange das angemeldete Konto eine offene Pflicht hat.
- Die Consumer fragen :func:`requirement_open` in ihrer eigenen Zugriffsprüfung ab, die sie während der Verbindung
  wiederholen. So endet auch eine offene Verbindung, wenn die Pflicht erst danach entsteht.
"""

from __future__ import annotations

from typing import Any

from channels.db import database_sync_to_async

#: Schließcode „kein Zugriff“ (wie in den Consumern)
CLOSE_FORBIDDEN = 4403


def requirement_open(user: Any) -> bool:
    """Hat das Konto eine offene Pflicht (Authenticator-App oder Sicherheitsschlüssel)? Ohne Zwischenspeicher."""
    from .middleware import open_requirement

    return bool(open_requirement(user))


class TwoFactorWebSocketMiddleware:
    """ASGI-Hülle: keine WebSocket-Verbindung für angemeldete Konten mit offener Pflicht."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope.get("type") == "websocket":
            user = scope.get("user")
            if getattr(user, "is_authenticated", False) and await database_sync_to_async(requirement_open)(user):
                # Verbindungswunsch abwarten und ablehnen, ohne den Consumer zu starten
                message = await receive()
                if message.get("type") == "websocket.connect":
                    await send({"type": "websocket.close", "code": CLOSE_FORBIDDEN})
                return None
        return await self.inner(scope, receive, send)
