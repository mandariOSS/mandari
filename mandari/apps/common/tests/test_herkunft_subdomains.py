# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertrauen zwischen Hauptdomain und Subdomains.

Eine Subdomain mit fremden oder öffentlich beschreibbaren Inhalten (z. B. eine Demo-Instanz)
darf weder Formulare der Hauptdomain im Namen angemeldeter Nutzer absenden noch deren
WebSocket-Verbindungen öffnen, noch Sitzungs- oder CSRF-Cookies der Hauptdomain setzen.
Organisations-Subdomains und eigene Bürgerportal-Hosts funktionieren weiter – jeweils für
sich selbst.
"""

from __future__ import annotations

import asyncio
import importlib
import runpy
from pathlib import Path
from typing import Any

import pytest
from channels.testing import WebsocketCommunicator
from django.conf import settings as django_settings
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.test import RequestFactory

SETTINGS_PY = Path(django_settings.BASE_DIR) / "mandari" / "settings.py"


def _produktion(monkeypatch: pytest.MonkeyPatch, **umgebung: str) -> dict[str, Any]:
    """settings.py mit Umgebung wie in der Produktionscompose auswerten."""
    werte = {
        "DEBUG": "false",
        "SITE_URL": "https://mandari.de",
        "ALLOWED_HOSTS": "mandari.de",
        "CSRF_TRUSTED_ORIGINS": "https://mandari.de",
        **umgebung,
    }
    for name, wert in werte.items():
        monkeypatch.setenv(name, wert)
    for name in ("SESSION_COOKIE_NAME", "CSRF_COOKIE_NAME"):
        if name not in umgebung:
            monkeypatch.delenv(name, raising=False)
    return runpy.run_path(str(SETTINGS_PY))


# ---------------------------------------------------------------- Einstellungen


def test_csrf_vertraut_keinen_subdomains(monkeypatch: pytest.MonkeyPatch) -> None:
    werte = _produktion(monkeypatch)
    assert werte["CSRF_TRUSTED_ORIGINS"] == ["https://mandari.de"]
    assert not any("*" in origin for origin in werte["CSRF_TRUSTED_ORIGINS"])


def test_weitere_urspruenge_nur_ausdruecklich(monkeypatch: pytest.MonkeyPatch) -> None:
    werte = _produktion(monkeypatch, CSRF_TRUSTED_ORIGINS="https://mandari.de, https://portal.stadt.example")
    assert werte["CSRF_TRUSTED_ORIGINS"] == ["https://mandari.de", "https://portal.stadt.example"]


def test_organisations_subdomains_bleiben_erreichbar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Weiterleitung volt.mandari.de → /work/volt/ braucht den Host in ALLOWED_HOSTS."""
    werte = _produktion(monkeypatch)
    assert ".mandari.de" in werte["ALLOWED_HOSTS"]
    assert werte["MAIN_DOMAIN"] == "mandari.de"


