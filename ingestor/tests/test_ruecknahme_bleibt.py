# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Abgleich hebt eine Rücknahme aus Datenschutzgründen nicht auf, und mit einer gelöschten Anlage verfällt die
KI-Zusammenfassung ihres Vorgangs.

Liefert die Quelle ein markiertes Objekt wieder, hebt der Upsert die Markierung auf: Die Quelle hat es wieder
eingestellt, oder Session hat es wieder veröffentlicht. Den Grund ``datenschutz`` setzt dagegen der Betrieb, gerade
weil die Quelle das Objekt weiter liefert. Diese Markierung bleibt über jeden Abgleich stehen, die Zeile schreibt
kein Ereignis, und ``oparl_modified`` bleibt der Zeitpunkt der Rücknahme.

Der erste Teil prüft die Anweisungen und den Ereignisweg ohne Datenbank. Der zweite läuft gegen PostgreSQL
(``INGESTOR_TEST_DATABASE_URL``, in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.storage import database, ris_events
from src.storage.database import DatabaseStorage
from src.storage.models import (
    Base,
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from src.sync.processor import OParlProcessor

OPARL = "https://schema.oparl.org/1.1/"
BASE = "https://ris.example.org/oparl"
KOMMUNE = f"{BASE}/body/1"
FRUEHER = "2026-10-01T10:00:00+02:00"
SPAETER = "2026-11-05T09:00:00+01:00"
RUECKNAHME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

MODELLE = (
    OParlBody,
    OParlMeeting,
    OParlPaper,
    OParlPerson,
    OParlOrganization,
    OParlAgendaItem,
    OParlFile,
    OParlLocation,
    OParlConsultation,
    OParlMembership,
    OParlLegislativeTerm,
)
MARKIERUNG = ("oparl_modified", "deleted", "deleted_at", "deletion_reason")


def _uebersetzt(stmt: Any) -> Any:
    """Die Anweisung, wie sie an PostgreSQL ginge (Text über ``str``, Werte unter ``params``)."""
    return stmt.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]


def _sql(stmt: Any) -> str:
    return re.sub(r"\s+", " ", str(_uebersetzt(stmt)))


# --- Anweisungen und Ereignisweg, ohne Datenbank -----------------------------------------------------------


@pytest.mark.parametrize("modell", MODELLE, ids=lambda modell: modell.__tablename__)
def test_baustein_erhaelt_die_markierung_nur_bei_datenschutz(modell: Any) -> None:
    tabelle = modell.__tablename__
    stmt = pg_insert(modell).values(id=uuid.uuid4(), external_id=f"{BASE}/x/1")
    stmt = stmt.on_conflict_do_update(index_elements=["external_id"], set_=database._tombstone_update(modell, stmt))
    sql = _sql(stmt)

    bedingung = f"CASE WHEN ({tabelle}.deleted AND {tabelle}.deletion_reason = %(deletion_reason_"
    for spalte in MARKIERUNG:
        # Jede der vier Spalten behält ihren Wert der Bestandszeile, solange die Rücknahme gilt
        assert re.search(rf"\b{spalte} = {re.escape(bedingung)}\d+\)s(::\w+)?\) THEN {tabelle}\.{spalte}\b", sql), (
            f"{tabelle}.{spalte}: {sql}"
        )
    assert "ELSE excluded.oparl_modified END" in sql
    assert ris_events.REASON_PRIVACY in _uebersetzt(stmt).params.values()


def test_alle_elf_upserts_nutzen_den_baustein() -> None:
    quelltext = (Path(database.__file__)).read_text(encoding="utf-8")
    assert quelltext.count("**_tombstone_update(") == len(MODELLE) == 11
    # Kein Upsert setzt die Markierung mehr unbedingt zurück
    assert '"deleted": False' not in quelltext
    assert '"deletion_reason": None' not in quelltext
    for modell in MODELLE:
        assert f"**_tombstone_update({modell.__name__}, stmt)" in quelltext


def test_stand_vor_dem_upsert_kennt_die_ruecknahme() -> None:
    assert ris_events.Prior(raw_json={}, deleted=True, deletion_reason=ris_events.REASON_PRIVACY).withheld
    # Andere Gründe hebt der Abgleich weiter auf: in der Quelle gelöscht, von Session zurückgenommen, ohne Grund
    for grund in (ris_events.REASON_DELETED_AT_SOURCE, ris_events.REASON_NOT_PUBLIC, "zurueckgenommen", None):
        assert not ris_events.Prior(raw_json={}, deleted=True, deletion_reason=grund).withheld
    # Hat der Betrieb die Markierung selbst aufgehoben, zählt ein stehen gebliebener Grund nicht
    assert not ris_events.Prior(raw_json={}, deleted=False, deletion_reason=ris_events.REASON_PRIVACY).withheld


class _Ergebnis:
    def __init__(self, zeile: tuple[Any, ...] | None) -> None:
        self._zeile = zeile

    def first(self) -> tuple[Any, ...] | None:
        return self._zeile

    def scalar_one_or_none(self) -> Any:
        return self._zeile[0] if self._zeile else None

    def scalar(self) -> Any:
        return self.scalar_one_or_none()

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _Sitzung:
    """Datenbanksitzung, die Anweisungen sammelt und auf die erste mit ``zeile`` antwortet."""

    def __init__(self, anweisungen: list[Any], zeile: tuple[Any, ...] | None) -> None:
        self.anweisungen = anweisungen
        self._zeile = zeile

    async def __aenter__(self) -> _Sitzung:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, *args: Any) -> _Ergebnis:
        self.anweisungen.append(stmt)
        return _Ergebnis(self._zeile if len(self.anweisungen) == 1 else None)

    async def commit(self) -> None:
        return None


