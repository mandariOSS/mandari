# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Keine Scheinänderungen (Issue #1025): Ein Abgleich ohne inhaltliche Änderung meldet nichts und schreibt nichts neu.

Beobachtet in Produktion (Vollabgleich 15.10.2026):

- Bezirksregierung Köln bettet Mitgliedschaften als ``…/1.0/Membership`` in die Person ein und liefert sie in der
  eigenen Liste als ``…/1.1/Membership``. Jeder Vollabgleich schrieb beide Fassungen und meldete jede der 809
  Mitgliedschaften zweimal mit ``"changed": ["type"]``.
- Ebenda tragen Tagesordnungspunkte ``body`` nur in der eigenen Liste, nicht eingebettet in der Sitzung
  (``"changed": ["body"]`` für 1.843 Punkte, je zweimal).
- Münster bettet in eine Vorlage zwei Beratungen unter derselben Kennung ein (``"changed": ["agendaItem",
  "meeting"]`` für 395 Beratungen, je zweimal).
- gremien.info liefert dieselbe Person in den Listen mehrerer Kommunen mit den Mitgliedschaften in anderer
  Reihenfolge; die Prüfsumme über die wörtliche Antwort schrieb sie in jedem Lauf neu (ohne Ereignis).

Antworten aus ``httpx.MockTransport``, nachgebildet und pseudonymisiert. Die Tests gegen PostgreSQL brauchen
``INGESTOR_TEST_DATABASE_URL`` (in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.client.oparl_client import OParlClient
from src.config import settings
from src.scrapers.base import CONTENT_HASH_FIELD
from src.storage import ris_events
from src.storage.database import DatabaseStorage, day_stamp_lists_of, missing_lists_of
from src.storage.models import Base
from src.sync.orchestrator import MissingLists, SyncOrchestrator
from src.sync.processor import OParlProcessor, unique_nested

T10 = "https://schema.oparl.org/1.0/"
T11 = "https://schema.oparl.org/1.1/"
HOST = "bezirk.example.org"
BASE = f"https://{HOST}/oparl/body/1"
SOURCE = f"https://{HOST}/oparl/system"
ORG = f"{BASE}/organization/1-10"
PERSON = f"{BASE}/person/2469"
MEMBERSHIP = f"{BASE}/membership/2902-1-10-2469"
MEETING = f"{BASE}/meeting/2355"
MEETING_ALT = f"{BASE}/meeting/2290"
TOP = f"{BASE}/agendaitem/5385"
TOP_ALT = f"{BASE}/agendaitem/5100"
PAPER = f"{BASE}/paper/4711"
CONSULTATION = f"{BASE}/consultation/91259"


# ---------------------------------------------------------------------------
# Objekte in beiden Fassungen
# ---------------------------------------------------------------------------


def mitgliedschaft_in_liste() -> dict[str, Any]:
    return {
        "id": MEMBERSHIP,
        "type": f"{T11}Membership",
        "role": "Beratende Mitglieder",
        "person": PERSON,
        "organization": ORG,
        "startDate": "2025-04-03",
        "votingRight": False,
        "created": "2025-04-03T15:02:30+02:00",
        "modified": "2025-04-03T15:02:30+02:00",
        "STERNBERG:sortierung": 7,
    }


def mitgliedschaft_eingebettet() -> dict[str, Any]:
    daten = mitgliedschaft_in_liste()
    for feld in ("person", "created", "modified"):
        daten.pop(feld)
    daten["type"] = f"{T10}Membership"
    return daten


def person() -> dict[str, Any]:
    return {
        "id": PERSON,
        "type": f"{T11}Person",
        "name": "Erika Musterfrau",
        "familyName": "Musterfrau",
        "givenName": "Erika",
        "body": "https://bezirk.example.org/oparl/body/1",
        "membership": [mitgliedschaft_eingebettet()],
        "created": "2021-06-21T08:36:52+02:00",
        "modified": "2026-09-22T12:04:40+02:00",
    }


def gremium() -> dict[str, Any]:
    return {
        "id": ORG,
        "type": f"{T11}Organization",
        "name": "Regionalrat",
        "created": "2021-06-21T08:36:52+02:00",
        "modified": "2026-07-13T09:11:35+02:00",
    }


def top_eingebettet() -> dict[str, Any]:
    return {
        "id": TOP,
        "type": f"{T11}AgendaItem",
        "name": "Begrüßung",
        "number": "1",
        "order": 1,
        "created": "2026-10-01T09:00:00+02:00",
        "modified": "2026-10-14T19:09:24+02:00",
    }


def top_in_liste() -> dict[str, Any]:
    return {**top_eingebettet(), "body": "https://bezirk.example.org/oparl/body/1", "meeting": MEETING}


def sitzung() -> dict[str, Any]:
    return {
        "id": MEETING,
        "type": f"{T11}Meeting",
        "name": "Sitzung des Regionalrats",
        "start": "2026-10-20T10:00:00+02:00",
        "organization": [ORG],
        "agendaItem": [top_eingebettet()],
        "created": "2026-10-01T09:00:00+02:00",
        "modified": "2026-10-14T19:09:24+02:00",
    }


def beratung(meeting: str, top: str, modified: str) -> dict[str, Any]:
    return {
        "id": CONSULTATION,
        "type": f"{T11}Consultation",
        "role": "Anregung",
        "paper": PAPER,
        "meeting": meeting,
        "agendaItem": top,
        "organization": [ORG],
        "authoritative": True,
        "created": "2026-05-28T14:22:12+02:00",
        "modified": modified,
    }


def vorlage() -> dict[str, Any]:
    # Münster: zwei Beratungen unter derselben Kennung, die ältere zuerst
    return {
        "id": PAPER,
        "type": f"{T11}Paper",
        "name": "Radweg an der Hauptstraße",
        "reference": "V/0815/2026",
        "consultation": [
            beratung(MEETING_ALT, TOP_ALT, "2026-06-10T09:51:38+02:00"),
            beratung(MEETING, TOP, "2026-09-09T08:19:57+02:00"),
        ],
        "created": "2026-05-28T14:22:12+02:00",
        "modified": "2026-09-09T08:19:57+02:00",
    }


LISTEN: dict[str, Any] = {
    "organization": gremium,
    "person": person,
    "membership": mitgliedschaft_in_liste,
    "meeting": sitzung,
    "agendaitem": top_in_liste,
    "paper": vorlage,
}


def kommune() -> dict[str, Any]:
    daten: dict[str, Any] = {"id": BASE, "type": f"{T11}Body", "name": "Bezirk"}
    for name in ("organization", "person", "membership", "meeting", "paper"):
        daten[name] = f"{BASE}/{name}"
    daten["agendaItem"] = f"{BASE}/agendaitem"
    return daten


def antwort(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[1]
    erzeugen = LISTEN.get(name)
    if erzeugen is None:
        return httpx.Response(404)
    return httpx.Response(200, json={"data": [erzeugen()], "links": {}})


# ---------------------------------------------------------------------------
# Vergleich ohne Datenbank
# ---------------------------------------------------------------------------


def test_mitgliedschaft_in_beiden_fassungen_ist_keine_aenderung():
    prior = ris_events.Prior(raw_json=mitgliedschaft_in_liste())
    assert ris_events.membership_events(uuid.uuid4(), mitgliedschaft_eingebettet(), prior) == []
    zurueck = ris_events.Prior(raw_json=mitgliedschaft_eingebettet())
    assert ris_events.membership_events(uuid.uuid4(), mitgliedschaft_in_liste(), zurueck) == []
    # Eine echte Änderung bleibt eine
    geaendert = {**mitgliedschaft_eingebettet(), "role": "Vorsitz"}
    [ereignis] = ris_events.membership_events(uuid.uuid4(), geaendert, prior)
    assert ereignis.payload["changed"] == ["role"]


def test_typ_zaehlt_nur_mit_dem_klassennamen():
    assert ris_events.differing_keys({"type": f"{T10}Membership"}, {"type": f"{T11}Membership"}) == []
    assert ris_events.differing_keys({"type": f"{T11}Membership"}, {"type": f"{T11}Person"}) == ["type"]
    # Auch eingebettet: TOPs einer Sitzung einmal als 1.0, einmal als 1.1
    alt = {"agendaItem": [{**top_eingebettet(), "type": f"{T10}AgendaItem"}]}
    assert ris_events.differing_keys(alt, {"agendaItem": [top_eingebettet()]}) == []
    # ALLRIS-Kürzel bleiben, wie sie sind
    assert ris_events.differing_keys({"type": "gr"}, {"type": "at"}) == ["type"]


def test_tagesordnungspunkt_mit_und_ohne_kommune_ist_keine_aenderung():
    prior = ris_events.Prior(raw_json=top_in_liste(), meeting_id=UUID(int=1))
    drafts = ris_events.agenda_item_events(UUID(int=2), top_eingebettet(), prior, meeting_id=UUID(int=1), public=True)
    assert drafts == []


def test_doppelte_eingebettete_beratung_wird_einmal_verarbeitet():
    paper = OParlProcessor().process_paper(vorlage(), BASE)
    beratungen = [e for e in paper.nested_entities if e.external_id == CONSULTATION]
    assert len(beratungen) == 1
    # Es bleibt die zuletzt geänderte Fassung
    assert beratungen[0].raw_json["meeting"] == MEETING
    # Auch wenn die Quelle die neuere zuerst nennt
    umgekehrt = vorlage()
    umgekehrt["consultation"].reverse()
    [einzige] = OParlProcessor().process_paper(umgekehrt, BASE).nested_entities
    assert einzige.raw_json["meeting"] == MEETING


def test_eindeutige_eingebettete_objekte_behalten_ihre_reihenfolge():
    processor = OParlProcessor()
    a = processor.process_consultation({"id": "a", "type": f"{T11}Consultation"}, BASE)
    b = processor.process_consultation({"id": "b", "type": f"{T11}Consultation"}, BASE)
    ohne = processor.process_consultation({"type": f"{T11}Consultation"}, BASE)
    a2 = processor.process_consultation({"id": "a", "type": f"{T11}Consultation", "role": "neu"}, BASE)
    assert [e.raw_json.get("role") for e in unique_nested([a, b, ohne, a2])] == ["neu", None, None]
    assert [e.external_id for e in unique_nested([a, b, ohne, a2])] == ["a", "b", ""]


# ---------------------------------------------------------------------------
# Inhaltsvergleich bei Quellen ohne verlässliche Zeitstempel (gremien.info)
# ---------------------------------------------------------------------------

GI = "https://mandant-a.gremien.example/oparl"
STEMPEL = "2026-10-15T00:00:00+02:00"  # Abrufdatum um Mitternacht: created == modified


def gi_mitgliedschaft(gremium: str) -> dict[str, Any]:
    return {
        "id": f"{GI}/membership/{gremium}.1063",
        "type": f"{T10}Membership",
        "person": f"{GI}/person/1063",
        "organization": f"{GI}/organization/{gremium}",
        "role": "Ratsmitglied",
        "votingRight": True,
        "created": STEMPEL,
        "modified": STEMPEL,
        "deleted": False,
        "more-software-gmbh:chairman": False,
    }


def gi_person(reihenfolge: list[str]) -> dict[str, Any]:
    return {
        "id": f"{GI}/person/1063",
        "type": f"{T10}Person",
        "name": "Max Mustermann",
        "created": STEMPEL,
        "modified": STEMPEL,
        "deleted": False,
        "membership": [gi_mitgliedschaft(g) for g in reihenfolge],
    }


def test_pruefsumme_ohne_reihenfolge_und_zeitstempel():
    ortsbezirk = gi_person(["600", "107", "202"])
    gemeinde = gi_person(["107", "600", "202"])
    assert ris_events.content_fingerprint(ortsbezirk) == ris_events.content_fingerprint(gemeinde)
    neu_gestempelt = copy.deepcopy(ortsbezirk)
    neu_gestempelt["modified"] = neu_gestempelt["created"] = "2026-10-16T00:00:00+02:00"
    neu_gestempelt[CONTENT_HASH_FIELD] = "sha256:alt"
    assert ris_events.content_fingerprint(neu_gestempelt) == ris_events.content_fingerprint(ortsbezirk)
    umbenannt = {**ortsbezirk, "name": "Max Mustermann-Beispiel"}
    assert ris_events.content_fingerprint(umbenannt) != ris_events.content_fingerprint(ortsbezirk)


class _HashSpeicher:
    def __init__(self) -> None:
        self.hashes: dict[str, str] = {}

    async def batch_check_entities_exist(self, entity_type: str, external_ids: list[str]) -> dict[str, Any]:
        return dict.fromkeys(external_ids, datetime(2026, 10, 15, tzinfo=UTC))

    async def get_entity_content_hashes(self, entity_type: str, external_ids: list[str]) -> dict[str, str | None]:
        return {eid: self.hashes.get(eid) for eid in external_ids}


async def test_person_in_mehreren_kommunen_wird_nicht_jedes_mal_neu_geschrieben():
    speicher = _HashSpeicher()
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    orchestrator.processor = OParlProcessor()
    geschrieben: list[str] = []

    async def speichern(entity: Any, body_id: UUID, entity_type: str, body_name: str | None = None) -> bool:
        geschrieben.append(entity.external_id)
        speicher.hashes[entity.external_id] = entity.raw_json[CONTENT_HASH_FIELD]
        return True

    orchestrator._store_entity = speichern  # type: ignore[method-assign]
    reihenfolgen = {"/oparl/body/6/person": ["600", "107", "202"], "/oparl/body/1/person": ["107", "600", "202"]}

    def quelle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [gi_person(reihenfolgen[request.url.path])], "links": {}})

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        for runde in range(2):
            for kommune_nr in (6, 1):
                await orchestrator._sync_entity_type(
                    client=client,
                    list_url=f"{GI}/body/{kommune_nr}/person",
                    entity_type="person",
                    body_id=UUID(int=kommune_nr),
                    body_external_id=f"{GI}/body/{kommune_nr}",
                    full=False,
                )
            # Nur die erste Liste der ersten Runde schreibt die (neue) Person, danach nichts mehr
            assert geschrieben == [f"{GI}/person/1063"], runde


