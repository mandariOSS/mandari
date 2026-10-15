# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe aus Django folgen Weiterleitungen nur nach der robots.txt des Ziels und im Takt (Issue #1027).

Bisher folgte ``httpx`` jeder Weiterleitung sofort: ohne Blick in die robots.txt des Ziel-Hosts und ohne eigenen
Zeitpunkt in der Drossel je Host. Jetzt gilt für Dokument-Cache, Vorschau, Löschabgleich, Textextraktion,
Personenfotos, den Abruf der robots.txt und das Anlegen einer Quelle dasselbe wie für die erste Anfrage. Dazu: ein
Takt je Hostname (Port und Schreibweise zählen nicht) und beim Anlegen einer Quelle der größte bekannte Abstand
einer registrierten Quelle auf dem Host. Keine Abrufe fremder Server: robots.txt aus ``robots._fetch``
(nachgebildet), alles andere aus ``httpx.MockTransport``.
"""

from __future__ import annotations

import uuid
from io import StringIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlPerson, OParlSource
from insight_core.services import (
    document_extraction,
    file_cache,
    file_reconcile,
    host_pacing,
    person_photos,
    redirects,
    robots,
    safe_fetch,
)

pytestmark = pytest.mark.django_db

START = "https://rat.example.de/getfile.asp?id=1"
ZIEL = "https://cdn.example.org/dokumente/vorlage.pdf"
NUR_DOKUMENTE_GESPERRT = (200, b"User-agent: *\nDisallow: /*.pdf$\n")


@pytest.fixture
def robots_txt(echte_robots: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[int | None, bytes]]:
    """robots.txt je Host nachbilden; ohne Eintrag gibt es keine (404, alles erlaubt)."""
    antworten: dict[str, tuple[int | None, bytes]] = {}
    monkeypatch.setattr(robots, "_fetch", lambda url, *a, **kw: antworten.get(httpx.URL(url).host, (404, b"")))
    # Testadressen lösen nicht auf; die Prüfung auf öffentliche Ziele sieht dann eine öffentliche Adresse
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    return antworten


@pytest.fixture
def takt(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Reservierungen der Drossel je Host festhalten (Adresse, sync_config), ohne zu warten."""
    gesehen: list[tuple[str, Any]] = []

    def wait(url: str, **kw: Any) -> bool:
        gesehen.append((url, kw.get("sync_config")))
        return True

    monkeypatch.setattr(host_pacing, "wait", wait)
    return gesehen


def _quelle(sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system",
        sync_config=sync_config or {},
    )
    return OParlBody.objects.create(
        source=source,
        external_id=f"https://rat.example.de/oparl/bodies/{uuid.uuid4()}",
        name="Beispiel",
        is_listed=True,
    )


def _datei(body: OParlBody, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://rat.example.de/oparl/files/{uuid.uuid4()}",
        name="Vorlage",
        download_url=START,
        mime_type="application/pdf",
        **felder,
    )


