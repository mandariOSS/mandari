# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sperr- und Störungserkennung im OParl-Client (Issue #123).

- HTTP 403 auf unseren User-Agent: genau eine Vergleichsanfrage mit neutralem
  Client-Header; antwortet der Server darauf mit 200, ist die Fehlerklasse
  ``ua_blocked``. Der Regelbetrieb läuft weiter mit unserem User-Agent.
- 5xx-Serien: ab N aufeinanderfolgenden Serverfehlern je Host gilt die Quelle
  als gestört (``server_error_series``) mit Statistik.
- Objektlisten, die nicht abrufbar sind, werden gemeldet statt still übergangen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.client.oparl_client import (
    ERROR_KIND_SERVER_ERROR_SERIES,
    ERROR_KIND_UA_BLOCKED,
    NEUTRAL_USER_AGENT,
    HostHealth,
    ListFetchError,
    OParlClient,
)
from src.config import DEFAULT_USER_AGENT, settings
from src.sync.orchestrator import SyncOrchestrator, SyncResult

SYSTEM_URL = "https://ris.example.org/oparl/system"
LIST_URL = "https://ris.example.org/oparl/bodies/1/meetings"


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-Backoff und Politeness-Pausen im Test nicht abwarten."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(settings, "oparl_server_error_series_threshold", 5)
    monkeypatch.setattr(settings, "oparl_ua_probe_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_enabled", False)
    OParlClient._modified_since_unsupported.clear()


class Recorder:
    """MockTransport-Handler, der gesehene User-Agents und Pfade festhält."""

    def __init__(self, respond: Callable[[httpx.Request, list[str]], httpx.Response]) -> None:
        self.agents: list[str] = []
        self.urls: list[str] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.agents.append(request.headers.get("User-Agent", ""))
        self.urls.append(str(request.url))
        return self._respond(request, self.agents)

    @property
    def neutral_requests(self) -> int:
        return sum(1 for agent in self.agents if agent == NEUTRAL_USER_AGENT)


async def make_client(handler: Recorder, **kwargs: Any) -> OParlClient:
    """Client mit MockTransport; ``async with`` würde einen echten AsyncClient bauen."""
    client = OParlClient(max_concurrent=2, **kwargs)
    client._semaphore = asyncio.Semaphore(2)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json", "User-Agent": client.user_agent},
    )
    return client


def ua_filter(request: httpx.Request, _agents: list[str]) -> httpx.Response:
    """Wie eine Quelle, die auf unseren Produkt-Token filtert: 403 für uns, 200 für andere."""
    if request.headers.get("User-Agent", "").startswith("mandari-ingestor/"):
        return httpx.Response(403, text="Forbidden")
    return httpx.Response(200, json={"id": SYSTEM_URL, "type": "https://schema.oparl.org/1.1/System"})


def always_forbidden(_request: httpx.Request, _agents: list[str]) -> httpx.Response:
    return httpx.Response(403, text="Forbidden")


def always_server_error(_request: httpx.Request, _agents: list[str]) -> httpx.Response:
    return httpx.Response(502, text="Bad Gateway")


# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------


def test_default_user_agent_is_identifiable_without_trigger_word() -> None:
    assert DEFAULT_USER_AGENT.startswith("mandari-ingestor/")
    assert "support@mandari.de" in DEFAULT_USER_AGENT
    assert "crawler" not in DEFAULT_USER_AGENT.lower()
    assert OParlClient().user_agent == settings.user_agent
    assert OParlClient(user_agent="  ").user_agent == settings.user_agent


async def test_user_agent_per_source_is_sent() -> None:
    recorder = Recorder(lambda _r, _a: httpx.Response(200, json={"type": "x"}))
    client = await make_client(recorder, user_agent="stadt-beispiel-abruf/1.0 (vereinbart)")
    try:
        await client.fetch(SYSTEM_URL, use_cache=False)
    finally:
        await client._client.aclose()
    assert recorder.agents == ["stadt-beispiel-abruf/1.0 (vereinbart)"]


# ---------------------------------------------------------------------------
# 403 auf User-Agent
# ---------------------------------------------------------------------------


