# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung vor dem Einschalten des OpenID-Connect-Anbieters (``manage.py oidc_pruefen``, Issue #1215).

Geprüft werden Schalter, Signaturschlüssel, Aussteller (``SITE_URL``) und ob Discovery und JWKS über den
Test-Client antworten, mit einer Schlüssel-ID im JWKS, die zum Schlüssel passt. Ist der Anbieter ausgeschaltet,
läuft die Prüfung trotzdem: Discovery und JWKS werden dann für die Dauer der Prüfung im eigenen Prozess
eingeschaltet abgefragt, mit dem Schlüssel aus der Umgebung. So zeigt sie, was zum Einschalten fehlt.

Jeder Befund hat eine Stufe: ``OK``, ``HINWEIS`` (Betrieb möglich, aber prüfenswert) oder ``FEHLER`` (so nicht
einschalten). Meldungen nennen nie den Inhalt eines Schlüssels, nur Länge und Schlüssel-ID.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from django.conf import settings
from django.db import DatabaseError
from django.http.request import validate_host
from django.test import Client, override_settings
from django.urls import reverse
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.utils import jwk_from_pem

from . import provider_enabled, signing_keys
from .models import LOOPBACK_HOSTS, OAuthApplication

OK = "OK"
HINWEIS = "HINWEIS"
FEHLER = "FEHLER"

#: Teile eines privaten RSA-Schlüssels im JWK-Format (RFC 7518, Abschnitt 6.3.2); im JWKS darf keiner stehen
PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth")


@dataclass(frozen=True)
class Finding:
    """Ein Befund der Prüfung."""

    level: str
    area: str
    text: str


@dataclass(frozen=True)
class _Keys:
    active: str
    previous: list[str]


def _key_id(pem: str) -> str:
    """Schlüssel-ID wie im JWKS der Bibliothek (RFC 7638-Thumbprint des öffentlichen Schlüssels)."""
    return str(jwk_from_pem(pem).thumbprint())


def _bits(pem: str) -> int:
    return int(getattr(load_pem_private_key(pem.encode("utf-8"), password=None), "key_size", 0))


def _configured_keys(environ: Mapping[str, str]) -> tuple[_Keys | None, list[Finding]]:
    """
    Aktiver und bisherige Schlüssel: aus der laufenden Konfiguration, sonst aus der Umgebung.

    ``settings.py`` liest einen eingestellten Schlüssel auch bei ausgeschaltetem Anbieter, verwirft ihn dann aber
    still, wenn er nicht taugt. Die Umgebung liefert in diesem Fall den Grund.
    """
    active = str(oauth2_settings.OIDC_RSA_PRIVATE_KEY or "")
    previous = [str(pem) for pem in oauth2_settings.OIDC_RSA_PRIVATE_KEYS_INACTIVE or []]
    if not active:
        try:
            active, previous = signing_keys.load(environ)
        except signing_keys.SigningKeyError as error:
            return None, [Finding(FEHLER, "Signaturschlüssel", str(error))]
    if not active:
        return None, [
            Finding(
                FEHLER,
                "Signaturschlüssel",
                f"Kein Schlüssel eingestellt. {signing_keys.ENV_KEY_FILE} (Pfad zur PEM-Datei) oder "
                f"{signing_keys.ENV_KEY} setzen; erzeugen etwa mit "
                "„openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out oidc.pem“.",
            )
        ]
    return _Keys(active=active, previous=previous), []


def _check_keys(environ: Mapping[str, str]) -> tuple[_Keys | None, list[Finding]]:
    keys, findings = _configured_keys(environ)
    if keys is None:
        return None, findings
    try:
        signing_keys.validate(keys.active, "Aktiver Schlüssel")
        for index, pem in enumerate(keys.previous, start=1):
            signing_keys.validate(pem, f"Bisheriger Schlüssel {index}")
    except signing_keys.SigningKeyError as error:
        return None, [Finding(FEHLER, "Signaturschlüssel", str(error))]
    findings.append(
        Finding(
            OK,
            "Signaturschlüssel",
            f"RSA mit {_bits(keys.active)} Bit, lesbar und unverschlüsselt, Schlüssel-ID {_key_id(keys.active)}.",
        )
    )
    for index, pem in enumerate(keys.previous, start=1):
        findings.append(
            Finding(
                HINWEIS,
                "Schlüsselwechsel",
                f"Bisheriger Schlüssel {index} (RSA mit {_bits(pem)} Bit, Schlüssel-ID {_key_id(pem)}) steht noch im "
                "JWKS. Nach Ablauf der ID-Tokens und des Zwischenspeichers des JWKS (eine Stunde) "
                f"{signing_keys.ENV_PREVIOUS_FILE} bzw. {signing_keys.ENV_PREVIOUS} wieder entfernen.",
            )
        )
    return keys, findings


