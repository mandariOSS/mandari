# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen, die ihre Vorlage nicht mehr nennt, gelten als in der Quelle gelöscht (Quellen ohne eigene Beratungsliste).

more! rubin (OParl 1.0) führt keine Beratungsliste am Body; Beratungen kommen nur eingebettet in ihre Vorlage. Bei jeder
Änderung einer Vorlage legt die Quelle alle ihre Beratungen unter neuen Kennungen an, die alten liefert sie nicht mehr
(Einzelabruf: leere Liste). Im Bestand standen dadurch Dubletten (Darmstadt: ein Tagesordnungspunkt mit neun
Beratungen), und die Beratungsfolge zeigte Phantome. ``upsert_paper`` markiert solche Beratungen jetzt in der
Transaktion der Vorlage (``deleted``, Grund ``quelle_geloescht``) und meldet die Rücknahme wie eine Löschmarkierung der
Quelle.

Der erste Teil prüft die Bedingungen ohne Datenbank, der zweite läuft gegen PostgreSQL (``INGESTOR_TEST_DATABASE_URL``,
in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage import ris_events
from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlConsultation
from src.sync.processor import OParlProcessor

OPARL = "https://schema.oparl.org/1.0/"
BASE = "https://stadt.gremien.example/oparl"
KOMMUNE = f"{BASE}/body/1"
VORLAGE = f"{BASE}/paper/2026"


def _beratung(nummer: int, **felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": f"{BASE}/consultation/{nummer}",
        "type": f"{OPARL}Consultation",
        "paper": VORLAGE,
        "agendaItem": f"{BASE}/agendaItem/{nummer}",
        "role": "Beratung",
    }
    daten.update(felder)
    return daten


def _vorlage(*beratungen: Any, **felder: Any) -> Any:
    daten: dict[str, Any] = {"id": VORLAGE, "type": f"{OPARL}Paper", "name": "Radweg", "consultation": list(beratungen)}
    daten.update(felder)
    return OParlProcessor().process_paper(daten, KOMMUNE)


# --- Bedingungen, ohne Datenbank ---------------------------------------------------------------------------


class _Sitzung:
    """Datenbanksitzung, die Anweisungen nur sammelt."""

    def __init__(self) -> None:
        self.anweisungen: list[Any] = []

    async def execute(self, stmt: Any, *args: Any) -> Any:
        self.anweisungen.append(stmt)
        raise AssertionError("keine Anweisung erwartet")


@pytest.mark.parametrize(
    "beratungen",
    [
        # Ein Eintrag ohne Kennung: Ob eine gespeicherte Beratung gemeint ist, lässt sich nicht sagen
        [_beratung(1), {"type": f"{OPARL}Consultation"}],
        [_beratung(1), 7],
        # Ein bloßer Verweis bringt keine Beratung mit, deren Upsert eine frühere Markierung aufhöbe
        [_beratung(1), f"{BASE}/consultation/2"],
    ],
    ids=["ohne_kennung", "kein_objekt", "nur_verweis"],
)
async def test_unlesbare_liste_markiert_nichts(beratungen: list[Any]) -> None:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt", events_enabled=False)
    sitzung = _Sitzung()

    markiert = await storage._mark_unlisted_consultations(sitzung, uuid.uuid4(), _vorlage(*beratungen), uuid.uuid4())  # type: ignore[arg-type]

    assert markiert == [] and sitzung.anweisungen == []


async def test_vorlage_ohne_angabe_und_kommune_mit_beratungsliste_markieren_nichts() -> None:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt", events_enabled=False)
    sitzung = _Sitzung()
    kommune = uuid.uuid4()
    ohne = OParlProcessor().process_paper({"id": VORLAGE, "type": f"{OPARL}Paper", "name": "Radweg"}, KOMMUNE)
    assert await storage._mark_unlisted_consultations(sitzung, uuid.uuid4(), ohne, kommune) == []  # type: ignore[arg-type]

    # OParl 1.1 mit Beratungsliste am Body: Löschungen meldet die Quelle dort
    storage._consultation_list_cache[kommune] = True
    assert await storage._mark_unlisted_consultations(sitzung, uuid.uuid4(), _vorlage(_beratung(1)), kommune) == []  # type: ignore[arg-type]
    assert sitzung.anweisungen == []


# --- Speicher gegen PostgreSQL -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_beratungen_{uuid.uuid4().hex[:12]}"


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
                    "CREATE TABLE oparl_papers_locations ("
                    " id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,"
                    " oparlpaper_id uuid NOT NULL REFERENCES oparl_papers (id),"
                    " oparllocation_id uuid NOT NULL REFERENCES oparl_locations (id),"
                    " UNIQUE (oparlpaper_id, oparllocation_id))"
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


async def _speicher(events: bool) -> DatabaseStorage:
    storage = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=events)
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen}, oparl_papers_locations CASCADE"))
    return storage


@pytest.fixture(params=[False, True], ids=["ohne_ereignisse", "mit_ereignissen"])
async def speicher(schema: None, request: pytest.FixtureRequest) -> AsyncIterator[DatabaseStorage]:
    storage = await _speicher(request.param)
    yield storage
    await storage.close()


async def _kommune(storage: DatabaseStorage, **listen: str) -> uuid.UUID:
    """Kommune wie bei more! rubin: Listen für Vorlagen und Sitzungen, keine Beratungsliste (außer ``listen``)."""
    source_id = await storage.upsert_source(f"{BASE}/system", "Musterstadt")
    daten = {"id": KOMMUNE, "type": f"{OPARL}Body", "name": "Musterstadt", "paper": f"{KOMMUNE}/paper", **listen}
    return await storage.upsert_body(OParlProcessor().process_body(daten, KOMMUNE), source_id)


