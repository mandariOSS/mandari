# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenabgleich mit der Quelle (Issue #1525), gegen PostgreSQL: Bestand je Kommune (nicht gelöscht und samt
Löschmarkierung) und Ablage unter Zeilensperre, ohne andere Schlüssel der ``sync_config`` anzufassen.

Braucht ``INGESTOR_TEST_DATABASE_URL`` (in der CI gesetzt; lokal ohne Datenbank übersprungen). Die Tests legen ein
eigenes Schema aus der Tabellenbeschreibung des Ingestors an und entfernen es wieder.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage.database import DatabaseStorage
from src.storage.models import (
    Base,
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from src.sync import datenabgleich

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_datenabgleich_{uuid.uuid4().hex[:12]}"
BASIS = "https://ris.example.org/oparl/body"

pytestmark = pytest.mark.integration


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
async def storage(schema: None) -> AsyncIterator[DatabaseStorage]:
    speicher = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
    await speicher._engine.dispose()
    speicher._engine = _engine()
    speicher._session_factory = async_sessionmaker(speicher._engine, class_=AsyncSession, expire_on_commit=False)
    async with speicher._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    yield speicher
    await speicher.close()


async def _quelle_mit_kommunen(storage: DatabaseStorage) -> tuple[OParlSource, list[uuid.UUID]]:
    async with storage.get_session() as session:
        quelle = OParlSource(
            name="Stadt", url="https://ris.example.org/oparl/system", sync_config={"sync_window": {"start": "23:00"}}
        )
        session.add(quelle)
        await session.flush()
        kommunen = []
        for nummer in (1, 2):
            kommune = OParlBody(
                external_id=f"{BASIS}/{nummer}",
                source_id=quelle.id,
                name=f"Kommune {nummer}",
                raw_json={"paper": f"{BASIS}/{nummer}/paper"},
            )
            session.add(kommune)
            await session.flush()
            kommunen.append(kommune.id)
        session.add(
            OParlBody(external_id=f"{BASIS}/9", source_id=quelle.id, name="Aufgelöst", deleted=True, raw_json={})
        )
        await session.commit()
        return quelle, kommunen


async def test_bestand_zaehlt_mit_und_ohne_loeschmarkierung(storage: DatabaseStorage) -> None:
    quelle, (eins, zwei) = await _quelle_mit_kommunen(storage)
    async with storage.get_session() as session:
        sitzung = OParlMeeting(external_id=f"{BASIS}/1/meeting/1", body_id=eins)
        session.add(sitzung)
        await session.flush()
        for nummer, geloescht in ((1, False), (2, False), (3, True)):
            session.add(OParlPaper(external_id=f"{BASIS}/1/paper/{nummer}", body_id=eins, deleted=geloescht))
            session.add(OParlAgendaItem(external_id=f"{BASIS}/1/ai/{nummer}", meeting_id=sitzung.id, deleted=geloescht))
        session.add(OParlPaper(external_id=f"{BASIS}/2/paper/1", body_id=zwei))
        session.add(OParlConsultation(external_id=f"{BASIS}/1/consultation/1", body_id=eins))
        session.add(OParlFile(external_id=f"{BASIS}/1/file/1", body_id=eins, deleted=True))
        await session.commit()

    kommunen = await datenabgleich._kommunen(storage, quelle.id)
    bestand = await datenabgleich._bestand(storage, eins)

    assert [k["name"] for k in kommunen] == ["Kommune 1", "Kommune 2"], "gelöschte Kommunen nicht"
    assert kommunen[0]["raw_json"] == {"paper": f"{BASIS}/1/paper"}
    assert bestand == {
        "paper": (2, 3),
        "meeting": (1, 1),
        "consultation": (1, 1),
        "agendaitem": (2, 3),
        "file": (0, 1),
    }


async def test_ablage_fuehrt_zusammen_und_laesst_die_uebrigen_schluessel(storage: DatabaseStorage) -> None:
    quelle, _ = await _quelle_mit_kommunen(storage)
    zeile = SimpleNamespace(id=quelle.id)

    await datenabgleich._speichern(storage, zeile, {f"{BASIS}/1": {"at": "a"}}, ersetzen=True)
    await datenabgleich._speichern(storage, zeile, {f"{BASIS}/2": {"at": "b"}}, ersetzen=False)

    async with storage.get_session() as session:
        config = (await session.execute(select(OParlSource.sync_config).where(OParlSource.id == quelle.id))).scalar()
    assert config["sync_window"] == {"start": "23:00"}
    assert config[datenabgleich.SCHLUESSEL] == {f"{BASIS}/1": {"at": "a"}, f"{BASIS}/2": {"at": "b"}}

    await datenabgleich._speichern(storage, zeile, {f"{BASIS}/2": {"at": "c"}}, ersetzen=True)
    async with storage.get_session() as session:
        config = (await session.execute(select(OParlSource.sync_config).where(OParlSource.id == quelle.id))).scalar()
    assert config[datenabgleich.SCHLUESSEL] == {f"{BASIS}/2": {"at": "c"}}
