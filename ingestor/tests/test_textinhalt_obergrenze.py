"""
Obergrenze des erkannten Texts je Datei im OCR-Worker des Ingestors.

Die Bibliothek kürzt den Text an ``MAX_TEXT_CHARS`` und vermerkt den Schnitt; der Worker gibt den Vermerk wie die
Aufträge der Anwendung an ``text_extraction_error`` weiter. ``update_file_text`` kürzt auch selbst, wenn ein Text
an der Bibliothek vorbei kommt.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mandari_dokumente import texterkennung as bibliothek

from src.config import settings
from src.extraction.extractor import TextExtractor
from src.storage.database import DatabaseStorage

INHALT = b"%PDF-1.4\n" + b"Ratsbeschluss " * 200


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _Session:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self.calls.append(stmt.compile().params)
        return _Result()

    async def commit(self) -> None:
        return None


def _storage(calls: list[Any]) -> DatabaseStorage:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")
    storage.get_session = lambda: _Session(calls)  # type: ignore[method-assign]
    return storage


async def test_speichern_kuerzt_an_der_obergrenze_und_vermerkt_es() -> None:
    grenze = getattr(bibliothek, "MAX_TEXT_CHARS", 5_000_000)
    calls: list[Any] = []

    await _storage(calls).update_file_text(uuid4(), text_content="x" * (grenze + 10), method="pypdf")

    assert len(calls[0]["text_content"]) == grenze
    assert "gekürzt" in (calls[0].get("text_extraction_error") or "")


async def test_speichern_laesst_kuerzere_texte_und_den_fehler_unberuehrt() -> None:
    calls: list[Any] = []

    await _storage(calls).update_file_text(uuid4(), text_content="Ratsbeschluss", method="pypdf")

    assert calls[0]["text_content"] == "Ratsbeschluss" and "text_extraction_error" not in calls[0]


def test_erkennung_im_worker_liefert_den_vermerk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bibliothek, "MAX_TEXT_CHARS", 1_000, raising=False)
    datei = tmp_path / "anlage.txt"
    datei.write_text("Ratsbeschluss " * 200, encoding="utf-8")

    ergebnis = TextExtractor._extract_text(datei, "text/plain", "anlage.txt")

    assert len(ergebnis[0]) == 1_000
    hinweise = ergebnis[3] if len(ergebnis) > 3 else ()
    assert any("gekürzt" in hinweis for hinweis in hinweise)


class _Storage:
    def __init__(self) -> None:
        self.texte: list[dict[str, Any]] = []

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def update_file_text(self, **werte: Any) -> None:
        self.texte.append(werte)

    async def body_stores_files(self, body_id: Any) -> bool:
        return False


async def test_vermerk_der_erkennung_kommt_in_der_datenbank_an(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oparl_files_root", "")
    echter_client = httpx.AsyncClient

    def client_mit_transport(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return echter_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=INHALT)), **kwargs
        )

    monkeypatch.setattr("src.extraction.extractor.httpx.AsyncClient", client_mit_transport)
    hinweis = "Text nach 1.000 Zeichen gekürzt (Obergrenze je Datei)"
    monkeypatch.setattr(
        TextExtractor, "_extract_text", staticmethod(lambda source, mime, name: ("Beschluss", 1, "pypdf", (hinweis,)))
    )
    storage = _Storage()
    datei = SimpleNamespace(
        id=uuid.uuid4(),
        body_id=uuid.uuid4(),
        download_url="https://ris.example.org/files/a.pdf",
        access_url=None,
        mime_type="application/pdf",
        file_name="a.pdf",
    )

    assert await TextExtractor(storage)._process_file(datei)

    gespeichert = storage.texte[-1]
    assert gespeichert["status"] == "completed" and gespeichert["text_content"] == "Beschluss"
    assert gespeichert.get("error") == hinweis