def _check_issuer() -> tuple[str, list[Finding]]:
    """Aussteller (``OIDC_ISS_ENDPOINT`` aus ``SITE_URL``); liefert ihn zurück, wenn er für Abfragen taugt."""
    issuer = str(oauth2_settings.OIDC_ISS_ENDPOINT or "")
    area = "Aussteller"
    if not issuer:
        return "", [Finding(FEHLER, area, "Kein Aussteller eingestellt (SITE_URL).")]
    parts = urlsplit(issuer)
    findings: list[Finding] = []
    host = parts.hostname or ""
    if not host:
        return "", [Finding(FEHLER, area, f"{issuer} ist keine vollständige Adresse (SITE_URL mit https://<host>).")]
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        findings.append(
            Finding(
                FEHLER,
                area,
                f"{issuer}: SITE_URL darf keinen Pfad, keine Abfrage und kein Fragment haben. Discovery und Endpunkte "
                "liegen an der Wurzel der Installation.",
            )
        )
    loopback = host in LOOPBACK_HOSTS
    if parts.scheme != "https":
        if settings.DEBUG and loopback:
            findings.append(Finding(HINWEIS, area, f"{issuer} ohne https, nur für die Entwicklung (DEBUG)."))
        else:
            findings.append(Finding(FEHLER, area, f"{issuer}: Der Aussteller muss https verwenden (SITE_URL)."))
    if loopback and not settings.DEBUG:
        findings.append(
            Finding(
                FEHLER,
                area,
                f"{issuer} zeigt auf diesen Rechner; Anwendungen erreichen den Anbieter so nicht. SITE_URL auf die "
                "öffentliche Adresse der Installation setzen.",
            )
        )
    if not validate_host(host, settings.ALLOWED_HOSTS):
        findings.append(Finding(FEHLER, area, f"Der Host {host} steht nicht in ALLOWED_HOSTS."))
    if any(finding.level == FEHLER for finding in findings):
        return "", findings
    findings.append(Finding(OK, area, f"{issuer} (Discovery unter {issuer}/.well-known/openid-configuration)."))
    return issuer, findings


def _enabled_for_check(keys: _Keys) -> AbstractContextManager[Any]:
    """Ausgeschaltet: Anbieter für die Dauer der Prüfung in diesem Prozess einschalten, mit den geprüften Schlüsseln."""
    if provider_enabled():
        return nullcontext()
    provider = {
        **settings.OAUTH2_PROVIDER,
        "OIDC_RSA_PRIVATE_KEY": keys.active,
        "OIDC_RSA_PRIVATE_KEYS_INACTIVE": list(keys.previous),
    }
    return override_settings(OIDC_PROVIDER_ENABLED=True, OAUTH2_PROVIDER=provider)


def _json(response: Any) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


#: Abruf eines Pfads über den Test-Client, mit Host und Schema des Ausstellers
Fetch = Callable[[str], Any]


def _fetcher(issuer: str) -> Fetch:
    """
    Abruf wie hinter dem Reverse Proxy: Host des Ausstellers, bei https mit gesichertem Schema und, falls eingestellt,
    dem Kopf aus ``SECURE_PROXY_SSL_HEADER``. Fehler der Anwendung kommen als Antwort zurück, nicht als Ausnahme.
    """
    parts = urlsplit(issuer)
    client = Client(HTTP_HOST=parts.netloc, raise_request_exception=False)
    secure = parts.scheme == "https"
    headers: dict[str, str] = {}
    proxy_header = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
    if secure and proxy_header and str(proxy_header[0]).startswith("HTTP_"):
        # META-Name (HTTP_X_FORWARDED_PROTO) → Kopfzeile (X-FORWARDED-PROTO)
        headers[str(proxy_header[0])[5:].replace("_", "-")] = str(proxy_header[1])

    def fetch(path: str) -> Any:
        return client.get(path, secure=secure, headers=headers)

    return fetch


def _check_discovery(fetch: Fetch, issuer: str) -> tuple[str, list[Finding]]:
    """Discovery abrufen und mit dem Aussteller abgleichen; liefert den Pfad des JWKS."""
    area = "Discovery"
    response = fetch(reverse("oauth2_provider:oidc-connect-discovery-info"))
    data = _json(response) if response.status_code == 200 else None
    if data is None:
        return "", [Finding(FEHLER, area, f"Antwort {response.status_code} statt eines JSON-Dokuments.")]
    problems: list[str] = []
    if data.get("issuer") != issuer:
        problems.append(f"issuer ist {data.get('issuer')!r} statt {issuer!r}")
    expected = {
        "authorization_endpoint": "authorize",
        "token_endpoint": "token",
        "userinfo_endpoint": "user-info",
        "jwks_uri": "jwks-info",
        "revocation_endpoint": "revoke-token",
        "introspection_endpoint": "introspect",
    }
    for field, name in expected.items():
        url = issuer + reverse(f"oauth2_provider:{name}")
        if data.get(field) != url:
            problems.append(f"{field} ist {data.get(field)!r} statt {url!r}")
    for field, value in (
        ("response_types_supported", ["code"]),
        ("code_challenge_methods_supported", ["S256"]),
        ("id_token_signing_alg_values_supported", ["RS256"]),
    ):
        if data.get(field) != value:
            problems.append(f"{field} ist {data.get(field)!r} statt {value!r}")
    if problems:
        return "", [Finding(FEHLER, area, "; ".join(problems) + ".")]
    return urlsplit(str(data["jwks_uri"])).path, [Finding(OK, area, "antwortet, Aussteller und Endpunkte passen.")]


