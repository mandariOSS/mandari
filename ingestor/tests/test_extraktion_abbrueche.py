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
from sqlalchemy import func, or_, select, text, update
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
        # Je Beanspruchen: (höchstens, retried, beansprucht)
        self.anfragen: list[tuple[int, bool | None, list[str]]] = []

    async def release_stale_extractions(self, **werte: Any) -> tuple[int, int]:
        self.aufloesungen.append(werte)
        return 2, 1

    async def get_pending_files(
        self, *, body_id: Any, batch_size: int, max_size_bytes: int | None = None, retried: bool | None = None
    ) -> list[Any]:
        passend = [d for d in self.dateien if retried is None or (d.text_extraction_attempts > 0) == retried]
        auswahl = passend[:batch_size]
        for datei in auswahl:
            self.dateien.remove(datei)
        self.anfragen.append((batch_size, retried, [d.id for d in auswahl]))
        return auswahl

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
    # Verdächtige Dateien laufen allein und werden erst direkt davor einzeln beansprucht
    assert dict(protokoll)["verdaechtig"] == {"verdaechtig"}
    assert dict(protokoll)["c"] == {"c"}
    assert speicher.anfragen == [
        (8, False, ["a", "b"]),
        (1, True, ["verdaechtig"]),
        (1, True, ["c"]),
        (1, True, []),
    ]


