# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dateien direkt am Tagesordnungspunkt (Issue #1528).

OParl erlaubt Dateien am AgendaItem (``auxiliaryFile``, ``resolutionFile``), etwa Anträge, die ohne Beratung und
Vorlage nur als Datei am Punkt hängen. Der Ingestor übernahm Dateien nur aus Sitzungen und Vorlagen. Jetzt verarbeitet
der Prozessor sie als eingebettete Objekte des Punkts, und der Abgleich schreibt sie nach dem Punkt mit Bezug zum Punkt
(``agenda_item_id``) und zu seiner Sitzung (``meeting_id``), aus der Sitzung eingebettet wie aus der Liste der Punkte.
Eine einzeln gelieferte Datei mit Rückverweis ``agendaItem`` (OParl 1.1) bekommt denselben Bezug.

Der erste Teil prüft den Prozessor ohne Datenbank, der zweite läuft gegen PostgreSQL (``INGESTOR_TEST_DATABASE_URL``,
in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from mandari_oparl import ProcessedAgendaItem, ProcessedFile
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage import ris_events
from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlAgendaItem, OParlFile
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor

OPARL = "https://schema.oparl.org/1.1/"
BASE = "https://oparl.stadt.example/bodies/0001"
KOMMUNE = BASE
SITZUNG = f"{BASE}/meetings/14548"
PUNKT = f"{BASE}/agendaitems/293100"


def _datei(nummer: int, **felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": f"{BASE}/files/{nummer}",
        "type": f"{OPARL}File",
        "name": f"Antrag {nummer}",
        "fileName": f"antrag-{nummer}.pdf",
        "mimeType": "application/pdf",
        "accessUrl": f"{BASE}/files/{nummer}/antrag.pdf",
    }
    daten.update(felder)
    return daten


def _punkt(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": PUNKT,
        "type": f"{OPARL}AgendaItem",
        "meeting": SITZUNG,
        "number": "5.1.1",
        "name": "Antrag der Fraktion",
        "public": True,
        "auxiliaryFile": [_datei(1), _datei(2)],
        "resolutionFile": _datei(3),
    }
    daten.update(felder)
    return daten


def _sitzung(*punkte: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": SITZUNG,
        "type": f"{OPARL}Meeting",
        "name": "Rat",
        "start": "2026-11-25T17:00:00+01:00",
        "agendaItem": list(punkte),
        "auxiliaryFile": [_datei(9)],
    }


# --- Prozessor -------------------------------------------------------------------------------------------------


def test_punkt_bettet_seine_dateien_ein() -> None:
    punkt = OParlProcessor().process_agenda_item(_punkt(), KOMMUNE)
    dateien = [nested for nested in punkt.nested_entities if isinstance(nested, ProcessedFile)]
    assert [d.external_id for d in dateien] == [f"{BASE}/files/{n}" for n in (1, 2, 3)]
    assert punkt.references["files"] == [f"{BASE}/files/{n}" for n in (1, 2, 3)]


def test_sitzung_haelt_dateien_der_punkte_beim_punkt() -> None:
    """Die Datei am Punkt gehört nicht unter die Dateien der Sitzung (sonst ohne Bezug zum Punkt gespeichert)."""
    sitzung = OParlProcessor().process_meeting(_sitzung(_punkt()), KOMMUNE)
    eigene = [n.external_id for n in sitzung.nested_entities if isinstance(n, ProcessedFile)]
    punkte = [n for n in sitzung.nested_entities if isinstance(n, ProcessedAgendaItem)]
    assert eigene == [f"{BASE}/files/9"]
    assert len(punkte) == 1 and len(punkte[0].nested_entities) == 3


def test_rueckverweis_agenda_item_einer_einzelnen_datei() -> None:
    processor = OParlProcessor()
    einzeln = processor.process_file(_datei(4, agendaItem=[PUNKT], meeting=SITZUNG), KOMMUNE)
    assert einzeln.agenda_item_external_ids == [PUNKT]
    # Eine einzelne Adresse statt einer Liste zerfällt nicht in Zeichen
    assert einzeln.meeting_external_ids == [SITZUNG]
    assert processor.process_file(_datei(5, agendaItem=PUNKT), KOMMUNE).agenda_item_external_ids == [PUNKT]
    assert processor.process_file(_datei(6), KOMMUNE).agenda_item_external_ids == []


def test_ereignis_nennt_den_punkt() -> None:
    datei, punkt = uuid.uuid4(), uuid.uuid4()
    neu = ris_events.file_events(datei, _datei(1), None, agenda_item_id=punkt)
    assert neu[0].payload == {"file": str(datei), "change": "added", "agenda_item": str(punkt)}
    # Bekannte Datei, erstmals am Punkt: hinzugefügt
    bekannt = ris_events.Prior(raw_json=_datei(1))
    angehaengt = ris_events.file_events(datei, _datei(1), bekannt, agenda_item_id=punkt)
    assert angehaengt[0].payload["change"] == "added"
    # Schon am Punkt, nichts geändert: kein Ereignis
    am_punkt = ris_events.Prior(raw_json=_datei(1), agenda_item_id=punkt)
    assert ris_events.file_events(datei, _datei(1), am_punkt, agenda_item_id=punkt) == []


# --- Speicher gegen PostgreSQL -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_top_dateien_{uuid.uuid4().hex[:12]}"


def _engine() -> AsyncEngine:
    url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})


