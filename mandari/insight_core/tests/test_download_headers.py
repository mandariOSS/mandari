# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Issue #116: Download-Header je Quelle (``sync_config["download_headers"]``) für Dateicache und
Textextraktion, und die robots-Sperre als Fehlerklasse mit Grund und Empfehlung im Betriebsmonitor.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.utils import timezone

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import document_extraction, file_cache
from insight_core.services.source_health import evaluate_source

HEADERS = {"Referer": "https://rat.example.de/bi/", "Cookie": "consent=1", "leer": None, "zahl": 7}


@pytest.fixture
def quelle_mit_headern(db: Any) -> OParlSource:
    return OParlSource.objects.create(
        name="Beispielstadt (SessionNet)",
        url="https://rat.example.de/bi/",
        sync_config={"source_type": "scraper:sessionnet", "download_headers": HEADERS},
    )


@pytest.fixture
def datei(quelle_mit_headern: OParlSource) -> OParlFile:
    body = OParlBody.objects.create(
        source=quelle_mit_headern, external_id="https://rat.example.de/bi/body/1", name="Beispielstadt"
    )
    return OParlFile.objects.create(
        body=body,
        external_id="https://rat.example.de/bi/file/1",
        name="Vorlage.pdf",
        file_name="Vorlage.pdf",
        mime_type="application/pdf",
        download_url="https://rat.example.de/bi/getfile.asp?id=1",
    )


def test_download_headers_nur_strings_und_leer_ohne_konfiguration(datei: OParlFile, db: Any) -> None:
    assert file_cache.download_headers(datei.body) == {
        "Referer": "https://rat.example.de/bi/",
        "Cookie": "consent=1",
        "zahl": "7",
    }
    assert file_cache.download_headers(None) == {}
    ohne = OParlSource.objects.create(name="Ohne", url="https://ohne.example/oparl/system", sync_config={})
    body = OParlBody.objects.create(source=ohne, external_id="https://ohne.example/body", name="Ohne")
    assert file_cache.download_headers(body) == {}


def test_dateicache_sendet_quellen_header(datei: OParlFile, tmp_path: Any, settings: Any, monkeypatch: Any) -> None:
    gesehen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.headers)
        return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"})

    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": file_cache.USER_AGENT})

    assert file_cache.fetch_and_cache(datei, client=client) == "ok"
    assert gesehen[0]["referer"] == "https://rat.example.de/bi/"
    assert gesehen[0]["cookie"] == "consent=1"
    assert gesehen[0]["user-agent"] == file_cache.USER_AGENT


def test_textextraktion_reicht_header_durch(monkeypatch: Any) -> None:
    gesehen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.headers)
        return httpx.Response(200, content=b"Nur Text", headers={"content-type": "text/plain"})

    echter_client = httpx.Client
    monkeypatch.setattr(
        "insight_core.services.document_extraction.httpx.Client",
        lambda *a, **kw: echter_client(
            transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}
        ),
    )
    ergebnis = document_extraction.download_and_extract(
        url="https://rat.example.de/bi/getfile.asp?id=2",
        mime_type="text/plain",
        extra_headers={"Referer": "https://rat.example.de/bi/"},
    )
    assert ergebnis.text.strip() == "Nur Text"
    assert gesehen[0]["referer"] == "https://rat.example.de/bi/"
    assert "Mandari" in gesehen[0]["user-agent"]


def test_robots_sperre_ist_kritisch_mit_empfehlung(db: Any) -> None:
    now = timezone.now()
    quelle = OParlSource.objects.create(
        name="Gesperrtstadt",
        url="https://rat.gesperrt.example/bi/",
        sync_config={"source_type": "scraper:sessionnet"},
        last_sync=now - timedelta(days=2),
        last_error="robots.txt verbietet Crawl von https://rat.gesperrt.example/bi/",
        last_error_at=now - timedelta(minutes=5),
        last_error_kind=OParlSource.ERROR_KIND_ROBOTS_BLOCKED,
        consecutive_failures=1,
    )
    item = evaluate_source(quelle)
    assert item["status"] == "critical" and item["paused"] is True
    assert item["error_kind_label"] == "robots.txt sperrt"
    assert "keine Umgehung" in " ".join(item["reasons"])
    assert "Betreiber" in item["recommendation"] and "täglich" in item["recommendation"]
