# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures für Tests der Ereignistechnik; Hilfsfunktionen in ``hilfen.py``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.db import connection
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from apps.events import registry
from apps.events.tests.hilfen import Sicht, direktverbindung, nur_postgres
from apps.events.tests.prozess import Probe


@pytest.fixture(autouse=True)
def direktverbindung_zur_testdatenbank(settings: Any) -> None:
    """``EVENTS_DB_DIRECT_URL`` nennt die Datenbank der Installation; Tests laufen in der Testdatenbank.

    In der CI läuft ``apps/events`` zusätzlich hinter PgBouncer (``DATABASE_URL`` über den Pooler,
    ``EVENTS_DB_DIRECT_URL`` direkt); Direktverbindungen der Tests gehen dann ebenfalls vorbei.
    """
    url = getattr(settings, "EVENTS_DB_DIRECT_URL", "")
    if url and connection.vendor == "postgresql":
        teile = conninfo_to_dict(url)
        teile["dbname"] = connection.settings_dict["NAME"]
        settings.EVENTS_DB_DIRECT_URL = make_conninfo("", **teile)


@pytest.fixture
def pg_verbindungen() -> Iterator[Callable[..., psycopg.Connection[Any]]]:
    """Fabrik für Direktverbindungen; schließt alle am Ende des Tests."""
    nur_postgres()
    offen: list[psycopg.Connection[Any]] = []

    def _neu(*, autocommit: bool = True) -> psycopg.Connection[Any]:
        verbindung = direktverbindung(autocommit=autocommit)
        offen.append(verbindung)
        return verbindung

    yield _neu
    for verbindung in offen:
        verbindung.close()


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> dict[str, registry.Subscriber]:
    """Eigenes Register für den Test; registrierte Abonnements anderer Tests stören nicht."""
    eintraege: dict[str, registry.Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    return eintraege


@pytest.fixture
def sicht() -> Iterator[Sicht]:
    """Behelfstabelle als Datenbank-Sicht; braucht ``django_db`` am Test."""
    tabelle = Sicht()
    tabelle.anlegen()
    yield tabelle
    tabelle.entfernen()


@pytest.fixture
def probe(tmp_path: Path) -> Iterator[Probe]:
    """Tabellen und Worker-Prozesse für Absturz- und Lasttests; braucht ``django_db(transaction=True)``."""
    nur_postgres()
    umgebung = Probe(tmp_path)
    umgebung.anlegen()
    yield umgebung
    umgebung.aufraeumen()
