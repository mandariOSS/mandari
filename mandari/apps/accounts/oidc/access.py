# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Access-Tokens des OpenID-Connect-Anbieters erzeugen und prüfen (Issue #1206).

Ressourcen wie die Session-API rufen :func:`authenticate` auf und bekommen ein :class:`OAuthAccess`: wer
handelt (eine Person oder ein Dienst), für welche Anwendung, mit welchen Scopes. Was daraus an Rechten folgt,
entscheidet die Ressource; die Plattform kennt keine Fachbegriffe.

Tokens tragen ein Präfix (``mandari_at_`` bzw. ``mandari_rt_``). So erkennen Secret-Scanner sie, und die
Session-API unterscheidet sie ohne Datenbankzugriff von ihren eigenen API-Tokens (64 Zeichen ohne Präfix).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from . import provider_enabled

ACCESS_TOKEN_PREFIX = "mandari_at_"
REFRESH_TOKEN_PREFIX = "mandari_rt_"
#: Zufallsbytes je Token (256 Bit; ``token_urlsafe`` liefert daraus 43 Zeichen)
TOKEN_BYTES = 32
#: Längere Eingaben sind nie ein Token und werden gar nicht erst gehasht
MAX_TOKEN_LENGTH = 256


def generate_access_token(request: Any = None) -> str:
    """Neues Access-Token (``ACCESS_TOKEN_GENERATOR``)."""
    return ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def generate_refresh_token(request: Any = None) -> str:
    """Neues Refresh-Token (``REFRESH_TOKEN_GENERATOR``)."""
    return REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def checksum(raw: str) -> str:
    """SHA-256 über das Token, wie ``token_checksum`` der Bibliothek."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InvalidAccessToken(Exception):  # noqa: N818 – Begriff aus RFC 6750 (invalid_token)
    """Das Token ist unbekannt, abgelaufen, widerrufen, oder seine Anwendung bzw. Person ist gesperrt."""


@dataclass(frozen=True)
class OAuthAccess:
    """Ein gültiges Access-Token: Anwendung, handelnde Person (oder keine) und wirksame Scopes."""

    token_id: int
    client_id: str
    application_name: str
    #: die Person, in deren Namen die Anwendung handelt; ``None`` bei einem Dienst (Client Credentials)
    user: Any | None
    scopes: frozenset[str]

    @property
    def acts_for_person(self) -> bool:
        return self.user is not None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def looks_like_access_token(raw: str) -> bool:
    """Trägt ``raw`` das Präfix der Access-Tokens, und ist der Anbieter eingeschaltet?"""
    return provider_enabled() and isinstance(raw, str) and raw.startswith(ACCESS_TOKEN_PREFIX)


def authenticate(raw: str, *, resource_uri: str = "") -> OAuthAccess:
    """
    Prüft ein Access-Token und liefert, wofür es gilt.

    Wirksam sind nur Scopes, die die Anwendung jetzt noch haben darf: Nimmt die Plattform-Administration einer
    Anwendung einen Scope weg, verlieren auch ihre bestehenden Tokens ihn. Ist das Token an Ressourcen gebunden
    (RFC 8707), muss ``resource_uri`` dazu passen.

    Raises: :class:`InvalidAccessToken`
    """
    if not looks_like_access_token(raw) or len(raw) > MAX_TOKEN_LENGTH:
        raise InvalidAccessToken
    from .models import OAuthAccessToken

    digest = checksum(raw)
    token = OAuthAccessToken.objects.select_related("application", "user").filter(token_checksum=digest).first()
    if token is None or not hmac.compare_digest(str(token.token_checksum), digest) or token.is_expired():
        raise InvalidAccessToken
    application = token.application
    if application is None or not application.is_usable(None):
        raise InvalidAccessToken
    user = token.user
    if user is not None and not user.is_active:
        raise InvalidAccessToken
    if token.resource and not (resource_uri and token.allows_audience(resource_uri)):
        raise InvalidAccessToken
    return OAuthAccess(
        token_id=int(token.pk),
        client_id=str(application.client_id),
        application_name=str(application.name or application.client_id),
        user=user,
        scopes=frozenset(str(token.scope).split()) & frozenset(application.scope_list),
    )
