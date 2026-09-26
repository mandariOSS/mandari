# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betriebs- und Konfigurationshärtung:

- Die Readiness-Prüfung nennt bei Fehlern nur die Art, nicht den Ausnahmetext.
- Die Systemeinstellungen im Admin geben SMTP-Passwort und API-Schlüssel nicht ins HTML aus;
  leer gelassene Felder behalten den gespeicherten Wert.
- Ausnahmen von der Zwei-Faktor-Pflicht per E-Mail-Domain gelten nie für Plattform-Administration.
- Ohne DEBUG startet die Anwendung nicht mit dem eingebauten Rückfall-``SECRET_KEY``.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client

from apps.common import health

GEHEIM = "postgres://mandari:GEHEIM123@db:5432/mandari"


@pytest.mark.django_db
def test_readiness_ohne_ausnahmetext(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> str:
        raise ConnectionError(GEHEIM)

    checks = {name: (lambda: "ok") for name in health.CHECKS}
    checks["cache"] = kaputt
    monkeypatch.setattr(health, "CHECKS", checks)

    antwort = client.get("/health/ready/")

    assert antwort.status_code == 503
    assert "GEHEIM123" not in antwort.content.decode()
    assert "ConnectionError" in antwort.json()["checks"]["cache"]["detail"]


@pytest.mark.django_db
class TestSystemeinstellungen:
    def _einstellungen(self) -> Any:
        from apps.common.models import SiteSettings

        einstellungen = cast(Any, SiteSettings).get_settings()
        einstellungen.set_email_host_password("SMTP-GEHEIM-123")
        einstellungen.set_nebius_api_key("NEBIUS-GEHEIM-456")
        einstellungen.save()
        return einstellungen

    def test_geheimnisse_nicht_im_formular(self, admin_client: Client) -> None:
        einstellungen = self._einstellungen()
        seite = admin_client.get(f"/admin/common/sitesettings/{einstellungen.pk}/change/")
        assert seite.status_code == 200
        inhalt = seite.content.decode()
        assert "SMTP-GEHEIM-123" not in inhalt
        assert "NEBIUS-GEHEIM-456" not in inhalt

    def test_leere_felder_behalten_die_werte(self) -> None:
        from django.forms.models import model_to_dict

        from apps.common.admin import SiteSettingsAdminForm

        einstellungen = self._einstellungen()
        daten = {k: v for k, v in model_to_dict(einstellungen).items() if v is not None}
        daten.update({"email_host_password": "", "nebius_api_key": ""})
        formular = cast(Any, SiteSettingsAdminForm)(data=daten, instance=einstellungen)
        assert formular.is_valid(), formular.errors
        formular.save()
        einstellungen.refresh_from_db()
        assert einstellungen.get_email_host_password() == "SMTP-GEHEIM-123"
        assert einstellungen.get_stored_nebius_api_key() == "NEBIUS-GEHEIM-456"


@pytest.mark.django_db
class TestAusnahmeVonDerZweiFaktorPflicht:
    @pytest.fixture(autouse=True)
    def _pflicht(self, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = True
        settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = ["demo.mandari.de"]

    def test_plattform_administration_nie_ausgenommen(self) -> None:
        from apps.accounts.models import User
        from apps.accounts.two_factor_policy import two_factor_required

        admin = User.objects.create_user(  # type: ignore[no-untyped-call]
            email="admin@demo.mandari.de", password="x", is_staff=True, is_superuser=True
        )
        assert two_factor_required(admin)

    def test_demo_zugaenge_bleiben_ausgenommen(self, org: Any, make_member: Any) -> None:
        from apps.accounts.two_factor_policy import two_factor_required

        mitglied = make_member(org, [], email="demo-vorsitz@demo.mandari.de", is_admin=True)
        assert not two_factor_required(mitglied.user)


SETTINGS_PY = Path(django_settings.BASE_DIR) / "mandari" / "settings.py"


def test_ohne_debug_kein_rueckfall_schluessel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured):
        runpy.run_path(str(SETTINGS_PY))


def test_mit_debug_bleibt_der_rueckfall_fuer_die_entwicklung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    werte = runpy.run_path(str(SETTINGS_PY))
    assert werte["SECRET_KEY"]


def test_ohne_debug_mit_eigenem_schluessel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "ein-eigener-schluessel-" + "x" * 40)
    werte = runpy.run_path(str(SETTINGS_PY))
    assert werte["DEBUG"] is False
