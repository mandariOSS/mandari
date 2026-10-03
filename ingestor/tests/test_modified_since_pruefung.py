# SPDX-License-Identifier: AGPL-3.0-or-later
"""
modified_since nach jedem Vollabgleich neu prüfen.

Der Befund „Quelle filtert nicht mit modified_since“ hing bisher dauerhaft am Host (Capability-Cache, in
``sync_config`` aller Quellen). Unterstützt die Quelle den Filter später, liefen die inkrementellen Läufe
weiter über die vollständigen Listen. Jetzt prüft eine Anfrage nach jedem Vollabgleich neu. Antworten aus
httpx.MockTransport, keine Abrufe fremder Server.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.client.oparl_client import (
    MODIFIED_SINCE_IGNORED,
    MODIFIED_SINCE_INCONCLUSIVE,
    MODIFIED_SINCE_SUPPORTED,
    MODIFIED_SINCE_UNSUPPORTED,
    OParlClient,
)
from src.sync.orchestrator import SyncOrchestrator

HOST = "rat.example.de"
LIST_URL = f"https://{HOST}/oparl/bodies/1/papers"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def _seite(request: httpx.Request, modified: list[datetime], filter_in_links: bool = True) -> httpx.Response:
    link = str(request.url) if filter_in_links else LIST_URL
    return httpx.Response(
        200,
        json={
            "data": [{"id": f"{LIST_URL}/{i}", "modified": m.isoformat()} for i, m in enumerate(modified)],
            "links": {"self": link, "next": link + ("&" if "?" in link else "?") + "page=2"},
        },
    )


async def _probe(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> tuple[str, list[str]]:
    gesehen: list[str] = []

    def aufzeichnen(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return handler(request)

    async with OParlClient(max_concurrent=1, wait_time=0, **kwargs) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(aufzeichnen))
        verdict = await client.probe_modified_since(LIST_URL, SINCE)
    return verdict, gesehen


@pytest.fixture(autouse=True)
def _capability_cache() -> Iterator[None]:
    saved = OParlClient.get_modified_since_unsupported()
    OParlClient._modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(saved)


class TestProbe:
    async def test_filtert(self):
        verdict, gesehen = await _probe(lambda r: _seite(r, [SINCE + timedelta(hours=2)]))
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert parse_qs(urlparse(gesehen[0]).query)["modified_since"] == ["2026-10-01T00:00:00+00:00"]

    async def test_auch_bei_bekanntem_befund_wird_der_parameter_gesendet(self):
        OParlClient.add_modified_since_unsupported({HOST})
        verdict, gesehen = await _probe(lambda r: _seite(r, []))
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert "modified_since=" in gesehen[0]

    @pytest.mark.parametrize("status", [400, 401, 403])
    async def test_lehnt_ab(self, status):
        verdict, _ = await _probe(lambda r: httpx.Response(status))
        assert verdict == MODIFIED_SINCE_UNSUPPORTED

    async def test_liefert_ungefiltert(self):
        verdict, _ = await _probe(lambda r: _seite(r, [SINCE + timedelta(hours=1), datetime(2019, 5, 1, tzinfo=UTC)]))
        assert verdict == MODIFIED_SINCE_IGNORED

    async def test_filter_fehlt_in_den_links(self):
        verdict, _ = await _probe(lambda r: _seite(r, [], filter_in_links=False))
        assert verdict == MODIFIED_SINCE_IGNORED

    async def test_filter_fehlt_in_den_links_mit_carry_schalter(self):
        verdict, _ = await _probe(lambda r: _seite(r, [], filter_in_links=False), carry_modified_since=True)
        assert verdict == MODIFIED_SINCE_SUPPORTED

    async def test_serverfehler_ist_keine_aussage(self, monkeypatch):
        monkeypatch.setattr("src.client.oparl_client.settings.oparl_max_retries", 1)
        verdict, _ = await _probe(lambda r: httpx.Response(503))
        assert verdict == MODIFIED_SINCE_INCONCLUSIVE


class _Speicher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bool]] = []

    async def apply_modified_since_check(self, source_url: str, host: str, verdict: str, supported: bool) -> None:
        self.calls.append((source_url, host, verdict, supported))


async def _recheck(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[str | None, _Speicher]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    speicher = _Speicher()
    orchestrator.storage = speicher  # type: ignore[assignment]
    bodies = [{"id": f"https://{HOST}/oparl/bodies/1", "meeting": f"{LIST_URL}-m", "paper": LIST_URL}]
    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        verdict = await orchestrator._recheck_modified_since(client, "https://rat.example.de/oparl/system", bodies)
    return verdict, speicher


class TestNachVollabgleich:
    async def test_host_wird_wieder_inkrementell_abgerufen(self):
        OParlClient.add_modified_since_unsupported({HOST})
        verdict, speicher = await _recheck(lambda r: _seite(r, [datetime.now(UTC)]))
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert HOST not in OParlClient.get_modified_since_unsupported()
        assert speicher.calls == [("https://rat.example.de/oparl/system", HOST, MODIFIED_SINCE_SUPPORTED, True)]

    async def test_ungefilterte_quelle_wird_eingetragen(self):
        verdict, speicher = await _recheck(lambda r: _seite(r, [datetime(2019, 1, 1, tzinfo=UTC)]))
        assert verdict == MODIFIED_SINCE_IGNORED
        assert HOST in OParlClient.get_modified_since_unsupported()
        assert speicher.calls[0][2:] == (MODIFIED_SINCE_IGNORED, False)

    async def test_ohne_aussage_bleibt_der_befund(self, monkeypatch):
        monkeypatch.setattr("src.client.oparl_client.settings.oparl_max_retries", 1)
        OParlClient.add_modified_since_unsupported({HOST})
        verdict, speicher = await _recheck(lambda r: httpx.Response(502))
        assert verdict == MODIFIED_SINCE_INCONCLUSIVE
        assert HOST in OParlClient.get_modified_since_unsupported()
        assert speicher.calls == []

    async def test_fehler_gefaehrden_den_abgleich_nicht(self):
        def kaputt(_request: httpx.Request) -> httpx.Response:
            raise RuntimeError("unerwartet")

        orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
        orchestrator.storage = _Speicher()  # type: ignore[assignment]
        async with OParlClient(max_concurrent=1, wait_time=0) as client:
            client.probe_modified_since = _wirft  # type: ignore[method-assign]
            assert await orchestrator._recheck_modified_since(client, "x", [{"paper": LIST_URL}]) is None


async def _wirft(*_args: Any, **_kwargs: Any) -> str:
    raise RuntimeError("unerwartet")


async def test_storage_traegt_host_bei_allen_quellen_aus(monkeypatch):
    """Der Capability-Cache ist die Vereinigung über alle Quellen: austragen muss überall wirken."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from src.storage.database import DatabaseStorage

    quellen = [
        SimpleNamespace(url="https://a.example/system", sync_config={"modified_since_unsupported_hosts": [HOST, "x"]}),
        SimpleNamespace(url="https://b.example/system", sync_config={"modified_since_unsupported_hosts": [HOST]}),
        SimpleNamespace(url="https://c.example/system", sync_config={"anderes": 1}),
    ]

    class _Sitzung:
        committed = False

        async def execute(self, _stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: quellen))

        async def commit(self) -> None:
            _Sitzung.committed = True

    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")

    @asynccontextmanager
    async def sitzung() -> Any:
        yield _Sitzung()

    monkeypatch.setattr(storage, "get_session", sitzung)
    await storage.apply_modified_since_check("https://a.example/system", HOST, MODIFIED_SINCE_SUPPORTED, True)

    assert quellen[0].sync_config["modified_since_unsupported_hosts"] == ["x"]
    assert quellen[0].sync_config["modified_since_check"]["result"] == MODIFIED_SINCE_SUPPORTED
    assert "modified_since_unsupported_hosts" not in quellen[1].sync_config
    assert quellen[2].sync_config == {"anderes": 1}
    assert _Sitzung.committed

    await storage.apply_modified_since_check("https://c.example/system", HOST, MODIFIED_SINCE_UNSUPPORTED, False)
    assert quellen[2].sync_config["modified_since_unsupported_hosts"] == [HOST]
