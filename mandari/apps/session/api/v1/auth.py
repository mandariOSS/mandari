# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Authentifizierung der Session-API v1.

Zwei Wege, beide ergeben ein ``Principal``:

- **API-Token** (``Authorization: Bearer <token>``, ``SessionAPIToken``): für Integrationen, z. B. das
  Work-Portal beim Einreichen von Anträgen. Token sind mandantengebunden; Rechte über die
  ``can_*``-Flags des Tokens, optional IP-Beschränkung und Ratenlimit je Minute.
- **Angemeldete Sitzung** (Cookie): für Nutzer des Session-RIS; Rechte über ``SessionUser``-Rollen.

Ohne beides ist der Aufrufer anonym und sieht nur öffentliche Daten. Die Auth-Klasse lehnt deshalb
nie ab; Endpunkte, die Rechte brauchen, werfen selbst 401/403 (RFC 9457).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest
from ninja.security.base import AuthBase

from apps.session.models import SessionAPIToken, SessionTenant, SessionUser
from apps.session.permissions import SessionPermissionChecker

from .problems import Problem

TOKEN_LENGTH = 64
TOKEN_PERMISSIONS: dict[str, str] = {
    # Berechtigungsname der Rollen → Flag am Token
    "view_non_public_meetings": "can_read_meetings",
    "view_non_public_papers": "can_read_papers",
    "submit_applications": "can_submit_applications",
}


def client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return str(forwarded.split(",")[0].strip())
    return str(request.META.get("REMOTE_ADDR", ""))


@dataclass
class Principal:
    """Aufrufer einer Anfrage: anonym, Sitzungsnutzer oder API-Token."""

    session_user: SessionUser | None = None
    token: SessionAPIToken | None = None

    @property
    def kind(self) -> str:
        if self.token is not None:
            return "token"
        if self.session_user is not None:
            return "user"
        return "anonymous"

    @property
    def authenticated(self) -> bool:
        return self.kind != "anonymous"

    def has_permission(self, permission: str) -> bool:
        if self.token is not None:
            flag = TOKEN_PERMISSIONS.get(permission)
            return bool(flag and getattr(self.token, flag, False))
        if self.session_user is not None:
            return bool(SessionPermissionChecker(self.session_user).has_permission(permission))
        return False

    def require(self, permission: str, detail: str) -> None:
        """401 für Anonyme, 403 ohne Recht – beides als RFC-9457-Problem."""
        if not self.authenticated:
            raise Problem(401, detail, kind="nicht-authentifiziert")
        if not self.has_permission(permission):
            raise Problem(403, detail, kind="keine-berechtigung")


def _token_from_request(request: HttpRequest, tenant: SessionTenant) -> SessionAPIToken | None:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None
    raw = header[7:].strip()
    if len(raw) != TOKEN_LENGTH:
        raise Problem(401, "Das API-Token hat ein ungültiges Format.", kind="nicht-authentifiziert")
    try:
        token = SessionAPIToken.objects.get(token=SessionAPIToken.hash_token(raw), tenant=tenant)
    except SessionAPIToken.DoesNotExist:
        raise Problem(
            401, "Das API-Token ist unbekannt oder gehört zu einem anderen Mandanten.", kind="nicht-authentifiziert"
        ) from None
    if not token.is_valid():
        raise Problem(401, "Das API-Token ist deaktiviert oder abgelaufen.", kind="nicht-authentifiziert")
    if not token.check_ip(client_ip(request)):
        raise Problem(
            403, "Das API-Token darf von dieser IP-Adresse nicht verwendet werden.", kind="keine-berechtigung"
        )
    _throttle(token)
    return token


def _throttle(token: SessionAPIToken) -> None:
    """Ratenlimit je Token und Minute (Feld ``rate_limit_per_minute``) über den Cache."""
    limit = int(getattr(token, "rate_limit_per_minute", 0) or 0)
    if limit <= 0:
        return
    key = f"session-api:token:{token.pk}:rate"
    if cache.add(key, 1, timeout=60):
        return
    try:
        count = cache.incr(key)
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        cache.add(key, 1, timeout=60)
        return
    if count > limit:
        raise Problem(
            429,
            f"Ratenlimit von {limit} Anfragen pro Minute für dieses Token erreicht.",
            kind="ratenlimit",
            headers={"Retry-After": "60"},
        )


def resolve_principal(request: HttpRequest, tenant: SessionTenant) -> Principal:
    token = _token_from_request(request, tenant)
    if token is not None:
        return Principal(token=token)
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        session_user = SessionUser.objects.filter(user=user, tenant=tenant, is_active=True).first()
        if session_user is not None:
            return Principal(session_user=session_user)
    return Principal()


class BearerOrSession(AuthBase):
    """Dokumentiert das Bearer-Schema in OpenAPI; die eigentliche Auflösung passiert je Mandant im Endpunkt."""

    openapi_type = "http"
    openapi_scheme = "bearer"

    def __call__(self, request: HttpRequest) -> Any:
        # Anonyme Aufrufer sind erlaubt (öffentliche Daten); Rechteprüfung im Endpunkt.
        return request
