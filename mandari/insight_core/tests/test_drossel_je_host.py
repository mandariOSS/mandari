# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host für Abrufe aus Django (insight_core/services/host_pacing.py, gemeinsam mit dem Ingestor).

Dokument-Cache, Textextraktion, Vorschau und Personenfotos fragten bisher ohne gemeinsamen Takt an; nur der
Dokument-Cache schlief 0,05 s zwischen zwei Dateien. Jetzt reservieren alle Prozesse über einen Zeitstempel je
Host in Redis. Kein echtes Redis, keine fremden Server: ein Ersatz bildet das Lua-Skript nach.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, host_pacing

pytestmark = pytest.mark.django_db


class _Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0
        self.geschlafen: list[float] = []

    def __call__(self) -> float:
        return self.jetzt

    def sleep(self, sekunden: float) -> None:
        self.geschlafen.append(round(sekunden, 3))


class _RedisErsatz:
    def __init__(self, uhr: _Uhr) -> None:
        self.uhr = uhr
        self.werte: dict[str, int] = {}

    def __call__(self, keys: list[str], args: list[int]) -> int:
        jetzt = int(self.uhr() * 1000)
        start = max(jetzt, self.werte.get(keys[0], 0))
        warten = start - jetzt
        if args[1] >= 0 and warten > args[1]:
            return -1
        self.werte[keys[0]] = start + args[0]
        return warten


@pytest.fixture
def uhr(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Iterator[_Uhr]:
    settings.RIS_REQUEST_INTERVAL = 1.0
    uhr = _Uhr()
    host_pacing.reset()
    monkeypatch.setattr(host_pacing, "_now", uhr)
    monkeypatch.setattr("insight_core.services.host_pacing.time.sleep", uhr.sleep)
    yield uhr
    host_pacing.reset()


def _quelle(sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(
        name="Quelle", url=f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system", sync_config=sync_config or {}
    )
    return OParlBody.objects.create(
        source=source, external_id=f"https://rat.example.de/bodies/{uuid.uuid4()}", name="Beispiel", is_listed=True
    )


def test_gemeinsamer_takt_ueber_redis(uhr: _Uhr, settings: Any) -> None:
    settings.REDIS_URL = "redis://ersatz:6379/0"
    host_pacing._state["script"] = _RedisErsatz(uhr)
    assert host_pacing.wait("https://rat.example.de/a.pdf")
    assert host_pacing.wait("https://rat.example.de/b.pdf")
    assert host_pacing.wait("https://anderer.example.org/c.pdf")
    assert host_pacing.wait("https://rat.example.de/d.pdf", sync_config={"request_interval": 0.5})
    assert uhr.geschlafen == [1.0, 2.0]


def test_ohne_redis_drosselt_der_prozess(uhr: _Uhr, settings: Any) -> None:
    settings.REDIS_URL = ""
    for _ in range(3):
        assert host_pacing.wait("https://rat.example.de/a.pdf")
    assert uhr.geschlafen == [1.0, 2.0]


def test_redis_stoerung_faellt_auf_den_prozess_zurueck(uhr: _Uhr, settings: Any) -> None:
    settings.REDIS_URL = "redis://ersatz:6379/0"

    def kaputt(**_kwargs: Any) -> int:
        raise ConnectionError("weg")

    host_pacing._state["script"] = kaputt
    assert host_pacing.wait("https://rat.example.de/a.pdf")
    assert host_pacing.wait("https://rat.example.de/a.pdf")
    assert uhr.geschlafen == [1.0]
    assert host_pacing._state["down_until"] == pytest.approx(uhr.jetzt + host_pacing.REDIS_RETRY_SECONDS)


def test_abstand_null_drosselt_nicht(uhr: _Uhr, settings: Any) -> None:
    settings.RIS_REQUEST_INTERVAL = 0.0
    for _ in range(3):
        assert host_pacing.wait("https://rat.example.de/a.pdf")
    assert uhr.geschlafen == []


def test_dokument_cache_nutzt_den_abstand_der_quelle(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, uhr: _Uhr) -> None:
    gesehen: list[tuple[str, Any]] = []
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)

    def wait(url: str, **kw: Any) -> bool:
        gesehen.append((url, kw.get("sync_config")))
        return True

    monkeypatch.setattr(host_pacing, "wait", wait)
    datei = OParlFile.objects.create(
        body=_quelle({"request_interval": 3}),
        external_id=f"https://rat.example.de/files/{uuid.uuid4()}",
        name="Vorlage",
        download_url="https://rat.example.de/getfile?id=1",
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"})
        )
    )
    assert file_cache.fetch_and_cache(datei, client=client) == "ok"
    assert gesehen == [("https://rat.example.de/getfile?id=1", {"request_interval": 3})]


