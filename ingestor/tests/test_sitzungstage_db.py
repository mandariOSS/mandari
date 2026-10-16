# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungstage für den Takt je Quelle (Issue #1026), gegen PostgreSQL.

Braucht ``INGESTOR_TEST_DATABASE_URL`` (in der CI gesetzt; lokal ohne Datenbank übersprungen). Die Tests legen ein
eigenes Schema aus der Tabellenbeschreibung des Ingestors an und entfernen es wieder.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlBody, OParlMeeting, OParlSource

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_takt_{uuid.uuid4().hex[:12]}"

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


async def _quelle_mit_kommune(storage: DatabaseStorage, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with storage.get_session() as session:
        quelle = OParlSource(name=name, url=f"https://{name}.example.org/oparl/system", sync_config={})
        session.add(quelle)
        await session.flush()
        kommune = OParlBody(external_id=f"https://{name}.example.org/oparl/body/1", source_id=quelle.id, name=name)
        session.add(kommune)
        await session.commit()
        return quelle.id, kommune.id


async def _sitzung(storage: DatabaseStorage, body_id: uuid.UUID, start: datetime, **felder: object) -> None:
    async with storage.get_session() as session:
        session.add(
            OParlMeeting(
                external_id=f"https://ris.example.org/meeting/{uuid.uuid4()}", body_id=body_id, start=start, **felder
            )
        )
        await session.commit()


async def test_sitzungen_der_quelle_im_zeitraum(storage: DatabaseStorage) -> None:
    quelle, kommune = await _quelle_mit_kommune(storage, "rat")
    _, andere = await _quelle_mit_kommune(storage, "kreis")
    beginn = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
    morgen = beginn + timedelta(days=1, hours=17)

    await _sitzung(storage, kommune, morgen)
    await _sitzung(storage, kommune, morgen + timedelta(hours=1), cancelled=True)
    await _sitzung(storage, kommune, morgen + timedelta(hours=2), deleted=True)
    await _sitzung(storage, kommune, beginn - timedelta(minutes=1))
    await _sitzung(storage, kommune, beginn + timedelta(days=4))
    await _sitzung(storage, andere, morgen)
    await _sitzung(storage, kommune, beginn + timedelta(hours=5), cancelled=None)

    gefunden = await storage.get_meeting_starts(quelle, beginn, beginn + timedelta(days=4))

    assert gefunden == [beginn + timedelta(hours=5), morgen]


async def test_zustand_des_takts_neben_anderen_schluesseln(storage: DatabaseStorage) -> None:
    quelle, _ = await _quelle_mit_kommune(storage, "rat")
    await storage.set_source_sync_config_value(quelle, "request_interval", 2.0)

    await storage.set_source_sync_config_value(quelle, "sync_state", {"next_due": "2026-10-13T08:10:00+00:00"})

    zeile = await storage.get_source_by_url("https://rat.example.org/oparl/system")
    assert zeile is not None
    assert zeile.sync_config == {"request_interval": 2.0, "sync_state": {"next_due": "2026-10-13T08:10:00+00:00"}}
