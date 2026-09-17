"""
Führung des Sync-Daemons über eine PostgreSQL-Advisory-Sperre (Issue #55).

Laufen zwei Ingestor-Instanzen gegen dieselbe Datenbank (doppeltes ``worker``-Profil,
Überlappung beim Deploy), würden sie dieselben Quellen doppelt synchronisieren. Die
Sperre ``pg_try_advisory_lock`` ist an eine Datenbankverbindung gebunden: Sie fällt
automatisch, wenn der Prozess endet oder die Verbindung abbricht – kein veralteter
Schlüssel, kein Aufräumen. Die zweite Instanz wartet und übernimmt, sobald die erste weg ist.

Für andere Datenbanken (SQLite in Tests) gibt es keine Advisory-Sperren; dort ist die
Führung sofort erteilt.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

SPERRNAME = "mandari-ingestor-daemon"
INHABER_TABELLE = "ingestor_daemon_inhaber"


def ist_postgres(database_url: str) -> bool:
    return database_url.startswith(("postgresql", "postgres"))


class Fuehrung:
    """Hält die Advisory-Sperre auf einer eigenen Verbindung, solange der Daemon läuft."""

    def __init__(self, database_url: str, *, name: str = SPERRNAME, wartezeit: float = 30.0) -> None:
        self.database_url = database_url
        self.name = name
        self.wartezeit = wartezeit
        self.inhaber = f"{socket.gethostname()}:{os.getpid()}"
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self.erhalten = False

    async def versuchen(self) -> bool:
        """Einmal versuchen, die Sperre zu bekommen (ohne Warten)."""
        if not ist_postgres(self.database_url):
            self.erhalten = True
            return True
        if self._engine is None:
            self._engine = create_async_engine(self.database_url, pool_size=1, max_overflow=0)
        if self._conn is None:
            self._conn = await self._engine.connect()
        ergebnis = await self._conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": self.name})
        self.erhalten = bool(ergebnis.scalar())
        if self.erhalten:
            # Wer hält die Sperre? Nur Diagnose, kein Schutzmechanismus.
            await self._conn.execute(
                text("SELECT set_config('application_name', :wer, false)"), {"wer": f"ingestor-daemon {self.inhaber}"}
            )
            await self._conn.commit()
        return self.erhalten

    async def warten(self, melden: Callable[[str], None] | None = None) -> None:
        """Blockiert, bis die Sperre gehalten wird; ``melden`` wird je Wartezyklus aufgerufen."""
        gemeldet = False
        while not await self.versuchen():
            if melden and not gemeldet:
                melden(await self.aktueller_inhaber())
                gemeldet = True
            await asyncio.sleep(self.wartezeit)

    async def aktueller_inhaber(self) -> str:
        if self._conn is None:
            return "unbekannt"
        try:
            # Die haltende Instanz trägt sich beim Erwerb in application_name ein (nur Diagnose)
            ergebnis = await self._conn.execute(
                text(
                    "SELECT application_name FROM pg_stat_activity "
                    "WHERE application_name LIKE 'ingestor-daemon %' AND pid <> pg_backend_pid() LIMIT 1"
                )
            )
            return str(ergebnis.scalar() or "unbekannt")
        except Exception:
            return "unbekannt"

    async def freigeben(self) -> None:
        if self._conn is not None:
            if self.erhalten and ist_postgres(self.database_url):
                await self._conn.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": self.name})
            await self._conn.close()
            self._conn = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self.erhalten = False