# ---------------------------------------------------------------------------
# Vollabgleich zweimal gegen PostgreSQL: der zweite meldet nichts
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_schein_{uuid.uuid4().hex[:12]}"


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
            # Was in der Installation die Django-Migrationen anlegen (wie tests/test_events_journal.py)
            await conn.execute(
                text(
                    "ALTER TABLE events_event"
                    " ADD COLUMN seq bigint NULL UNIQUE,"
                    " ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now(),"
                    " ADD COLUMN xid xid8 NOT NULL DEFAULT pg_current_xact_id()"
                )
            )
            for tabelle, von, nach in (
                ("oparl_meetings_organizations", "oparlmeeting_id", "oparlorganization_id"),
                ("oparl_papers_locations", "oparlpaper_id", "oparllocation_id"),
            ):
                await conn.execute(
                    text(
                        f"CREATE TABLE {tabelle} (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,"
                        f" {von} uuid NOT NULL, {nach} uuid NOT NULL, UNIQUE ({von}, {nach}))"
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


@pytest.fixture
async def speicher(schema: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DatabaseStorage]:
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    storage = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=True)
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    yield storage
    await storage.close()


async def _ereignisse(storage: DatabaseStorage) -> list[tuple[str, Any]]:
    async with storage.get_session() as session:
        result = await session.execute(text("SELECT type, payload FROM events_event ORDER BY id"))
        return [(row.type, row.payload) for row in result]


async def _vollabgleich(storage: DatabaseStorage, source_id: UUID) -> dict[str, Any]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = storage
    orchestrator.processor = OParlProcessor(storage.id_bases)
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = True
    async with OParlClient(max_concurrent=2, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
        stats = await orchestrator._sync_body(
            client=client, body_data=kommune(), source_id=source_id, full=True, missing_lists=MissingLists(SOURCE)
        )
    storage.clear_uuid_caches()
    return stats


@pytest.mark.integration
async def test_vollabgleich_ohne_inhaltliche_aenderung_meldet_nichts(speicher: DatabaseStorage) -> None:
    source_id = await speicher.upsert_source(SOURCE, "Bezirk")

    stats = await _vollabgleich(speicher, source_id)
    assert stats["incomplete"] == []
    erste = await _ereignisse(speicher)
    assert {typ for typ, _ in erste} >= {"ris.body.changed", "ris.membership.changed", "ris.agendaitem.changed"}

    # Zweiter und dritter Vollabgleich mit denselben Antworten: kein einziges Ereignis
    async with speicher._engine.begin() as conn:
        await conn.execute(text("DELETE FROM events_event"))
    for _ in range(2):
        stats = await _vollabgleich(speicher, source_id)
        assert stats["incomplete"] == []
        assert await _ereignisse(speicher) == []

    # Eine echte Änderung meldet sich weiterhin
    alt = LISTEN["membership"]
    LISTEN["membership"] = lambda: {**alt(), "role": "Vorsitz"}
    try:
        await _vollabgleich(speicher, source_id)
    finally:
        LISTEN["membership"] = alt
    geaendert = [p for typ, p in await _ereignisse(speicher) if typ == "ris.membership.changed"]
    assert [p.get("changed") for p in geaendert] == [["role"]]


@pytest.mark.integration
async def test_zeitpunkt_und_vermerke_in_sync_config(speicher: DatabaseStorage) -> None:
    source_id = await speicher.upsert_source(SOURCE, "Bezirk")
    async with speicher._engine.begin() as conn:
        await conn.execute(
            text("UPDATE oparl_sources SET sync_config = CAST(:c AS jsonb)"), {"c": '{"request_interval": 2.0}'}
        )
    beginn = datetime(2026, 10, 15, 12, 30, 15, 987654, tzinfo=UTC)
    geprueft = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    # Alle Schreiber gleichzeitig: Jeder liest unter Zeilensperre, keiner überschreibt den anderen (Issue #1025)
    await asyncio.gather(
        speicher.set_body_sync_started(source_id, BASE, beginn),
        speicher.set_body_sync_started(source_id, f"{BASE}-2", beginn),
        speicher.set_list_missing(SOURCE, f"{BASE}/membership", True, checked_at=geprueft),
        speicher.add_modified_since_unsupported_hosts(SOURCE, {"ris.example.org"}),
        speicher.update_scraper_state(SOURCE, {"runs": 1}),
        speicher.set_source_sync_config_value(source_id, "probe", {"system": "oparl"}),
    )
    assert await speicher.get_body_sync_started(source_id, BASE) == beginn.replace(microsecond=0)
    assert await speicher.get_body_sync_started(source_id, f"{BASE}-2") == beginn.replace(microsecond=0)
    assert await speicher.get_body_sync_started(source_id, "unbekannt") is None
    await speicher.set_list_missing(SOURCE, f"{BASE}/agendaitem", True, checked_at=geprueft)
    await speicher.set_list_missing(SOURCE, f"{BASE}/membership", False)
    await speicher.set_body_sync_started(source_id, f"{BASE}-2", None)
    quelle = await speicher.get_source_by_url(SOURCE)
    assert quelle is not None
    assert quelle.sync_config["missing_lists"] == {f"{BASE}/agendaitem": "2026-10-15T13:00:00+00:00"}
    assert missing_lists_of(quelle.sync_config) == {f"{BASE}/agendaitem": geprueft}
    assert quelle.sync_config["request_interval"] == 2.0  # andere Einstellungen bleiben
    assert quelle.sync_config["modified_since_unsupported_hosts"] == ["ris.example.org"]
    assert quelle.sync_config["scraper_state"] == {"runs": 1}
    assert quelle.sync_config["probe"] == {"system": "oparl"}
    assert set(quelle.sync_config["body_sync_started"]) == {BASE}
    await speicher.set_list_missing(SOURCE, f"{BASE}/agendaitem", False)
    await speicher.set_body_sync_started(source_id, BASE, None)
    quelle = await speicher.get_source_by_url(SOURCE)
    assert quelle is not None
    assert "missing_lists" not in quelle.sync_config and "body_sync_started" not in quelle.sync_config

    # Listen mit Tagesstempeln: Der Zeitpunkt der ersten Erkennung bleibt, bis der Vermerk gelöscht wird
    await speicher.set_list_day_stamped(SOURCE, f"{BASE}/meeting", True)
    quelle = await speicher.get_source_by_url(SOURCE)
    assert quelle is not None
    erkannt = quelle.sync_config["day_stamp_lists"][f"{BASE}/meeting"]
    await speicher.set_list_day_stamped(SOURCE, f"{BASE}/meeting", True)
    quelle = await speicher.get_source_by_url(SOURCE)
    assert quelle is not None and quelle.sync_config["day_stamp_lists"] == {f"{BASE}/meeting": erkannt}
    assert day_stamp_lists_of(quelle.sync_config) == {f"{BASE}/meeting"}
    await speicher.set_list_day_stamped(SOURCE, f"{BASE}/meeting", False)
    quelle = await speicher.get_source_by_url(SOURCE)
    assert quelle is not None and "day_stamp_lists" not in quelle.sync_config
