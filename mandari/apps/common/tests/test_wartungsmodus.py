# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wartungsmodus (Issue #588): sperrt Besucher mit 503, Administration und Prüfpfade bleiben offen."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import OperationalError, connection
from django.test import Client

from apps.common import maintenance
from apps.common.admin import SiteSettingsAdmin
from apps.common.models import SiteSettings
from apps.common.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _frischer_cache(settings: Any) -> Iterator[None]:
    settings.MAINTENANCE_MODE_ENFORCEMENT = True  # in settings_test aus
    # Die Einstellungen liegen im Prozess-Cache und überdauern das Zurückrollen der Testdatenbank
    cache.delete(SiteSettings.CACHE_KEY)
    yield
    cache.delete(SiteSettings.CACHE_KEY)


def _wartung(nachricht: str = "Umstellung auf die Datendrehscheibe bis 18 Uhr.") -> None:
    einstellungen = SiteSettings.get_settings()
    einstellungen.maintenance_mode = True
    einstellungen.maintenance_message = nachricht
    einstellungen.save()


def test_ohne_wartungsmodus_ist_alles_erreichbar(client: Client) -> None:
    assert client.get("/accounts/password-reset/").status_code == 200


def test_wartungsmodus_sperrt_besucher_mit_503(client: Client) -> None:
    _wartung()

    antwort = client.get("/accounts/password-reset/")

    assert antwort.status_code == 503
    assert antwort["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)
    assert "no-store" in antwort["Cache-Control"]
    assert "Umstellung auf die Datendrehscheibe bis 18 Uhr." in antwort.content.decode()


@pytest.mark.parametrize("pfad", ["/", "/insight/", "/work/", "/session/", "/feedback/", "/media/bodies/x.png"])
def test_wartungsmodus_gilt_fuer_alle_bereiche(client: Client, pfad: str) -> None:
    _wartung()
    assert client.get(pfad).status_code == 503


@pytest.mark.parametrize("pfad", ["/api/v1/session/openapi.json", "/api/stats/", "/oparl/"])
def test_apis_erhalten_json(client: Client, pfad: str) -> None:
    _wartung("Kurz weg.")

    antwort = client.get(pfad)

    assert antwort.status_code == 503
    assert antwort.json() == {"error": "maintenance", "detail": "Kurz weg."}
    assert antwort["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)


@pytest.mark.parametrize(
    ("pfad", "erwartet"),
    [
        ("/health/live/", 200),
        ("/accounts/login/", 200),
        ("/accounts/logout/", None),
        ("/admin/", None),
        ("/metrics/", None),
    ],
)
def test_administration_und_pruefpfade_bleiben_erreichbar(client: Client, pfad: str, erwartet: int | None) -> None:
    _wartung()

    antwort = client.get(pfad)

    assert antwort.status_code != 503
    if erwartet is not None:
        assert antwort.status_code == erwartet


def test_gesundheitspruefung_braucht_auch_im_wartungsmodus_keine_datenbank(
    client: Client, django_assert_num_queries: Any
) -> None:
    _wartung()
    with django_assert_num_queries(0):
        assert client.get("/health/live/").status_code == 200


def test_mitarbeiter_sehen_die_anwendung_weiter(client: Client) -> None:
    _wartung()
    client.force_login(cast(Any, UserFactory)(is_staff=True))

    assert client.get("/accounts/password-reset/").status_code == 200


def test_angemeldete_ohne_mitarbeiterstatus_bleiben_gesperrt(client: Client) -> None:
    _wartung()
    client.force_login(cast(Any, UserFactory)())

    assert client.get("/accounts/password-reset/").status_code == 503


def test_htmx_anfrage_laedt_die_seite_neu(client: Client) -> None:
    _wartung()

    antwort = client.get("/accounts/password-reset/", headers={"HX-Request": "true"})

    assert antwort.status_code == 503
    assert antwort["HX-Refresh"] == "true"


def test_nachricht_wird_escaped_und_leere_nachricht_hat_ersatztext(client: Client) -> None:
    _wartung("<script>alert(1)</script>")
    inhalt = client.get("/").content.decode()
    assert "<script>alert(1)</script>" not in inhalt
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in inhalt

    _wartung("   ")
    assert client.get("/api/stats/").json()["detail"] == maintenance.STANDARD_NACHRICHT


def test_abschalten_wirkt_sofort(client: Client) -> None:
    _wartung()
    assert client.get("/accounts/password-reset/").status_code == 503

    einstellungen = SiteSettings.get_settings()
    einstellungen.maintenance_mode = False
    einstellungen.save()

    assert client.get("/accounts/password-reset/").status_code == 200


def test_unlesbarer_schalter_sperrt_nicht(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> SiteSettings:
        raise ConnectionError("Cache nicht erreichbar")

    monkeypatch.setattr(SiteSettings, "get_settings", kaputt)

    assert client.get("/accounts/password-reset/").status_code == 200


def test_fehlende_datenbank_antwortet_sofort_mit_503(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def weg() -> SiteSettings:
        raise OperationalError("could not connect to server: Connection refused")

    monkeypatch.setattr(SiteSettings, "get_settings", weg)

    antwort = client.get("/api/stats/")

    assert antwort.status_code == 503
    assert antwort.json()["error"] == "service_unavailable"


def test_ohne_durchsetzung_wirkt_der_schalter_nicht(client: Client, settings: Any) -> None:
    _wartung()
    settings.MAINTENANCE_MODE_ENFORCEMENT = False

    assert client.get("/accounts/password-reset/").status_code == 200


def test_middleware_ist_eingehaengt() -> None:
    from django.conf import settings as django_settings

    stapel = django_settings.MIDDLEWARE
    eintrag = "apps.common.maintenance.MaintenanceModeMiddleware"
    assert stapel.index(eintrag) > stapel.index("django.contrib.auth.middleware.AuthenticationMiddleware")


def test_seitenname_und_beschreibung_sind_entfallen() -> None:
    felder = {feld.name for feld in SiteSettings._meta.get_fields()}
    assert not felder & {"site_name", "site_description"}
    im_admin = {
        name
        for _titel, optionen in SiteSettingsAdmin.fieldsets
        for eintrag in optionen["fields"]
        for name in (eintrag if isinstance(eintrag, tuple) else (eintrag,))
    }
    assert not im_admin & {"site_name", "site_description"}


def test_alte_spalten_bleiben_mit_standardwert_fuer_den_rueckfall() -> None:
    """Eine ältere Version liest die Spalten weiter; neue Zeilen erhalten den Datenbank-Standardwert."""
    SiteSettings.objects.all().delete()
    cache.delete(SiteSettings.CACHE_KEY)

    SiteSettings.get_settings()

    with connection.cursor() as cursor:
        cursor.execute("SELECT site_name, site_description FROM common_sitesettings WHERE id = 1")
        assert cursor.fetchone() == ("Mandari", "Kommunalpolitische Transparenz")