async def _schema(anlegen: bool) -> None:
    engine = _engine()
    async with engine.begin() as conn:
        if not anlegen:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        else:
            await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
            # Was in der Installation die Django-Migration der Ereignistechnik anlegt (test_events_journal.py)
            await conn.execute(
                text(
                    "ALTER TABLE events_event"
                    " ADD COLUMN seq bigint NULL UNIQUE,"
                    " ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now(),"
                    " ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id()"
                )
            )
            # Zwischentabelle, die nur Django beschreibt; der Ingestor schreibt sie mit SQL
            await conn.execute(
                text(
                    "CREATE TABLE oparl_meetings_organizations ("
                    " id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,"
                    " oparlmeeting_id uuid NOT NULL REFERENCES oparl_meetings (id),"
                    " oparlorganization_id uuid NOT NULL REFERENCES oparl_organizations (id),"
                    " UNIQUE (oparlmeeting_id, oparlorganization_id))"
                )
            )
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


@pytest.fixture(params=[False, True], ids=["ohne_ereignisse", "mit_ereignissen"])
async def speicher(schema: None, request: pytest.FixtureRequest) -> AsyncIterator[DatabaseStorage]:
    storage = DatabaseStorage(
        DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=request.param
    )
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen}, oparl_meetings_organizations CASCADE"))
    yield storage
    await storage.close()


async def _abgleich(storage: DatabaseStorage) -> tuple[SyncOrchestrator, uuid.UUID]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = storage
    orchestrator.processor = OParlProcessor(storage.id_bases)
    source_id = await storage.upsert_source("https://oparl.stadt.example/system", "Musterstadt")
    body = OParlProcessor().process_body({"id": KOMMUNE, "type": f"{OPARL}Body", "name": "Musterstadt"}, KOMMUNE)
    return orchestrator, await storage.upsert_body(body, source_id)


async def _dateien(storage: DatabaseStorage) -> dict[str, tuple[Any, Any, Any]]:
    """Dateien im Bestand: Nummer -> (Punkt, Sitzung, Vorgang)."""
    async with storage.get_session() as session:
        zeilen = await session.execute(
            select(OParlFile.external_id, OParlFile.agenda_item_id, OParlFile.meeting_id, OParlFile.paper_id)
        )
        return {kennung.rsplit("/", 1)[1]: (punkt, sitzung, vorgang) for kennung, punkt, sitzung, vorgang in zeilen}


async def _punkt_und_sitzung(storage: DatabaseStorage) -> tuple[uuid.UUID, uuid.UUID]:
    async with storage.get_session() as session:
        zeile = (
            await session.execute(
                select(OParlAgendaItem.id, OParlAgendaItem.meeting_id).where(OParlAgendaItem.external_id == PUNKT)
            )
        ).one()
        return zeile[0], zeile[1]


@pytest.mark.integration
async def test_eingebettete_dateien_am_punkt_mit_bezug(speicher: DatabaseStorage) -> None:
    orchestrator, kommune = await _abgleich(speicher)
    sitzung = orchestrator.processor.process_meeting(_sitzung(_punkt()), KOMMUNE)

    assert await orchestrator._store_entity(sitzung, kommune, "meeting") is True

    punkt, sitzung_id = await _punkt_und_sitzung(speicher)
    assert await _dateien(speicher) == {
        "1": (punkt, sitzung_id, None),
        "2": (punkt, sitzung_id, None),
        "3": (punkt, sitzung_id, None),
        # Datei der Sitzung selbst: ohne Punkt
        "9": (None, sitzung_id, None),
    }
    if speicher.events_enabled:
        async with speicher.get_session() as session:
            zeilen = await session.execute(
                text("SELECT payload FROM events_event WHERE type = 'ris.file.changed' ORDER BY id")
            )
            nutzlasten = [zeile[0] for zeile in zeilen]
        am_punkt = [n for n in nutzlasten if n.get("agenda_item")]
        assert len(am_punkt) == 3
        assert {n["agenda_item"] for n in am_punkt} == {str(punkt)}
        assert {n["meeting"] for n in am_punkt} == {str(sitzung_id)}


@pytest.mark.integration
async def test_punkt_aus_der_liste_und_einzelne_datei_mit_rueckverweis(speicher: DatabaseStorage) -> None:
    orchestrator, kommune = await _abgleich(speicher)
    leer = orchestrator.processor.process_meeting({**_sitzung(), "auxiliaryFile": []}, KOMMUNE)
    await orchestrator._store_entity(leer, kommune, "meeting")

    punkt = orchestrator.processor.process(_punkt(auxiliaryFile=[_datei(1)], resolutionFile=None), KOMMUNE)
    assert punkt is not None
    assert await orchestrator._store_entity(punkt, kommune, "agendaitem") is True
    einzeln = orchestrator.processor.process(_datei(4, agendaItem=[PUNKT]), KOMMUNE)
    assert einzeln is not None
    assert await orchestrator._store_entity(einzeln, kommune, "file") is True

    punkt_id, sitzung_id = await _punkt_und_sitzung(speicher)
    assert await _dateien(speicher) == {"1": (punkt_id, sitzung_id, None), "4": (punkt_id, sitzung_id, None)}

    # Ein späterer Abgleich der Datei ohne Bezug lässt den Bezug stehen
    ohne = orchestrator.processor.process(_datei(4, name="Antrag 4, neue Fassung"), KOMMUNE)
    assert ohne is not None
    await orchestrator._store_entity(ohne, kommune, "file")
    assert (await _dateien(speicher))["4"] == (punkt_id, sitzung_id, None)


@pytest.mark.integration
async def test_punkt_einer_anderen_quelle_bekommt_keine_dateien(speicher: DatabaseStorage) -> None:
    """Steht der Punkt nicht im Bestand derselben Quelle, schreibt der Abgleich seine Dateien nicht."""
    orchestrator, kommune = await _abgleich(speicher)
    punkt = OParlProcessor().process_agenda_item(_punkt(), KOMMUNE)

    assert await speicher.upsert_agenda_item_files(punkt, kommune) == 0
    assert await _dateien(speicher) == {}
