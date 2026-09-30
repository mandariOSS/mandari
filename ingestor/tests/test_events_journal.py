# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse in derselben Transaktion wie die Datenänderung (Issue #513), gegen PostgreSQL.

Braucht ``INGESTOR_TEST_DATABASE_URL`` (in der CI gesetzt; lokal ohne Datenbank übersprungen). Die
Tests legen ein eigenes Schema an und entfernen es wieder; vorhandene Tabellen der Datenbank fassen
sie nicht an. Die Tabellen entstehen aus der Beschreibung des Ingestors. Dass diese zur Tabelle aus
den Django-Migrationen passt, prüft der Schema-Vertrag (``mandari/insight_core/schema_contract.py``);
``seq``, ``xid`` und ``recorded_at`` kommen hier wie dort aus der Datenbank.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mandari_oparl.ids import canonical_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage import events, ris_events
from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlPaper
from src.sync.processor import OParlProcessor

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_test_{uuid.uuid4().hex[:12]}"

BASE = "https://ris.example.org/oparl"
BODY = f"{BASE}/body/1"
MEETING = f"{BASE}/meeting/1"
PAPER = f"{BASE}/paper/1"
ITEM = f"{BASE}/agendaitem/1"
FILE = f"{BASE}/file/1"
CONSULTATION = f"{BASE}/consultation/1"

pytestmark = pytest.mark.integration


def _engine() -> AsyncEngine:
    url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})


async def _schema_anlegen() -> None:
    engine = _engine()
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
        await conn.run_sync(Base.metadata.create_all)
        # Was in der Installation die Django-Migration der Ereignistechnik anlegt und der Ingestor
        # nicht kennt: Folgenummer, Transaktionskennung, Erfassungszeit.
        await conn.execute(
            text(
                "ALTER TABLE events_event"
                " ADD COLUMN seq bigint NULL UNIQUE,"
                " ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now(),"
                " ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id()"
            )
        )
    await engine.dispose()


async def _schema_entfernen() -> None:
    engine = _engine()
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    await engine.dispose()


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    if not DATABASE_URL:
        if os.environ.get("CI"):
            pytest.fail("INGESTOR_TEST_DATABASE_URL fehlt: In der CI müssen diese Tests laufen.")
        pytest.skip("braucht PostgreSQL (INGESTOR_TEST_DATABASE_URL)")
    asyncio.run(_schema_anlegen())
    yield
    asyncio.run(_schema_entfernen())


async def _speicher(events_enabled: bool) -> DatabaseStorage:
    storage = DatabaseStorage(
        DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=events_enabled
    )
    # Eigenes Schema der Tests statt der Tabellen der Datenbank
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    return storage


@pytest.fixture
async def storage(schema: None) -> AsyncIterator[DatabaseStorage]:
    speicher = await _speicher(events_enabled=True)
    async with speicher._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    yield speicher
    await speicher.close()


class Bestand:
    """Eine Quelle mit einer Kommune und Hilfen, um Objekte wie der Abgleich zu schreiben."""

    def __init__(self, storage: DatabaseStorage) -> None:
        self.storage = storage
        self.processor = OParlProcessor()
        self.source_id: uuid.UUID
        self.body_id: uuid.UUID

    async def anlegen(self) -> Bestand:
        self.source_id = await self.storage.upsert_source(f"{BASE}/system", "Musterstadt")
        body = self.processor.process_body(
            {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}, BODY
        )
        self.body_id = await self.storage.upsert_body(body, self.source_id)
        return self

    async def paper(self, **felder: Any) -> uuid.UUID:
        daten: dict[str, Any] = {
            "id": PAPER,
            "type": "https://schema.oparl.org/1.1/Paper",
            "name": "Mehr Bänke im Park",
            "paperType": "Antrag",
            "modified": "2026-09-01T10:00:00+02:00",
        }
        daten.update(felder)
        return await self.storage.upsert_paper(self.processor.process_paper(daten, BODY), self.body_id)

    async def meeting(self, **felder: Any) -> uuid.UUID:
        daten: dict[str, Any] = {
            "id": MEETING,
            "type": "https://schema.oparl.org/1.1/Meeting",
            "name": "Rat",
            "start": "2026-10-01T17:00:00+02:00",
            "modified": "2026-09-01T10:00:00+02:00",
        }
        daten.update(felder)
        return await self.storage.upsert_meeting(self.processor.process_meeting(daten, BODY), self.body_id)

    async def ereignisse(self) -> list[dict[str, Any]]:
        async with self.storage.get_session() as session:
            result = await session.execute(text("SELECT *, xid::text AS xid_text FROM events_event ORDER BY id"))
            return [dict(row._mapping) for row in result]

    async def wert(self, sql: str, **parameter: Any) -> Any:
        async with self.storage.get_session() as session:
            return (await session.execute(text(sql), parameter)).scalar()


