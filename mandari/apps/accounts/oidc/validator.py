# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfregeln des OpenID-Connect-Anbieters (``OAUTH2_VALIDATOR_CLASS``, Issue #1206).

Ergänzt den Validator von django-oauth-toolkit um:

- Angaben (Claims) zu Name und E-Mail-Adresse, nur mit den Scopes ``profile`` bzw. ``email``
- Tokens gesperrter Konten gelten nicht (``userinfo``), ihre Refresh-Tokens geben keine neuen aus
- Wirksam sind nur Scopes, die die Anwendung jetzt noch haben darf: Nimmt die Plattform-Administration ihr einen
  Scope weg, fehlt er sofort auch bei ``userinfo`` und in Tokens aus bestehenden Refresh-Tokens (wie bei
  :func:`.access.authenticate` und der Introspektion)
- ``prompt=none``: still angemeldet wird nur, wer gerade mit erfülltem zweitem Faktor angemeldet ist
  (die Ansicht ``AuthorizeView`` setzt die Person für die Dauer der Anfrage), und nur bei Anwendungen ohne
  Zustimmungsseite
- ``id_token_hint``: Nennt ein von mandari signiertes ID-Token eine andere Person als die angemeldete (oder lässt
  es sich nicht prüfen), gibt es keinen Code, sondern ``login_required`` (OpenID Connect Core 3.1.2.2)
