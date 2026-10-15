# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlerseiten ohne Datenbank (Issue #1024).

Anfragen ohne passende Route kosteten rund 15 % der Serverzeit: Die 404-Seite lief durch alle Kontextprozessoren,
fragte die Kommunen ab und legte für Besucher ohne Sitzung eine an (Kommunenauswahl „alle“). Gemessen vor der
Änderung, anonym ohne Cookie wie ein Scanner, mit gelisteter Kommune und Sitzungen wie in Produktion
(``cached_db``): fünf Abfragen je 404 (Kommunen, Sitzung prüfen, Sicherungspunkt, INSERT, Freigabe) und ein
Sitzungscookie. Dasselbe kostete jede Weiterleitung von ``APPEND_SLASH`` (in den Metriken ``view="unresolved"``,
Status 3xx), weil ``CommonMiddleware`` erst die 404 rendert und sie dann ersetzt.

Jetzt rendern die Fehler-Handler ohne Kontextprozessoren (``mandari.urls.fehlerseite``). Die Seiten sehen gleich
aus: Die Vorlagen brauchen nur ``request`` und ``csp_nonce``.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import path

import mandari.urls as urls
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db


def _verboten(request: HttpRequest) -> HttpResponse:
    raise PermissionDenied


#: Eigene URL-Konfiguration für eine View, die 403 auslöst (die echten Views prüfen vorher Anmeldung und Rechte)
urlpatterns = [path("verboten/", _verboten), *urls.urlpatterns]
handler400 = urls.handler400
handler403 = urls.handler403
handler404 = urls.handler404
handler500 = urls.handler500


@pytest.fixture(autouse=True)
def _wie_in_produktion(settings: Any) -> None:
    """Sitzungen, Wartungsmodus und 2FA-Pflicht wie im Betrieb; eine gelistete Kommune wie in jeder Installation."""
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    settings.MAINTENANCE_MODE_ENFORCEMENT = True
    settings.TWO_FACTOR_ENFORCEMENT = True
    cache.clear()
    quelle = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=quelle, name="Stadt", slug="stadt")
    # Die Einstellung des Wartungsmodus liegt im Betrieb im Cache (5 Minuten); den ersten Abruf nicht mitzählen
    Client().get("/wp-login.php")


def _abfragen(pfad: str, **extra: Any) -> tuple[Any, list[str]]:
    with CaptureQueriesContext(connection) as erfasst:
        antwort = Client(**extra).get(pfad)
    return antwort, [abfrage["sql"] for abfrage in erfasst.captured_queries]


@pytest.mark.parametrize("pfad", ["/wp-login.php", "/.env", "/wp-admin/", "/insight/gibt-es-nicht/", "/xmlrpc.php"])
def test_404_ohne_abfrage_und_ohne_sitzung(pfad: str, settings: Any) -> None:
    antwort, abfragen = _abfragen(pfad)

    assert antwort.status_code == 404
    assert abfragen == []
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies, "keine Sitzung je Scanner-Anfrage"
    inhalt = antwort.content.decode()
    assert "Seite nicht gefunden" in inhalt
    assert "/insight/" in inhalt, "Link zum Bürgerportal"
    assert antwort.wsgi_request.resolver_match is None, "in den Metriken view=unresolved"


@pytest.mark.parametrize(
    ("pfad", "ziel"), [("/insight/gremien", "/insight/gremien/"), ("/accounts/login", "/accounts/login/")]
)
def test_weiterleitung_mit_schraegstrich_ohne_abfrage(pfad: str, ziel: str, settings: Any) -> None:
    """APPEND_SLASH: CommonMiddleware rendert erst die 404 und ersetzt sie dann durch die Weiterleitung."""
    antwort, abfragen = _abfragen(pfad)

    assert antwort.status_code == 301
    assert antwort["Location"] == ziel
    assert abfragen == []
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies


def test_400_bei_fremdem_host_ohne_abfrage() -> None:
    antwort, abfragen = _abfragen("/insight/", HTTP_HOST="scanner.example")

    assert antwort.status_code == 400
    assert abfragen == []
    assert "Ungültige Anfrage" in antwort.content.decode()


def test_403_ohne_abfrage(settings: Any) -> None:
    settings.ROOT_URLCONF = __name__
    antwort, abfragen = _abfragen("/verboten/")

    assert antwort.status_code == 403
    assert abfragen == []
    assert "/accounts/login/?next=/verboten/" in antwort.content.decode()


def test_500_ohne_abfrage() -> None:
    request = RequestFactory().get("/irgendwo/")
    with CaptureQueriesContext(connection) as erfasst:
        try:
            raise RuntimeError("Fehler in einer View")
        except RuntimeError:
            antwort = urls.handler_500(request)
    assert antwort.status_code == 500
    assert erfasst.captured_queries == []
    assert "Fehler-ID" in antwort.content.decode()


@pytest.mark.parametrize(
    ("vorlage", "status", "kontext"),
    [
        ("400.html", 400, {}),
        ("403.html", 403, {}),
        ("404.html", 404, {}),
        ("500.html", 500, {"request_id": "ab12cd34"}),
    ],
)
def test_seite_sieht_aus_wie_mit_kontextprozessoren(vorlage: str, status: int, kontext: dict[str, object]) -> None:
    """Für Menschen ändert sich nichts: gleiches HTML wie vorher mit allen Kontextprozessoren (gleiche Anfrage)."""
    anfrage = Client().get("/wp-login.php").wsgi_request  # mit Sitzung, Anmeldung und CSP-Nonce der Middleware

    neu = urls.fehlerseite(anfrage, vorlage, status, kontext)
    vorher = render(anfrage, vorlage, kontext, status=status)

    assert neu.status_code == vorher.status_code == status
    assert neu.content == vorher.content
    assert b'nonce="' in neu.content and b'nonce=""' not in neu.content, "Nonce der Content-Security-Policy"
