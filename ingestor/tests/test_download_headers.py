"""
Issue #116: Download-Header je Quelle in der Textextraktion und robots-Sperre als
Fehlerklasse mit täglicher Schonung – ohne Datenbank (Storage nachgestellt).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.client.oparl_client import ERROR_KIND_ROBOTS_BLOCKED
from src.extraction.extractor import TextExtractor
from src.sync.orchestrator import BACKOFF_MIN_MINUTES_BY_KIND, source_backoff_until


class _Storage:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers, self.abfragen = headers, 0

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        self.abfragen += 1
        return self.headers


@pytest.mark.asyncio
async def test_extractor_sendet_quellen_header_und_cached_je_body(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append({k: v for k, v in request.headers.items() if k in ("referer", "cookie", "user-agent")})
        return httpx.Response(200, content=b"%PDF-1.4 fake")

    transport = httpx.MockTransport(handler)
    echter_client = httpx.AsyncClient

    def client_mit_transport(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return echter_client(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr("src.extraction.extractor.httpx.AsyncClient", client_mit_transport)

    storage = _Storage({"Referer": "https://rat.example.de/bi/", "Cookie": "consent=1"})
    extractor = TextExtractor(storage)
    body_id = uuid.uuid4()

    for _ in range(2):
        daten = await extractor._download(
            "https://rat.example.de/bi/getfile.asp?id=1", await extractor._download_headers(body_id)
        )
        assert daten.startswith(b"%PDF")

    assert gesehen[0]["referer"] == "https://rat.example.de/bi/" and gesehen[0]["cookie"] == "consent=1"
    assert "Mandari" in gesehen[0]["user-agent"], "eigener User-Agent bleibt"
    assert storage.abfragen == 1, "Header je Body nur einmal aus der Datenbank"
    assert await extractor._download_headers(None) == {}


def test_robots_sperre_wird_taeglich_geschont() -> None:
    assert BACKOFF_MIN_MINUTES_BY_KIND[ERROR_KIND_ROBOTS_BLOCKED] == 24 * 60
    jetzt = datetime.now(UTC)
    quelle = SimpleNamespace(consecutive_failures=1, last_error_at=jetzt, last_error_kind=ERROR_KIND_ROBOTS_BLOCKED)
    bis = source_backoff_until(quelle, jetzt)
    assert bis is not None and bis - jetzt >= timedelta(hours=23), "schon der erste Befund schont die Quelle einen Tag"
