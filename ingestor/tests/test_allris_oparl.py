# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-Schnittstelle von ALLRIS unter ``/oparl/`` (beobachtet im Oktober 2026) und Abrufoptionen je Quelle.

Die Antworten sind nachgebaut (erfundener Host, erfundene Inhalte) und bilden nur die Eigenheiten ab:
Organisationen mit ``"type": "gr"`` und der Typ-URL in ``Type``, Beratungen mit ``agendaitem``,
``modified_since`` filtert, fehlt aber in ``links.next``, Seitengröße über ``size``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mandari_oparl import OParlType, ProcessedConsultation, ProcessedOrganization

from src.client.oparl_client import OParlClient, SyncStats
from src.client.oparl_compat import consultation_agenda_item, oparl_type_url
from src.client.source_options import MAX_REQUEST_INTERVAL, SourceFetchOptions
from src.config import settings
from src.extraction.extractor import TextExtractor
from src.sync.processor import OParlProcessor

HOST = "https://www.rat.example"
BASE = f"{HOST}/oparl"
BODY = f"{BASE}/bodies/1"
ORG_TYPE = "https://schema.oparl.org/1.1/Organization"
SINCE = datetime(2026, 9, 25, tzinfo=UTC)


@pytest.fixture
def isolated_capability_cache():
    """Der modified_since-Capability-Cache ist prozessweit — je Test zurücksetzen."""
    before = OParlClient.get_modified_since_unsupported()
    OParlClient._modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(before)


# ---------------------------------------------------------------------------
# Typ-URL und Tagesordnungspunkt
# ---------------------------------------------------------------------------


def gremium(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": f"{BASE}/organizations/gr/1",
        "Type": ORG_TYPE,
        "type": "gr",
        "body": BODY,
        "name": "Rat",
        "organizationType": "Gremium",
        "created": "2000-01-01T00:00:00+01:00",
        "modified": "2000-01-01T00:00:00+01:00",
        "deleted": False,
    }
    daten.update(felder)
    return daten


class TestTypUrl:
    def test_type_mit_kuerzel_nimmt_type_gross(self):
        assert oparl_type_url(gremium()) == ORG_TYPE
        assert oparl_type_url(gremium(type="at")) == ORG_TYPE

    def test_regulaere_typ_url_hat_vorrang(self):
        assert oparl_type_url({"type": ORG_TYPE, "Type": "https://schema.oparl.org/1.1/Paper"}) == ORG_TYPE

    def test_ohne_typ_url_bleibt_der_wert(self):
        assert oparl_type_url({"type": "gr"}) == "gr"
        assert oparl_type_url({"type": "gr", "Type": "Gremium"}) == "gr"
        assert oparl_type_url({}) == ""
        assert oparl_type_url(None) == ""

    def test_prozessor_verarbeitet_gremium_mit_kuerzel(self):
        processor = OParlProcessor()
        assert processor.get_type(gremium()) is OParlType.ORGANIZATION
        entity = processor.process(gremium(), BODY)
        assert isinstance(entity, ProcessedOrganization)
        assert (entity.external_id, entity.name, entity.organization_type) == (
            f"{BASE}/organizations/gr/1",
            "Rat",
            "Gremium",
        )

    def test_unbekannter_typ_wird_weiter_uebersprungen(self):
        assert OParlProcessor().process({"id": f"{BASE}/x/1", "type": "gr"}, BODY) is None


class TestBeratung:
    def test_agendaitem_klein_geschrieben(self):
        daten = {
            "id": f"{BASE}/consultations/7",
            "type": "https://schema.oparl.org/1.1/Consultation",
            "paper": f"{BASE}/papers/3",
            "meeting": f"{BASE}/meetings/5",
            "agendaitem": f"{BASE}/agendaItems/9",
        }
        entity = OParlProcessor().process(daten, BODY)
        assert isinstance(entity, ProcessedConsultation)
        assert entity.agenda_item_external_id == f"{BASE}/agendaItems/9"
        assert entity.meeting_external_id == f"{BASE}/meetings/5"

    def test_spezifikation_hat_vorrang(self):
        daten = {"agendaItem": f"{BASE}/agendaItems/1", "agendaitem": f"{BASE}/agendaItems/2"}
        assert consultation_agenda_item(daten) == f"{BASE}/agendaItems/1"
        assert consultation_agenda_item({"agendaItem": ""}) is None
        assert consultation_agenda_item({}) is None


# ---------------------------------------------------------------------------
# Abrufoptionen je Quelle
# ---------------------------------------------------------------------------