@pytest.fixture
async def bestand(storage: DatabaseStorage) -> Bestand:
    return await Bestand(storage).anlegen()


def top(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": ITEM,
        "type": "https://schema.oparl.org/1.1/AgendaItem",
        "number": "1",
        "name": "Mehr Bänke im Park",
        "public": True,
    }
    daten.update(felder)
    return daten


def datei(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": FILE,
        "type": "https://schema.oparl.org/1.1/File",
        "name": "Anlage 1",
        "accessUrl": f"{FILE}/download",
    }
    daten.update(felder)
    return daten


# --- Schalter ----------------------------------------------------------------------------------------


async def test_ohne_schalter_schreibt_der_ingestor_kein_ereignis(bestand: Bestand) -> None:
    aus = await _speicher(events_enabled=False)
    try:
        body = bestand.processor.process_body(
            {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}, BODY
        )
        body_id = await aus.upsert_body(body, bestand.source_id)
        await aus.upsert_paper(bestand.processor.process_paper({"id": PAPER, "name": "Bänke"}, BODY), body_id)
        await aus.upsert_paper(bestand.processor.process_paper({"id": PAPER, "name": "Mehr Bänke"}, BODY), body_id)
        assert await aus.mark_entity_deleted("paper", PAPER) is not None
    finally:
        await aus.close()
    assert await bestand.wert("SELECT count(*) FROM oparl_papers") == 1
    assert await bestand.ereignisse() == []


async def test_eingeschaltet_ohne_journal_bricht_der_start_ab(bestand: Bestand) -> None:
    """Ohne die Tabelle nähme jedes gescheiterte Ereignis seinen Upsert mit zurück."""
    await bestand.storage.initialize()
    async with bestand.storage._engine.begin() as conn:
        await conn.execute(text("ALTER TABLE events_event RENAME TO events_event_fehlt"))
    try:
        with pytest.raises(RuntimeError, match="events_event fehlt"):
            await bestand.storage.initialize()
        aus = await _speicher(events_enabled=False)
        try:
            await aus.initialize()
        finally:
            await aus.close()
    finally:
        async with bestand.storage._engine.begin() as conn:
            await conn.execute(text("ALTER TABLE events_event_fehlt RENAME TO events_event"))


# --- Upsert und Ereignis in einer Transaktion -------------------------------------------------------------


async def test_neue_vorlage_ereignis_in_der_transaktion_des_upserts(bestand: Bestand) -> None:
    vorher = datetime.now(UTC) - timedelta(seconds=5)
    paper_id = await bestand.paper()

    (ereignis,) = await bestand.ereignisse()
    assert paper_id == canonical_id(PAPER)
    assert (ereignis["type"], ereignis["version"]) == ("ris.paper.released", 1)
    assert (ereignis["aggregate_type"], ereignis["aggregate_id"]) == ("Paper", paper_id)
    assert ereignis["tenant_ref"] == f"source:{bestand.source_id}"
    assert ereignis["body_id"] == bestand.body_id
    assert (ereignis["visibility"], ereignis["operation"]) == ("oeffentlich", "upsert")
    assert ereignis["actor_ref"] == "system:ingestor"
    assert ereignis["causation_id"] is None
    assert ereignis["payload"] == {"paper": str(paper_id)}
    # Zeitpunkt laut Quelle (modified), erfasst wird beim Abgleich
    assert ereignis["occurred_at"] == datetime.fromisoformat("2026-09-01T10:00:00+02:00")
    assert ereignis["recorded_at"] >= vorher
    # Der Ingestor vergibt keine Folgenummer.
    assert ereignis["seq"] is None
    # Dieselbe Transaktion hat die Vorlage und das Ereignis geschrieben.
    assert await bestand.wert("SELECT xmin::text FROM oparl_papers WHERE id = :id", id=paper_id) == ereignis["xid_text"]


