# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfen für Tests des OpenID-Connect-Anbieters (Issue #1206), auch für die Session-API.

Die Tests schalten den Anbieter mit :func:`eingeschaltet` ein und bekommen dafür einen frisch erzeugten
RSA-Schlüssel (nie aus dem Repository). Anwendungen entstehen mit :func:`anwendung`, Tokens ohne Ablauf über
:func:`access_token` direkt in der Datenbank, wie die Bibliothek sie speichert (nur die Prüfsumme).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import Client, override_settings
from django.utils import timezone
from oauth2_provider.models import set_token_value

from apps.accounts.oidc import access, catalog, reauth
from apps.accounts.oidc.models import OAuthAccessToken, OAuthApplication

REDIRECT_URI = "https://app.example.org/rueckweg"
PERSON_SCOPES = [catalog.OPENID, catalog.PROFILE, catalog.EMAIL, catalog.RATSDATEN_LESEN]


@lru_cache(maxsize=4)
def rsa_schluessel(nummer: int = 1, bits: int = 2048) -> str:
    """Unverschlüsselter RSA-Schlüssel als PEM, je Nummer einmal je Testlauf erzeugt."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def eingeschaltet(**weitere: Any) -> override_settings:
    """Anbieter an, mit Testschlüssel, bisherigem Schlüssel im JWKS und frischem Kern der Bibliothek je Anfrage."""
    provider = {
        **settings.OAUTH2_PROVIDER,
        "OIDC_RSA_PRIVATE_KEY": rsa_schluessel(1),
        "OIDC_RSA_PRIVATE_KEYS_INACTIVE": [rsa_schluessel(2)],
        "ALWAYS_RELOAD_OAUTHLIB_CORE": True,
        **weitere.pop("provider", {}),
    }
    return override_settings(OIDC_PROVIDER_ENABLED=True, OAUTH2_PROVIDER=provider, **weitere)


def anwendung(
    *,
    name: str = "Testanwendung",
    ablauf: str = OAuthApplication.GRANT_AUTHORIZATION_CODE,
    scopes: list[str] | None = None,
    vertrauenswuerdig: bool = False,
    oeffentlich: bool = False,
) -> tuple[OAuthApplication, str]:
    """Anwendung und ihr Client-Secret im Klartext (gespeichert ist nur der Hash)."""
    secret = "secret-" + secrets.token_urlsafe(24)
    person = ablauf == OAuthApplication.GRANT_AUTHORIZATION_CODE
    app = OAuthApplication(
        name=name,
        client_type=OAuthApplication.CLIENT_PUBLIC if oeffentlich else OAuthApplication.CLIENT_CONFIDENTIAL,
        authorization_grant_type=ablauf,
        redirect_uris=REDIRECT_URI if person else "",
        algorithm=OAuthApplication.RS256_ALGORITHM if person else OAuthApplication.NO_ALGORITHM,
        allowed_scopes=list(PERSON_SCOPES if scopes is None and person else scopes or []),
        skip_authorization=vertrauenswuerdig,
        client_secret=secret,
    )
    app.save()
    return app, secret


def access_token(
    app: OAuthApplication, user: Any = None, scope: str = catalog.RATSDATEN_LESEN, gueltig: int = 3600
) -> str:
    """Access-Token direkt in der Datenbank, gespeichert wie von der Bibliothek (nur Prüfsumme)."""
    raw = access.generate_access_token()
    token = OAuthAccessToken(
        user=user, application=app, scope=scope, expires=timezone.now() + timedelta(seconds=gueltig)
    )
    set_token_value(token, raw)
    token.save()
    return raw


def anmeldung_zurueckdatieren(client: Client, sekunden: int) -> int:
    """Die Anmeldung in der Sitzung von ``client`` liegt ``sekunden`` zurück; Ergebnis: der Zeitpunkt (Unix)."""
    zeitpunkt = int((timezone.now() - timedelta(seconds=sekunden)).timestamp())
    session = client.session
    session[reauth.LOGIN_TIME_KEY] = zeitpunkt
    session.save()
    return zeitpunkt


def pkce() -> tuple[str, str]:
    """``code_verifier`` und ``code_challenge`` (S256)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def autorisierung(app: OAuthApplication, challenge: str = "", **parameter: str) -> dict[str, str]:
    """Parameter einer Autorisierungsanfrage im Code-Ablauf."""
    werte = {
        "client_id": app.client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "openid ratsdaten:lesen",
        "state": "zustand-123",
        "nonce": "nonce-456",
    }
    if challenge:
        werte.update(code_challenge=challenge, code_challenge_method="S256")
    werte.update(parameter)
    return {key: value for key, value in werte.items() if value != ""}


def rueckweg(location: str) -> dict[str, str]:
    """Parameter der Weiterleitung zur Anwendung."""
    teile = urlsplit(location)
    assert f"{teile.scheme}://{teile.netloc}{teile.path}" == REDIRECT_URI, location
    return {key: values[0] for key, values in parse_qs(teile.query).items()}


def basic(app: OAuthApplication, secret: str) -> dict[str, Any]:
    wert = base64.b64encode(f"{app.client_id}:{secret}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {wert}"}


def code_holen(client: Client, app: OAuthApplication, challenge: str, **parameter: str) -> str:
    """Code über die Zustimmungsseite (bzw. direkt bei vertrauenswürdigen Anwendungen) holen."""
    werte = autorisierung(app, challenge, **parameter)
    response = client.get("/oauth/authorize/", werte)
    if response.status_code == 200:
        form = response.context["form"]
        daten = {name: form[name].value() or "" for name in form.fields if name != "allow"}
        response = client.post(f"/oauth/authorize/?{_query(werte)}", {**daten, "allow": "on"})
    assert response.status_code == 302, response.status_code
    return rueckweg(response["Location"])["code"]


def tokens_holen(client: Client, app: OAuthApplication, secret: str, code: str, verifier: str) -> dict[str, Any]:
    response = client.post(
        "/oauth/token/",
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier},
        **basic(app, secret),
    )
    assert response.status_code == 200, response.content
    return dict(response.json())


def _query(werte: dict[str, str]) -> str:
    return urlencode(werte)


def parse_qs_first(url: str, key: str) -> str:
    """Erster Wert eines Parameters der Adresse."""
    return parse_qs(urlsplit(url).query)[key][0]
