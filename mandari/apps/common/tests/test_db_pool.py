# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verbindungspool der Datenbank (Issue #257).

Unter ASGI hielt jeder Anfrage-Thread seine Verbindung noch ``CONN_MAX_AGE``
Sekunden. Bei einer Anfragespitze wuchs die Zahl der Verbindungen schneller, als
sie zurückgegeben wurden — bis ``max_connections`` erreicht war und *jeder*
Dienst an derselben Datenbank „sorry, too many clients already" bekam.

Geprüft wird die Konfiguration, nicht das Verhalten von psycopg: dass der Pool
für PostgreSQL greift, dass er bei SQLite ausbleibt, und dass die von Django
geforderte Bedingung ``CONN_MAX_AGE = 0`` eingehalten ist.
"""

from __future__ import annotations

import importlib
import os
from typing import Any
from unittest import mock

import pytest


def lade_einstellungen(umgebung: dict[str, str]) -> Any:
    """settings-Modul mit einer bestimmten Umgebung neu auswerten."""
    with mock.patch.dict(os.environ, umgebung, clear=False):
        modul = importlib.import_module("mandari.settings")
        return importlib.reload(modul)


@pytest.fixture(autouse=True)
def _einstellungen_wiederherstellen() -> Any:
    """Nach jedem Test den ursprünglichen Zustand zurückholen."""
    yield
    importlib.reload(importlib.import_module("mandari.settings"))


class TestPoolFuerPostgres:
    def test_pool_ist_gesetzt_und_conn_max_age_null(self) -> None:
        s = lade_einstellungen(
            {"DATABASE_URL": "postgres://u:p@localhost:5432/db", "DB_POOL": "true", "DEBUG": "false"}
        )
        db = s.DATABASES["default"]
        assert "pool" in db["OPTIONS"], "Ohne Pool wächst die Verbindungszahl mit der Last"
        assert db["CONN_MAX_AGE"] == 0, "Django verlangt CONN_MAX_AGE=0, sonst ImproperlyConfigured"

    def test_vorgaben_passen_zum_thread_pool(self) -> None:
        s = lade_einstellungen({"DATABASE_URL": "postgres://u:p@localhost:5432/db", "DEBUG": "false"})
        pool = s.DATABASES["default"]["OPTIONS"]["pool"]
        assert pool["min_size"] == 2
        assert pool["max_size"] == 10, "Mehr als der ASGI-Thread-Pool kann ein Prozess nicht nutzen"
        assert pool["timeout"] > 0, "Bei erschöpftem Pool warten, nicht sofort scheitern"

    def test_grenzen_sind_einstellbar(self) -> None:
        s = lade_einstellungen(
            {
                "DATABASE_URL": "postgres://u:p@localhost:5432/db",
                "DEBUG": "false",
                "DB_POOL_MIN": "1",
                "DB_POOL_MAX": "4",
                "DB_POOL_TIMEOUT": "3.5",
            }
        )
        pool = s.DATABASES["default"]["OPTIONS"]["pool"]
        assert (pool["min_size"], pool["max_size"], pool["timeout"]) == (1, 4, 3.5)

    def test_abschaltbar(self) -> None:
        """Wer den Pool nicht will, muss ihn ausschalten können."""
        s = lade_einstellungen(
            {"DATABASE_URL": "postgres://u:p@localhost:5432/db", "DB_POOL": "false", "DEBUG": "false"}
        )
        db = s.DATABASES["default"]
        assert "pool" not in db.get("OPTIONS", {})
        assert db["CONN_MAX_AGE"] > 0, "Ohne Pool bleibt die bisherige Wiederverwendung"


class TestSqliteBleibtUnberuehrt:
    def test_kein_pool_bei_sqlite(self) -> None:
        """SQLite kennt weder das Problem noch die Option — Tests und Selbst-Hoster
        mit kleiner Installation dürfen davon nichts merken."""
        s = lade_einstellungen({"DATABASE_URL": "sqlite:///tmp/test.sqlite3", "DB_POOL": "true", "DEBUG": "false"})
        db = s.DATABASES["default"]
        assert "pool" not in db.get("OPTIONS", {})


@pytest.mark.django_db(transaction=True)
class TestGrenzeUnterLast:
    """Nachweis, dass die Obergrenze unter Last tatsächlich hält.

    Läuft nur gegen PostgreSQL — SQLite kennt weder Pool noch
    ``pg_stat_activity``. In der CI ist Postgres vorhanden, lokal meist nicht.
    """

    def test_verbindungen_bleiben_unter_der_obergrenze(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection, connections

        if "postgresql" not in connection.settings_dict["ENGINE"]:
            pytest.skip("Nachweis nur gegen PostgreSQL sinnvoll")

        obergrenze = connection.settings_dict.get("OPTIONS", {}).get("pool", {}).get("max_size")
        if not obergrenze:
            pytest.skip("Pool in dieser Konfiguration nicht aktiv")

        hoechststand = 0
        fehler: list[str] = []

        def abfrage(_: int) -> None:
            nonlocal hoechststand
            try:
                with connections["default"].cursor() as cur:
                    cur.execute("SELECT pg_sleep(0.05)")
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE backend_type = 'client backend' AND datname = current_database()"
                    )
                    hoechststand = max(hoechststand, cur.fetchone()[0])
            except Exception as exc:  # noqa: BLE001 — jeder Fehler ist hier ein Befund
                fehler.append(f"{type(exc).__name__}: {exc}")
            finally:
                connections["default"].close()

        # Deutlich mehr gleichzeitige Anfragen als der Pool Verbindungen hat.
        with ThreadPoolExecutor(max_workers=obergrenze * 4) as pool:
            list(pool.map(abfrage, range(obergrenze * 8)))

        assert not fehler, f"Unter Last sind Fehler aufgetreten: {fehler[:3]}"
        # Grosszuegige Reserve: pytest, andere Tests und Postgres selbst halten
        # ebenfalls Verbindungen. Entscheidend ist, dass die Zahl NICHT mit der
        # Zahl der Anfragen waechst.
        assert hoechststand < obergrenze * 4, (
            f"Hoechststand {hoechststand} bei Obergrenze {obergrenze} — "
            "die Verbindungszahl waechst offenbar weiter mit der Last"
        )
