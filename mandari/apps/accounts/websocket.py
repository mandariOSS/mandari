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

Dieselbe Zugriffsprüfung fragt :func:`session_valid` ab: Ein WebSocket kennt sein Konto nur vom Verbindungsaufbau.
Abmelden, „Sitzung beenden“ und ein neues Passwort beenden die Sitzung für HTTP; die offene Verbindung endet erst
dadurch, dass sie ihre Sitzung nachprüft. Sie endet dann mit :data:`CLOSE_SESSION_ENDED` statt mit „kein Zugriff“:
Der Zugriff der Person besteht weiter, nur diese Sitzung nicht. Nach einer erneuten Anmeldung oder einem
Passwortwechsel im eigenen Profil hat der Browser schon eine neue Sitzung, und die Seite verbindet damit neu.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.utils.crypto import constant_time_compare

#: Schließcode „kein Zugriff“ (wie in den Consumern)
CLOSE_FORBIDDEN = 4403
#: Schließcode „die Sitzung dieser Verbindung gilt nicht mehr“. Die Seite verbindet einmal neu (mit dem Cookie, das
#: der Browser jetzt hat) und lädt neu, wenn das nicht gelingt (``frontend/editor/collaboration.ts``).
CLOSE_SESSION_ENDED = 4440


def session_valid(consumer: Any, user: Any) -> bool:
    """
    Gilt die Sitzung, über die die Verbindung des Consumers aufgebaut wurde, noch für das Konto?

    Geprüft wird wie bei jeder HTTP-Anfrage, aber nur lesend: Die Sitzung gibt es noch, sie gehört dem Konto, und
    ihr Passwortstand ist der aktuelle. Beides kommt frisch aus dem Sitzungsspeicher und der Datenbank; der
    ``scope`` der Verbindung hält nur den Stand vom Verbindungsaufbau.

    Ohne Sitzungsschicht im ``scope`` gibt es nichts zu prüfen. In ``mandari/asgi.py`` liegt jede Verbindung hinter
    ``AuthMiddlewareStack``, dort kommt das Konto immer aus einer Sitzung; ohne Schicht arbeiten nur Tests, die
    einen Consumer direkt ansprechen.

    ``user`` ist hinter ``AuthMiddlewareStack`` eine Hülle (``UserLazyObject``), kein Objekt des Kontomodells:
    Gelesen wird deshalb über das Kontomodell selbst, nicht über die Klasse des übergebenen Objekts.
    """
    scope = getattr(consumer, "scope", None) or {}
    if "session" not in scope:
        return True
    session_key = getattr(scope["session"], "session_key", None)
    if not session_key or user is None:
        return False
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=session_key)
    if str(store.get(SESSION_KEY, "")) != str(user.pk):
        return False
    stored_hash = str(store.get(HASH_SESSION_KEY, ""))
    current = get_user_model()._default_manager.filter(pk=user.pk, is_active=True).first()
    if not stored_hash or current is None:
        return False
    # Wie django.contrib.auth.get_user: der aktuelle Schlüssel und die aus SECRET_KEY_FALLBACKS
    hashes = (current.get_session_auth_hash(), *current.get_session_auth_fallback_hash())
    return any(constant_time_compare(stored_hash, candidate) for candidate in hashes)


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
