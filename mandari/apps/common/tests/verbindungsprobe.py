# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfhilfe (Issue #1024): Ist die Datenbankverbindung während eines langen externen Aufrufs (KI, Kartenkacheln)
an den Pool zurückgegeben?

Der Ersatz des externen Aufrufs ruft ``probe.aufruf()``. Die Probe schreibt mit, in welcher Reihenfolge
SQL-Abfragen, Freigaben (``release_idle_thread_connections``) und Aufrufe kamen, und ob der Thread während des
Aufrufs eine Verbindung hielt. ``pruefen()`` verlangt:

- vor jedem Aufruf unmittelbar eine Freigabe, keine Abfrage dazwischen (sonst holte sie die Verbindung zurück);
- während des Aufrufs keine Verbindung im Thread (``connection.connection is None``) und mit psycopg-Pool nicht
  mehr verliehene Verbindungen als vor der Anfrage.

Der zweite Teil braucht einen Test ohne umschließende Transaktion (``django_db(transaction=True)``), sonst bleibt
die Verbindung bewusst bestehen. Und er gilt nur, wo Django die Verbindung wirklich schließt: SQLite im Arbeitsspeicher
(lokale Testläufe) schließt Django nie, dort prüft die Probe nur die Reihenfolge. In der CI (PostgreSQL mit Pool)
prüft sie beides.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from django.db import connection, connections

from apps.common import db_connections


def _pool() -> Any:
    if connection.vendor != "postgresql" or not connection.settings_dict.get("OPTIONS", {}).get("pool"):
        return None
    return cast(Any, connections["default"]).pool  # Stubs kennen den Pool nicht


def _ausgeliehen(pool: Any) -> int:
    stats = pool.get_stats()
    return int(stats.get("pool_size", 0)) - int(stats.get("pool_available", 0))


def verbindung_schliessbar() -> bool:
    """Schließt Django die Verbindung? Nicht bei SQLite im Arbeitsspeicher (sonst wäre die Datenbank weg)."""
    return not (connection.vendor == "sqlite" and cast(Any, connection).is_in_memory_db())


class Verbindungsprobe:
    def __init__(self) -> None:
        self.ereignisse: list[str] = []
        #: je Aufruf: hielt der Thread eine Verbindung?
        self.belegt: list[bool] = []
        #: je Aufruf: verliehene Verbindungen des Pools (nur mit Pool)
        self.ausgeliehen: list[int] = []
        self.vorher: int | None = None

    def _sql(self, execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        self.ereignisse.append("sql")
        return execute(sql, params, many, context)

    def aufruf(self) -> None:
        """Aus dem Ersatz des externen Aufrufs heraus aufrufen."""
        self.ereignisse.append("aufruf")
        self.belegt.append(connection.connection is not None)
        pool = _pool()
        if pool is not None:
            self.ausgeliehen.append(_ausgeliehen(pool))

    @contextmanager
    def mitschreiben(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Verbindungsprobe]:
        # Ausgangslage: Der Testthread selbst hält nichts (außer in einer Testtransaktion), der Pool zählt nur, was
        # andere halten
        db_connections.release_idle_thread_connections()
        pool = _pool()
        echt = db_connections.close_thread_connections

        def freigeben(ausser: frozenset[str] = frozenset()) -> None:
            self.ereignisse.append("frei")
            echt(ausser)

        # release_idle_thread_connections ruft close_thread_connections über das Modul auf
        monkeypatch.setattr(db_connections, "close_thread_connections", freigeben)
        self.vorher = _ausgeliehen(pool) if pool is not None else None
        with connection.execute_wrapper(self._sql):
            yield self

    def pruefen(self, *, aufrufe: int) -> None:
        assert self.ereignisse.count("aufruf") == aufrufe, self.ereignisse
        for index, ereignis in enumerate(self.ereignisse):
            if ereignis == "aufruf":
                assert index > 0 and self.ereignisse[index - 1] == "frei", (
                    f"Vor Aufruf Nr. {self.ereignisse[: index + 1].count('aufruf')} keine Freigabe bzw. eine "
                    f"Abfrage danach: {self.ereignisse}"
                )
        if verbindung_schliessbar():
            assert not any(self.belegt), f"Verbindung während des Aufrufs belegt: {self.belegt}"
        if self.vorher is not None:
            assert all(zahl <= self.vorher for zahl in self.ausgeliehen), (
                f"Während des Aufrufs mehr Pool-Verbindungen verliehen ({self.ausgeliehen}) als vorher ({self.vorher})"
            )