class TestSourceFetchOptions:
    def test_ohne_konfiguration_gelten_die_standards(self):
        for config in (None, {}, [], "x"):
            options = SourceFetchOptions.from_sync_config(config)
            assert options == SourceFetchOptions()
            assert options.file_downloads is True
            assert options.client_kwargs() == {
                "list_params": {},
                "carry_modified_since": False,
                "robots_override": None,
            }

    def test_gueltige_werte(self):
        options = SourceFetchOptions.from_sync_config(
            {
                "request_interval": 0.5,
                "list_params": {"size": 100},
                "carry_modified_since": True,
                "file_downloads": False,
            }
        )
        assert options.request_interval == 0.5
        assert options.list_params == {"size": "100"}
        assert options.carry_modified_since is True
        assert options.file_downloads is False
        assert options.client_kwargs() == {
            "list_params": {"size": "100"},
            "carry_modified_since": True,
            "robots_override": None,
            "request_interval": 0.5,
        }

    def test_ungueltige_werte_gelten_als_nicht_gesetzt(self):
        options = SourceFetchOptions.from_sync_config(
            {
                "request_interval": MAX_REQUEST_INTERVAL + 1,
                "list_params": {"size": True, "modified_since": "2026-01-01", "bad key": "1", "limit": "x" * 101},
                "carry_modified_since": "ja",
                "file_downloads": "nein",
            }
        )
        assert options == SourceFetchOptions()
        assert SourceFetchOptions.from_sync_config({"request_interval": True}).request_interval is None
        assert SourceFetchOptions.from_sync_config({"request_interval": -1}).request_interval is None
        assert SourceFetchOptions.from_sync_config({"request_interval": 0}).request_interval == 0.0


# ---------------------------------------------------------------------------
# Client gegen eine nachgebaute ALLRIS-Schnittstelle
# ---------------------------------------------------------------------------