def _server(abrufe: list[str], antworten: dict[str, httpx.Response] | None = None) -> httpx.MockTransport:
    """START leitet auf ZIEL weiter; ZIEL liefert ein PDF."""
    feste = {START: httpx.Response(302, headers={"Location": ZIEL}), **(antworten or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        abrufe.append(f"{request.method} {request.url}")
        return feste.get(
            str(request.url), httpx.Response(200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"})
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Ein Takt je Hostname
# ---------------------------------------------------------------------------


def test_port_und_schreibweise_ergeben_keinen_eigenen_takt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.RIS_REQUEST_INTERVAL = 1.0
    settings.REDIS_URL = ""
    geschlafen: list[float] = []
    host_pacing.reset()
    monkeypatch.setattr(host_pacing, "_now", lambda: 1000.0)
    monkeypatch.setattr("insight_core.services.host_pacing.time.sleep", lambda s: geschlafen.append(round(s, 3)))
    try:
        assert host_pacing.wait("https://rat.example.de/a.pdf")
        assert host_pacing.wait("https://RAT.example.de:443/b.pdf")
        assert host_pacing.wait("http://rat.example.de:80/c.pdf")
    finally:
        host_pacing.reset()
    assert geschlafen == [1.0, 2.0]


# ---------------------------------------------------------------------------
# Weiterleitungen
# ---------------------------------------------------------------------------


class TestDokumentCache:
    def test_gesperrtes_ziel_wird_nicht_abgerufen(
        self, robots_txt: dict[str, Any], takt: list[Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["cdn.example.org"] = NUR_DOKUMENTE_GESPERRT
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle())
        abrufe: list[str] = []

        assert file_cache.fetch_and_cache(datei, client=httpx.Client(transport=_server(abrufe))) == "refused"

        assert abrufe == [f"GET {START}"], "dem gesperrten Ziel nicht gefolgt"
        datei.refresh_from_db()
        assert (datei.local_status, datei.fetch_error) == ("refused", "robots")
        assert datei.local_error.startswith("robots.txt sperrt") and "Weiterleitung" in datei.local_error

    def test_jede_weiterleitung_im_takt_der_quelle(
        self, robots_txt: dict[str, Any], takt: list[Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle({"request_interval": 3}))
        abrufe: list[str] = []

        assert file_cache.fetch_and_cache(datei, client=httpx.Client(transport=_server(abrufe))) == "ok"

        assert abrufe == [f"GET {START}", f"GET {ZIEL}"]
        anfragen = [eintrag for eintrag in takt if not eintrag[0].endswith("/robots.txt")]
        assert anfragen == [(START, {"request_interval": 3}), (ZIEL, {"request_interval": 3})]


class TestVorschau:
    def test_gesperrtes_ziel_verweist_auf_das_original(
        self, robots_txt: dict[str, Any], takt: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["cdn.example.org"] = NUR_DOKUMENTE_GESPERRT
        abrufe: list[str] = []
        transport = _server(abrufe)
        monkeypatch.setattr(
            httpx.HTTPTransport, "handle_request", lambda self, request: transport.handle_request(request)
        )
        datei = _datei(_quelle())

        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")

        assert abrufe == [f"GET {START}"]
        body = response.content.decode()
        assert "robots.txt" in body and "%PDF" not in body
        datei.refresh_from_db()
        assert datei.fetch_error == "robots"

    def test_weiterleitung_wartet_nur_die_restzeit(
        self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        """
        Eine Frist für die ganze Vorschau (``FILE_PROXY_PACE_MAX_WAIT_SECONDS``): Die Weiterleitung wartet nur die
        Restzeit nach der Prüfung der ersten Anfrage, nicht erneut die volle Höchstwartezeit je Schritt.
        """
        settings.FILE_PROXY_PACE_MAX_WAIT_SECONDS = 5
        uhr = SimpleNamespace(jetzt=100.0)
        monkeypatch.setattr(redirects, "time", SimpleNamespace(monotonic=lambda: uhr.jetzt))
        wartezeiten: list[tuple[str, float | None]] = []

        def wait(url: str, **kw: Any) -> bool:
            """Jede Reservierung kostet drei Sekunden; mit weniger Restzeit gibt es keinen Zeitpunkt."""
            max_wait = kw.get("max_wait")
            wartezeiten.append((url, max_wait))
            if max_wait is not None and max_wait < 3.0:
                return False
            uhr.jetzt += 3.0
            return True

        monkeypatch.setattr(host_pacing, "wait", wait)
        abrufe: list[str] = []
        transport = _server(abrufe)
        monkeypatch.setattr(
            httpx.HTTPTransport, "handle_request", lambda self, request: transport.handle_request(request)
        )
        datei = _datei(_quelle())

        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")

        assert wartezeiten == [(START, 5.0), (ZIEL, 2.0)]
        assert abrufe == [f"GET {START}"], "binnen der Restzeit kein Zeitpunkt: dem Ziel nicht gefolgt"
        assert response.status_code == 503 and response["Retry-After"] == "30"


def test_frist_gilt_fuer_alle_weiterleitungen(robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """``guard(max_wait=…)``: eine Frist ab dem Aufruf für alle Weiterleitungen eines Abrufs, nicht je Schritt."""
    uhr = SimpleNamespace(jetzt=100.0)
    monkeypatch.setattr(redirects, "time", SimpleNamespace(monotonic=lambda: uhr.jetzt))
    wartezeiten: list[float | None] = []

    def wait(url: str, **kw: Any) -> bool:
        wartezeiten.append(kw.get("max_wait"))
        uhr.jetzt += 2.0
        return True

    monkeypatch.setattr(host_pacing, "wait", wait)
    vor_weiterleitung = redirects.guard(max_wait=5)
    for ziel in ("https://a.example.org/1", "https://b.example.org/2", "https://c.example.org/3"):
        vor_weiterleitung(httpx.Request("GET", ziel))

    assert wartezeiten == [5.0, 3.0, 1.0]


class TestLoeschabgleich:
    def test_gesperrtes_ziel_wird_nicht_abgerufen(self, robots_txt: dict[str, Any], takt: list[Any]) -> None:
        robots_txt["cdn.example.org"] = NUR_DOKUMENTE_GESPERRT
        datei = _datei(_quelle(), sha256_hash="0" * 64)
        abrufe: list[str] = []
        client = httpx.Client(transport=_server(abrufe))

        assert file_reconcile.verify(datei, client) == file_reconcile.ROBOTS
        assert file_reconcile.head_check(datei, client) == file_reconcile.ROBOTS
        assert abrufe == [f"GET {START}", f"HEAD {START}"]


class TestRobotsTxt:
    def test_weiterleitung_der_robots_txt_im_takt(
        self, echte_robots: None, takt: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        abrufe: list[str] = []
        ziel = "https://www.rat.example.de/robots.txt"
        transport = _server(
            abrufe,
            {
                "https://rat.example.de/robots.txt": httpx.Response(301, headers={"Location": ziel}),
                ziel: httpx.Response(200, text="User-agent: *\nDisallow: /intern/\n"),
            },
        )
        monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
        monkeypatch.setattr(
            httpx.HTTPTransport, "handle_request", lambda self, request: transport.handle_request(request)
        )

        assert not robots.check("https://rat.example.de/intern/a.pdf", sync_config={"request_interval": 2}).allowed

        assert abrufe == ["GET https://rat.example.de/robots.txt", f"GET {ziel}"]
        assert takt == [("https://rat.example.de/robots.txt", {"request_interval": 2}), (ziel, {"request_interval": 2})]


class TestFotosUndTextextraktion:
    def test_foto_hinter_gesperrter_weiterleitung(self, robots_txt: dict[str, Any], takt: list[Any]) -> None:
        robots_txt["cdn.example.org"] = (200, b"User-agent: mandari-ingestor\nDisallow: /\n")
        person = OParlPerson.objects.create(
            body=_quelle(),
            external_id=f"https://rat.example.de/oparl/persons/{uuid.uuid4()}",
            name="Beispiel",
            raw_json={"image": START},
        )
        abrufe: list[str] = []
        assert person_photos.fetch_person_photo(person, client=httpx.Client(transport=_server(abrufe))) == "error"
        assert abrufe == [f"GET {START}"]
        person.refresh_from_db()
        assert person.photo_error.startswith("robots.txt sperrt")

    def test_textextraktion_hinter_gesperrter_weiterleitung(
        self, robots_txt: dict[str, Any], takt: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["cdn.example.org"] = NUR_DOKUMENTE_GESPERRT
        abrufe: list[str] = []
        transport = _server(abrufe)
        monkeypatch.setattr(safe_fetch, "guarded_client", lambda **kw: httpx.Client(transport=transport, **kw))

        with pytest.raises(document_extraction.RobotsBlockedError):
            document_extraction.download_and_extract(url=START)
        assert abrufe == [f"GET {START}"]


class TestQuelleAnlegen:
    def _anlegen(self, monkeypatch: pytest.MonkeyPatch, handler: Any, url: str) -> str:
        echter_client = httpx.Client
        monkeypatch.setattr(
            "insight_core.management.commands.add_oparl_source.httpx.Client",
            lambda **kw: echter_client(transport=httpx.MockTransport(handler), **kw),
        )
        ausgabe = StringIO()
        call_command("add_oparl_source", url, stdout=ausgabe)
        return ausgabe.getvalue()

    def test_gesperrtes_ziel_einer_weiterleitung(
        self, robots_txt: dict[str, Any], takt: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["oparl.example.org"] = (200, b"User-agent: *\nDisallow: /oparl/\n")
        system_url = f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system"
        abrufe: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            abrufe.append(str(request.url))
            return httpx.Response(301, headers={"Location": "https://oparl.example.org/oparl/system"})

        with pytest.raises(CommandError):
            self._anlegen(monkeypatch, handler, system_url)
        assert abrufe == [system_url]
        assert not OParlSource.objects.filter(url=system_url).exists()

    def test_groesserer_abstand_einer_bekannten_quelle_auf_dem_host(
        self, takt: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        OParlSource.objects.create(
            name="Alt", url="https://RAT.example.de:443/alt/system", sync_config={"request_interval": 4}
        )
        system_url = f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == system_url:
                return httpx.Response(200, json={"id": system_url, "name": "Rat", "body": []})
            return httpx.Response(404)

        self._anlegen(monkeypatch, handler, system_url)
        assert takt == [(system_url, {"request_interval": 4.0})]
