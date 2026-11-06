# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anmeldung einer Anwendung mit ``private_key_jwt`` (RFC 7523, Abschnitt 2.2 und 3; OpenID Connect Core 1.0,
Abschnitt 9), Issue #565.

Statt eines Client-Secrets schickt die Anwendung am Token-Endpunkt ein JWT, das sie mit ihrem privaten Schlüssel
signiert hat (``client_assertion``), und den Typ ``urn:ietf:params:oauth:client-assertion-type:jwt-bearer``
(``client_assertion_type``). Die öffentlichen Schlüssel stehen als JWK Set an der Anwendung (``jwks``). Geprüft wird:

- Signatur mit einem der Schlüssel der Anwendung, nur ``RS256`` (RSA ab 2048 Bit) und ``ES256`` (P-256); ``kid`` im
  Kopf wählt den Schlüssel, ohne ``kid`` gilt jeder passende
- ``iss`` und ``sub`` sind die Client-ID
- ``aud`` ist die Adresse des Token-Endpunkts (eine Zeichenkette oder eine Liste mit genau dieser Adresse)
- ``exp`` liegt in der Zukunft, höchstens :data:`MAX_LIFETIME` entfernt; ``nbf`` und ``iat`` nicht in der Zukunft
  (mit :data:`CLOCK_SKEW` Spielraum)
- ``jti`` ist gesetzt und wurde bis zu seinem Ablauf noch nicht angenommen (:class:`.models.OAuthClientAssertion`)

Jede Abweichung lehnt die Anmeldung ab (``invalid_client``); der Grund steht nur im Betriebslog, nie in der Antwort.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from jwcrypto import jwk, jwt
from jwcrypto.common import JWException
from oauth2_provider.settings import oauth2_settings

logger = logging.getLogger(__name__)

ASSERTION_TYPE: Final = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
#: Erlaubte Signaturverfahren und die Schlüsselart dazu
ALGORITHMS: Final[dict[str, str]] = {"RS256": "RSA", "ES256": "EC"}
#: Höchste Lebensdauer einer Assertion: ``exp`` höchstens so weit in der Zukunft
MAX_LIFETIME: Final = timedelta(minutes=5)
#: Spielraum für abweichende Uhren bei ``nbf`` und ``iat``
CLOCK_SKEW: Final = timedelta(seconds=30)
#: Längere Assertions werden gar nicht erst gelesen
MAX_ASSERTION_LENGTH: Final = 8192
MIN_RSA_BITS: Final = 2048
MAX_KEYS: Final = 10


class AssertionInvalid(Exception):  # noqa: N818 – Begriff aus RFC 7523 (invalid client assertion)
    """Die Client-Assertion taugt nicht; der Grund ist nur für das Betriebslog."""


def token_endpoint() -> str:
    """Adresse des Token-Endpunkts (``aud`` der Assertions): Aussteller und Pfad, wie in der Discovery."""
    return f"{str(oauth2_settings.OIDC_ISS_ENDPOINT).rstrip('/')}{reverse('oauth2_provider:token')}"


# ---------------------------------------------------------------------------
# Schlüssel der Anwendung
# ---------------------------------------------------------------------------


def _keys(jwks: Any) -> list[dict[str, Any]]:
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError('Erwartet wird ein JWK Set: {"keys": [ … ]}.')
    return [key for key in jwks["keys"] if isinstance(key, dict)]