async def _stand(storage: DatabaseStorage) -> dict[str, tuple[bool, str | None]]:
    """Beratungen im Bestand: Nummer -> (gelöscht, Grund)."""
    async with storage.get_session() as session:
        zeilen = await session.execute(
            select(OParlConsultation.external_id, OParlConsultation.deleted, OParlConsultation.deletion_reason)
        )
        return {kennung.rsplit("/", 1)[1]: (geloescht, grund) for kennung, geloescht, grund in zeilen}


GELOESCHT = (True, ris_events.REASON_DELETED_AT_SOURCE)
AKTIV = (False, None)


@pytest.mark.integration
async def test_neue_kennungen_markieren_die_alten_beratungen(speicher: DatabaseStorage) -> None:
    kommune = await _kommune(speicher)
    await speicher.upsert_paper(_vorlage(_beratung(16033), _beratung(16034)), kommune)
    assert await _stand(speicher) == {"16033": AKTIV, "16034": AKTIV}

    # Die Quelle hat die Vorlage geändert und ihre Beratungen neu nummeriert
    await speicher.upsert_paper(_vorlage(_beratung(16267), _beratung(16268)), kommune)

    assert await _stand(speicher) == {"16033": GELOESCHT, "16034": GELOESCHT, "16267": AKTIV, "16268": AKTIV}


@pytest.mark.integration
async def test_wieder_genannte_beratung_ist_wieder_aktiv(speicher: DatabaseStorage) -> None:
    kommune = await _kommune(speicher)
    await speicher.upsert_paper(_vorlage(_beratung(1), _beratung(2)), kommune)
    await speicher.upsert_paper(_vorlage(_beratung(2)), kommune)
    assert await _stand(speicher) == {"1": GELOESCHT, "2": AKTIV}

    await speicher.upsert_paper(_vorlage(_beratung(1), _beratung(2)), kommune)

    assert await _stand(speicher) == {"1": AKTIV, "2": AKTIV}


@pytest.mark.integration
async def test_leere_liste_markiert_alle_und_ohne_angabe_bleibt_alles(speicher: DatabaseStorage) -> None:
    kommune = await _kommune(speicher)
    await speicher.upsert_paper(_vorlage(_beratung(1)), kommune)

    # Ohne die Angabe ``consultation`` sagt die Vorlage nichts über ihre Beratungen
    ohne = OParlProcessor().process_paper({"id": VORLAGE, "type": f"{OPARL}Paper", "name": "Radweg"}, KOMMUNE)
    await speicher.upsert_paper(ohne, kommune)
    assert await _stand(speicher) == {"1": AKTIV}

    await speicher.upsert_paper(_vorlage(), kommune)
    assert await _stand(speicher) == {"1": GELOESCHT}


@pytest.mark.integration
async def test_kommune_mit_beratungsliste_markiert_nichts(speicher: DatabaseStorage) -> None:
    """OParl 1.1: Gelöschte Beratungen meldet die Quelle in ihrer Liste (``deleted: true``), nicht durch Weglassen."""
    kommune = await _kommune(speicher, consultation=f"{KOMMUNE}/consultation")
    await speicher.upsert_paper(_vorlage(_beratung(1), _beratung(2)), kommune)
    await speicher.upsert_paper(_vorlage(_beratung(2)), kommune)

    assert await _stand(speicher) == {"1": AKTIV, "2": AKTIV}


@pytest.mark.integration
async def test_ruecknahme_aus_datenschutzgruenden_bleibt(speicher: DatabaseStorage) -> None:
    kommune = await _kommune(speicher)
    await speicher.upsert_paper(_vorlage(_beratung(1), _beratung(2)), kommune)
    async with speicher.get_session() as session:
        await session.execute(
            update(OParlConsultation)
            .where(OParlConsultation.external_id == f"{BASE}/consultation/1")
            .values(deleted=True, deletion_reason=ris_events.REASON_PRIVACY)
        )
        await session.commit()

    await speicher.upsert_paper(_vorlage(_beratung(3)), kommune)

    assert await _stand(speicher) == {"1": (True, ris_events.REASON_PRIVACY), "2": GELOESCHT, "3": AKTIV}


@pytest.mark.integration
async def test_markierung_meldet_die_ruecknahme(schema: None) -> None:
    storage = await _speicher(events=True)
    try:
        kommune = await _kommune(storage)
        await storage.upsert_paper(_vorlage(_beratung(1), _beratung(2)), kommune)
        async with storage.get_session() as session:
            alt = (
                await session.execute(
                    select(OParlConsultation.id).where(OParlConsultation.external_id == f"{BASE}/consultation/1")
                )
            ).scalar_one()
            await session.execute(text("DELETE FROM events_event"))
            await session.commit()

        await storage.upsert_paper(_vorlage(_beratung(2)), kommune)

        async with storage.get_session() as session:
            zeilen = await session.execute(text("SELECT type, aggregate_id, payload FROM events_event ORDER BY id"))
            ereignisse = [dict(zeile._mapping) for zeile in zeilen]
    finally:
        await storage.close()
    rueckgaenge = [e for e in ereignisse if e["type"] == "ris.object.depublished"]
    assert len(rueckgaenge) == 1
    assert rueckgaenge[0]["aggregate_id"] == alt
    assert rueckgaenge[0]["payload"] == {
        "object_type": "Consultation",
        "object": str(alt),
        "reason": ris_events.REASON_DELETED_AT_SOURCE,
    }
