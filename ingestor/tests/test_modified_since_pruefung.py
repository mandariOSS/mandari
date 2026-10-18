# SPDX-License-Identifier: AGPL-3.0-or-later
"""
modified_since nach jedem Vollabgleich neu prüfen.

Der Befund „Quelle filtert nicht mit modified_since“ hing früher dauerhaft am Host. Unterstützt die Quelle den Filter
später, liefen die inkrementellen Läufe weiter über die vollständigen Listen. Jetzt prüft eine Anfrage nach jedem
Vollabgleich neu, und das Ergebnis ändert nur die Vermerke der eigenen Quelle (Issue #1041). Antworten aus
httpx.MockTransport, keine Abrufe fremder Server.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.client.filter_notes import FilterNote, FilterNotes
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


def _server_vermerkt() -> FilterNotes:
    """Vermerke einer Quelle, die den Filter auf ``HOST`` ganz ignoriert."""
    return FilterNotes(hosts={HOST: FilterNote("Filter fehlt in den Listen-Links", datetime.now(UTC))})


class TestProbe:
    async def test_filtert(self):
        verdict, gesehen = await _probe(lambda r: _seite(r, [SINCE + timedelta(hours=2)]))
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert parse_qs(urlparse(gesehen[0]).query)["modified_since"] == ["2026-10-01T00:00:00+00:00"]

    async def test_auch_bei_bekanntem_befund_wird_der_parameter_gesendet(self):
        verdict, gesehen = await _probe(lambda r: _seite(r, []), filter_notes=_server_vermerkt())
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
        self.calls: list[tuple[str, str, str]] = []

    async def set_modified_since_check(self, source_url: str, host: str, verdict: str) -> None:
        self.calls.append((source_url, host, verdict))


async def _recheck(
    handler: Callable[[httpx.Request], httpx.Response], notes: FilterNotes | None = None
) -> tuple[str | None, _Speicher, FilterNotes]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    speicher = _Speicher()
    orchestrator.storage = speicher  # type: ignore[assignment]
    bodies = [{"id": f"https://{HOST}/oparl/bodies/1", "meeting": f"{LIST_URL}-m", "paper": LIST_URL}]
    async with OParlClient(max_concurrent=1, wait_time=0, filter_notes=notes) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        verdict = await orchestrator._recheck_modified_since(client, "https://rat.example.de/oparl/system", bodies)
    return verdict, speicher, client.filter_notes


class TestNachVollabgleich:
    async def test_host_wird_wieder_inkrementell_abgerufen(self):
        notes = _server_vermerkt()
        notes.note_list(LIST_URL, "HTTP 400")
        verdict, speicher, notes = await _recheck(lambda r: _seite(r, [datetime.now(UTC)]), notes)
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert notes.hosts == {} and notes.lists == {}
        assert notes.changed_hosts == {HOST: None} and notes.changed_lists == {LIST_URL: None}
        assert speicher.calls == [("https://rat.example.de/oparl/system", HOST, MODIFIED_SINCE_SUPPORTED)]

    async def test_ungefilterte_quelle_wird_eingetragen(self):
        verdict, speicher, notes = await _recheck(lambda r: _seite(r, [datetime(2019, 1, 1, tzinfo=UTC)]))
        assert verdict == MODIFIED_SINCE_IGNORED
        assert set(notes.hosts) == {HOST} and notes.lists == {}
        assert speicher.calls[0][2] == MODIFIED_SINCE_IGNORED

    async def test_abgelehnter_filter_vermerkt_nur_die_gepruefte_liste(self):
        """Lehnt die geprüfte Liste den Filter ab, gilt das für sie, nicht für den Server (Issue #1041)."""
        verdict, speicher, notes = await _recheck(lambda r: httpx.Response(400))
        assert verdict == MODIFIED_SINCE_UNSUPPORTED
        assert set(notes.lists) == {LIST_URL} and notes.hosts == {}
        assert speicher.calls[0][2] == MODIFIED_SINCE_UNSUPPORTED

    async def test_ohne_aussage_bleibt_der_befund(self, monkeypatch):
        monkeypatch.setattr("src.client.oparl_client.settings.oparl_max_retries", 1)
        monkeypatch.setattr(SyncOrchestrator, "MODIFIED_SINCE_RETRY_SECONDS", 0)
        anfragen: list[httpx.Request] = []
        verdict, speicher, notes = await _recheck(
            lambda r: anfragen.append(r) or httpx.Response(502), _server_vermerkt()
        )
        assert verdict == MODIFIED_SINCE_INCONCLUSIVE
        assert len(anfragen) == 2  # einmal wiederholt
        assert set(notes.hosts) == {HOST} and not notes.dirty
        assert speicher.calls == []

    async def test_einzelne_stoerung_haelt_veralteten_befund_nicht_fest(self, monkeypatch):
        """Münster, 15.10.2026: eine Nacht „keine Aussage“ ließ den falschen Vermerk wochenlang stehen."""
        monkeypatch.setattr("src.client.oparl_client.settings.oparl_max_retries", 1)
        monkeypatch.setattr(SyncOrchestrator, "MODIFIED_SINCE_RETRY_SECONDS", 0)
        antworten = iter([httpx.Response(502)])

        def handler(request: httpx.Request) -> httpx.Response:
            return next(antworten, None) or _seite(request, [datetime.now(UTC)])

        verdict, speicher, notes = await _recheck(handler, _server_vermerkt())
        assert verdict == MODIFIED_SINCE_SUPPORTED
        assert notes.hosts == {}
        assert speicher.calls[0][2] == MODIFIED_SINCE_SUPPORTED

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


async def test_storage_schreibt_nur_in_die_eigene_quelle(monkeypatch):
    """Prüfung und Vermerke ändern nur die ``sync_config`` der geprüften Quelle, keine andere (Issue #1041)."""
    from src.storage.database import DatabaseStorage

    configs: dict[str, dict[str, Any]] = {
        "https://a.example/system": {"anderes": 1, "modified_since_unsupported_hosts": [HOST]},
        "https://b.example/system": {"unfiltered_hosts": {HOST: {"reason": "x"}}},
    }
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")

    async def aendern(where: Any, change: Callable[[dict[str, Any]], bool]) -> None:
        url = where.right.value  # OParlSource.url == source_url
        kopie = dict(configs[url])
        if change(kopie):
            configs[url] = kopie

    monkeypatch.setattr(storage, "_change_sync_config", aendern)
    await storage.set_modified_since_check("https://a.example/system", HOST, MODIFIED_SINCE_SUPPORTED)
    await storage.save_filter_notes(
        "https://a.example/system", {LIST_URL: {"reason": "HTTP 400"}}, {HOST: None}, drop_legacy=True
    )

    quelle_a = configs["https://a.example/system"]
    assert quelle_a["modified_since_check"]["result"] == MODIFIED_SINCE_SUPPORTED
    assert quelle_a["unfiltered_lists"] == {LIST_URL: {"reason": "HTTP 400"}}
    assert "modified_since_unsupported_hosts" not in quelle_a and "unfiltered_hosts" not in quelle_a
    assert quelle_a["anderes"] == 1
    assert configs["https://b.example/system"] == {"unfiltered_hosts": {HOST: {"reason": "x"}}}
