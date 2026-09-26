"""update_file_text entfernt Null-Bytes, die PostgreSQL in Textfeldern ablehnt."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from src import observability
from src.storage.database import DatabaseStorage


class _Session:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any) -> None:
        self.calls.append(stmt.compile().params)

    async def commit(self) -> None:
        return None


async def test_null_bytes_werden_vor_dem_speichern_entfernt() -> None:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")
    calls: list[Any] = []
    storage.get_session = lambda: _Session(calls)  # type: ignore[method-assign]

    await storage.update_file_text(uuid4(), text_content="Rat\x00sbeschluss\x00", error="kaputt\x00", status="failed")

    werte = calls[0]
    assert "\x00" not in werte["text_content"] and werte["text_content"] == "Ratsbeschluss"
    assert werte["text_extraction_error"] == "kaputt"


async def test_ohne_text_bleibt_das_feld_unberuehrt() -> None:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")
    calls: list[Any] = []
    storage.get_session = lambda: _Session(calls)  # type: ignore[method-assign]

    await storage.update_file_text(uuid4(), status="completed", method="none")

    assert "text_content" not in calls[0]


def test_pypdf_warnungen_sind_gedaempft(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(observability, "_logging_ready", False)
    observability.setup_logging(log_level="INFO")
    assert logging.getLogger("pypdf").getEffectiveLevel() >= logging.ERROR