def jwks_problems(jwks: Any) -> list[str]:
    """Gründe, aus denen ein JWK Set nicht taugt (leer: tauglich). Nur öffentliche RSA- und P-256-Schlüssel."""
    try:
        keys = _keys(jwks)
    except ValueError as exc:
        return [str(exc)]
    if not keys or len(keys) != len(jwks["keys"]):
        return ["Das JWK Set braucht mindestens einen Schlüssel, jeder als JSON-Objekt."]
    if len(keys) > MAX_KEYS:
        return [f"Höchstens {MAX_KEYS} Schlüssel."]
    problems: list[str] = []
    kids = [key.get("kid") for key in keys if key.get("kid")]
    if len(kids) != len(set(kids)):
        problems.append("Jede Schlüssel-ID (kid) darf nur einmal vorkommen.")
    if len(keys) > 1 and len(kids) != len(keys):
        problems.append("Bei mehreren Schlüsseln braucht jeder eine Schlüssel-ID (kid).")
    for index, data in enumerate(keys, start=1):
        label = f"Schlüssel {data.get('kid') or index}"
        if any(part in data for part in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")):
            problems.append(f"{label}: enthält private Teile. Eingetragen wird nur der öffentliche Schlüssel.")
            continue
        try:
            key = jwk.JWK(**data)
        except (JWException, ValueError, TypeError):
            problems.append(f"{label}: kein gültiger JSON Web Key.")
            continue
        kty = str(data.get("kty", ""))
        if kty == "RSA":
            if key.get_op_key("verify").key_size < MIN_RSA_BITS:
                problems.append(f"{label}: RSA-Schlüssel brauchen mindestens {MIN_RSA_BITS} Bit.")
        elif kty == "EC":
            if data.get("crv") != "P-256":
                problems.append(f"{label}: Elliptische Kurven nur P-256 (ES256).")
        else:
            problems.append(f"{label}: nur RSA (RS256) oder EC P-256 (ES256).")
        if data.get("alg") not in (None, *ALGORITHMS):
            problems.append(f"{label}: Verfahren nur RS256 oder ES256.")
        elif data.get("alg") and ALGORITHMS[str(data["alg"])] != kty:
            problems.append(f"{label}: Verfahren {data['alg']} passt nicht zur Schlüsselart {kty}.")
        if data.get("use") not in (None, "sig"):
            problems.append(f"{label}: Verwendung nur zum Signieren (use: sig).")
    return problems


def _candidate_keys(jwks: Any, alg: str, kid: str | None) -> list[jwk.JWK]:
    """Schlüssel der Anwendung, mit denen eine Signatur dieses Verfahrens geprüft werden kann."""
    kty = ALGORITHMS[alg]
    found = []
    for data in _keys(jwks):
        if data.get("kty") != kty or (kid is not None and data.get("kid") != kid):
            continue
        if data.get("alg") not in (None, alg) or data.get("use") not in (None, "sig"):
            continue
        if any(part in data for part in ("d", "p", "q", "k")):
            continue
        try:
            found.append(jwk.JWK(**data))
        except (JWException, ValueError, TypeError):
            continue
    return found


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


def _segment(assertion: str, index: int) -> dict[str, Any]:
    """Kopf (0) oder Inhalt (1) einer kompakten JWS, ungeprüft gelesen."""
    parts = assertion.split(".")
    if len(parts) != 3:
        raise AssertionInvalid("keine kompakte JWS")
    raw = parts[index]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise AssertionInvalid("Kopf oder Inhalt nicht lesbar") from exc
    if not isinstance(data, dict):
        raise AssertionInvalid("Kopf oder Inhalt ist kein JSON-Objekt")
    return data


def unverified_issuer(assertion: str) -> str | None:
    """``iss`` der Assertion ohne Prüfung, um die Anwendung zu finden; ``None``, wenn nicht lesbar."""
    if not isinstance(assertion, str) or len(assertion) > MAX_ASSERTION_LENGTH:
        return None
    try:
        issuer = _segment(assertion, 1).get("iss")
    except AssertionInvalid:
        return None
    return issuer if isinstance(issuer, str) and issuer else None


def _number(claims: dict[str, Any], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AssertionInvalid(f"{name} ist keine Zahl")
    return float(value)


def verify(application: Any, assertion: str, *, now: datetime | None = None) -> dict[str, Any]:
    """
    Prüft die Assertion für ``application`` und sperrt ihr ``jti``; liefert die Angaben (Claims).

    Raises: :class:`AssertionInvalid`
    """
    if not isinstance(assertion, str) or not assertion or len(assertion) > MAX_ASSERTION_LENGTH:
        raise AssertionInvalid("Assertion fehlt oder ist zu lang")
    if not application.uses_private_key_jwt:
        raise AssertionInvalid("Anwendung ohne öffentliche Schlüssel")
    header = _segment(assertion, 0)
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        raise AssertionInvalid(f"Verfahren {alg!r} nicht erlaubt")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise AssertionInvalid("kid ist keine Zeichenkette")
    claims: dict[str, Any] | None = None
    for key in _candidate_keys(application.jwks, str(alg), kid):
        try:
            token = jwt.JWT(jwt=assertion, key=key, algs=[str(alg)], check_claims=False)
            loaded = json.loads(token.claims)
        except (JWException, ValueError, TypeError):
            continue
        if isinstance(loaded, dict):
            claims = loaded
            break
    if claims is None:
        raise AssertionInvalid("Signatur passt zu keinem Schlüssel der Anwendung")

    client_id = str(application.client_id)
    if claims.get("iss") != client_id or claims.get("sub") != client_id:
        raise AssertionInvalid("iss und sub sind nicht die Client-ID")
    audience = claims.get("aud")
    endpoint = token_endpoint()
    if not (audience == endpoint or (isinstance(audience, list) and audience == [endpoint])):
        raise AssertionInvalid("aud ist nicht der Token-Endpunkt")

    current = (now or timezone.now()).timestamp()
    expires = _number(claims, "exp")
    if expires is None:
        raise AssertionInvalid("exp fehlt")
    if expires <= current:
        raise AssertionInvalid("abgelaufen")
    if expires > current + MAX_LIFETIME.total_seconds():
        raise AssertionInvalid("exp liegt mehr als fünf Minuten in der Zukunft")
    for name in ("nbf", "iat"):
        value = _number(claims, name)
        if value is not None and value > current + CLOCK_SKEW.total_seconds():
            raise AssertionInvalid(f"{name} liegt in der Zukunft")

    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 255:
        raise AssertionInvalid("jti fehlt oder ist zu lang")
    _lock_jti(client_id, jti, datetime.fromtimestamp(expires, tz=UTC), now=now)
    return claims


def _lock_jti(client_id: str, jti: str, expires: datetime, *, now: datetime | None = None) -> None:
    """``jti`` bis zum Ablauf sperren; schon gesperrt: :class:`AssertionInvalid` (Wiederholung)."""
    from .models import OAuthClientAssertion

    current = now or timezone.now()
    OAuthClientAssertion.objects.filter(expires__lt=current).delete()
    try:
        with transaction.atomic():
            OAuthClientAssertion.objects.create(client_id=client_id, jti=jti, expires=expires)
    except IntegrityError:
        raise AssertionInvalid("jti schon benutzt") from None


def authenticate(request: Any, load_application: Any) -> bool:
    """
    Anwendung einer oauthlib-Anfrage über ``client_assertion`` anmelden; setzt ``request.client`` und
    ``request.client_id``. ``load_application(client_id, request)`` lädt eine nutzbare Anwendung (vom Validator).
    """
    assertion_type = getattr(request, "client_assertion_type", None)
    assertion = getattr(request, "client_assertion", None)
    if assertion_type != ASSERTION_TYPE or not isinstance(assertion, str) or not assertion:
        logger.info("private_key_jwt abgelehnt: falscher Typ oder keine Assertion")
        return False
    issuer = unverified_issuer(assertion)
    given = getattr(request, "client_id", None)
    if issuer is None or (given and given != issuer):
        logger.info("private_key_jwt abgelehnt: Client-ID fehlt oder passt nicht")
        return False
    application = load_application(issuer, request)
    if application is None:
        logger.info("private_key_jwt abgelehnt: keine nutzbare Anwendung %s", issuer)
        return False
    try:
        verify(application, assertion)
    except AssertionInvalid as exc:
        logger.info("private_key_jwt für %s abgelehnt: %s", issuer, exc)
        request.client = None
        return False
    request.client = application
    request.client_id = application.client_id
    return True
