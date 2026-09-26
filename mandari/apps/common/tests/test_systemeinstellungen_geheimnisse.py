# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SMTP-Passwort und Nebius-Schlüssel der Systemeinstellungen liegen verschlüsselt in der Datenbank.

- Setter verschlüsseln mit dem Hauptschlüssel; Getter, Mail-Konfiguration und Mail-Backend
  liefern den Klartext wie bisher.
- Die Migration common/0006 verschlüsselt den Bestand und leert die Klartextspalten. Sie ist
  wiederholbar, bricht ohne gültigen Hauptschlüssel ab, ohne etwas zu ändern, und lässt sich
  zurückdrehen.
- Das Admin-Formular gibt nie einen Wert aus; ein leeres Feld behält den gespeicherten Wert.

Alle Werte hier sind Testwerte; geprüft wird zusätzlich, dass Protokoll und Fehlermeldungen
sie nicht enthalten.
"""

from __future__ import annotations

import base64
import importlib
import logging
import secrets
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.forms.models import model_to_dict
from django.test import Client, override_settings

from apps.common.admin import SiteSettingsAdminForm
from apps.common.email_backend import SiteSettingsEmailBackend
from apps.common.models import SiteSettings

MIGRATION = importlib.import_module("apps.common.migrations.0006_systemeinstellungen_verschluesselt")
VORHER = ("common", "0005_protokoll_kettenkopf")
NACHHER = ("common", "0006_systemeinstellungen_verschluesselt")

SMTP = "smtp-testgeheimnis-" + secrets.token_hex(8)
NEBIUS = "nebius-testgeheimnis-" + secrets.token_hex(8)
FREMDER_SCHLUESSEL = base64.b64encode(secrets.token_bytes(32)).decode()


def _roh() -> dict[str, Any]:
    """Gespeicherte Spalten, an Modell und Cache vorbei."""
    werte = SiteSettings.objects.filter(pk=1).values(
        "email_host_password_legacy",
        "email_host_password_encrypted",
        "nebius_api_key_legacy",
        "nebius_api_key_encrypted",
    )
    return {name: bytes(wert) if isinstance(wert, memoryview) else wert for name, wert in werte.get().items()}


def _enthaelt_klartext(roh: dict[str, Any]) -> bool:
    gespeichert = b"".join(wert if isinstance(wert, bytes) else str(wert).encode() for wert in roh.values() if wert)
    return SMTP.encode() in gespeichert or NEBIUS.encode() in gespeichert


def _mit_geheimnissen() -> SiteSettings:
    einstellungen = SiteSettings.get_settings()
    einstellungen.email_host = "smtp.example.org"
    einstellungen.email_host_user = "versand@example.org"
    einstellungen.set_email_host_password(SMTP)
    einstellungen.set_nebius_api_key(NEBIUS)
    cast(Any, einstellungen).save()
    return SiteSettings.objects.get(pk=1)


def _altbestand() -> None:
    """Stand vor der Migration: Klartext in den früheren Spalten, nichts verschlüsselt."""
    SiteSettings.objects.get_or_create(pk=1)
    SiteSettings.objects.filter(pk=1).update(
        email_host="smtp.example.org",
        email_host_password_legacy=SMTP,
        email_host_password_encrypted=None,
        nebius_api_key_legacy=NEBIUS,
        nebius_api_key_encrypted=None,
    )
    cache.delete(SiteSettings.CACHE_KEY)


# =============================================================================
# Modell: verschlüsselt speichern, Klartext liefern
# =============================================================================


@pytest.mark.django_db
class TestModell:
    def test_gespeichert_wird_nur_geheimtext(self) -> None:
        _mit_geheimnissen()
        roh = _roh()
        assert roh["email_host_password_encrypted"] and roh["nebius_api_key_encrypted"]
        assert roh["email_host_password_legacy"] == "" and roh["nebius_api_key_legacy"] == ""
        assert not _enthaelt_klartext(roh)

    def test_getter_liefern_den_klartext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
        einstellungen = _mit_geheimnissen()
        assert einstellungen.get_email_host_password() == SMTP
        assert einstellungen.get_stored_nebius_api_key() == NEBIUS
        assert SiteSettings.get_nebius_api_key() == NEBIUS
        assert SiteSettings.get_email_config()["EMAIL_HOST_PASSWORD"] == SMTP

    def test_umgebungsvariable_geht_vor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mit_geheimnissen()
        monkeypatch.setenv("NEBIUS_API_KEY", "aus-der-umgebung")
        assert SiteSettings.get_nebius_api_key() == "aus-der-umgebung"

    def test_mail_backend_meldet_sich_mit_dem_klartext_an(self) -> None:
        _mit_geheimnissen()
        backend = cast(Any, SiteSettingsEmailBackend)()
        assert backend.host == "smtp.example.org"
        assert backend.password == SMTP

    def test_leerer_wert_loescht(self) -> None:
        einstellungen = _mit_geheimnissen()
        einstellungen.set_email_host_password("")
        cast(Any, einstellungen).save()
        assert _roh()["email_host_password_encrypted"] is None
        assert not SiteSettings.objects.get(pk=1).has_email_host_password

    def test_klartext_einer_aelteren_version_ist_der_juengere(self) -> None:
        """Nach einem Rückfall schreibt eine ältere Version wieder in die Klartextspalte – der Wert gilt."""
        _mit_geheimnissen()
        SiteSettings.objects.filter(pk=1).update(email_host_password_legacy="nach-rueckfall-eingetragen")
        assert SiteSettings.objects.get(pk=1).get_email_host_password() == "nach-rueckfall-eingetragen"

    def test_unlesbar_ergibt_leer_ohne_wert_im_protokoll(self, caplog: pytest.LogCaptureFixture) -> None:
        _mit_geheimnissen()
        with override_settings(ENCRYPTION_MASTER_KEY=FREMDER_SCHLUESSEL), caplog.at_level(logging.ERROR):
            einstellungen = SiteSettings.objects.get(pk=1)
            assert einstellungen.get_email_host_password() == ""
            assert einstellungen.get_stored_nebius_api_key() == ""
        assert "nicht lesbar" in caplog.text
        assert SMTP not in caplog.text and NEBIUS not in caplog.text


# =============================================================================
# Migration common/0006
# =============================================================================


@pytest.mark.django_db
class TestMigration:
    def test_verschluesselt_den_bestand_und_leert_die_klartextspalten(self) -> None:
        _altbestand()
        MIGRATION.encrypt_secrets(django_apps, None)
        roh = _roh()
        assert roh["email_host_password_legacy"] == "" and roh["nebius_api_key_legacy"] == ""
        assert not _enthaelt_klartext(roh)
        einstellungen = SiteSettings.get_settings()
        assert einstellungen.get_email_host_password() == SMTP
        assert einstellungen.get_stored_nebius_api_key() == NEBIUS

    def test_wiederholbar(self) -> None:
        _altbestand()
        MIGRATION.encrypt_secrets(django_apps, None)
        erster_lauf = _roh()
        MIGRATION.encrypt_secrets(django_apps, None)
        assert _roh() == erster_lauf  # nichts doppelt verschlüsselt

    @pytest.mark.parametrize("schluessel", ["", base64.b64encode(b"x" * 24).decode()], ids=["fehlt", "zu-kurz"])
    def test_ohne_gueltigen_hauptschluessel_bricht_ab_ohne_etwas_zu_aendern(self, schluessel: str) -> None:
        _altbestand()
        vorher = _roh()
        with override_settings(ENCRYPTION_MASTER_KEY=schluessel), pytest.raises(RuntimeError) as fehler:
            MIGRATION.encrypt_secrets(django_apps, None)
        assert "ENCRYPTION_MASTER_KEY" in str(fehler.value)
        assert SMTP not in str(fehler.value) and NEBIUS not in str(fehler.value)
        assert _roh() == vorher

    def test_ohne_geheimnisse_braucht_es_keinen_schluessel(self) -> None:
        SiteSettings.objects.get_or_create(pk=1)
        with override_settings(ENCRYPTION_MASTER_KEY=""):
            MIGRATION.encrypt_secrets(django_apps, None)

    def test_verwirft_die_zwischengespeicherte_instanz_mit_klartext(self) -> None:
        _altbestand()
        cache.set(SiteSettings.CACHE_KEY, SiteSettings.objects.get(pk=1))
        MIGRATION.encrypt_secrets(django_apps, None)
        assert cache.get(SiteSettings.CACHE_KEY) is None

    def test_rueckwaerts_stellt_die_klartextspalten_wieder_her(self) -> None:
        _mit_geheimnissen()
        MIGRATION.restore_plaintext(django_apps, None)
        roh = _roh()
        assert roh["email_host_password_legacy"] == SMTP and roh["nebius_api_key_legacy"] == NEBIUS
        assert roh["email_host_password_encrypted"] is None


@pytest.mark.django_db(transaction=True)
def test_migration_im_schema_vor_und_zurueck() -> None:
    """Echter Lauf über die Migrationsgeschichte: Abbruch ohne Schlüssel, Verschlüsseln, Zurückdrehen."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    alt = executor.loader.project_state([VORHER]).apps.get_model("common", "SiteSettings")
    alt.objects.create(pk=1, email_host="smtp.example.org", email_host_password=SMTP, nebius_api_key=NEBIUS)

    try:
        executor = MigrationExecutor(connection)
        with override_settings(ENCRYPTION_MASTER_KEY=""), pytest.raises(RuntimeError):
            executor.migrate([NACHHER])
        assert alt.objects.values_list("email_host_password", "nebius_api_key").get() == (SMTP, NEBIUS)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        roh = _roh()
        assert not _enthaelt_klartext(roh)
        assert SiteSettings.objects.get(pk=1).get_email_host_password() == SMTP

        executor = MigrationExecutor(connection)
        executor.migrate([VORHER])
        assert alt.objects.values_list("email_host_password", "nebius_api_key").get() == (SMTP, NEBIUS)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


