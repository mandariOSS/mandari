"""
Führung des Sync-Daemons (Issue #55): ohne PostgreSQL sofort erteilt; mit PostgreSQL wartet
eine zweite Instanz, bis die Advisory-Sperre frei ist (Verbindung wird nachgestellt).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.scheduler import singleton as sg


def test_ohne_postgres_sofort_fuehrung() -> None:
    assert sg.ist_postgres("sqlite+aiosqlite:///x.db") is False
    assert sg.ist_postgres("postgresql+asyncpg://u:p@h/db") is True


@pytest.mark.asyncio
async def test_sqlite_erteilt_ohne_datenbank() -> None:
    f = sg.Fuehrung("sqlite+aiosqlite:///x.db", wartezeit=0)
    gemeldet: list[str] = []
    await f.warten(gemeldet.append)
    assert f.erhalten is True and gemeldet == []
    await f.freigeben()
    assert f.erhalten is False


class _Ergebnis:
    def __init__(self, wert: Any) -> None:
        self._wert = wert

    def scalar(self) -> Any:
        return self._wert


class _Verbindung:
    """Nachgestellte Verbindung: die ersten ``frei_ab`` Versuche scheitern."""

    def __init__(self, frei_ab: int) -> None:
        self.frei_ab, self.versuche, self.befehle = frei_ab, 0, []
        self.geschlossen = False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Ergebnis:
        sql = str(stmt)
        self.befehle.append(sql)
        if "pg_try_advisory_lock" in sql:
            self.versuche += 1
            return _Ergebnis(self.versuche > self.frei_ab)
        if "pg_stat_activity" in sql:
            return _Ergebnis("ingestor-daemon knoten-a:1")
        return _Ergebnis(None)

    async def commit(self) -> None:
        pass

    async def close(self) -> None:
        self.geschlossen = True


class _Engine:
    def __init__(self, conn: _Verbindung) -> None:
        self.conn, self.entsorgt = conn, False

    async def connect(self) -> _Verbindung:
        return self.conn

    async def dispose(self) -> None:
        self.entsorgt = True


@pytest.mark.asyncio
async def test_zweite_instanz_wartet_und_uebernimmt(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Verbindung(frei_ab=2)
    monkeypatch.setattr(sg, "create_async_engine", lambda *a, **k: _Engine(conn))
    f = sg.Fuehrung("postgresql+asyncpg://u:p@h/db", wartezeit=0)
    gemeldet: list[str] = []

    await f.warten(gemeldet.append)

    assert f.erhalten is True and conn.versuche == 3
    assert gemeldet == ["ingestor-daemon knoten-a:1"], "genau eine Meldung, wer die Sperre hält"
    assert any("application_name" in b for b in conn.befehle), "Inhaber trägt sich für die Diagnose ein"

    await f.freigeben()
    assert any("pg_advisory_unlock" in b for b in conn.befehle)
    assert conn.geschlossen and f.erhalten is False
