# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfregeln des OpenID-Connect-Anbieters (``OAUTH2_VALIDATOR_CLASS``, Issue #1206).

Ergänzt den Validator von django-oauth-toolkit um:

- Angaben (Claims) zu Name und E-Mail-Adresse, nur mit den Scopes ``profile`` bzw. ``email``
- Tokens gesperrter Konten gelten nicht (``userinfo``)
- ``prompt=none``: still angemeldet wird nur, wer gerade mit erfülltem zweitem Faktor angemeldet ist
  (die Ansicht ``AuthorizeView`` setzt die Person für die Dauer der Anfrage), und nur bei Anwendungen ohne
  Zustimmungsseite
- Widerruf durch die Anwendung (RFC 7009) im Prüfprotokoll
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from oauth2_provider.oauth2_validators import OAuth2Validator

from . import audit
from .access import checksum

#: Person, die in dieser Anfrage am Autorisierungs-Endpunkt angemeldet ist (für ``prompt=none``)
_session_user: ContextVar[Any] = ContextVar("oidc_session_user", default=None)


@contextmanager
def signed_in_as(user: Any) -> Iterator[None]:
    """Für die Dauer des Blocks gilt ``user`` als angemeldet (``None``: niemand, etwa ohne zweiten Faktor)."""
    marker = _session_user.set(user)
    try:
        yield
    finally:
        _session_user.reset(marker)


def _name(request: Any) -> str:
    return str(request.user.get_full_name() or "")


class MandariOAuth2Validator(OAuth2Validator):  # type: ignore[misc]  # Bibliothek ohne Typen
    def get_additional_claims(self) -> dict[str, Any]:
        # Ohne request-Parameter: Die Bibliothek nennt die Claims dann auch in der Discovery
        return {
            "name": _name,
            "given_name": lambda request: str(request.user.first_name or ""),
            "family_name": lambda request: str(request.user.last_name or ""),
            "email": lambda request: str(request.user.email),
            "email_verified": lambda request: bool(request.user.email_verified),
        }

    def validate_bearer_token(self, token: Any, scopes: Any, request: Any) -> bool:
        if not super().validate_bearer_token(token, scopes, request):
            return False
        user = getattr(request, "user", None)
        if user is not None and not getattr(user, "is_active", False):
            request.oauth2_error = {"error": "invalid_token", "error_description": "The access token is invalid."}
            return False
        return True

    def validate_silent_login(self, request: Any) -> bool:
        return _session_user.get() is not None

    def validate_silent_authorization(self, request: Any) -> bool:
        client = getattr(request, "client", None)
        return bool(client is not None and client.skip_authorization)

    def revoke_token(self, token: Any, token_type_hint: Any, request: Any, *args: Any, **kwargs: Any) -> None:
        from .models import OAuthAccessToken, OAuthRefreshToken

        client = getattr(request, "client", None)
        hit: dict[str, Any] | None = None
        if client is not None and getattr(client, "pk", None) is not None and isinstance(token, str):
            lookup = {"token_checksum": checksum(token), "application_id": client.pk}
            hit = (
                OAuthAccessToken.objects.filter(**lookup).values("user_id").first()
                or OAuthRefreshToken.objects.filter(**lookup, revoked__isnull=True).values("user_id").first()
            )
        super().revoke_token(token, token_type_hint, request, *args, **kwargs)
        if hit is not None:
            audit.record(
                audit.EVENT_REVOKED,
                audit.http_request_of(request),
                user_ref=hit.get("user_id"),
                anwendung=getattr(client, "client_id", ""),
                durch=audit.BY_APPLICATION,
            )
