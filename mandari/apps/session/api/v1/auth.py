# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Authentifizierung der Session-API v1.

Zwei Wege, beide ergeben ein ``Principal``:

- **API-Token** (``Authorization: Bearer <token>``, ``SessionAPIToken``): für Integrationen, z. B. das
  Work-Portal beim Einreichen von Anträgen. Token sind mandantengebunden; Rechte über die
  ``can_*``-Flags des Tokens, optional IP-Beschränkung und Ratenlimit je Minute. Ein Token liest nur
  Öffentliches – „Öffentliche Sitzungen/Vorlagen lesen“ heißt genau das. Nichtöffentliche Sitzungen,
  Vorlagen, Texte und interne Notizen gibt es nur für angemeldete Personen mit dem NÖ-Recht: Ein Token
  gehört zu keiner Person, deren Berechtigung für Nichtöffentliches geprüft wäre.
- **Angemeldete Sitzung** (Cookie): für Nutzer des Session-RIS mit dem Recht „API-Zugang“
  (``access_api``); weitere Rechte über ``SessionUser``-Rollen. Ohne „API-Zugang“ gilt die Person in
  der Schnittstelle als anonym.

Ohne beides ist der Aufrufer anonym und sieht nur öffentliche Daten – und auch die erst nach der
Freischaltung der OParl-Schnittstelle. Vorher lesen Sitzungen bzw. Vorlagen nur Token mit dem
jeweiligen Lese-Flag und Personen mit „API-Zugang“ und dem Sichtrecht (``READ_RIGHTS``). Die
Auth-Klasse lehnt deshalb nie ab; Endpunkte, die Rechte brauchen, werfen selbst 401/403 (RFC 9457).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from django.core.cache import cache
from django.http import HttpRequest
from ninja.security.base import AuthBase

from apps.session.models import SessionAPIToken, SessionTenant, SessionUser
from apps.session.permissions import SessionPermissionChecker

from .problems import Problem

TOKEN_LENGTH = 64
TOKEN_PERMISSIONS: dict[str, str] = {
    # Berechtigungsname der Rollen → Flag am Token. Die Lese-Flags (can_read_meetings/-papers) stehen
    # bewusst nicht hier: Sie erlauben nur das Lesen öffentlicher Daten (READ_RIGHTS) und geben nie ein
    # NÖ-Recht (view_non_public_*).
    "submit_applications": "can_submit_applications",
}

#: Leserecht je Bereich: Sichtrecht der Rolle bzw. Lese-Flag des Tokens. Vor der Freischaltung der
#: OParl-Schnittstelle liest ein Bereich nur, wer dieses Recht hat; danach ist Öffentliches für alle frei.
READ_RIGHTS: dict[str, tuple[str, str]] = {
    "meetings": ("view_meetings", "can_read_meetings"),
    "papers": ("view_papers", "can_read_papers"),
}

#: Recht der Rolle, die Schnittstelle mit der eigenen Anmeldung zu nutzen („API-Zugang“)
API_ACCESS = "access_api"


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
            checker = cast(Any, SessionPermissionChecker)(self.session_user)  # permissions.py ist noch untypisiert
            return bool(checker.has_permission(permission))
        return False

    def can_read(self, area: str) -> bool:
        """Darf der Aufrufer diesen Bereich lesen (Sichtrecht der Rolle bzw. Lese-Flag des Tokens)?"""
        user_right, token_flag = READ_RIGHTS[area]
        if self.token is not None:
            return bool(getattr(self.token, token_flag, False))
        return self.has_permission(user_right)

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


def rate_limit_exceeded(token: SessionAPIToken) -> bool:
    """
    Anfrage zählen und melden, ob das Ratenlimit des Tokens je Minute (``rate_limit_per_minute``)
    überschritten ist – ein Zähler je Token für alle Wege der Session-API (auch die alten Pfade).
    """
    limit = int(getattr(token, "rate_limit_per_minute", 0) or 0)
    if limit <= 0:
        return False
    key = f"session-api:token:{token.pk}:rate"
    if cache.add(key, 1, timeout=60):
        return False
    try:
        count = cache.incr(key)
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        cache.add(key, 1, timeout=60)
        return False
    return int(count) > limit


def _throttle(token: SessionAPIToken) -> None:
    """Ratenlimit je Token und Minute über den Cache; überschritten → 429 mit ``Retry-After``."""
    if rate_limit_exceeded(token):
        limit = int(getattr(token, "rate_limit_per_minute", 0) or 0)
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
    session_user = api_session_user(request, tenant)
    return Principal(session_user=session_user) if session_user is not None else Principal()


def api_session_user(request: HttpRequest, tenant: SessionTenant) -> SessionUser | None:
    """
    Angemeldete Person des Mandanten mit dem Recht „API-Zugang“, sonst ``None``.

    Ohne dieses Recht gilt eine angemeldete Person in der Schnittstelle wie ein anonymer Aufruf.
    Auch für die abgekündigten Pfade unter ``/session/<slug>/api/session/``.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    session_user = (
        SessionUser.objects.filter(user=user, tenant=tenant, is_active=True).prefetch_related("roles").first()
    )
    if session_user is None:
        return None
    checker = cast(Any, SessionPermissionChecker)(session_user)  # permissions.py ist noch untypisiert
    return session_user if checker.has_permission(API_ACCESS) else None


class BearerOrSession(AuthBase):
    """Dokumentiert das Bearer-Schema in OpenAPI; die eigentliche Auflösung passiert je Mandant im Endpunkt."""

    openapi_type = "http"
    openapi_scheme = "bearer"

    def __call__(self, request: HttpRequest) -> Any:
        # Anonyme Aufrufer sind erlaubt (öffentliche Daten); Rechteprüfung im Endpunkt.
        return request
