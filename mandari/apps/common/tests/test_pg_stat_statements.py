# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statistik je SQL-Abfrage mit ``pg_stat_statements`` (Issue #1024).

- Compose und Helm laden die Bibliothek beim Start, abschaltbar (leer); die Roh-Manifeste folgen dem Chart.
- Die Migration ``common.0012`` legt die Erweiterung an, wo das geht, und scheitert nie: ohne PostgreSQL, ohne
  Erweiterung auf dem Server oder ohne Rechte bleibt es bei einer Warnung, und die Transaktion der Migration bleibt
  benutzbar.

Die Fälle mit echtem PostgreSQL laufen in der CI (lokal meist SQLite); die übrigen bilden die Verbindung nach.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.db import DatabaseError, connection

MIGRATION = importlib.import_module("apps.common.migrations.0012_pg_stat_statements")
REPO = Path(__file__).resolve().parents[4]


# --- Laden der Bibliothek ----------------------------------------------------------------------------------


def test_compose_laedt_die_bibliothek_abschaltbar() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    befehl = compose["services"]["postgres"]["command"]
    # "-" statt ":-": Ein leerer Wert in der .env schaltet ab, statt wieder die Vorgabe zu nehmen
    eintrag = "shared_preload_libraries=${POSTGRES_SHARED_PRELOAD_LIBRARIES-pg_stat_statements}"
    assert eintrag in befehl
    assert befehl[befehl.index(eintrag) - 1] == "-c"


def test_helm_laedt_die_bibliothek_abschaltbar() -> None:
    chart = REPO / "deploy" / "kubernetes" / "helm" / "mandari"
    werte = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    assert werte["postgres"]["sharedPreloadLibraries"] == "pg_stat_statements"
    vorlage = (chart / "templates" / "postgres.yaml").read_text(encoding="utf-8")
    assert "{{- with .Values.postgres.sharedPreloadLibraries }}" in vorlage
    manifest = (REPO / "deploy" / "kubernetes" / "manifests" / "mandari.yaml").read_text(encoding="utf-8")
    assert 'args: ["-c", "shared_preload_libraries=pg_stat_statements"]' in manifest


# --- Migration ohne PostgreSQL bzw. mit nachgebildeter Verbindung ------------------------------------------


class _Cursor:
    def __init__(self, verbindung: _Verbindung) -> None:
        self.verbindung = verbindung

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.verbindung.befehle.append(sql)
        if sql.startswith("CREATE EXTENSION") and self.verbindung.fehler is not None:
            raise self.verbindung.fehler

    def fetchone(self) -> tuple[bool] | None:
        return self.verbindung.verfuegbar


class _Verbindung:
    alias = "default"

    def __init__(self, vendor: str, verfuegbar: tuple[bool] | None, fehler: Exception | None = None) -> None:
        self.vendor = vendor
        self.verfuegbar = verfuegbar
        self.fehler = fehler
        self.befehle: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _SchemaEditor:
    def __init__(self, verbindung: _Verbindung) -> None:
        self.connection = verbindung


def test_ohne_postgres_nichts() -> None:
    verbindung = _Verbindung("sqlite", None)
    MIGRATION.erweiterung_anlegen(None, _SchemaEditor(verbindung))
    assert verbindung.befehle == []


def test_erweiterung_auf_dem_server_nicht_vorhanden(caplog: pytest.LogCaptureFixture) -> None:
    verbindung = _Verbindung("postgresql", None)
    with caplog.at_level(logging.WARNING):
        MIGRATION.erweiterung_anlegen(None, _SchemaEditor(verbindung))
    assert not any(befehl.startswith("CREATE") for befehl in verbindung.befehle)
    assert "nicht verfügbar" in caplog.text


def test_schon_angelegt_nichts_zu_tun() -> None:
    verbindung = _Verbindung("postgresql", (True,))
    MIGRATION.erweiterung_anlegen(None, _SchemaEditor(verbindung))
    assert not any(befehl.startswith("CREATE") for befehl in verbindung.befehle)


@pytest.mark.django_db
def test_ohne_rechte_nur_eine_warnung(caplog: pytest.LogCaptureFixture) -> None:
    fehler = DatabaseError('permission denied to create extension "pg_stat_statements" GEHEIM')
    verbindung = _Verbindung("postgresql", (False,), fehler)
    with caplog.at_level(logging.WARNING):
        MIGRATION.erweiterung_anlegen(None, _SchemaEditor(verbindung))
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in verbindung.befehle
    assert "nicht angelegt (DatabaseError)" in caplog.text
    assert "GEHEIM" not in caplog.text, "nur die Art des Fehlers"


# --- Migration gegen PostgreSQL (CI) -----------------------------------------------------------------------


def _nur_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Nachweis nur gegen PostgreSQL (CI)")


def _angelegt() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'")
        zeile = cursor.fetchone()
    return bool(zeile and zeile[0])


@contextmanager
def _ohne_erweiterung() -> Iterator[None]:
    """Erweiterung in der Testtransaktion entfernen; das Zurückrollen stellt den Stand wieder her."""
    with connection.cursor() as cursor:
        cursor.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
    yield


@pytest.mark.django_db
def test_postgres_legt_die_erweiterung_an() -> None:
    _nur_postgres()
    with _ohne_erweiterung(), connection.schema_editor() as editor:
        assert not _angelegt()
        MIGRATION.erweiterung_anlegen(None, editor)
        assert _angelegt()
        # Ein zweiter Lauf (etwa nach einem Rückfall) ändert nichts
        MIGRATION.erweiterung_anlegen(None, editor)
        assert _angelegt()


@pytest.mark.django_db
def test_postgres_ohne_superuser_warnung_und_transaktion_bleibt_benutzbar(caplog: pytest.LogCaptureFixture) -> None:
    _nur_postgres()
    rolle = f"ohne_rechte_{uuid.uuid4().hex[:12]}"
    with _ohne_erweiterung(), connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {rolle} NOLOGIN")
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {rolle}")
        cursor.execute(f"SET LOCAL ROLE {rolle}")
        try:
            with caplog.at_level(logging.WARNING), connection.schema_editor() as editor:
                MIGRATION.erweiterung_anlegen(None, editor)
            # Die Transaktion der Migration ist nicht abgebrochen
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
        finally:
            cursor.execute("RESET ROLE")
        assert not _angelegt()
    assert "nicht angelegt" in caplog.text


@pytest.mark.django_db
def test_postgres_statistik_lesbar_wenn_geladen() -> None:
    """Mit geladener Bibliothek (docker compose) liefert die Statistik ein Ergebnis, sonst eine klare Fehlermeldung."""
    _nur_postgres()
    with connection.cursor() as cursor:
        cursor.execute("SHOW shared_preload_libraries")
        geladen = "pg_stat_statements" in str(cursor.fetchone()[0])
        if not geladen:
            pytest.skip("Bibliothek in diesem Server nicht geladen (CI-Dienst ohne shared_preload_libraries)")
        cursor.execute("SELECT count(*) FROM pg_stat_statements")
        assert cursor.fetchone()[0] >= 0