- ``auth_time`` im ID-Token: die Anmeldung in der Sitzung, aus der der Code stammt, auch nach jedem Refresh
  (nicht ``User.last_login``, #1215)
- Refresh: Beim Speichern wird das Refresh-Token gesperrt neu gelesen. Wurde es inzwischen widerrufen oder
  rotiert, gibt es ``invalid_grant`` und die ganze Kette wird widerrufen, wie bei einer erneuten Nutzung (#1215)
- Widerruf durch die Anwendung (RFC 7009) im Prüfprotokoll
- Anmeldung der Anwendung mit ``private_key_jwt`` (RFC 7523, :mod:`.client_assertion`, #565): Anwendungen mit
  öffentlichen Schlüsseln (``jwks``) melden sich nur so an, nie mit Client-Secret
- Geschützte Scopes (``catalog.PROTECTED``, etwa ``ratsdaten:nichtoeffentlich``, #565): Tokens gibt es nur mit zweitem
  Faktor und höchstens ``catalog.PROTECTED_MAX_LOGIN_AGE`` nach der Anmeldung; danach endet der Refresh mit
  ``invalid_grant``. Das Access-Token läuft spätestens zu diesem Zeitpunkt ab.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.db import router, transaction
from django.utils import timezone
from jwcrypto import jwt
from jwcrypto.common import JWException
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.utils import jwk_from_pem
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from oauthlib.oauth2.rfc6749.utils import scope_to_list

from . import audit, catalog, client_assertion
from .access import checksum, has_second_factor

#: Person, die in dieser Anfrage am Autorisierungs-Endpunkt angemeldet ist (für ``prompt=none``)
_session_user: ContextVar[Any] = ContextVar("oidc_session_user", default=None)
#: Anmeldezeitpunkt ihrer Sitzung (für ``auth_time``)
_session_login_time: ContextVar[datetime | None] = ContextVar("oidc_session_login_time", default=None)


@contextmanager
def signed_in_as(user: Any, login_time: datetime | None = None) -> Iterator[None]:
    """
    Für die Dauer des Blocks gilt ``user`` als angemeldet (``None``: niemand, etwa ohne zweiten Faktor).

    ``login_time``: wann sich die Person in dieser Sitzung angemeldet hat; der Code bekommt ihn mit.
    """
    user_marker = _session_user.set(user)
    time_marker = _session_login_time.set(login_time if user is not None else None)
    try:
        yield
    finally:
        _session_login_time.reset(time_marker)
        _session_user.reset(user_marker)


def _name(request: Any) -> str:
    return str(request.user.get_full_name() or "")


def _still_allowed(scopes: Any, application: Any) -> list[str]:
    """Die Scopes, die ``application`` jetzt noch haben darf, in der Reihenfolge von ``scopes``."""
    allowed = set(application.scope_list) if application is not None else set()
    return [scope for scope in scope_to_list(scopes) or [] if scope in allowed]


class RefreshNoLongerValidError(InvalidGrantError):  # type: ignore[misc]  # Bibliothek ohne Typen
    """
    ``invalid_grant`` beim Speichern eines Refresh.

    Fehler aus ``save_bearer_token`` beantwortet oauthlib ohne die üblichen Kopfzeilen der Token-Antwort; sie kommen
    deshalb hier mit (JSON, nicht zwischenspeichern).
    """

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Cache-Control": "no-store", "Pragma": "no-cache"}


class ProtectedScopeError(InvalidGrantError):  # type: ignore[misc]  # Bibliothek ohne Typen
    """
    ``invalid_grant`` für einen geschützten Scope (#565): Die Anmeldung liegt zu lange zurück, ihr Zeitpunkt ist
    unbekannt, oder das Konto hat keinen zweiten Faktor. Die Anwendung muss die Person erneut anmelden lassen.
    """

    description = "Für diesen Scope ist eine neue Anmeldung mit zweitem Faktor nötig."

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Cache-Control": "no-store", "Pragma": "no-cache"}


def _protected_seconds_left(request: Any, auth_time: datetime | None) -> int | None:
    """
    Sekunden, die ein Token mit geschütztem Scope noch gelten darf; ``None``: keins mehr (zu alt, Zeitpunkt unbekannt,
    kein zweiter Faktor).
    """
    if auth_time is None or not has_second_factor(getattr(request, "user", None)):
        return None
    left = int((auth_time + catalog.PROTECTED_MAX_LOGIN_AGE - timezone.now()).total_seconds())
    return left if left > 0 else None


def _inactive(user: Any) -> bool:
    return user is not None and not getattr(user, "is_active", False)


def _auth_time(request: Any) -> datetime | None:
    """Anmeldezeitpunkt, aus dem die Tokens dieser Anfrage am Token-Endpunkt stammen; ``None``: unbekannt."""
    from .models import OAuthGrant

    grant_type = getattr(request, "grant_type", None)
    if grant_type == "refresh_token":
        value = getattr(getattr(request, "refresh_token_instance", None), "auth_time", None)
    elif grant_type == "authorization_code":
        value = (
            OAuthGrant.objects.filter(code=request.code, application=request.client)
            .values_list("auth_time", flat=True)
            .first()
        )
    else:
        value = None
    return value if isinstance(value, datetime) else None


def _refresh_still_valid(request: Any) -> tuple[bool, Any]:
    """
    Gilt das Refresh-Token der Anfrage noch? Liest es dazu gesperrt neu (nur innerhalb einer Transaktion).

    ``validate_refresh_token`` liest ohne Sperre. Ein Widerruf zwischen dieser Prüfung und dem Speichern (oder ein
    gleichzeitiger Refresh mit demselben Token) fiele sonst nicht auf: Die Bibliothek prüft ``revoked`` beim
    Speichern nicht noch einmal und gäbe ein neues, gültiges Paar heraus.

    Returns: ``(gilt, kette)``; ``kette`` ist die Token-Familie, wenn das Token inzwischen widerrufen oder ersetzt
    ist (dann wird sie wie bei einer erneuten Nutzung ganz widerrufen), sonst ``None``.
    """
    from .models import OAuthApplication, OAuthRefreshToken

    previous = getattr(request, "refresh_token_instance", None)
    if getattr(request, "grant_type", None) != "refresh_token" or previous is None:
        return True, None
    current = OAuthRefreshToken.objects.select_for_update().filter(pk=previous.pk).first()
    if current is None:
        return False, None
    if current.revoked is not None:
        return False, current.token_family
    if current.access_token_id is None:
        return False, None
    if not get_user_model().objects.filter(pk=current.user_id, is_active=True).exists():
        return False, None
    if not OAuthApplication.objects.filter(pk=current.application_id, is_active=True).exists():
        return False, None
    return True, None


def _signing_keys() -> list[Any]:
    pems = [oauth2_settings.OIDC_RSA_PRIVATE_KEY, *(oauth2_settings.OIDC_RSA_PRIVATE_KEYS_INACTIVE or [])]
    return [jwk_from_pem(pem) for pem in pems if pem]


def hinted_subject(id_token_hint: str) -> str | None:
    """
    ``sub`` eines ``id_token_hint``, wenn mandari das ID-Token signiert hat (aktiver oder bisheriger Schlüssel).

    Ein abgelaufenes ID-Token taugt als Hinweis (OpenID Connect Core 3.1.2.1); nicht prüfbar: ``None``.
    """
    for key in _signing_keys():
        try:
            token = jwt.JWT(jwt=id_token_hint, key=key, algs=["RS256"], check_claims=False, strict_serialization=True)
            claims = json.loads(token.claims)
        except (JWException, KeyError, ValueError, TypeError):
            continue
        subject = claims.get("sub") if isinstance(claims, dict) else None
        return str(subject) if subject not in (None, "") else None
    return None


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
        if _inactive(getattr(request, "user", None)):
            request.oauth2_error = {"error": "invalid_token", "error_description": "The access token is invalid."}
            return False
        request.scopes = _still_allowed(request.scopes, getattr(request, "client", None))
        if not set(scope_to_list(scopes) or []).issubset(request.scopes):
            request.oauth2_error = {
                "error": "insufficient_scope",
                "error_description": "The access token lacks a required scope.",
            }
            return False
        return True

    def validate_refresh_token(self, refresh_token: Any, client: Any, request: Any, *args: Any, **kwargs: Any) -> bool:
        if not super().validate_refresh_token(refresh_token, client, request, *args, **kwargs):
            return False
        # Gesperrtes Konto: keine neuen Tokens (und keine Ausgabe im Prüfprotokoll)
        return not _inactive(getattr(request, "user", None))

    def get_original_scopes(self, refresh_token: Any, request: Any, *args: Any, **kwargs: Any) -> list[str]:
        # Refresh-Token: nur die Scopes, die die Anwendung jetzt noch haben darf; mehr verlangt gibt invalid_scope
        original = super().get_original_scopes(refresh_token, request, *args, **kwargs)
        return _still_allowed(original, getattr(request, "client", None))

    def validate_silent_login(self, request: Any) -> bool:
        return _session_user.get() is not None

    def validate_user_match(self, id_token_hint: Any, scopes: Any, claims: Any, request: Any) -> bool:
        # Ohne angemeldete Person entscheidet die Anmeldung (prompt=none scheitert vorher an login_required)
        user = _session_user.get()
        if not id_token_hint or user is None:
            return True
        return hinted_subject(str(id_token_hint)) == str(user.pk)

    def save_authorization_code(self, client_id: Any, code: Any, request: Any, *args: Any, **kwargs: Any) -> None:
        from .models import OAuthGrant

        super().save_authorization_code(client_id, code, request, *args, **kwargs)
        login_time = _session_login_time.get()
        if login_time is not None:
            OAuthGrant.objects.filter(code=code["code"], application=request.client).update(auth_time=login_time)

    def get_id_token_dictionary(self, token: Any, token_handler: Any, request: Any) -> tuple[dict[str, Any], Any]:
        claims, expiration_time = super().get_id_token_dictionary(token, token_handler, request)
        # Die Bibliothek nimmt User.last_login; das gilt je Konto, nicht je Sitzung
        auth_time = _auth_time(request)
        if auth_time is None:
            claims.pop("auth_time", None)
        else:
            claims["auth_time"] = int(auth_time.timestamp())
        return claims, expiration_time

    def authenticate_client(self, request: Any, *args: Any, **kwargs: Any) -> bool:
        # private_key_jwt (RFC 7523, #565): mit Assertion nur so, Anwendungen mit Schlüsseln nie mit Client-Secret
        if getattr(request, "client_assertion", None) or getattr(request, "client_assertion_type", None):
            return client_assertion.authenticate(request, self._load_application)
        if not super().authenticate_client(request, *args, **kwargs):
            return False
        client = getattr(request, "client", None)
        if client is not None and getattr(client, "uses_private_key_jwt", False):
            request.client = None
            return False
        return True

    def client_authentication_required(self, request: Any, *args: Any, **kwargs: Any) -> bool:
        # Mit Assertion ist die Anmeldung der Anwendung immer verlangt (sonst ginge sie als öffentlicher Client durch)
        if getattr(request, "client_assertion", None) or getattr(request, "client_assertion_type", None):
            return True
        return bool(super().client_authentication_required(request, *args, **kwargs))

    def save_bearer_token(self, token: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        from .models import OAuthAccessToken, OAuthIDToken, OAuthRefreshToken

        # Vorher lesen: Beim Speichern wird das bisherige Refresh-Token widerrufen
        auth_time = _auth_time(request)
        if catalog.protected_in(str(token.get("scope", ""))):
            # Geschützter Scope (#565): nur mit zweitem Faktor und höchstens sieben Tage nach der Anmeldung
            left = _protected_seconds_left(request, auth_time)
            if left is None:
                id_token = getattr(request, "id_token", None)
                if isinstance(id_token, OAuthIDToken):
                    id_token.delete()
                raise ProtectedScopeError(request=request)
            token["expires_in"] = min(int(token.get("expires_in") or oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS), left)
        with transaction.atomic(using=router.db_for_write(OAuthAccessToken)):
            valid, family = _refresh_still_valid(request)
            if valid:
                result = self._save_bearer_token(token, request, *args, **kwargs)
                if auth_time is not None and token.get("refresh_token"):
                    OAuthRefreshToken.objects.filter(
                        token_checksum=checksum(str(token["refresh_token"])), revoked__isnull=True
                    ).update(auth_time=auth_time)
                return result
        # Außerhalb der Transaktion, sonst nähme der Fehler den Widerruf der Kette mit zurück
        if family:
            OAuthRefreshToken.revoke_family(family)
        id_token = getattr(request, "id_token", None)
        if isinstance(id_token, OAuthIDToken):
            id_token.delete()
        raise RefreshNoLongerValidError(request=request)

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
