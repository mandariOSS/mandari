"""
Abgebrochene Textextraktion (Issue #817): Abbruchzähler je Datei, Zurückstellen hängender Dateien, Aufgeben
nach wiederholten Abbrüchen statt Endlosschleife.

Die Speicher-Tests laufen gegen PostgreSQL (``INGESTOR_TEST_DATABASE_URL``, in der CI gesetzt; lokal ohne
Datenbank übersprungen) in einem eigenen Schema aus der Tabellenbeschreibung des Ingestors.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.extraction.extractor import DownloadedFile, TextExtractor
from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlFile
from src.sync.processor import OParlProcessor

# --- Extraktor (ohne Datenbank) ----------------------------------------------------------------------------


class _Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class _Speicher:
    def __init__(self, dateien: list[Any] | None = None, versuche: int | None = 1) -> None:
        self.dateien = dateien or []
        self.versuche = versuche
        self.ablauf: list[str] = []
        self.aufloesungen: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def release_stale_extractions(self, **werte: Any) -> tuple[int, int]:
        self.aufloesungen.append(werte)
        return 2, 1

    async def get_pending_files(self, **_werte: Any) -> list[Any]:
        dateien, self.dateien = self.dateien, []
        return dateien

    async def mark_extraction_started(self, file_id: Any) -> int | None:
        self.ablauf.append(f"start {file_id}")
        return self.versuche

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def update_file_text(self, **werte: Any) -> None:
        self.updates.append(werte)


def _datei(name: str, versuche: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=name,
        body_id=uuid.UUID(int=1),
        download_url=f"https://rat.example.de/dokumente/{name}.pdf",
        access_url=None,
        mime_type="application/pdf",
        file_name=f"{name}.pdf",
        text_extraction_attempts=versuche,
    )


def _geladen(tmp_path: Path) -> DownloadedFile:
    pfad = tmp_path / f"{uuid.uuid4().hex}.part"
    pfad.write_bytes(b"%PDF-1.4")
    return DownloadedFile(path=pfad, size=8, sha256="0" * 64, head=b"%PDF-1.4")


async def test_versuch_wird_vor_dem_download_gezaehlt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = _Speicher()
    extractor = TextExtractor(speicher)

    async def download(url: str, *_args: Any) -> DownloadedFile:
        speicher.ablauf.append("download")
        return _geladen(tmp_path)

    monkeypatch.setattr(extractor, "_download_to_file", download)
    monkeypatch.setattr(TextExtractor, "_extract_text", staticmethod(lambda *a: ("Beschluss", 1, "pypdf")))

    assert await extractor._process_file(_datei("a")) is True
    assert speicher.ablauf == ["start a", "download"]


async def test_datei_nicht_mehr_in_bearbeitung_wird_nicht_geladen(monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = _Speicher(versuche=None)
    extractor = TextExtractor(speicher)

    async def kein_download(*_args: Any) -> DownloadedFile:
        raise AssertionError("bereits erledigte Datei wird nicht erneut geladen")

    monkeypatch.setattr(extractor, "_download_to_file", kein_download)
    assert await extractor._process_file(_datei("a")) is False
    assert speicher.updates == []


async def test_dateien_nach_abbruch_laufen_zuletzt_und_einzeln(monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = _Speicher([_datei("a"), _datei("verdaechtig", versuche=1), _datei("b"), _datei("c", versuche=2)])
    extractor = TextExtractor(speicher)
    extractor.concurrency = 4
    laufend: set[str] = set()
    protokoll: list[tuple[str, frozenset[str]]] = []

    async def bearbeiten(datei: Any) -> bool:
        laufend.add(datei.id)
        protokoll.append((datei.id, frozenset(laufend)))
        await asyncio.sleep(0.01)
        laufend.discard(datei.id)
        return True

    monkeypatch.setattr(extractor, "_process_file", bearbeiten)
    assert await extractor.extract_pending_files(uuid.UUID(int=1)) == 4

    reihenfolge = [name for name, _ in protokoll]
    assert set(reihenfolge[:2]) == {"a", "b"} and reihenfolge[2:] == ["verdaechtig", "c"]
    # Verdächtige Dateien laufen allein
    assert dict(protokoll)["verdaechtig"] == {"verdaechtig"}
    assert dict(protokoll)["c"] == {"c"}


async def test_haengende_dateien_werden_hoechstens_einmal_je_minute_aufgeloest() -> None:
    speicher = _Speicher()
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)

    await extractor.extract_pending_files(uuid.UUID(int=1))
    uhr.jetzt += 30
    await extractor.extract_pending_files(uuid.UUID(int=2))
    assert len(speicher.aufloesungen) == 1
    uhr.jetzt += 31
    await extractor.extract_pending_files(uuid.UUID(int=1))
    assert len(speicher.aufloesungen) == 2
    assert speicher.aufloesungen[0] == {
        "stale_after": timedelta(minutes=settings.text_extraction_stale_minutes),
        "max_attempts": settings.text_extraction_max_attempts,
    }


# --- Speicher gegen PostgreSQL -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_abbruch_{uuid.uuid4().hex[:12]}"
BASE = "https://ris.example.org/oparl"


def _engine() -> AsyncEngine:
    url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})


async def _schema(anlegen: bool) -> None:
    engine = _engine()
    async with engine.begin() as conn:
        if anlegen:
            await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
        else:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    await engine.dispose()


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    if not DATABASE_URL:
        if os.environ.get("CI"):
            pytest.fail("INGESTOR_TEST_DATABASE_URL fehlt: In der CI müssen diese Tests laufen.")
        pytest.skip("braucht PostgreSQL (INGESTOR_TEST_DATABASE_URL)")
    asyncio.run(_schema(anlegen=True))
    yield
    asyncio.run(_schema(anlegen=False))


@pytest.fixture
async def speicher(schema: None) -> AsyncIterator[tuple[DatabaseStorage, uuid.UUID]]:
    storage = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=False)
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    source_id = await storage.upsert_source(f"{BASE}/system", "Musterstadt")
    body = OParlProcessor().process_body(
        {"id": f"{BASE}/body/1", "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}, f"{BASE}/body/1"
    )
    body_id = await storage.upsert_body(body, source_id)
    yield storage, body_id
    await storage.close()


async def _datei_anlegen(storage: DatabaseStorage, body_id: uuid.UUID, nummer: int, **werte: Any) -> uuid.UUID:
    file_id = uuid.uuid4()
    zeile = {
        "id": file_id,
        "external_id": f"{BASE}/file/{nummer}",
        "body_id": body_id,
        "download_url": f"{BASE}/file/{nummer}.pdf",
        "raw_json": {},
        **werte,
    }
    async with storage.get_session() as session:
        await session.execute(pg_insert(OParlFile).values(**zeile))
        await session.commit()
    return file_id


async def _zeile(storage: DatabaseStorage, file_id: uuid.UUID) -> Any:
    async with storage.get_session() as session:
        result = await session.execute(
            select(
                OParlFile.text_extraction_status,
                OParlFile.text_extraction_attempts,
                OParlFile.text_extraction_started_at,
                OParlFile.text_extraction_error,
            ).where(OParlFile.id == file_id)
        )
        return result.one()


@pytest.mark.integration
async def test_zaehler_beim_start_und_zuruecksetzen_beim_ende(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    storage, body_id = speicher
    datei = await _datei_anlegen(storage, body_id, 1)

    beansprucht = await storage.get_pending_files(body_id=body_id)
    assert [f.id for f in beansprucht] == [datei]
    status, versuche, beginn, _ = await _zeile(storage, datei)
    assert (status, versuche) == ("processing", 0) and beginn is not None

    assert await storage.mark_extraction_started(datei) == 1
    assert await storage.mark_extraction_started(datei) == 2
    await storage.update_file_text(datei, text_content="Beschluss", method="pypdf", status="completed")
    status, versuche, beginn, _ = await _zeile(storage, datei)
    assert (status, versuche, beginn) == ("completed", 0, None)
    # Erledigte Dateien nimmt niemand mehr in Bearbeitung
    assert await storage.mark_extraction_started(datei) is None


@pytest.mark.integration
async def test_haengende_dateien_zurueckstellen_oder_aufgeben(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    storage, body_id = speicher
    alt = datetime.now(UTC) - timedelta(hours=2)
    dreimal = await _datei_anlegen(
        storage,
        body_id,
        1,
        text_extraction_status="processing",
        text_extraction_attempts=3,
        text_extraction_started_at=alt,
    )
    einmal = await _datei_anlegen(
        storage,
        body_id,
        2,
        text_extraction_status="processing",
        text_extraction_attempts=1,
        text_extraction_started_at=alt,
    )
    nie_begonnen = await _datei_anlegen(
        storage, body_id, 3, text_extraction_status="processing", text_extraction_started_at=alt
    )
    # Zeile aus der Zeit vor dem Zähler: ohne Beginn zählt die letzte Änderung
    altbestand = await _datei_anlegen(storage, body_id, 4, text_extraction_status="processing", updated_at=alt)
    laeuft = await _datei_anlegen(
        storage,
        body_id,
        5,
        text_extraction_status="processing",
        text_extraction_attempts=3,
        text_extraction_started_at=datetime.now(UTC),
    )

    # Hängende Dateien werden nicht direkt beansprucht, nur über das Zurückstellen
    assert await storage.get_pending_files(body_id=body_id) == []

    assert await storage.release_stale_extractions(timedelta(hours=1), max_attempts=3) == (3, 1)

    status, versuche, beginn, fehler = await _zeile(storage, dreimal)
    assert (status, beginn) == ("failed", None)
    assert fehler.startswith("Speichergrenze")
    assert versuche == 3
    assert (await _zeile(storage, einmal))[:3] == ("pending", 1, None)
    assert (await _zeile(storage, nie_begonnen))[:2] == ("pending", 0)
    assert (await _zeile(storage, altbestand))[:2] == ("pending", 0)
    assert (await _zeile(storage, laeuft))[:2] == ("processing", 3)

    # Die zurückgestellte Datei läuft wieder; der Zähler zählt weiter
    beansprucht = {f.id: f.text_extraction_attempts for f in await storage.get_pending_files(body_id=body_id)}
    assert beansprucht == {einmal: 1, nie_begonnen: 0, altbestand: 0}
    assert await storage.mark_extraction_started(einmal) == 2
