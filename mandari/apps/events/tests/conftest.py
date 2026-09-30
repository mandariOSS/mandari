# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures für Tests der Ereignistechnik; Hilfsfunktionen in ``hilfen.py``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from apps.events import registry
from apps.events.tests.hilfen import Sicht, direktverbindung, nur_postgres


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