def test_cookies_nur_fuer_genau_diesen_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Präfix __Host-: Browser nehmen das Cookie nur ohne Domain-Attribut an – Subdomains können es nicht setzen."""
    werte = _produktion(monkeypatch)
    assert werte["SESSION_COOKIE_NAME"] == "__Host-sessionid"
    assert werte["CSRF_COOKIE_NAME"] == "__Host-csrftoken"
    assert werte["SESSION_COOKIE_DOMAIN"] is None
    assert werte["CSRF_COOKIE_DOMAIN"] is None
    assert werte["SESSION_COOKIE_SECURE"] is True
    assert werte["CSRF_COOKIE_SECURE"] is True
    assert werte.get("SESSION_COOKIE_PATH", "/") == "/"
    assert werte.get("CSRF_COOKIE_PATH", "/") == "/"


def test_ohne_https_bleiben_die_standardnamen(monkeypatch: pytest.MonkeyPatch) -> None:
    werte = _produktion(monkeypatch, DEBUG="true", SITE_URL="http://localhost:8000")
    assert werte["SESSION_COOKIE_NAME"] == "sessionid"
    assert werte["CSRF_COOKIE_NAME"] == "csrftoken"


# ---------------------------------------------------------------- CSRF-Prüfung


def _csrf_antwort(origin: str, host: str, trusted: list[str], settings: Any) -> int:
    settings.CSRF_TRUSTED_ORIGINS = trusted
    settings.ALLOWED_HOSTS = ["mandari.de", ".mandari.de"]
    factory = RequestFactory()
    vorlage = factory.get("/", HTTP_HOST=host, secure=True)
    token = get_token(vorlage)
    anfrage = factory.post(
        "/",
        {"csrfmiddlewaretoken": token},
        HTTP_HOST=host,
        HTTP_ORIGIN=origin,
        secure=True,
    )
    anfrage.COOKIES[settings.CSRF_COOKIE_NAME] = vorlage.META["CSRF_COOKIE"]
    middleware = CsrfViewMiddleware(lambda request: HttpResponse("ok"))
    middleware.process_request(anfrage)
    antwort = middleware.process_view(anfrage, lambda request: HttpResponse("ok"), (), {})
    return 200 if antwort is None else antwort.status_code


def test_formular_von_fremder_subdomain_wird_abgelehnt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    trusted = list(_produktion(monkeypatch)["CSRF_TRUSTED_ORIGINS"])
    assert _csrf_antwort("https://demo.mandari.de", "mandari.de", trusted, settings) == 403
    assert _csrf_antwort("https://evil.example", "mandari.de", trusted, settings) == 403


def test_formular_vom_eigenen_host_bleibt_erlaubt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    trusted = list(_produktion(monkeypatch)["CSRF_TRUSTED_ORIGINS"])
    assert _csrf_antwort("https://mandari.de", "mandari.de", trusted, settings) == 200
    # Organisations-Subdomain: Formulare auf volt.mandari.de gehen an volt.mandari.de
    assert _csrf_antwort("https://volt.mandari.de", "volt.mandari.de", trusted, settings) == 200


# ---------------------------------------------------------------- WebSockets


async def _annehmen(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "websocket.accept"})


def _websocket_erlaubt(origin: str, host: str) -> bool:
    from apps.common.websocket_origin import SameOriginWebSocketValidator

    # Die Anwendung prüft WebSockets mit dieser Hülle ...
    eingebaut = importlib.import_module("mandari.asgi").application.application_mapping["websocket"]
    assert isinstance(eingebaut, SameOriginWebSocketValidator)
    # ... geprüft wird die Hülle selbst, ohne Anmeldung und Consumer
    pruefung = SameOriginWebSocketValidator(_annehmen)

    async def lauf() -> bool:
        kommunikator = WebsocketCommunicator(
            pruefung, "/ws/documents/1/", headers=[(b"origin", origin.encode()), (b"host", host.encode())]
        )
        verbunden, _ = await kommunikator.connect()
        await kommunikator.disconnect()
        return bool(verbunden)

    return asyncio.run(lauf())


@pytest.fixture
def produktionsartig(settings: Any) -> None:
    settings.ALLOWED_HOSTS = ["mandari.de", ".mandari.de", "localhost"]
    settings.CSRF_TRUSTED_ORIGINS = ["https://mandari.de"]


def test_websocket_von_fremder_subdomain_wird_abgelehnt(produktionsartig: None) -> None:
    assert not _websocket_erlaubt("https://demo.mandari.de", "mandari.de")
    assert not _websocket_erlaubt("https://evil.example", "mandari.de")


def test_websocket_vom_eigenen_host_bleibt_erlaubt(produktionsartig: None) -> None:
    assert _websocket_erlaubt("https://mandari.de", "mandari.de")
    assert _websocket_erlaubt("https://volt.mandari.de", "volt.mandari.de")
