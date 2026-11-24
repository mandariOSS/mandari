# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migration common/0016: Tabellen der früheren Protokollierung (``apps.minutes``) nach dem Deploy löschen.

Geprüft wird der Schritt selbst gegen nachgebaute Tabellen und Einträge in ``django_migrations`` (frische
Datenbanken haben die Tabellen nie bekommen) und dass ``safemigrate`` ihn vor dem Umschalten auslässt.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.utils import timezone
from django_safemigrate import When

MIGRATION = importlib.import_module("apps.common.migrations.0016_protokollierung_tabellen_loeschen")

pytestmark = pytest.mark.django_db


class _Schemaeditor:
    """Das, was der Schritt vom Schema-Editor braucht; ohne den echten, der unter SQLite keine Transaktion mag."""

    connection = connection

    def execute(self, sql: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(sql)

    def quote_name(self, name: str) -> str:
        return str(connection.ops.quote_name(name))


def _tabellen() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def _eintraege(app: str) -> list[Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM django_migrations WHERE app = %s", [app])
        return [zeile[0] for zeile in cursor.fetchall()]


def test_loescht_vorhandene_tabellen_und_die_eintraege_des_moduls() -> None:
    with connection.cursor() as cursor:
        for tabelle in ("minutes_recordings", "minutes_gpu_nodes"):
            cursor.execute(f"CREATE TABLE {connection.ops.quote_name(tabelle)} (id integer PRIMARY KEY)")
        for name in ("0001_initial", "0002_rechenknoten"):
            cursor.execute(
                "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)",
                ["minutes", name, timezone.now()],
            )
    common_vorher = _eintraege("common")

    MIGRATION.tabellen_loeschen(django_apps, _Schemaeditor())

    assert not {"minutes_recordings", "minutes_gpu_nodes"} & _tabellen()
    assert _eintraege("minutes") == []
    assert _eintraege("common") == common_vorher, "Einträge anderer Module bleiben"


def test_ohne_tabellen_ist_der_schritt_wirkungslos() -> None:
    vorher = _tabellen()

    MIGRATION.tabellen_loeschen(django_apps, _Schemaeditor())
    MIGRATION.tabellen_loeschen(django_apps, _Schemaeditor())

    assert _tabellen() == vorher
    assert not any(tabelle.startswith("minutes_") for tabelle in vorher)


def test_laeuft_erst_nach_dem_deploy() -> None:
    """Das laufende Image vor dem Rückbau liest die Tabellen noch; ``safemigrate`` lässt den Schritt deshalb aus."""
    assert MIGRATION.Migration.safe.when is When.AFTER_DEPLOY
    assert set(MIGRATION.TABELLEN) == {
        "minutes_protocol_drafts",
        "minutes_transcript_segments",
        "minutes_voice_profiles",
        "minutes_speaker_consents",
        "minutes_transcription_jobs",
        "minutes_recording_segments",
        "minutes_recordings",
        "minutes_protocol_styles",
        "minutes_gpu_nodes",
        "minutes_compute_settings",
    }
