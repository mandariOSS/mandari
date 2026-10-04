# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlerseiten der Dateivorschau verweisen auf das Original im Ratsinformationssystem.

Die Dokumentzeile im Bürgerportal führt „Ansehen“ und „Herunterladen“ über den Dateiabruf des Portals.
Liegt ein Dokument nicht im Zwischenspeicher und kann das Portal es nicht holen (Quelle geschont, Datei
zu groß, Server nicht erreichbar, Zeitüberschreitung, Hinweisseite statt Datei, Abruf abgelehnt), ist
der Browser der Besucher oft trotzdem beim Original erfolgreich. Die Fehlerseite nennt deshalb den Link.
Keinen Link gibt es bei nicht gefundenen Dateien (dort fehlt auch das Original) und bei Adressen, die
nicht öffentlich erreichbar sind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, safe_fetch

pytestmark = pytest.mark.django_db

ORIGINAL = "https://ris.fremd.example/files/vorlage.pdf?typ=1&nr=2"
LINK = 'href="https://ris.fremd.example/files/vorlage.pdf?typ=1&amp;nr=2"'


@pytest.fixture
def datei() -> OParlFile:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", slug="beispiel"
    )
    return OParlFile.objects.create(
        external_id="https://ris.fremd.example/oparl/file/1",
        body=body,
        name="Vorlage",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        download_url=ORIGINAL,
    )


@pytest.fixture
def quelle(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    """Quell-RIS auf Transportebene; ``antwort`` ist eine Response oder eine Ausnahme (sonst ein kleines PDF)."""
    zustand: dict[str, Any] = {"abrufe": [], "antwort": None}

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        zustand["abrufe"].append(str(request.url))
        antwort = zustand["antwort"]
        if str(request.url).endswith("/robots.txt") or antwort is None:
            return httpx.Response(200, content=b"%PDF-1.4 klein", headers={"content-type": "application/pdf"})
        if isinstance(antwort, Exception):
            raise antwort
        return cast(httpx.Response, antwort)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    return zustand


def _abruf(datei: OParlFile, *, download: bool = False) -> tuple[int, str]:
    url = f"/insight/dokumente/{datei.id}/preview/" + ("?download=1" if download else "")
    response = Client().get(url)
    return response.status_code, response.content.decode()


def _verweist_auf_original(html: str) -> None:
    assert LINK in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "Dokument im Ratsinformationssystem öffnen" in html


@pytest.mark.parametrize("download", [False, True])
def test_geschonte_quelle(datei: OParlFile, quelle: dict[str, Any], download: bool) -> None:
    OParlSource.objects.update(consecutive_failures=file_cache.backoff_failures())
    status, html = _abruf(datei, download=download)
    assert status == 503
    assert quelle["abrufe"] == [], "geschonte Quelle wird nicht angefragt"
    _verweist_auf_original(html)


def test_zu_grosse_datei(datei: OParlFile, quelle: dict[str, Any], settings: Any) -> None:
    settings.FILE_CACHE_MAX_MB = 1
    quelle["antwort"] = httpx.Response(
        200, content=b"%PDF" + b"0" * 10, headers={"content-type": "application/pdf", "content-length": "5000000"}
    )
    status, html = _abruf(datei, download=True)
    assert status == 413
    _verweist_auf_original(html)


def test_server_nicht_erreichbar(datei: OParlFile, quelle: dict[str, Any]) -> None:
    quelle["antwort"] = httpx.ConnectError("keine Verbindung")
    status, html = _abruf(datei)
    assert "Server nicht erreichbar" in html
    _verweist_auf_original(html)
    assert "keine Verbindung" not in html, "nie Ausnahmetexte ausgeben"


def test_zeitueberschreitung(datei: OParlFile, quelle: dict[str, Any], monkeypatch: Any) -> None:
    uhr = iter([0.0])
    monkeypatch.setattr(safe_fetch, "_now", lambda: next(uhr, 1000.0))
    quelle["antwort"] = httpx.Response(
        200, content=b"%PDF" + b"0" * (256 * 1024), headers={"content-type": "application/pdf"}
    )
    status, html = _abruf(datei)
    assert status == 504
    _verweist_auf_original(html)


def test_hinweisseite_statt_datei(datei: OParlFile, quelle: dict[str, Any]) -> None:
    quelle["antwort"] = httpx.Response(
        200, content=b"<!DOCTYPE html><html><body>Wartung</body></html>", headers={"content-type": "text/html"}
    )
    status, html = _abruf(datei)
    assert "Quelle liefert derzeit keine Datei" in html
    _verweist_auf_original(html)


def test_abruf_abgelehnt(datei: OParlFile, quelle: dict[str, Any]) -> None:
    quelle["antwort"] = httpx.Response(403, content=b"verboten")
    status, html = _abruf(datei)
    assert "Fehler 403" in html
    _verweist_auf_original(html)


def test_nicht_gefunden_ohne_link(datei: OParlFile, quelle: dict[str, Any]) -> None:
    quelle["antwort"] = httpx.Response(404, content=b"weg")
    status, html = _abruf(datei)
    assert "Datei nicht gefunden" in html
    assert LINK not in html


def test_nicht_oeffentliche_adresse_ohne_link(datei: OParlFile, quelle: dict[str, Any]) -> None:
    datei.download_url = "http://10.0.0.5/intern.pdf"
    datei.save(update_fields=["download_url"])
    status, html = _abruf(datei)
    assert quelle["abrufe"] == []
    assert "10.0.0.5" not in html


def test_gerade_viele_abrufe(datei: OParlFile, quelle: dict[str, Any], monkeypatch: Any) -> None:
    from insight_core.views import files

    class _Besetzt:
        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            return False

        def release(self) -> None:
            raise AssertionError("nicht belegt")

    monkeypatch.setattr(files, "_LIVE_FETCH_SLOTS", _Besetzt())
    status, html = _abruf(datei)
    assert status == 503
    _verweist_auf_original(html)