async def test_scheitert_das_ereignis_bleibt_auch_die_aenderung_aus(
    bestand: Bestand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beides oder nichts: Ein Ereignis, das sich nicht schreiben lässt, nimmt den Upsert mit zurück."""

    def ungueltig(paper_id: uuid.UUID, raw: Any, prior: Any) -> list[ris_events.Draft]:
        return [
            ris_events.Draft("ris.paper.released", "Paper", paper_id, {"paper": str(paper_id)}, visibility="geheim")
        ]

    monkeypatch.setattr(ris_events, "paper_events", ungueltig)
    with pytest.raises(events.InvalidEventError):
        await bestand.paper()

    assert await bestand.wert("SELECT count(*) FROM oparl_papers") == 0
    assert await bestand.ereignisse() == []


async def test_scheitert_der_commit_gibt_es_weder_aenderung_noch_ereignis(
    bestand: Bestand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Das Ereignis steht schon in der Transaktion; bricht sie vor dem Commit ab, ist es nie sichtbar."""
    geschrieben: list[int] = []
    original = events.publish_many

    async def schreiben_und_abbrechen(session: AsyncSession, neue: Any) -> list[uuid.UUID]:
        await original(session, neue)
        anzahl = (await session.execute(text("SELECT count(*) FROM events_event"))).scalar()
        geschrieben.append(int(anzahl or 0))
        raise RuntimeError("Abbruch vor dem Commit")

    monkeypatch.setattr(events, "publish_many", schreiben_und_abbrechen)
    with pytest.raises(RuntimeError, match="Abbruch"):
        await bestand.paper()

    # In der Transaktion war das Ereignis geschrieben, außerhalb ist nichts davon zu sehen.
    assert geschrieben == [1]
    assert await bestand.wert("SELECT count(*) FROM oparl_papers") == 0
    assert await bestand.ereignisse() == []


async def test_publish_in_der_sitzung_der_datenaenderung(bestand: Bestand) -> None:
    """Die Hilfe selbst: Änderung und Ereignis in einer Sitzung, festgeschrieben vom Aufrufer."""
    tenant = f"source:{bestand.source_id}"
    paper_id = uuid.uuid4()
    async with bestand.storage.get_session() as session:
        await session.execute(text("UPDATE oparl_bodies SET short_name = 'MS' WHERE id = :id"), {"id": bestand.body_id})
        event_id = await events.publish(
            session,
            "ris.paper.changed",
            aggregate_type="Paper",
            aggregate_id=paper_id,
            tenant_ref=tenant,
            body_id=bestand.body_id,
            visibility="oeffentlich",
            payload={"paper": str(paper_id), "changed": ["name"]},
        )
        await session.rollback()
    assert await bestand.ereignisse() == []
    assert await bestand.wert("SELECT short_name FROM oparl_bodies") is None

    async with bestand.storage.get_session() as session:
        await session.execute(text("UPDATE oparl_bodies SET short_name = 'MS' WHERE id = :id"), {"id": bestand.body_id})
        event_id = await events.publish(
            session,
            "ris.paper.changed",
            aggregate_type="Paper",
            aggregate_id=paper_id,
            tenant_ref=tenant,
            visibility="oeffentlich",
            payload={"paper": str(paper_id), "changed": ["name"]},
        )
        await session.commit()
    (ereignis,) = await bestand.ereignisse()
    assert ereignis["event_id"] == event_id
    assert await bestand.wert("SELECT short_name FROM oparl_bodies") == "MS"


# --- Nur echte Änderungen -----------------------------------------------------------------------------------


async def test_vollabgleich_ohne_aenderung_schreibt_kein_ereignis(bestand: Bestand) -> None:
    await bestand.paper()
    await bestand.paper()
    await bestand.paper(modified="2026-09-30T00:00:00+02:00")
    assert [e["type"] for e in await bestand.ereignisse()] == ["ris.paper.released"]
    # Der Upsert selbst läuft wie bisher: Der neue Zeitstempel der Quelle steht in der Zeile.
    assert await bestand.wert("SELECT raw_json->>'modified' FROM oparl_papers") == "2026-09-30T00:00:00+02:00"


async def test_geaenderte_vorlage_nennt_die_felder(bestand: Bestand) -> None:
    paper_id = await bestand.paper()
    await bestand.paper(name="Mehr Bänke", reference="A/1", modified="2026-09-02T09:00:00+02:00")

    _, ereignis = await bestand.ereignisse()
    assert ereignis["type"] == "ris.paper.changed"
    assert ereignis["payload"] == {"paper": str(paper_id), "changed": ["name", "reference"]}
    assert ereignis["occurred_at"] == datetime.fromisoformat("2026-09-02T09:00:00+02:00")


async def test_zeitpunkt_in_der_zukunft_wird_nicht_uebernommen(bestand: Bestand) -> None:
    vorher = datetime.now(UTC) - timedelta(seconds=5)
    await bestand.paper(modified="2099-01-01T00:00:00+00:00")
    (ereignis,) = await bestand.ereignisse()
    assert vorher <= ereignis["occurred_at"] <= datetime.now(UTC) + timedelta(seconds=5)


# --- Sitzung mit eingebetteten Objekten ------------------------------------------------------------------------


async def test_sitzung_mit_tagesordnung_und_datei(bestand: Bestand) -> None:
    meeting_id = await bestand.meeting(agendaItem=[top()], invitation=datei())

    ereignisse = await bestand.ereignisse()
    assert [e["type"] for e in ereignisse] == ["ris.meeting.scheduled", "ris.agendaitem.changed", "ris.file.changed"]
    sitzung, punkt, anlage = ereignisse
    assert sitzung["payload"] == {"meeting": str(meeting_id)}
    assert punkt["payload"] == {
        "agenda_item": str(canonical_id(ITEM)),
        "meeting": str(meeting_id),
        "change": "added",
    }
    assert anlage["payload"] == {"file": str(canonical_id(FILE)), "change": "added", "meeting": str(meeting_id)}
    assert {e["tenant_ref"] for e in ereignisse} == {f"source:{bestand.source_id}"}
    assert {e["body_id"] for e in ereignisse} == {bestand.body_id}
    # Jedes Objekt wird wie bisher in seiner eigenen Transaktion geschrieben, mit seinem Ereignis.
    assert len({e["xid_text"] for e in ereignisse}) == 3

    # Zweiter Abgleich ohne Änderung, einmal eingebettet, einmal aus der eigenen Liste (mit Rückverweis)
    await bestand.meeting(agendaItem=[top()], invitation=datei())
    einzeln = bestand.processor.process_agenda_item(top(meeting=MEETING), BODY)
    await bestand.storage.upsert_agenda_item(einzeln, meeting_id)
    assert len(await bestand.ereignisse()) == 3


async def test_nichtoeffentlicher_punkt_und_wechsel_der_sichtbarkeit(bestand: Bestand) -> None:
    meeting_id = await bestand.meeting(agendaItem=[top()])
    await bestand.meeting(agendaItem=[top(public=False, name="Personalangelegenheit")])

    ereignisse = await bestand.ereignisse()
    assert [(e["type"], e["visibility"], e["operation"]) for e in ereignisse] == [
        ("ris.meeting.scheduled", "oeffentlich", "upsert"),
        ("ris.agendaitem.changed", "oeffentlich", "upsert"),
        ("ris.meeting.changed", "oeffentlich", "upsert"),
        ("ris.object.depublished", "oeffentlich", "delete"),
        ("ris.agendaitem.changed", "nichtoeffentlich", "upsert"),
    ]
    ruecknahme, aenderung = ereignisse[3:]
    assert ruecknahme["payload"] == {
        "object_type": "AgendaItem",
        "object": str(canonical_id(ITEM)),
        "reason": "nichtoeffentlich",
    }
    assert aenderung["payload"] == {
        "agenda_item": str(canonical_id(ITEM)),
        "meeting": str(meeting_id),
        "change": "changed",
        "changed": ["name", "public"],
    }
    # Beide Ereignisse stehen in der Transaktion des Upserts.
    assert ruecknahme["xid_text"] == aenderung["xid_text"]
    # Kein Inhalt im Journal, auch nicht der Titel
    assert "Personalangelegenheit" not in repr(ereignisse)


# --- Vorlage mit Beratung und Datei ----------------------------------------------------------------------------


async def test_vorlage_mit_beratung_und_datei(bestand: Bestand) -> None:
    beratung = {
        "id": CONSULTATION,
        "type": "https://schema.oparl.org/1.1/Consultation",
        "role": "Vorberatung",
        "meeting": MEETING,
        "agendaItem": ITEM,
    }
    paper_id = await bestand.paper(consultation=[beratung], mainFile=datei())

    ereignisse = {e["type"]: e for e in await bestand.ereignisse()}
    assert set(ereignisse) == {"ris.paper.released", "ris.consultation.changed", "ris.file.changed"}
    assert ereignisse["ris.consultation.changed"]["payload"] == {
        "consultation": str(canonical_id(CONSULTATION)),
        "paper": str(paper_id),
        "change": "added",
        "meeting": str(canonical_id(MEETING)),
        "agenda_item": str(canonical_id(ITEM)),
    }
    assert ereignisse["ris.file.changed"]["payload"] == {
        "file": str(canonical_id(FILE)),
        "change": "added",
        "paper": str(paper_id),
    }

    await bestand.paper(consultation=[beratung], mainFile=datei(name="Anlage 1 (neu)"))
    neu = (await bestand.ereignisse())[3:]
    assert [(e["type"], e["payload"].get("change")) for e in neu] == [
        ("ris.paper.changed", None),
        ("ris.file.changed", "renamed"),
    ]
    assert neu[0]["payload"]["changed"] == ["mainFile"]


# --- Löschmarkierung ----------------------------------------------------------------------------------------------


async def test_loeschmarkierung_meldet_die_ruecknahme_in_derselben_transaktion(bestand: Bestand) -> None:
    paper_id = await bestand.paper()
    geloescht_am = datetime.fromisoformat("2026-09-03T12:00:00+02:00")
    assert await bestand.storage.mark_entity_deleted("paper", PAPER, modified=geloescht_am) == paper_id

    _, ereignis = await bestand.ereignisse()
    assert (ereignis["type"], ereignis["operation"], ereignis["visibility"]) == (
        "ris.object.depublished",
        "delete",
        "oeffentlich",
    )
    assert (ereignis["aggregate_type"], ereignis["aggregate_id"]) == ("Paper", paper_id)
    assert ereignis["payload"] == {"object_type": "Paper", "object": str(paper_id), "reason": "quelle_geloescht"}
    assert ereignis["occurred_at"] == geloescht_am
    assert ereignis["tenant_ref"] == f"source:{bestand.source_id}"
    assert await bestand.wert("SELECT xmin::text FROM oparl_papers WHERE id = :id", id=paper_id) == ereignis["xid_text"]

    # Schon markiert: keine zweite Meldung
    assert await bestand.storage.mark_entity_deleted("paper", PAPER) is None
    assert len(await bestand.ereignisse()) == 2

    # Die Quelle liefert das Objekt wieder: Es ist erneut veröffentlicht.
    await bestand.paper()
    assert [e["type"] for e in await bestand.ereignisse()][2:] == ["ris.paper.released"]


async def test_loeschmarkierung_eines_tagesordnungspunkts_findet_die_kommune_ueber_die_sitzung(
    bestand: Bestand,
) -> None:
    await bestand.meeting(agendaItem=[top()])
    bestand.storage.clear_uuid_caches()
    assert await bestand.storage.mark_entity_deleted("agendaitem", ITEM) == canonical_id(ITEM)

    ereignis = (await bestand.ereignisse())[-1]
    assert ereignis["payload"] == {
        "object_type": "AgendaItem",
        "object": str(canonical_id(ITEM)),
        "reason": "quelle_geloescht",
    }
    assert (ereignis["tenant_ref"], ereignis["body_id"]) == (f"source:{bestand.source_id}", bestand.body_id)


async def test_loeschmarkierung_von_gremium_person_und_mitgliedschaft(bestand: Bestand) -> None:
    """Auch Typen ohne eigenes Änderungsereignis melden ihre Rücknahme."""
    org_url, person_url, mitgliedschaft_url = f"{BASE}/organization/1", f"{BASE}/person/1", f"{BASE}/membership/1"
    verarbeiter = bestand.processor
    organisation = {"id": org_url, "type": "https://schema.oparl.org/1.1/Organization", "name": "Rat"}
    person = {"id": person_url, "type": "https://schema.oparl.org/1.1/Person", "name": "Ratsmitglied"}
    mitgliedschaft = {
        "id": mitgliedschaft_url,
        "type": "https://schema.oparl.org/1.1/Membership",
        "person": person_url,
        "organization": org_url,
    }
    await bestand.storage.upsert_organization(verarbeiter.process_organization(organisation, BODY), bestand.body_id)
    await bestand.storage.upsert_person(verarbeiter.process_person(person, BODY), bestand.body_id)
    assert await bestand.storage.upsert_membership(
        verarbeiter.process_membership(mitgliedschaft, BODY), bestand.body_id
    )
    # Für Änderungen an diesen Typen gibt es noch keinen Vertrag.
    assert await bestand.ereignisse() == []

    for typ, url in (("membership", mitgliedschaft_url), ("person", person_url), ("organization", org_url)):
        assert await bestand.storage.mark_entity_deleted(typ, url) == canonical_id(url)

    ereignisse = await bestand.ereignisse()
    assert [e["payload"]["object_type"] for e in ereignisse] == ["Membership", "Person", "Organization"]
    assert {e["type"] for e in ereignisse} == {"ris.object.depublished"}
    assert {e["body_id"] for e in ereignisse} == {bestand.body_id}


# --- Korrelation und Nebenläufigkeit ----------------------------------------------------------------------------


async def test_ereignisse_eines_abgleichs_tragen_dieselbe_korrelation(bestand: Bestand) -> None:
    vorgang = events.start_correlation()
    await bestand.meeting(agendaItem=[top()])
    await bestand.paper()
    assert {e["correlation_id"] for e in await bestand.ereignisse()} == {vorgang}


async def test_gleichzeitige_abgleiche_desselben_objekts_melden_die_aenderung_einmal(bestand: Bestand) -> None:
    """
    Der Vergleich gilt für genau den Stand, den der Upsert überschreibt (Zeilensperre bis zum Commit).

    Zwei Abgleiche schreiben gleichzeitig dieselbe neue Fassung. Der zweite wartet, bis der erste
    festgeschrieben hat, und findet die Vorlage dann schon geändert vor. Ohne Sperre vergliche er mit
    dem alten Stand und meldete dieselbe Änderung ein zweites Mal.
    """
    await bestand.paper()
    neu = {
        "id": PAPER,
        "type": "https://schema.oparl.org/1.1/Paper",
        "name": "Neue Fassung",
        "paperType": "Antrag",
        "modified": "2026-09-01T10:00:00+02:00",
    }
    erster = bestand.storage.get_session()
    async with erster:
        # Der erste Abgleich hat den bisherigen Stand gelesen und hält die Zeile.
        assert await bestand.storage._prior(erster, OParlPaper, PAPER) is not None
        zweiter = asyncio.create_task(bestand.paper(name="Neue Fassung"))
        await asyncio.sleep(0.5)
        assert not zweiter.done(), "Der zweite Abgleich muss auf die Zeilensperre warten"
        await erster.execute(
            text(
                "UPDATE oparl_papers SET name = 'Neue Fassung', raw_json = cast(:roh AS jsonb) WHERE external_id = :id"
            ),
            {"roh": json.dumps(neu), "id": PAPER},
        )
        await erster.commit()
    await zweiter

    # Nur die Anlage der Vorlage steht im Journal: Der zweite Abgleich fand nichts mehr zu ändern.
    assert [e["type"] for e in await bestand.ereignisse()] == ["ris.paper.released"]
    assert await bestand.wert("SELECT name FROM oparl_papers") == "Neue Fassung"