# =============================================================================
# Admin: nichts ausgeben, leer behält, neu verschlüsselt
# =============================================================================


def _formulardaten(einstellungen: SiteSettings, **geheimnisse: str) -> dict[str, Any]:
    daten = {name: wert for name, wert in model_to_dict(einstellungen).items() if wert is not None}
    return {**daten, "email_host_password": "", "nebius_api_key": "", **geheimnisse}


@pytest.mark.django_db
class TestAdmin:
    def test_seite_gibt_weder_klartext_noch_geheimtext_aus(self, admin_client: Client) -> None:
        einstellungen = _mit_geheimnissen()
        inhalt = admin_client.get(f"/admin/common/sitesettings/{einstellungen.pk}/change/").content.decode()
        assert SMTP not in inhalt and NEBIUS not in inhalt
        for geheimtext in (einstellungen.email_host_password_encrypted, einstellungen.nebius_api_key_encrypted):
            assert geheimtext is not None
            assert base64.b64encode(bytes(geheimtext)).decode()[:24] not in inhalt
        assert "Passwort ist gesetzt" in inhalt

    def test_speichern_ueber_den_admin_verschluesselt(self, admin_client: Client) -> None:
        einstellungen = _mit_geheimnissen()
        daten = _formulardaten(einstellungen, email_host_password="neues-smtp-testpasswort")
        antwort = admin_client.post(f"/admin/common/sitesettings/{einstellungen.pk}/change/", daten)
        assert antwort.status_code == 302
        gespeichert = SiteSettings.objects.get(pk=1)
        assert gespeichert.get_email_host_password() == "neues-smtp-testpasswort"
        assert gespeichert.get_stored_nebius_api_key() == NEBIUS  # leer gelassen: bleibt
        assert gespeichert.email_host_password_encrypted is not None
        assert b"neues-smtp-testpasswort" not in bytes(gespeichert.email_host_password_encrypted)
        assert gespeichert.email_host_password_legacy == ""

    def test_klartext_einer_aelteren_version_wird_beim_speichern_verschluesselt(self) -> None:
        _altbestand()
        einstellungen = SiteSettings.objects.get(pk=1)
        formular = cast(Any, SiteSettingsAdminForm)(data=_formulardaten(einstellungen), instance=einstellungen)
        assert formular.is_valid(), formular.errors
        formular.save()
        roh = _roh()
        assert roh["email_host_password_legacy"] == "" and roh["nebius_api_key_legacy"] == ""
        assert not _enthaelt_klartext(roh)
        gespeichert = SiteSettings.objects.get(pk=1)
        assert gespeichert.get_email_host_password() == SMTP
        assert gespeichert.get_stored_nebius_api_key() == NEBIUS

    def test_ohne_hauptschluessel_formularfehler_ohne_wert(self) -> None:
        einstellungen = _mit_geheimnissen()
        daten = _formulardaten(einstellungen, nebius_api_key="nicht-speicherbar-testwert")
        with override_settings(ENCRYPTION_MASTER_KEY=""):
            formular = cast(Any, SiteSettingsAdminForm)(data=daten, instance=einstellungen)
            assert not formular.is_valid()
        assert "ENCRYPTION_MASTER_KEY" in str(formular.non_field_errors())
        assert "nicht-speicherbar-testwert" not in str(formular)
        assert SiteSettings.objects.get(pk=1).get_stored_nebius_api_key() == NEBIUS