def _check_jwks(fetch: Fetch, path: str, keys: _Keys) -> list[Finding]:
    area = "JWKS"
    response = fetch(path)
    data = _json(response) if response.status_code == 200 else None
    entries = data.get("keys") if data is not None else None
    if not isinstance(entries, list) or not entries:
        return [Finding(FEHLER, area, f"Antwort {response.status_code} ohne Schlüssel.")]
    if any(isinstance(entry, dict) and set(entry) & set(PRIVATE_JWK_MEMBERS) for entry in entries):
        return [Finding(FEHLER, area, "Das JWKS enthält Teile eines privaten Schlüssels. Nicht einschalten.")]
    published = [str(entry.get("kid", "")) for entry in entries if isinstance(entry, dict)]
    expected = [_key_id(keys.active), *(_key_id(pem) for pem in keys.previous)]
    if published[:1] != expected[:1]:
        return [
            Finding(
                FEHLER,
                area,
                f"Die Schlüssel-ID im JWKS ({published[0] or 'keine'}) passt nicht zum aktiven Schlüssel "
                f"({expected[0]}).",
            )
        ]
    if sorted(published) != sorted(expected):
        return [
            Finding(
                FEHLER,
                area,
                f"Das JWKS nennt die Schlüssel-IDs {', '.join(published)}, erwartet sind {', '.join(expected)}.",
            )
        ]
    if any(entry.get("alg") != "RS256" or entry.get("use") != "sig" for entry in entries if isinstance(entry, dict)):
        return [Finding(FEHLER, area, "Jeder Schlüssel im JWKS braucht alg RS256 und use sig.")]
    anzahl = "einem Schlüssel" if len(entries) == 1 else f"{len(entries)} Schlüsseln"
    return [Finding(OK, area, f"antwortet mit {anzahl}; die Schlüssel-ID passt zum aktiven Schlüssel.")]


def _check_endpoints(issuer: str, keys: _Keys) -> list[Finding]:
    """Discovery und JWKS über den Test-Client, mit Host und Schema des Ausstellers wie hinter dem Proxy."""
    fetch = _fetcher(issuer)
    with _enabled_for_check(keys):
        jwks_path, findings = _check_discovery(fetch, issuer)
        if jwks_path:
            findings.extend(_check_jwks(fetch, jwks_path, keys))
    return findings


def _check_operation() -> list[Finding]:
    findings: list[Finding] = []
    if settings.DEBUG:
        findings.append(
            Finding(
                HINWEIS,
                "Betrieb",
                "DEBUG ist an: http-Redirects für localhost sind erlaubt. In Produktion DEBUG=false.",
            )
        )
    limit = int(getattr(settings, "OIDC_TOKEN_RATE_LIMIT", 30) or 0)
    if limit <= 0:
        findings.append(
            Finding(HINWEIS, "Betrieb", "Die Ratenbegrenzung an Token-Endpunkt, Widerruf und Introspektion ist aus.")
        )
    try:
        active = int(OAuthApplication.objects.filter(is_active=True).count())
    except DatabaseError as error:
        findings.append(Finding(HINWEIS, "Anwendungen", f"Nicht gezählt, die Datenbank antwortet nicht ({error})."))
        return findings
    if active:
        findings.append(Finding(OK, "Anwendungen", f"{active} aktive Anwendung(en)."))
    else:
        findings.append(
            Finding(
                HINWEIS,
                "Anwendungen",
                "Noch keine aktive Anwendung. Anlegen mit „manage.py oidc_anwendung anlegen …“.",
            )
        )
    return findings


def check(environ: Mapping[str, str] | None = None) -> Iterator[Finding]:
    """Alle Befunde der Reihe nach; ``environ`` für den Schlüssel, wenn die Konfiguration keinen hat."""
    environ = os.environ if environ is None else environ
    if provider_enabled():
        yield Finding(OK, "Schalter", "Der Anbieter ist eingeschaltet (OIDC_PROVIDER_ENABLED).")
    else:
        yield Finding(
            HINWEIS,
            "Schalter",
            "Der Anbieter ist ausgeschaltet (OIDC_PROVIDER_ENABLED); alle Endpunkte antworten mit 404. Die Prüfung "
            "zeigt, ob er sich einschalten lässt.",
        )
    keys, findings = _check_keys(environ)
    yield from findings
    issuer, findings = _check_issuer()
    yield from findings
    if keys is None or not issuer:
        yield Finding(
            HINWEIS, "Discovery und JWKS", "Nicht abgefragt, solange Schlüssel oder Aussteller nicht stimmen."
        )
    else:
        yield from _check_endpoints(issuer, keys)
    yield from _check_operation()
