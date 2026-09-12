# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsschlüssel und Passkeys (WebAuthn/FIDO2) als zusätzlicher zweiter Faktor.

Registrierung und Anmeldung laufen in zwei Schritten: Der Server erzeugt
Optionen mit einer Einmal-Challenge (in der Session), der Browser signiert mit
dem Schlüssel, der Server prüft die Antwort. Relying-Party-ID ist die
Hauptdomain (``WEBAUTHN_RP_ID``, Standard ``MAIN_DOMAIN``) – Schlüssel gelten
damit auch auf Organisations-Subdomains. Gespeichert werden nur öffentliche
Schlüsseldaten.
"""

from __future__ import annotations

import time
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.http import HttpRequest
from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .models import WebAuthnCredential

REGISTRATION_SESSION_KEY = "auth_webauthn_registration"
AUTHENTICATION_SESSION_KEY = "auth_webauthn_authentication"
CHALLENGE_MAX_AGE_SECONDS = 300
TIMEOUT_MS = 120_000
_TRANSPORTS = {transport.value for transport in AuthenticatorTransport}


class WebAuthnError(Exception):
    """Fehler mit einer für Nutzerinnen und Nutzer verständlichen Meldung."""


def rp_id() -> str:
    """Relying-Party-ID (Hauptdomain ohne Port)."""
    configured = getattr(settings, "WEBAUTHN_RP_ID", "") or getattr(settings, "MAIN_DOMAIN", "") or "localhost"
    return str(configured).split(":")[0].lower()


def expected_origin(request: HttpRequest) -> str:
    """Origin der Anfrage – nur für die Relying-Party-ID oder deren Subdomains."""
    host = request.get_host()
    hostname = host.split(":")[0].lower()
    relying_party = rp_id()
    if hostname != relying_party and not hostname.endswith("." + relying_party):
        raise WebAuthnError("Sicherheitsschlüssel sind unter dieser Adresse nicht verfügbar.")
    return f"{request.scheme}://{host}"


def has_credentials(user: Any) -> bool:
    return WebAuthnCredential.objects.filter(user=user).exists()


def _descriptors(user: Any) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(credential.credential_id),
            transports=[AuthenticatorTransport(t) for t in credential.transports if t in _TRANSPORTS] or None,
        )
        for credential in WebAuthnCredential.objects.filter(user=user)
    ]


def _store_challenge(request: HttpRequest, key: str, user: Any, challenge: bytes) -> None:
    request.session[key] = {"user": str(user.pk), "challenge": bytes_to_base64url(challenge), "at": int(time.time())}


def _take_challenge(request: HttpRequest, key: str, user: Any) -> bytes:
    """Challenge einmalig entnehmen; abgelaufene oder fremde Challenges scheitern."""
    state = request.session.pop(key, None)
    if (
        not isinstance(state, dict)
        or state.get("user") != str(user.pk)
        or int(time.time()) - int(state.get("at", 0)) > CHALLENGE_MAX_AGE_SECONDS
    ):
        raise WebAuthnError("Die Anfrage ist abgelaufen. Bitte versuche es erneut.")
    return base64url_to_bytes(str(state["challenge"]))


def registration_options(request: HttpRequest, user: Any) -> str:
    """Optionen für ``navigator.credentials.create`` (JSON) erzeugen."""
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=str(getattr(settings, "WEBAUTHN_RP_NAME", "mandari")),
        user_name=user.email,
        user_id=user.pk.bytes,
        user_display_name=user.get_full_name() or user.email,
        exclude_credentials=_descriptors(user),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        timeout=TIMEOUT_MS,
    )
    _store_challenge(request, REGISTRATION_SESSION_KEY, user, options.challenge)
    return options_to_json(options)


def register(request: HttpRequest, user: Any, credential: dict[str, Any], name: str) -> WebAuthnCredential:
    """Antwort des Browsers prüfen und den Schlüssel speichern."""
    challenge = _take_challenge(request, REGISTRATION_SESSION_KEY, user)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=expected_origin(request),
        )
    except WebAuthnException as exc:
        raise WebAuthnError("Der Sicherheitsschlüssel konnte nicht registriert werden.") from exc

    response = credential.get("response")
    raw_transports = response.get("transports", []) if isinstance(response, dict) else []
    device_type = getattr(verified.credential_device_type, "value", verified.credential_device_type)
    try:
        return WebAuthnCredential.objects.create(
            user=user,
            name=(name or "").strip()[:100] or "Sicherheitsschlüssel",
            credential_id=bytes_to_base64url(verified.credential_id),
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=[t for t in raw_transports if t in _TRANSPORTS],
            aaguid=str(verified.aaguid or ""),
            device_type=str(device_type or ""),
            backed_up=bool(verified.credential_backed_up),
        )
    except IntegrityError as exc:
        raise WebAuthnError("Dieser Sicherheitsschlüssel ist bereits registriert.") from exc


def authentication_options(request: HttpRequest, user: Any) -> str:
    """Optionen für ``navigator.credentials.get`` (JSON) mit den Schlüsseln des Kontos."""
    descriptors = _descriptors(user)
    if not descriptors:
        raise WebAuthnError("Für dieses Konto ist kein Sicherheitsschlüssel registriert.")
    options = generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=descriptors,
        user_verification=UserVerificationRequirement.PREFERRED,
        timeout=TIMEOUT_MS,
    )
    _store_challenge(request, AUTHENTICATION_SESSION_KEY, user, options.challenge)
    return options_to_json(options)


def authenticate(request: HttpRequest, user: Any, credential: dict[str, Any]) -> WebAuthnCredential:
    """Signierte Anmeldung prüfen; der Signaturzähler erkennt geklonte Schlüssel."""
    challenge = _take_challenge(request, AUTHENTICATION_SESSION_KEY, user)
    stored = WebAuthnCredential.objects.filter(user=user, credential_id=str(credential.get("id", ""))).first()
    if stored is None:
        raise WebAuthnError("Dieser Sicherheitsschlüssel ist für das Konto nicht registriert.")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=expected_origin(request),
            credential_public_key=bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except WebAuthnException as exc:
        raise WebAuthnError("Die Anmeldung mit dem Sicherheitsschlüssel ist fehlgeschlagen.") from exc
    stored.sign_count = verified.new_sign_count
    stored.last_used_at = timezone.now()
    stored.save(update_fields=["sign_count", "last_used_at"])
    return stored