async def test_beansprucht_in_kleinen_portionen_direkt_vor_der_bearbeitung(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Das Beanspruchen startet die Zeitgrenze (Issue #817): Mit Parallelität 1 höchstens zwei Dateien auf einmal,
    nie der ganze Stapel, sonst gälten wartende Dateien eines langen Stapels als abgebrochen.
    """
    speicher = _Speicher([_datei(f"d{nummer}") for nummer in range(7)])
    extractor = TextExtractor(speicher)
    extractor.concurrency = 1
    extractor.batch_size = 500
    beansprucht_bei_beginn: list[int] = []

    async def bearbeiten(datei: Any) -> bool:
        # Wie viele Dateien sind beansprucht, aber noch nicht begonnen?
        beansprucht = sum(len(namen) for _, _, namen in speicher.anfragen)
        beansprucht_bei_beginn.append(beansprucht - len(beansprucht_bei_beginn) - 1)
        return True

    monkeypatch.setattr(extractor, "_process_file", bearbeiten)
    assert await extractor.extract_pending_files(uuid.UUID(int=1)) == 7

    assert [(hoechstens, retried) for hoechstens, retried, _ in speicher.anfragen] == [
        (2, False),
        (2, False),
        (2, False),
        (2, False),
        (1, True),
    ]
    # Beim Beginn einer Datei wartet höchstens eine weitere beanspruchte Datei
    assert max(beansprucht_bei_beginn) <= 1


async def test_hoechstens_batch_size_dateien_je_aufruf(monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = _Speicher([_datei(f"d{nummer}") for nummer in range(5)] + [_datei("v", versuche=1)])
    extractor = TextExtractor(speicher)
    extractor.concurrency = 2
    extractor.batch_size = 3

    async def bearbeiten(datei: Any) -> bool:
        return True

    monkeypatch.setattr(extractor, "_process_file", bearbeiten)
    assert await extractor.extract_pending_files(uuid.UUID(int=1)) == 3
    assert [(hoechstens, retried) for hoechstens, retried, _ in speicher.anfragen] == [(3, False)]
    assert len(speicher.dateien) == 3


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


async def test_aufloesen_hoechstens_einmal_je_minute_auch_mit_einem_extraktor_je_kommune() -> None:
    """Sync und Scraper legen je Kommune einen Extraktor an; die Grenze gilt je Speicher, nicht je Extraktor."""
    speicher = _Speicher()
    uhr = _Uhr()

    for nummer in range(5):
        await TextExtractor(speicher, clock=uhr).extract_pending_files(uuid.UUID(int=nummer))
        uhr.jetzt += 10
    assert len(speicher.aufloesungen) == 1
    uhr.jetzt += 20
    await TextExtractor(speicher, clock=uhr).extract_pending_files(uuid.UUID(int=1))
    assert len(speicher.aufloesungen) == 2


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
    # Zurückgestellt vor dem Beginn (robots.txt nicht erreichbar): der Zähler bleibt
    await storage.update_file_text(datei, status="pending", reset_attempts=False)
    status, versuche, beginn, _ = await _zeile(storage, datei)
    assert (status, versuche, beginn) == ("pending", 2, None)
    assert await storage.mark_extraction_started(datei) == 3
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
    # Zeitpunkt der Aufgabe für die Prüfung „texterkennung“ (aufgegeben in 24 h), unabhängig von updated_at
    async with storage.get_session() as session:
        aufgegeben_am = await session.scalar(select(OParlFile.text_extracted_at).where(OParlFile.id == dreimal))
    assert aufgegeben_am is not None and datetime.now(UTC) - aufgegeben_am < timedelta(minutes=5)
    assert (await _zeile(storage, einmal))[:3] == ("pending", 1, None)
    assert (await _zeile(storage, nie_begonnen))[:2] == ("pending", 0)
    assert (await _zeile(storage, altbestand))[:2] == ("pending", 0)
    assert (await _zeile(storage, laeuft))[:2] == ("processing", 3)

    # Die zurückgestellte Datei läuft wieder; der Zähler zählt weiter
    beansprucht = {f.id: f.text_extraction_attempts for f in await storage.get_pending_files(body_id=body_id)}
    assert beansprucht == {einmal: 1, nie_begonnen: 0, altbestand: 0}
    assert await storage.mark_extraction_started(einmal) == 2


async def _zeit_vergeht(storage: DatabaseStorage, dauer: timedelta) -> None:
    """Uhr vorstellen: Beginn und letzte Änderung aller Dateien in Bearbeitung rücken um ``dauer`` zurück."""
    async with storage.get_session() as session:
        await session.execute(
            update(OParlFile)
            .where(OParlFile.text_extraction_status == "processing")
            .values(
                text_extraction_started_at=OParlFile.text_extraction_started_at - dauer,
                updated_at=OParlFile.updated_at - dauer,
            )
        )
        await session.commit()


async def _haengend(storage: DatabaseStorage, grenze: timedelta) -> int:
    """Wie die Prüfung „texterkennung“: Dateien in Bearbeitung, deren Beginn länger als ``grenze`` zurückliegt."""
    cutoff = datetime.now(UTC) - grenze
    async with storage.get_session() as session:
        anzahl = await session.scalar(
            select(func.count())
            .select_from(OParlFile)
            .where(
                OParlFile.text_extraction_status == "processing",
                or_(
                    OParlFile.text_extraction_started_at < cutoff,
                    (OParlFile.text_extraction_started_at.is_(None)) & (OParlFile.updated_at < cutoff),
                ),
            )
        )
    return int(anzahl or 0)


@pytest.mark.integration
async def test_langer_stapel_laesst_wartende_dateien_nicht_haengen(
    speicher: tuple[DatabaseStorage, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Ein Stapel, der viel länger als die Zeitgrenze läuft (Parallelität 1, je Datei 30 min Texterkennung), ergibt
    weder „hängende“ Dateien noch ein Zurückstellen noch wartender Dateien durch einen zweiten Extraktor: Der
    Worker beansprucht erst kurz vor der Bearbeitung, nicht den ganzen Stapel auf einmal (Issue #817).
    """
    storage, body_id = speicher
    for nummer in range(1, 7):
        await _datei_anlegen(storage, body_id, nummer)
    grenze = timedelta(minutes=60)
    extractor = TextExtractor(storage)
    extractor.concurrency = 1
    extractor.batch_size = 500
    befunde: list[tuple[int, tuple[int, int]]] = []

    async def bearbeiten(datei: Any) -> bool:
        assert await storage.mark_extraction_started(datei.id) == 1
        await _zeit_vergeht(storage, timedelta(minutes=30))
        # Prüfung „texterkennung“ und ein zweiter Extraktor (Sync, Scraper) während des Stapels
        befunde.append((await _haengend(storage, grenze), await storage.release_stale_extractions(grenze, 3)))
        await storage.update_file_text(datei.id, text_content="Beschluss", method="pypdf", status="completed")
        return True

    monkeypatch.setattr(extractor, "_process_file", bearbeiten)
    assert await extractor.extract_pending_files(body_id) == 6
    assert befunde == [(0, (0, 0))] * 6


async def test_ruht_wenn_die_auftraege_der_anwendung_den_text_erkennen(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEXT_EXTRACTION_RUNNER=worker (Issue #530): kein Doppelbetrieb, der OCR-Worker beansprucht nichts."""
    monkeypatch.setattr(settings, "text_extraction_runner", "worker")
    speicher = _Speicher([_datei("a")])

    assert await TextExtractor(speicher).extract_pending_files(uuid.UUID(int=1)) == 0
    assert speicher.dateien and speicher.aufloesungen == []