def _speicher_ohne_datenbank(
    zeile: tuple[Any, ...] | None, *, events: bool = False
) -> tuple[DatabaseStorage, list[Any]]:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt", events_enabled=events)
    anweisungen: list[Any] = []
    storage.get_session = lambda: _Sitzung(anweisungen, zeile)  # type: ignore[method-assign,assignment,return-value]
    return storage, anweisungen


def _person(name: str = "Erika Muster", modified: str = FRUEHER) -> Any:
    daten = {"id": f"{BASE}/person/1", "type": f"{OPARL}Person", "name": name, "modified": modified}
    return OParlProcessor().process_person(daten, KOMMUNE)


async def test_upsert_einer_person_traegt_die_bedingung() -> None:
    storage, anweisungen = _speicher_ohne_datenbank((uuid.uuid4(),))

    await storage.upsert_person(_person(), uuid.uuid4())

    sql = _sql(anweisungen[0])
    assert "deleted = CASE WHEN (oparl_persons.deleted AND oparl_persons.deletion_reason = " in sql
    assert "deletion_reason = CASE WHEN (oparl_persons.deleted AND oparl_persons.deletion_reason = " in sql
    # Die übrigen Felder folgen weiter der Quelle
    assert "SET name = excluded.name" in sql


@pytest.mark.parametrize(
    ("grund", "meldet"),
    [
        (ris_events.REASON_PRIVACY, False),
        (ris_events.REASON_DELETED_AT_SOURCE, True),
        ("zurueckgenommen", True),
        (None, True),
    ],
)
async def test_zurueckgenommene_zeile_schreibt_kein_ereignis(
    grund: str | None, meldet: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, _ = _speicher_ohne_datenbank((uuid.uuid4(),), events=True)
    gemeldet: list[list[ris_events.Draft]] = []

    async def events_for(session: Any, body_id: Any) -> bool:
        return True

    async def prior(session: Any, model: Any, external_id: str) -> ris_events.Prior:
        return ris_events.Prior(raw_json={"name": "Erika Muster"}, deleted=True, deletion_reason=grund)

    async def emit(session: Any, body_id: Any, drafts: list[ris_events.Draft], modified: Any = None) -> None:
        gemeldet.append(drafts)

    monkeypatch.setattr(storage, "_events_for", events_for)
    monkeypatch.setattr(storage, "_prior", prior)
    monkeypatch.setattr(storage, "_emit", emit)

    await storage.upsert_person(_person(), uuid.uuid4())

    # Eine stehen gebliebene Markierung gälte sonst in jedem Abgleich als „wieder geliefert“
    assert bool(gemeldet and gemeldet[0]) is meldet


async def test_geloeschte_anlage_verwirft_die_zusammenfassung_des_vorgangs() -> None:
    datei, kommune, vorgang = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    storage, anweisungen = _speicher_ohne_datenbank((datei, kommune, vorgang))

    assert await storage.mark_entity_deleted("file", f"{BASE}/file/1", body_id=kommune) == datei

    assert len(anweisungen) == 2
    verwerfen = _uebersetzt(anweisungen[1])
    sql = _sql(anweisungen[1])
    assert sql.startswith("UPDATE oparl_papers SET ")
    assert "WHERE oparl_papers.id = " in sql
    werte = verwerfen.params
    assert vorgang in werte.values()
    assert (werte["summary"], werte["summary_einfach"]) == (None, None)
    assert (werte["summary_hinweis"], werte["summary_stand"], werte["summary_prompt_fassung"]) == ("", "", 0)


@pytest.mark.parametrize(
    ("art", "zeile"),
    [
        # Sitzungsdokument ohne Vorgang
        ("file", (uuid.uuid4(), uuid.uuid4(), None)),
        ("person", (uuid.uuid4(), uuid.uuid4())),
    ],
)
async def test_ohne_vorgang_bleibt_jede_zusammenfassung_stehen(art: str, zeile: tuple[Any, ...]) -> None:
    storage, anweisungen = _speicher_ohne_datenbank(zeile)

    assert await storage.mark_entity_deleted(art, f"{BASE}/{art}/1", body_id=uuid.uuid4()) == zeile[0]

    assert len(anweisungen) == 1


# --- Speicher gegen PostgreSQL -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_ruecknahme_{uuid.uuid4().hex[:12]}"


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
    body = OParlProcessor().process_body({"id": KOMMUNE, "type": f"{OPARL}Body", "name": "Musterstadt"}, KOMMUNE)
    body_id = await storage.upsert_body(body, source_id)
    yield storage, body_id
    await storage.close()


async def _markieren(storage: DatabaseStorage, modell: Any, zeile: uuid.UUID, grund: str | None) -> None:
    """Markierung, wie sie ``mark_deleted`` der Anwendung setzt: Zeitpunkt, Grund und ``oparl_modified``."""
    async with storage.get_session() as session:
        await session.execute(
            update(modell)
            .where(modell.id == zeile)
            .values(deleted=True, deleted_at=RUECKNAHME, deletion_reason=grund, oparl_modified=RUECKNAHME)
        )
        await session.commit()


async def _stand(storage: DatabaseStorage, modell: Any, zeile: uuid.UUID) -> tuple[Any, ...]:
    async with storage.get_session() as session:
        gefunden = await session.execute(
            select(modell.deleted, modell.deleted_at, modell.deletion_reason, modell.oparl_modified).where(
                modell.id == zeile
            )
        )
        return tuple(gefunden.one())


async def _schreiben(storage: DatabaseStorage, body_id: uuid.UUID, art: str, name: str, modified: str) -> Any:
    """Ein Objekt der Art wie im Abgleich verarbeiten und schreiben; liefert (Modell, UUID, verarbeitetes Objekt)."""
    processor = OParlProcessor()
    daten = {"id": f"{BASE}/{art}/1", "name": name, "modified": modified}
    objekt: Any
    if art == "person":
        objekt = processor.process_person({**daten, "type": f"{OPARL}Person"}, KOMMUNE)
        return OParlPerson, await storage.upsert_person(objekt, body_id), objekt
    if art == "organization":
        objekt = processor.process_organization({**daten, "type": f"{OPARL}Organization"}, KOMMUNE)
        return OParlOrganization, await storage.upsert_organization(objekt, body_id), objekt
    if art == "meeting":
        objekt = processor.process_meeting({**daten, "type": f"{OPARL}Meeting"}, KOMMUNE)
        return OParlMeeting, await storage.upsert_meeting(objekt, body_id), objekt
    if art == "paper":
        objekt = processor.process_paper({**daten, "type": f"{OPARL}Paper"}, KOMMUNE)
        return OParlPaper, await storage.upsert_paper(objekt, body_id), objekt
    objekt = processor.process_file(
        {**daten, "type": f"{OPARL}File", "accessUrl": f"{BASE}/file/1.pdf", "mimeType": "application/pdf"}, KOMMUNE
    )
    return OParlFile, await storage.upsert_file(objekt, body_id), objekt


ARTEN = ("person", "organization", "meeting", "paper", "file")


@pytest.mark.integration
@pytest.mark.parametrize("art", ARTEN)
async def test_abgleich_laesst_die_ruecknahme_aus_datenschutzgruenden_stehen(
    speicher: tuple[DatabaseStorage, uuid.UUID], art: str
) -> None:
    storage, body_id = speicher
    modell, zeile, _ = await _schreiben(storage, body_id, art, "Erste Fassung", FRUEHER)
    await _markieren(storage, modell, zeile, ris_events.REASON_PRIVACY)

    # Die Quelle liefert das Objekt weiter, mit neuerem Stand: wie im nächsten Vollabgleich
    _, wieder, _ = await _schreiben(storage, body_id, art, "Zweite Fassung", SPAETER)

    assert wieder == zeile
    assert await _stand(storage, modell, zeile) == (True, RUECKNAHME, ris_events.REASON_PRIVACY, RUECKNAHME)
    async with storage.get_session() as session:
        name = (await session.execute(select(modell.name).where(modell.id == zeile))).scalar_one()
    assert name == "Zweite Fassung"


@pytest.mark.integration
@pytest.mark.parametrize("art", ARTEN)
@pytest.mark.parametrize("grund", [ris_events.REASON_DELETED_AT_SOURCE, "zurueckgenommen", "nichtoeffentlich", None])
async def test_abgleich_hebt_andere_markierungen_weiter_auf(
    speicher: tuple[DatabaseStorage, uuid.UUID], art: str, grund: str | None
) -> None:
    storage, body_id = speicher
    modell, zeile, _ = await _schreiben(storage, body_id, art, "Erste Fassung", FRUEHER)
    await _markieren(storage, modell, zeile, grund)

    _, _, objekt = await _schreiben(storage, body_id, art, "Zweite Fassung", SPAETER)

    assert await _stand(storage, modell, zeile) == (False, None, None, objekt.oparl_modified)


@pytest.mark.integration
async def test_aufgehobene_ruecknahme_gilt_im_abgleich_nicht_mehr(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    storage, body_id = speicher
    modell, zeile, _ = await _schreiben(storage, body_id, "person", "Erste Fassung", FRUEHER)
    await _markieren(storage, modell, zeile, ris_events.REASON_PRIVACY)
    # Der Betrieb hebt die Markierung bewusst auf und lässt den Grund stehen (Verwaltungsformular)
    async with storage.get_session() as session:
        await session.execute(update(modell).where(modell.id == zeile).values(deleted=False))
        await session.commit()

    _, _, objekt = await _schreiben(storage, body_id, "person", "Zweite Fassung", SPAETER)

    assert await _stand(storage, modell, zeile) == (False, None, None, objekt.oparl_modified)


@pytest.mark.integration
async def test_stand_vor_dem_upsert_liest_den_grund(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    storage, body_id = speicher
    modell, zeile, objekt = await _schreiben(storage, body_id, "person", "Erika Muster", FRUEHER)

    async with storage.get_session() as session:
        assert not (await storage._prior(session, modell, objekt.external_id)).withheld  # type: ignore[union-attr]
    await _markieren(storage, modell, zeile, ris_events.REASON_PRIVACY)
    async with storage.get_session() as session:
        vorher = await storage._prior(session, modell, objekt.external_id)
    assert vorher is not None and vorher.withheld and vorher.deletion_reason == ris_events.REASON_PRIVACY


@pytest.mark.integration
async def test_tombstone_einer_anlage_verwirft_die_zusammenfassung(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    storage, body_id = speicher
    processor = OParlProcessor()
    vorgaenge: list[uuid.UUID] = []
    for nummer in (1, 2):
        paper = processor.process_paper(
            {"id": f"{BASE}/paper/{nummer}", "type": f"{OPARL}Paper", "name": f"Vorlage {nummer}"}, KOMMUNE
        )
        vorgaenge.append(await storage.upsert_paper(paper, body_id))
    async with storage.get_session() as session:
        await session.execute(
            update(OParlPaper).values(
                summary="Fasst die Anlage zusammen.",
                summary_einfach="Einfach gesagt.",
                summary_hinweis="Anbieter",
                summary_stand="abc",
                summary_prompt_fassung=3,
            )
        )
        await session.commit()
    datei = processor.process_file(
        {"id": f"{BASE}/file/1", "type": f"{OPARL}File", "name": "Anlage", "accessUrl": f"{BASE}/file/1.pdf"}, KOMMUNE
    )
    datei_id = await storage.upsert_file(datei, body_id, paper_id=vorgaenge[0])

    assert await storage.mark_entity_deleted("file", datei.external_id, body_id=body_id) == datei_id

    async with storage.get_session() as session:
        zeilen = await session.execute(
            select(
                OParlPaper.id,
                OParlPaper.summary,
                OParlPaper.summary_einfach,
                OParlPaper.summary_hinweis,
                OParlPaper.summary_stand,
                OParlPaper.summary_prompt_fassung,
            )
        )
        stand = {zeile[0]: tuple(zeile[1:]) for zeile in zeilen.all()}
    assert stand[vorgaenge[0]] == (None, None, "", "", 0)
    # Der Vorgang ohne diese Anlage behält seine Zusammenfassung
    assert stand[vorgaenge[1]] == ("Fasst die Anlage zusammen.", "Einfach gesagt.", "Anbieter", "abc", 3)
