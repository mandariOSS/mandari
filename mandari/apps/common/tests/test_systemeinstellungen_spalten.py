# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migration common/0014: Altspalten der Systemeinstellungen löschen (Issue #604).

Läuft auf PostgreSQL und SQLite. Die Datenbanken führen die Spalten unterschiedlich (siehe Migration), deshalb stellt
jeder Test seinen Ausgangszustand selbst her. Roh-SQL nennt Spalten ohne Anführungszeichen: SQLite läse einen fehlenden
Namen in Anführungszeichen sonst als Text, und der Test bliebe grün.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.common.models import SiteSettings

VORHER = ("common", "0013_ki_anbieter_auswahl_als_aufruf")
NACHHER = ("common", "0014_systemeinstellungen_spalten_entfernen")
TABELLE = "common_sitesettings"
ALTSPALTEN = {
    "site_name": "varchar(100) NOT NULL DEFAULT 'Mandari'",
    "site_description": "text NOT NULL DEFAULT 'Kommunalpolitische Transparenz'",
}

pytestmark = [pytest.mark.migrationen, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _neuester_stand_danach() -> Iterator[None]:
    cache.delete(SiteSettings.CACHE_KEY)
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    cache.delete(SiteSettings.CACHE_KEY)


def _migrieren(ziel: tuple[str, str]) -> None:
    MigrationExecutor(connection).migrate([ziel])


def _modell_ohne_0014() -> type[SiteSettings]:
    """Das Modell, wie es ein Image ohne diese Migration kennt."""
    return MigrationExecutor(connection).loader.project_state([VORHER]).apps.get_model("common", "SiteSettings")


def _spalten() -> set[str]:
    with connection.cursor() as cursor:
        return {spalte.name for spalte in connection.introspection.get_table_description(cursor, TABELLE)}


def _altspalten_herstellen(vorhanden: set[str]) -> None:
    da = _spalten()
    with connection.cursor() as cursor:
        for spalte, definition in ALTSPALTEN.items():
            if spalte in vorhanden and spalte not in da:
                cursor.execute(f"ALTER TABLE {TABELLE} ADD COLUMN {spalte} {definition}")
            elif spalte not in vorhanden and spalte in da:
                cursor.execute(f"ALTER TABLE {TABELLE} DROP COLUMN {spalte}")


def _bestand_anlegen() -> None:
    _modell_ohne_0014().objects.update_or_create(pk=1, defaults={"maintenance_message": "Bleibt"})
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {TABELLE} SET email_backend = 'bleibt' WHERE id = 1")


def _bestand() -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT maintenance_message, email_backend FROM {TABELLE} WHERE id = 1")
        zeile: tuple[str, str] = cursor.fetchone()
    return zeile


@pytest.mark.parametrize(
    "vorhanden",
    [set(ALTSPALTEN), set(), {"site_name"}, {"site_description"}],
    ids=["beide-da", "beide-fehlen", "nur-name", "nur-beschreibung"],
)
def test_vorwaerts_loescht_was_da_ist_und_laesst_den_rest(vorhanden: set[str]) -> None:
    _migrieren(VORHER)
    _altspalten_herstellen(vorhanden)
    _bestand_anlegen()

    _migrieren(NACHHER)

    assert not _spalten() & set(ALTSPALTEN)
    assert _bestand() == ("Bleibt", "bleibt")


def test_rueckweg_laesst_email_backend_und_laeuft_wieder_vor() -> None:
    _migrieren(VORHER)
    _bestand_anlegen()
    _migrieren(NACHHER)

    _migrieren(VORHER)

    assert _bestand() == ("Bleibt", "bleibt")
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT site_name, site_description FROM {TABELLE} WHERE id = 1")
            assert cursor.fetchone() == ("Mandari", "Kommunalpolitische Transparenz")
    else:
        assert not _spalten() & set(ALTSPALTEN), "SQLite führt sie seit common/0008 nicht mehr"

    _migrieren(NACHHER)
    assert not _spalten() & set(ALTSPALTEN)
    assert _bestand() == ("Bleibt", "bleibt")


def test_stand_ohne_diese_migration_laeuft_auf_der_geleerten_tabelle() -> None:
    """Rückfall ohne Rückbau: Das ältere Modell liest und schreibt, obwohl die Spalten fehlen."""
    _migrieren(NACHHER)
    alt = _modell_ohne_0014()
    alt.objects.all().delete()

    zeile = alt.objects.create(pk=1, maintenance_message="Neu angelegt")
    zeile.maintenance_message = "Geändert"
    zeile.save()

    assert alt.objects.get(pk=1).maintenance_message == "Geändert"


def test_modellstand_bleibt_gleich() -> None:
    """Die Felder kommen nur für die Dauer der Migration zurück."""
    stand = MigrationExecutor(connection).loader.project_state
    felder_vorher = stand([VORHER]).models["common", "sitesettings"].fields
    felder_nachher = stand([NACHHER]).models["common", "sitesettings"].fields
    assert list(felder_vorher) == list(felder_nachher)
    assert not set(felder_nachher) & set(ALTSPALTEN)