async def test_403_with_ua_difference_is_classified_as_ua_blocked() -> None:
    recorder = Recorder(ua_filter)
    client = await make_client(recorder)
    try:
        result = await client.fetch(SYSTEM_URL, use_cache=False)
        # Zweiter Abruf am selben Host: keine zweite Vergleichsanfrage
        second = await client.fetch(LIST_URL, use_cache=False)
    finally:
        await client._client.aclose()

    assert result.status_code == 403
    assert result.data is None
    assert result.error_kind == ERROR_KIND_UA_BLOCKED
    assert "User-Agent gesperrt" in (result.error or "")
    assert second.error_kind == ERROR_KIND_UA_BLOCKED

    # Genau eine Vergleichsanfrage, der Regelbetrieb bleibt bei unserem User-Agent
    assert recorder.neutral_requests == 1
    assert recorder.agents[0].startswith("mandari-ingestor/")
    assert recorder.agents[-1].startswith("mandari-ingestor/")

    health = client.host_health["ris.example.org"]
    assert health.ua_blocked_at is not None
    assert health.ua_probe_status == 200
    assert health.forbidden_count == 2
    assert client.error_kind == ERROR_KIND_UA_BLOCKED
    assert "neutraler Client erhält HTTP 200" in health.describe()


async def test_403_for_everyone_is_not_a_ua_block() -> None:
    recorder = Recorder(always_forbidden)
    client = await make_client(recorder)
    try:
        result = await client.fetch(SYSTEM_URL, use_cache=False)
        await client.fetch(SYSTEM_URL, use_cache=False)
    finally:
        await client._client.aclose()

    assert result.status_code == 403
    assert result.error_kind is None
    assert "auch für neutralen Client" in (result.error or "")
    assert recorder.neutral_requests == 1  # nicht bei jedem 403 erneut vergleichen
    assert client.error_kind is None
    assert client.host_findings() == []


async def test_probe_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oparl_ua_probe_enabled", False)
    recorder = Recorder(ua_filter)
    client = await make_client(recorder)
    try:
        result = await client.fetch(SYSTEM_URL, use_cache=False)
    finally:
        await client._client.aclose()
    assert result.status_code == 403
    assert result.error_kind is None
    assert recorder.neutral_requests == 0


async def test_ua_block_is_not_mistaken_for_missing_modified_since_support() -> None:
    from datetime import UTC, datetime

    recorder = Recorder(ua_filter)
    client = await make_client(recorder)
    try:
        with pytest.raises(ListFetchError) as excinfo:
            async for _page in client.fetch_list(LIST_URL, modified_since=datetime(2026, 9, 1, tzinfo=UTC)):
                pass
    finally:
        await client._client.aclose()
    assert excinfo.value.error_kind == ERROR_KIND_UA_BLOCKED
    assert excinfo.value.list_url == LIST_URL
    assert "ris.example.org" not in OParlClient.get_modified_since_unsupported()


# ---------------------------------------------------------------------------
# 5xx-Serien
# ---------------------------------------------------------------------------


async def test_server_error_series_is_detected_with_statistics() -> None:
    recorder = Recorder(always_server_error)
    client = await make_client(recorder)
    try:
        first = await client.fetch(SYSTEM_URL, use_cache=False)  # 3 Versuche -> 3 Fehler
        second = await client.fetch(LIST_URL, use_cache=False)  # 6 Fehler >= Schwelle 5
    finally:
        await client._client.aclose()

    assert first.error_kind is None
    assert second.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
    assert second.status_code == 0
    assert recorder.neutral_requests == 0

    health = client.host_health["ris.example.org"]
    assert health.server_error_series
    assert health.consecutive_server_errors == 6
    assert health.server_error_total == 6
    assert list(health.last_status_codes) == [502] * 6
    assert health.server_error_series_started_at is not None
    assert health.last_server_error_at is not None

    text = health.describe()
    assert text.startswith("5xx-Serie: ris.example.org lieferte 6 Serverfehler in Folge")
    assert "502" in text
    assert client.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
    assert health.as_dict()["error_kind"] == ERROR_KIND_SERVER_ERROR_SERIES