class FakeAllris:
    """
    Liste ``/oparl/bodies/1/papers`` mit 25 Einträgen, aufsteigend sortiert, Seitengröße ``size`` (Standard 10).

    Mit ``modified_since`` enthält die Liste nur die Einträge 21–25, die Links verlieren den Filter aber
    (wie ALLRIS): ``links.next`` = ``?page=N&size=S``.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        query = parse_qs(request.url.query.decode())
        size = int(query.get("size", ["10"])[0])
        page = int(query.get("page", ["1"])[0])
        ids = list(range(21, 26)) if "modified_since" in query else list(range(1, 26))
        chunk = ids[(page - 1) * size : page * size]
        total_pages = max(1, -(-len(ids) // size))
        links = {"self": f"{BASE}/bodies/1/papers?page={page}&size={size}"}
        if page < total_pages:
            links["next"] = f"{BASE}/bodies/1/papers?page={page + 1}&size={size}"
        return httpx.Response(
            200,
            json={
                "data": [{"id": f"{BASE}/papers/{i}", "type": "https://schema.oparl.org/1.1/Paper"} for i in chunk],
                "pagination": {"totalElements": len(ids), "elementsPerPage": size, "currentPage": page},
                "links": links,
            },
        )


def make_client(server: FakeAllris, **kwargs: Any) -> OParlClient:
    client = OParlClient(max_concurrent=1, wait_time=0, **kwargs)
    client._semaphore = asyncio.Semaphore(1)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    client.stats = SyncStats()
    client._circuit_breakers = {}
    return client


def parallel_client(zeiten: list[float], *, max_concurrent: int, **kwargs: Any) -> OParlClient:
    """Client mit mehreren Abrufplätzen; die Schnittstelle notiert den Beginn jeder Anfrage."""

    def handler(request: httpx.Request) -> httpx.Response:
        zeiten.append(time.monotonic())
        return httpx.Response(200, json={"id": str(request.url)})

    client = OParlClient(max_concurrent=max_concurrent, **kwargs)
    client._semaphore = asyncio.Semaphore(max_concurrent)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.stats = SyncStats()
    client._circuit_breakers = {}
    return client


def ids_of(items: list[dict[str, Any]]) -> list[int]:
    return [int(item["id"].rsplit("/", 1)[1]) for item in items]


class TestClientAllris:
    async def test_wartezeit_null_gilt(self):
        assert OParlClient(wait_time=0).wait_time == 0
        assert OParlClient().wait_time == settings.oparl_wait_time

    async def test_listenparameter_nur_auf_der_ersten_seite_und_ohne_ueberschreiben(self, isolated_capability_cache):
        server = FakeAllris()
        client = make_client(server, list_params={"size": "100"})
        items = await client.fetch_list_all(f"{BASE}/bodies/1/papers")
        assert ids_of(items) == list(range(1, 26))
        assert server.requests == [f"{BASE}/bodies/1/papers?size=100"]

        server.requests.clear()
        await client.fetch_list_all(f"{BASE}/bodies/1/papers?size=5")
        assert parse_qs(urlparse(server.requests[0]).query)["size"] == ["5"]

    async def test_ohne_schalter_gilt_die_quelle_als_ohne_filter(self, isolated_capability_cache):
        server = FakeAllris()
        client = make_client(server, list_params={"size": "2"})
        items = await client.fetch_list_all(f"{BASE}/bodies/1/papers", modified_since=SINCE)
        # Erste Seite gefiltert, danach die ungefilterte Liste ab Seite 2 (bisheriges Verhalten)
        assert ids_of(items)[:4] == [21, 22, 3, 4]
        assert "www.rat.example" in OParlClient.get_modified_since_unsupported()

    async def test_carry_haengt_den_filter_an_jede_folgeseite(self, isolated_capability_cache):
        server = FakeAllris()
        client = make_client(server, list_params={"size": "2"}, carry_modified_since=True)
        items = await client.fetch_list_all(f"{BASE}/bodies/1/papers", modified_since=SINCE)

        assert ids_of(items) == [21, 22, 23, 24, 25]
        assert len(server.requests) == 3
        assert all("modified_since" in parse_qs(urlparse(u).query) for u in server.requests)
        assert [parse_qs(urlparse(u).query).get("page", ["1"])[0] for u in server.requests] == ["1", "2", "3"]
        assert "www.rat.example" not in OParlClient.get_modified_since_unsupported()

    async def test_abstand_gilt_ueber_alle_parallelen_abrufe(self):
        """``request_interval``: Anfragen beginnen nacheinander im Abstand, auch bei mehreren Abrufplätzen."""
        zeiten: list[float] = []
        client = parallel_client(zeiten, max_concurrent=5, request_interval=0.1)
        try:
            ergebnisse = await asyncio.gather(
                *(client.fetch(f"{BASE}/papers/{i}", use_cache=False) for i in range(6)),
                # skip_wait umgeht nur die Wartezeit je Platz, nicht den Abstand der Quelle
                client.fetch(f"{BASE}/system", use_cache=False, skip_wait=True),
            )
        finally:
            assert client._client is not None
            await client._client.aclose()

        assert [r.status_code for r in ergebnisse] == [200] * 7
        abstaende = [b - a for a, b in zip(zeiten, zeiten[1:], strict=False)]
        assert len(abstaende) == 6
        assert min(abstaende) >= 0.09
        assert zeiten[-1] - zeiten[0] >= 0.59

    async def test_ohne_abstand_wartet_jeder_platz_fuer_sich(self):
        """Bisheriges Verhalten ohne ``request_interval``: ``wait_time`` je Abrufplatz, Plätze parallel."""
        zeiten: list[float] = []
        client = parallel_client(zeiten, max_concurrent=5, wait_time=0.1)
        try:
            await asyncio.gather(*(client.fetch(f"{BASE}/papers/{i}", use_cache=False) for i in range(5)))
        finally:
            assert client._client is not None
            await client._client.aclose()
        assert len(zeiten) == 5
        assert max(zeiten) - min(zeiten) < 0.09

    async def test_carry_gilt_auch_bei_frueher_vermerktem_host(self, isolated_capability_cache):
        OParlClient.add_modified_since_unsupported({"www.rat.example"})
        server = FakeAllris()
        client = make_client(server, carry_modified_since=True)
        items = await client.fetch_list_all(f"{BASE}/bodies/1/papers", modified_since=SINCE)
        assert ids_of(items) == [21, 22, 23, 24, 25]
        assert "modified_since" in parse_qs(urlparse(server.requests[0]).query)


# ---------------------------------------------------------------------------
# Dateiabruf je Quelle abschaltbar
# ---------------------------------------------------------------------------


class _Storage:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.claimed = 0

    async def file_downloads_enabled_for_body(self, body_id: Any) -> bool:
        return self.enabled

    async def get_pending_files(self, body_id: Any, batch_size: int, max_size_bytes: int | None, **_: Any) -> list[Any]:
        self.claimed += 1
        return []


class TestDateiabruf:
    async def test_abgeschaltete_quelle_beansprucht_keine_dateien(self):
        storage = _Storage(enabled=False)
        assert await TextExtractor(storage).extract_pending_files(uuid.uuid4()) == 0
        assert storage.claimed == 0, "Dateien bleiben offen und werden später nachgeholt"

    async def test_eingeschaltete_quelle_laeuft_wie_bisher(self):
        storage = _Storage(enabled=True)
        assert await TextExtractor(storage).extract_pending_files(uuid.uuid4()) == 0
        assert storage.claimed >= 1

    async def test_storage_ohne_schalter_laeuft_wie_bisher(self):
        class Alt:
            claimed = 0

            async def get_pending_files(
                self, body_id: Any, batch_size: int, max_size_bytes: int | None, **_: Any
            ) -> list:
                Alt.claimed += 1
                return []

        await TextExtractor(Alt()).extract_pending_files(uuid.uuid4())
        assert Alt.claimed >= 1
