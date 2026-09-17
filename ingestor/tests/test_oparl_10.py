# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-1.0-Kompatibilität (Issue #122): more! rubin auf gremien.info.

Die Fixtures unter tests/fixtures/oparl10/ sind eingefrorene, gekürzte und
pseudonymisierte Antworten eines gremien.info-Mandanten (Host ersetzt durch
mandant-a.gremien.example, Personen erfunden, Dokumenttexte gekürzt).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.client.oparl_client import OParlClient, SyncStats
from src.client.oparl_compat import (
    detect_oparl_version,
    is_oparl_error,
    modified_since_dropped,
    oparl_10_fallback_urls,
    oparl_error_message,
    timestamps_unreliable,
)
from src.scrapers.base import CONTENT_HASH_FIELD
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor

FIXTURES = Path(__file__).parent / "fixtures" / "oparl10"
HOST = "https://mandant-a.gremien.example"
BODY_ID = f"{HOST}/oparl/body/KH"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fake-Server: verhält sich wie more! rubin (OParl 1.0)
# ---------------------------------------------------------------------------


class FakeGremienInfo:
    """
    Simuliert die beobachteten Eigenheiten:
    - /oparl/system und /oparl liefern das System, /oparl/v1.1/... ein
      Fehlerobjekt mit HTTP 200
    - Listen ignorieren modified_since (Links ohne den Parameter)
    - Paper-Liste hat zwei Seiten (page/1 -> page/2)
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.paper_overrides: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        if path.startswith("/oparl/v1.1/"):
            return httpx.Response(200, json=load("error_class_missing.json"))
        if path in ("/oparl/system", "/oparl"):
            return httpx.Response(200, json=load("system.json"))
        if path == "/oparl/Body":
            return httpx.Response(200, json=load("body_list.json"))
        if path in ("/oparl/body/KH/paper", "/oparl/body/KH/paper/page/1"):
            page = load("paper_page.json")
            for item in page["data"]:
                item.update(self.paper_overrides.get(item["id"], {}))
            return httpx.Response(200, json=page)
        if path == "/oparl/body/KH/paper/page/2":
            page = load("paper_page.json")
            page["data"] = []
            page["links"] = {
                "first": f"{HOST}/oparl/body/KH/paper/page/1",
                "self": f"{HOST}/oparl/body/KH/paper/page/2",
            }
            return httpx.Response(200, json=page)
        if path in ("/oparl/body/KH/meeting", "/oparl/body/KH/meeting/page/1"):
            page = load("meeting_page.json")
            page["links"].pop("next")
            return httpx.Response(200, json=page)
        if path in ("/oparl/body/KH/organization", "/oparl/body/KH/organization/page/1"):
            return httpx.Response(200, json=load("organization_page.json"))
        return httpx.Response(404, json={"error": "not found"})


def make_client(server: FakeGremienInfo) -> OParlClient:
    """OParlClient ohne Netzwerk: MockTransport statt echtem AsyncClient."""
    client = OParlClient(max_concurrent=2, wait_time=0)
    client._semaphore = asyncio.Semaphore(2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    client.stats = SyncStats()
    client._circuit_breakers = {}
    return client


class FakeStorage:
    """Nur die Methoden, die _sync_entity_type im inkrementellen Lauf braucht."""

    def __init__(self) -> None:
        self.modified: dict[str, object] = {}
        self.hashes: dict[str, str] = {}

    async def batch_check_entities_exist(self, entity_type, external_ids):
        return {eid: self.modified.get(eid) for eid in external_ids}

    async def get_entity_content_hashes(self, entity_type, external_ids):
        return {eid: self.hashes.get(eid) for eid in external_ids}

    async def get_person_ids_by_external_ids(self, ids):
        return {}

    async def get_organization_ids_by_external_ids(self, ids):
        return {}


def make_orchestrator(storage: FakeStorage) -> SyncOrchestrator:
    """Orchestrator ohne Datenbank-Engine."""
    orch = SyncOrchestrator.__new__(SyncOrchestrator)
    orch.storage = storage
    orch.processor = OParlProcessor()
    orch.max_concurrent = 1
    orch._parallel_mode = False
    orch._event_emitter = None
    return orch


@pytest.fixture
def isolated_capability_cache():
    """Der modified_since-Capability-Cache ist prozessweit — je Test zurücksetzen."""
    before = OParlClient.get_modified_since_unsupported()
    OParlClient._modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(before)


# ---------------------------------------------------------------------------
# Hilfsfunktionen (oparl_compat)
# ---------------------------------------------------------------------------


class TestCompatHelpers:
    def test_version_from_system_and_type(self):
        assert detect_oparl_version(load("system.json")) == "1.0"
        assert detect_oparl_version(load("body_list.json")) == "1.0"
        assert detect_oparl_version({"type": "https://schema.oparl.org/1.1/Body"}) == "1.1"
        assert detect_oparl_version({"name": "ohne Typ"}) is None
        assert detect_oparl_version([]) is None

    def test_error_object_detection(self):
        error = load("error_class_missing.json")
        assert is_oparl_error(error)
        assert oparl_error_message(error) == "Requested class doesn't exist."
        assert is_oparl_error({"error": 'Webservice "OParl" ist nicht aktiviert!'})
        assert not is_oparl_error(load("system.json"))
        assert not is_oparl_error(None)

    def test_fallback_urls_strip_version_segment(self):
        assert oparl_10_fallback_urls(f"{HOST}/oparl/v1.1/system") == [f"{HOST}/oparl/system", f"{HOST}/oparl"]
        assert oparl_10_fallback_urls("https://ris.example/webservice/oparl/1.1/system") == [
            "https://ris.example/webservice/oparl/system",
            "https://ris.example/webservice/oparl",
        ]
        # Bereits versionslos: nur noch die Wurzel probieren
        assert oparl_10_fallback_urls(f"{HOST}/oparl/system") == [f"{HOST}/oparl"]
        assert oparl_10_fallback_urls(f"{HOST}/oparl") == []

    def test_modified_since_dropped_from_links(self):
        assert modified_since_dropped(load("paper_page.json")["links"])
        assert not modified_since_dropped({"next": f"{HOST}/x?modified_since=2026-09-01T00:00:00%2B02:00&page=2"})
        assert not modified_since_dropped({"first": f"{HOST}/x"})
        assert not modified_since_dropped(None)

    def test_synthetic_timestamps(self):
        paper = load("paper_page.json")["data"][0]
        assert timestamps_unreliable(paper)  # created == modified == Mitternacht
        assert timestamps_unreliable(load("organization_page.json")["data"][0])  # keine Stempel
        assert not timestamps_unreliable(
            {"created": "2026-09-01T10:00:00+02:00", "modified": "2026-09-02T11:30:00+02:00"}
        )
        assert not timestamps_unreliable(
            {"created": "2026-09-01T00:00:00+02:00", "modified": "2026-09-02T00:00:00+02:00"}
        )


# ---------------------------------------------------------------------------
# Client: Fehlerobjekte, ignoriertes modified_since, Paginierung
# ---------------------------------------------------------------------------


class TestClientOParl10:
    async def test_error_object_yields_no_items(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        pages = [page async for page in client.fetch_list(f"{HOST}/oparl/v1.1/body/KH/paper")]
        assert pages == []
        assert OParlClient._extract_items(load("error_class_missing.json")) == []

    async def test_modified_since_ignored_is_detected_and_cached(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        from datetime import UTC, datetime

        since = datetime(2026, 9, 10, tzinfo=UTC)
        items = await client.fetch_list_all(f"{HOST}/oparl/body/KH/paper", modified_since=since)

        assert len(items) == 3
        assert "modified_since=" in server.requests[0]
        # Folgeseite über links.next, ohne den ignorierten Parameter
        assert server.requests[1] == f"{HOST}/oparl/body/KH/paper/page/2"
        assert "mandant-a.gremien.example" in OParlClient.get_modified_since_unsupported()

        # Zweite Liste desselben Hosts sendet den Parameter gar nicht mehr
        server.requests.clear()
        await client.fetch_list_all(f"{HOST}/oparl/body/KH/meeting", modified_since=since)
        assert "modified_since" not in server.requests[0]

    async def test_body_list_via_system(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        bodies = await client.fetch_list_all(load("system.json")["body"])
        assert [b["id"] for b in bodies] == [BODY_ID]


# ---------------------------------------------------------------------------
# Autodiscovery: 1.1-Pfad -> Fehlerobjekt -> Rückfall auf 1.0
# ---------------------------------------------------------------------------


class TestAutoDetect:
    async def test_fallback_from_1_1_path(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        orch = make_orchestrator(FakeStorage())

        url_type, bodies, version = await orch.auto_detect_url(client, f"{HOST}/oparl/v1.1/system")

        assert url_type == "system"
        assert version == "1.0"
        assert [b["id"] for b in bodies] == [BODY_ID]
        assert server.requests == [
            f"{HOST}/oparl/v1.1/system",
            f"{HOST}/oparl/system",
            f"{HOST}/oparl/Body",
        ]

    async def test_direct_1_0_system_url(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        orch = make_orchestrator(FakeStorage())

        url_type, bodies, version = await orch.auto_detect_url(client, f"{HOST}/oparl/system")
        assert (url_type, version, len(bodies)) == ("system", "1.0", 1)

    async def test_unresolvable_url_raises(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        orch = make_orchestrator(FakeStorage())

        with pytest.raises(ValueError, match="Could not fetch OParl endpoint"):
            await orch.auto_detect_url(client, f"{HOST}/other/v1.1/system")
        # Kandidaten wurden probiert, dann sauber aufgegeben
        assert server.requests == [f"{HOST}/other/v1.1/system", f"{HOST}/other/system", f"{HOST}/other"]

    async def test_body_field_as_list_of_urls(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        orch = make_orchestrator(FakeStorage())
        bodies = await orch._fetch_bodies(client, [f"{HOST}/oparl/Body", {"id": "embedded", "type": "x/Body"}])
        assert [b["id"] for b in bodies] == [BODY_ID, "embedded"]


# ---------------------------------------------------------------------------
# Parsing der 1.0-Feldvarianten
# ---------------------------------------------------------------------------


class TestProcessorOParl10:
    def test_body_lists_and_embedded_terms(self):
        body = OParlProcessor().process_body(load("body_list.json")["data"][0])
        assert body.name == "Stadt Musterstadt"
        assert body.paper_list_url == f"{BODY_ID}/paper"
        assert body.meeting_list_url == f"{BODY_ID}/meeting"
        assert body.person_list_url == f"{BODY_ID}/person"
        assert body.organization_list_url == f"{BODY_ID}/organization"
        # 1.0 kennt keine Body-weiten Listen für Membership/AgendaItem/File
        assert body.membership_list_url is None
        assert body.agenda_item_list_url is None
        assert body.file_list_url is None
        terms = [e for e in body.nested_entities if e.oparl_type.name == "LEGISLATIVE_TERM"]
        assert [t.name for t in terms] == ["Legi", "Wahl"]

    def test_paper_with_embedded_files_and_consultation(self):
        paper = OParlProcessor().process_paper(load("paper_page.json")["data"][2], BODY_ID)
        assert paper.reference == "18/401"
        assert paper.paper_type == "Anträge und Anfragen"
        assert str(paper.date) == "2018-11-13"
        kinds = sorted(e.oparl_type.name for e in paper.nested_entities)
        assert kinds == ["CONSULTATION", "FILE", "FILE"]
        assert paper.references["files"][0] == f"{HOST}/oparl/file/01311100340"

    def test_meeting_with_embedded_location(self):
        meeting = OParlProcessor().process_meeting(load("meeting_page.json")["data"][0], BODY_ID)
        assert meeting.meeting_state == "terminiert"
        assert meeting.start is not None and meeting.start.isoformat() == "2026-12-17T17:30:00+01:00"
        assert meeting.location_name == "Stadtwerke Musterstadt"
        assert meeting.references["organization"] == [f"{HOST}/oparl/organization/GVSWK"]

    def test_person_with_embedded_membership_and_list_fields(self):
        person = OParlProcessor().process_person(load("person_page.json")["data"][0], BODY_ID)
        assert person.name == "Erika Musterfrau"
        assert person.email == "erika.musterfrau@musterstadt.example"  # Liste -> erster Wert
        assert person.phone is None  # leere Liste
        memberships = [e for e in person.nested_entities if e.oparl_type.name == "MEMBERSHIP"]
        assert len(memberships) == 1
        assert memberships[0].person_external_id == person.external_id
        assert memberships[0].role == "Ratsmitglied"

    def test_organization_without_timestamps(self):
        org = OParlProcessor().process_organization(load("organization_page.json")["data"][0], BODY_ID)
        assert org.short_name == "WBKSE"
        assert org.organization_type == "Gremium"
        assert org.oparl_modified is None


# ---------------------------------------------------------------------------
# Inkrementeller Lauf: Content-Hash statt synthetischer Zeitstempel
# ---------------------------------------------------------------------------


class TestIncrementalSyncWithSyntheticTimestamps:
    async def _run(self, orch, client, full=False):
        stored: list = []

        async def fake_store(entity, body_id, entity_type, body_name=None):
            stored.append(entity)
            return True

        orch._store_entity = AsyncMock(side_effect=fake_store)
        from datetime import UTC, datetime
        from uuid import uuid4

        total = await orch._sync_entity_type(
            client=client,
            list_url=f"{BODY_ID}/paper",
            entity_type="paper",
            body_id=uuid4(),
            body_external_id=BODY_ID,
            body_name="Musterstadt",
            modified_since=None if full else datetime(2026, 9, 10, tzinfo=UTC),
            full=full,
        )
        return total, stored

    def _remember(self, storage: FakeStorage, stored) -> None:
        """Simuliert die Persistenz: Hash und modified landen im raw_json bzw. in der Tabelle."""
        for entity in stored:
            storage.hashes[entity.external_id] = entity.raw_json[CONTENT_HASH_FIELD]
            storage.modified[entity.external_id] = entity.oparl_modified

    async def test_second_run_skips_unchanged_and_detects_content_change(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        storage = FakeStorage()
        orch = make_orchestrator(storage)

        # Lauf 1: alles neu, Content-Hash wird mitgeschrieben
        total, stored = await self._run(orch, client)
        assert total == 3
        assert all(CONTENT_HASH_FIELD in e.raw_json for e in stored)
        self._remember(storage, stored)

        # Lauf 2: identischer Inhalt, synthetische Stempel -> nichts upserten
        total, stored = await self._run(orch, client)
        assert (total, stored) == (0, [])

        # Lauf 3: eine Vorlage inhaltlich geändert -> genau diese wird aktualisiert
        changed = f"{HOST}/oparl/paper/00811100331"
        server.paper_overrides[changed] = {"name": "Beispielvorlage 2 (geändert)"}
        total, stored = await self._run(orch, client)
        assert total == 1
        assert [e.external_id for e in stored] == [changed]

    async def test_full_sync_writes_content_hash(self, isolated_capability_cache):
        server = FakeGremienInfo()
        client = make_client(server)
        orch = make_orchestrator(FakeStorage())

        total, stored = await self._run(orch, client, full=True)
        assert total == 3
        assert all(CONTENT_HASH_FIELD in e.raw_json for e in stored)