async def test_successful_response_ends_the_series() -> None:
    def flaky(_request: httpx.Request, agents: list[str]) -> httpx.Response:
        if len(agents) <= 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"type": "x"})

    recorder = Recorder(flaky)
    client = await make_client(recorder)
    try:
        result = await client.fetch(SYSTEM_URL, use_cache=False)
    finally:
        await client._client.aclose()

    assert result.status_code == 200
    health = client.host_health["ris.example.org"]
    assert health.consecutive_server_errors == 0
    assert health.server_error_total == 2
    assert not health.server_error_series
    assert client.error_kind is None


async def test_failed_list_is_reported_not_swallowed() -> None:
    def respond(request: httpx.Request, _agents: list[str]) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"id": "m1"}], "links": {"next": LIST_URL + "?page=2"}})

    recorder = Recorder(respond)
    client = await make_client(recorder)
    pages: list[list[dict[str, Any]]] = []
    try:
        with pytest.raises(ListFetchError) as excinfo:
            async for page in client.fetch_list(LIST_URL):
                pages.append(page)
    finally:
        await client._client.aclose()

    assert pages == [[{"id": "m1"}]]  # erste Seite kam durch
    err = excinfo.value
    assert err.list_url == LIST_URL
    assert err.page_url == LIST_URL + "?page=2"
    assert LIST_URL in str(err) and "Seite" in str(err)
    assert client.host_health["ris.example.org"].failed_lists == [LIST_URL]


# ---------------------------------------------------------------------------
# Orchestrator: Befund in Laufergebnis und Quellenstatus
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str, str | None]] = []

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.failures.append((url, error, error_kind))


def orchestrator_with_fake_storage() -> tuple[SyncOrchestrator, FakeStorage]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    storage = FakeStorage()
    orchestrator.storage = storage  # type: ignore[assignment]
    return orchestrator, storage


async def test_orchestrator_records_ua_block_as_sync_warning() -> None:
    recorder = Recorder(ua_filter)
    client = await make_client(recorder)
    try:
        await client.fetch(SYSTEM_URL, use_cache=False)
    finally:
        await client._client.aclose()

    orchestrator, storage = orchestrator_with_fake_storage()
    result = SyncResult(source_url=SYSTEM_URL, source_name="Beispielstadt", success=True)
    await orchestrator._apply_host_findings(result, client)

    assert result.error_kind == ERROR_KIND_UA_BLOCKED
    assert any(e.startswith("User-Agent gesperrt") for e in result.errors)
    assert result.host_findings[0]["host"] == "ris.example.org"
    assert storage.failures == [(SYSTEM_URL, result.errors[0], ERROR_KIND_UA_BLOCKED)]
    details = orchestrator._finding_details(result)
    assert details["error_kind"] == ERROR_KIND_UA_BLOCKED
    assert details["host_findings"][0]["ua_probe_status"] == 200


async def test_orchestrator_names_affected_lists_for_server_error_series() -> None:
    recorder = Recorder(always_server_error)
    client = await make_client(recorder)
    try:
        for _ in range(2):
            with pytest.raises(ListFetchError):
                async for _page in client.fetch_list(LIST_URL):
                    pass
    finally:
        await client._client.aclose()

    orchestrator, storage = orchestrator_with_fake_storage()
    result = SyncResult(source_url=SYSTEM_URL, source_name="Beispielstadt", success=True)
    await orchestrator._apply_host_findings(result, client)

    assert result.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
    assert len(storage.failures) == 1
    _url, message, kind = storage.failures[0]
    assert kind == ERROR_KIND_SERVER_ERROR_SERIES
    assert "betroffene Listen: " + LIST_URL in message


async def test_orchestrator_without_findings_changes_nothing() -> None:
    orchestrator, storage = orchestrator_with_fake_storage()
    client = OParlClient()
    client.host_health["ris.example.org"] = HostHealth(host="ris.example.org", server_error_total=2)
    result = SyncResult(source_url=SYSTEM_URL, source_name="Beispielstadt", success=True)
    await orchestrator._apply_host_findings(result, client)
    assert result.error_kind is None
    assert result.errors == []
    assert storage.failures == []
    assert orchestrator._finding_details(result) == {}
