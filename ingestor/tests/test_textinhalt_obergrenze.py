"""
Obergrenze des erkannten Texts je Datei im Ingestor.

``update_file_text`` ist der eine Weg, auf dem der Ingestor erkannten Text speichert: höchstens ``MAX_TEXT_CHARS``
Zeichen (gemeinsamer Wert mit der Anwendung, ``mandari_oparl``), der Schnitt steht in ``text_extraction_error``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import mandari_oparl

from src.storage.database import DatabaseStorage

GRENZE = 5_000_000


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


def _storage(calls: list[Any]) -> DatabaseStorage:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")
    storage.get_session = lambda: _Session(calls)  # type: ignore[method-assign]
    return storage


def test_gemeinsame_obergrenze() -> None:
    assert getattr(mandari_oparl, "MAX_TEXT_CHARS", None) == GRENZE


async def test_speichern_kuerzt_an_der_obergrenze_und_vermerkt_es() -> None:
    calls: list[Any] = []

    await _storage(calls).update_file_text(uuid4(), text_content="x" * (GRENZE + 10), method="pypdf")

    assert len(calls[0]["text_content"]) == GRENZE
    assert calls[0]["text_extraction_error"] == "Text nach 5.000.000 Zeichen gekürzt (Obergrenze je Datei)"


async def test_vermerk_ergaenzt_einen_vorhandenen_hinweis() -> None:
    calls: list[Any] = []

    await _storage(calls).update_file_text(
        uuid4(), text_content="\x00" + "y" * (GRENZE + 1), method="tesseract", error="Seite 3 übersprungen"
    )

    assert calls[0]["text_content"] == "y" * GRENZE
    assert calls[0]["text_extraction_error"].startswith("Seite 3 übersprungen; Text nach 5.000.000 Zeichen gekürzt")


async def test_kuerzere_texte_und_der_fehler_bleiben_unberuehrt() -> None:
    calls: list[Any] = []

    await _storage(calls).update_file_text(uuid4(), text_content="z" * GRENZE, method="pypdf")

    assert calls[0]["text_content"] == "z" * GRENZE and "text_extraction_error" not in calls[0]