def test_vorschau_wartet_nicht_endlos(uhr: _Uhr, settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.REDIS_URL = ""
    settings.FILE_PROXY_PACE_MAX_WAIT_SECONDS = 2

    def kein_abruf(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("ohne freien Zeitpunkt kein Abruf")

    monkeypatch.setattr("insight_core.services.safe_fetch.download_to", kein_abruf)
    datei = OParlFile.objects.create(
        body=_quelle(),
        external_id=f"https://rat.example.de/files/{uuid.uuid4()}",
        name="Vorlage",
        download_url="https://rat.example.de/getfile?id=2",
    )
    # Andere Abrufe (Ingestor, Cache) haben den Host für die nächsten Sekunden belegt
    for _ in range(4):
        host_pacing.wait("https://rat.example.de/oparl/papers")
    response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
    assert response.status_code == 503
    assert response["Retry-After"] == "30"


def _datei(nummer: int = 3) -> OParlFile:
    return OParlFile.objects.create(
        body=_quelle(),
        external_id=f"https://rat.example.de/files/{uuid.uuid4()}",
        name="Vorlage",
        download_url=f"https://rat.example.de/getfile?id={nummer}",
    )


def test_vorschau_wartet_mit_abrufplatz_und_ohne_datenbankverbindung(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Reihenfolge: Abrufplatz belegen (begrenzt die wartenden Threads), Verbindung an den Pool zurückgeben, erst
    dann auf den Takt warten. Vorher wartete die Vorschau mit ausgeliehener Verbindung und ohne Grenze.
    """
    from django.http import HttpResponse

    from apps.common import db_connections
    from insight_core.views import files as views

    ablauf: list[str] = []
    echte_freigabe = db_connections.release_idle_thread_connections

    def freigabe() -> None:
        ablauf.append("freigabe")
        echte_freigabe()

    def takt(url: str, **kw: Any) -> bool:
        ablauf.append(f"takt max_wait={kw.get('max_wait') is not None}")
        return True

    class _Platz:
        def acquire(self, timeout: float | None = None) -> bool:
            ablauf.append("platz")
            return True

        def release(self) -> None:
            ablauf.append("platz frei")

    def abruf(*_a: Any, **_kw: Any) -> HttpResponse:
        ablauf.append("abruf")
        return HttpResponse(b"%PDF-1.4", content_type="application/pdf")

    monkeypatch.setattr(db_connections, "release_idle_thread_connections", freigabe)
    monkeypatch.setattr(host_pacing, "wait", takt)
    monkeypatch.setattr(views, "_LIVE_FETCH_SLOTS", _Platz())
    monkeypatch.setattr(views, "_fetch_live", abruf)
    datei = _datei()
    response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
    assert response.status_code == 200
    assert ablauf == ["platz", "freigabe", "takt max_wait=True", "abruf", "platz frei"]


def test_vorschau_laedt_robots_txt_nur_mit_hoechstwartezeit(
    uhr: _Uhr, settings: Any, monkeypatch: pytest.MonkeyPatch, echte_robots: None
) -> None:
    """
    Fehlt die robots.txt im Cache, zählt ihr Abruf zur Höchstwartezeit der Vorschau. Vorher wartete er ohne
    Grenze und schlief in der Web-Anfrage den ganzen reservierten Takt des Hosts ab.
    """
    settings.REDIS_URL = ""
    settings.FILE_PROXY_PACE_MAX_WAIT_SECONDS = 2
    robots_abrufe: list[str] = []

    def robots_404(request: httpx.Request) -> httpx.Response:
        robots_abrufe.append(request.url.path)
        return httpx.Response(404)

    def client(*_a: Any, **_kw: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(robots_404))

    def kein_abruf(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("ohne freien Zeitpunkt kein Abruf des Dokuments")

    monkeypatch.setattr("insight_core.services.safe_fetch.guarded_client", client)
    monkeypatch.setattr("insight_core.services.safe_fetch.download_to", kein_abruf)
    datei = _datei(4)
    # Andere Abrufe haben den Host für die nächsten zwei Sekunden belegt
    for _ in range(2):
        host_pacing.wait("https://rat.example.de/oparl/papers")
    vorher = len(uhr.geschlafen)
    response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
    assert response.status_code == 503
    assert response["Retry-After"] == "30"
    assert robots_abrufe == ["/robots.txt"]
    # robots.txt und Dokument zusammen höchstens FILE_PROXY_PACE_MAX_WAIT_SECONDS
    assert all(sekunden <= 2 for sekunden in uhr.geschlafen[vorher:])


def test_zusammenfassung_wartet_nicht_auf_den_takt(uhr: _Uhr, settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Zusammenfassung lädt nie selbst (Issue #919): auch bei belegtem Takt kein Abruf, Hinweis statt Fehler."""
    from unittest import mock

    from insight_ai.services.summarizer import NoTextContentError, SummaryError, SummaryService
    from insight_core.models import OParlPaper

    settings.REDIS_URL = ""

    def kein_abruf(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("ohne freien Zeitpunkt kein Abruf")

    monkeypatch.setattr("insight_core.services.safe_fetch.guarded_client", kein_abruf)
    datei = _datei(5)
    assert datei.body is not None
    paper = OParlPaper.objects.create(
        external_id=f"https://rat.example.de/papers/{uuid.uuid4()}", body=datei.body, name="Vorlage"
    )
    datei.paper = paper
    datei.save(update_fields=["paper"])
    for _ in range(10):
        host_pacing.wait("https://rat.example.de/oparl/papers")

    anbieter = mock.Mock()
    anbieter.is_available.return_value = True
    with pytest.raises(SummaryError) as fehler:
        SummaryService(provider=anbieter).generate_summary(paper, save=False)
    # Die Erkennung steht aus: Hinweis, nicht „kein Text“ (das würde sich die Ansicht merken)
    assert not isinstance(fehler.value, NoTextContentError)
    anbieter.chat_completion.assert_not_called()
