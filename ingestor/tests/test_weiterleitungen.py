# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine robots.txt-Regel für ``mandari-ingestor`` gilt für alle Abrufe, höchstens eine Anfrage je Sekunde und Server
(Issue #1027), auch über Weiterleitungen, Schreibweisen des Hosts und die Sondierung hinweg:

- Gruppen für frühere Kennungen (``mandari-file-cache``, ``mandari``) gelten zusätzlich, können aber nur sperren.
- Ziele von Weiterleitungen prüfen wir gegen die robots.txt ihres Hosts, bevor wir ihnen folgen, und jede
  Weiterleitung bekommt ihren eigenen Zeitpunkt in der Drossel (OParl-Abgleich, Textextraktion, Scraper,
  robots.txt, Sondierung).
- Der Schlüssel der Drossel ist der Hostname: Port und Groß-/Kleinschreibung ergeben keinen eigenen Takt.
- Sondierung: Ist für den Host eine Quelle mit größerem ``request_interval`` registriert, gilt dieser Abstand.

Keine Abrufe fremder Server: alle Antworten kommen aus ``httpx.MockTransport``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mandari_oparl.pacing import host_intervals, host_of, interval_for_host, key
from mandari_oparl.robots import KIND_API, RobotsTxt
from src.client import redirects
from src.client.host_pacing import HostPacer, host_pacer
from src.client.oparl_client import ERROR_KIND_ROBOTS_BLOCKED, OParlClient
from src.client.robots import RobotsGate, robots_gate
from src.client.source_options import SourceFetchOptions
from src.extraction.extractor import TextExtractor
from src.probe import ris_probe as rp
from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError

UA = "mandari-ingestor/1.0 (+https://mandari.de/crawler/; support@mandari.de)"
SPERRT_ALLES = RobotsTxt.parse("User-agent: *\nDisallow: /\n")
ERLAUBT_ALLES = RobotsTxt.parse("User-agent: *\nAllow: /\n")


# ---------------------------------------------------------------------------
# Produkt-Token: mandari-ingestor für alle Abrufe, frühere Kennungen nur zusätzlich
# ---------------------------------------------------------------------------


class TestFruehereKennungen:
    def test_gruppe_mandari_ingestor_gilt_vor_stern(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: mandari-ingestor\nAllow: /\n"
        assert RobotsTxt.parse(text).decide("/oparl/system").allowed

    @pytest.mark.parametrize("kennung", ["mandari-file-cache", "Mandari"])
    def test_sperre_fuer_fruehere_kennung_gilt_weiter(self, kennung: str) -> None:
        robots = RobotsTxt.parse(f"User-agent: {kennung}\nDisallow: /dokumente/\n")
        assert not robots.decide("/dokumente/a.pdf").allowed
        assert robots.decide("/oparl/system").allowed

    def test_freigabe_fuer_fruehere_kennung_hebt_stern_nicht_auf(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: mandari-file-cache\nAllow: /\n"
        assert not RobotsTxt.parse(text).decide("/oparl/system").allowed

    def test_ohne_eigene_gruppe_zaehlen_fruehere_kennungen_nicht(self) -> None:
        # Kein Rückgriff auf „*“ für die frühere Kennung: Die Gruppe für mandari-ingestor erlaubt
        text = "User-agent: mandari-ingestor\nAllow: /\n\nUser-agent: *\nDisallow: /\n"
        assert RobotsTxt.parse(text).decide("/dokumente/a.pdf").allowed


# ---------------------------------------------------------------------------
# Schlüssel der Drossel: ein Takt je Hostname
# ---------------------------------------------------------------------------


class TestHostSchluessel:
    @pytest.mark.parametrize(
        "adresse",
        [
            "https://RAT.Example.de/oparl/system",
            "https://rat.example.de:443/dokumente/a.pdf",
            "http://rat.example.de:80/",
            "http://rat.example.de/",
            "https://rat.example.de.:443/",
            "https://nutzer:geheim@rat.example.de/x",
            "rat.example.de",
            "RAT.example.de:443",
        ],
    )
    def test_gleicher_server_gleicher_schluessel(self, adresse: str) -> None:
        assert host_of(adresse) == "rat.example.de"
        assert key(adresse) == key("https://rat.example.de/")

    def test_internationaler_name_wie_in_weiterleitungen(self) -> None:
        assert host_of("https://bürgerinfo.example.de/bi/") == host_of(str(httpx.URL("https://bürgerinfo.example.de/")))

    async def test_standardport_teilt_den_takt(self) -> None:
        uhr = SimpleNamespace(jetzt=1000.0, geschlafen=[])

        async def sleep(sekunden: float) -> None:
            uhr.geschlafen.append(round(sekunden, 3))

        pacer = HostPacer(clock=lambda: uhr.jetzt, sleep=sleep)
        pacer.redis_enabled = False
        assert await pacer.wait("https://rat.example.de/a", 1.0)
        assert await pacer.wait("https://RAT.example.de:443/b", 1.0)
        assert await pacer.wait("http://rat.example.de:80/c", 1.0)
        assert uhr.geschlafen == [1.0, 2.0], "drei Anfragen an denselben Server, je eine Sekunde Abstand"


class TestBekannteAbstaende:
    def test_groesster_abstand_je_host(self) -> None:
        quellen = [
            ("https://rat.example.de/oparl/system", {"request_interval": 2.0}),
            ("https://RAT.example.de:443/oparl/v2/system", {"request_interval": 5}),
            ("https://rat.example.de/alt/system", {"request_interval": 0.2}),  # unter der Untergrenze: 1 s
            ("https://andere.example.org/system", {}),
            ("https://kaputt.example.org/system", {"request_interval": "schnell"}),
            (None, {"request_interval": 9}),
        ]
        assert host_intervals(quellen) == {"rat.example.de": 5.0}

    def test_nur_langsamer(self) -> None:
        bekannt = {"rat.example.de": 3.0}
        assert interval_for_host("https://rat.example.de/x", bekannt, 1.0) == 3.0
        assert interval_for_host("https://rat.example.de/x", bekannt, 4.0) == 4.0
        assert interval_for_host("https://andere.example.org/x", bekannt, 1.0) == 1.0


# ---------------------------------------------------------------------------
# Weiterleitungen: robots.txt des Ziels und eigener Zeitpunkt
# ---------------------------------------------------------------------------


def _reservierungen(monkeypatch: pytest.MonkeyPatch, ablauf: list[tuple[str, str]]) -> None:
    async def wait(url: str, interval: float, **_kwargs: Any) -> bool:
        ablauf.append(("warten", url))
        return True

    monkeypatch.setattr(host_pacer, "wait", wait)


def _server(antworten: dict[str, httpx.Response], ablauf: list[tuple[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        ablauf.append(("abruf", str(request.url)))
        return antworten.get(str(request.url), httpx.Response(200, json={"id": str(request.url)}))

    return httpx.MockTransport(handler)


async def _oparl_fetch(transport: httpx.MockTransport, url: str) -> tuple[Any, OParlClient]:
    async with OParlClient(max_concurrent=1, wait_time=0, request_interval=1.0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=transport, headers={"Accept": "application/json"})
        return await client.fetch(url), client


class TestOParlClient:
    async def test_folgt_nicht_auf_gesperrtes_ziel(self, echte_robots: None, monkeypatch: pytest.MonkeyPatch) -> None:
        robots_gate.seed("https://rat.example.de/", ERLAUBT_ALLES)
        robots_gate.seed("https://dokumente.example.org/", SPERRT_ALLES)
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start, ziel = "https://rat.example.de/oparl/system", "https://dokumente.example.org/oparl/system"
        transport = _server({start: httpx.Response(302, headers={"Location": ziel})}, ablauf)

        result, client = await _oparl_fetch(transport, start)

        assert ("abruf", ziel) not in ablauf, "das gesperrte Ziel wird nicht angefragt"
        assert result.data is None and result.error_kind == ERROR_KIND_ROBOTS_BLOCKED
        assert result.url == start and ziel in (result.error or "")
        befund = client.host_health["dokumente.example.org"]
        assert befund.robots_blocked_count == 1

    async def test_jede_weiterleitung_im_takt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start = "https://rat.example.de/oparl/system"
        mitte, ziel = "https://rat.example.de/oparl/v1/system", "https://www.rat.example.de/oparl/v1/system"
        transport = _server(
            {
                start: httpx.Response(301, headers={"Location": mitte}),
                mitte: httpx.Response(302, headers={"Location": ziel}),
            },
            ablauf,
        )

        result, _ = await _oparl_fetch(transport, start)

        assert result.data == {"id": ziel}
        assert ablauf == [
            ("warten", start),
            ("abruf", start),
            ("warten", mitte),
            ("abruf", mitte),
            ("warten", ziel),
            ("abruf", ziel),
        ]

    async def test_hoechstens_fuenf_weiterleitungen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)

        def kreis(request: httpx.Request) -> httpx.Response:
            ablauf.append(("abruf", str(request.url)))
            n = int(request.url.params.get("n", "0"))
            return httpx.Response(302, headers={"Location": f"/oparl/system?n={n + 1}"})

        async with OParlClient(max_concurrent=1, wait_time=0) as client:
            assert client._client is not None
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(kreis))
            client.max_retries = 1
            result = await client.fetch("https://rat.example.de/oparl/system")

        assert result.data is None
        assert len([a for a in ablauf if a[0] == "abruf"]) == 1 + redirects.MAX_REDIRECTS

    async def test_vergleichsanfrage_an_die_403_adresse_ohne_weiterleitung(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.config import settings

        monkeypatch.setattr(settings, "oparl_ua_probe_enabled", True)
        start, ziel = "https://rat.example.de/oparl/system", "https://rat.example.de/oparl/v1/system"
        gesehen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            agent = request.headers.get("User-Agent", "")
            gesehen.append((str(request.url), agent))
            if str(request.url) == start:
                return httpx.Response(301, headers={"Location": ziel})
            if "mandari" in agent:
                return httpx.Response(403)
            return httpx.Response(302, headers={"Location": "https://rat.example.de/login"})

        async with OParlClient(max_concurrent=1, wait_time=0) as client:
            assert client._client is not None
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": UA})
            result = await client.fetch(start)

        neutrale = [(url, agent) for url, agent in gesehen if "mandari" not in agent]
        assert neutrale == [(ziel, f"python-httpx/{httpx.__version__}")], "genau eine, an die Adresse mit 403"
        assert result.status_code == 403


class TestRobotsTxtSelbst:
    async def test_weiterleitungen_der_robots_txt_im_takt(self, echte_robots: None) -> None:
        ablauf: list[tuple[str, str]] = []
        ziel = "https://www.rat.example.de/robots.txt"

        async def pace(url: str) -> None:
            ablauf.append(("warten", url))

        transport = _server(
            {
                "https://rat.example.de/robots.txt": httpx.Response(301, headers={"Location": ziel}),
                ziel: httpx.Response(200, text="User-agent: *\nDisallow: /intern/\n"),
            },
            ablauf,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            decision = await RobotsGate().decide(
                http, "https://rat.example.de/intern/a", user_agent=UA, kind=KIND_API, pace=pace
            )

        assert not decision.allowed
        assert ablauf == [
            ("warten", "https://rat.example.de/robots.txt"),
            ("abruf", "https://rat.example.de/robots.txt"),
            ("warten", ziel),
            ("abruf", ziel),
        ]


class _Speicher:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.updates: list[dict[str, Any]] = []
        self.config = config or {}

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def get_fetch_options_for_body(self, body_id: Any) -> SourceFetchOptions:
        return SourceFetchOptions.from_sync_config(self.config)

    async def update_file_text(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    async def file_downloads_enabled_for_body(self, body_id: Any) -> bool:
        return True


class TestTextextraktion:
    async def test_folgt_nicht_auf_gesperrte_datei(self, echte_robots: None, monkeypatch: pytest.MonkeyPatch) -> None:
        robots_gate.seed("https://rat.example.de/", ERLAUBT_ALLES)
        robots_gate.seed("https://cdn.example.org/", RobotsTxt.parse("User-agent: *\nDisallow: /*.pdf$\n"))
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start, ziel = "https://rat.example.de/getfile.asp?id=1", "https://cdn.example.org/dokumente/a.pdf"
        transport = _server({start: httpx.Response(302, headers={"Location": ziel})}, ablauf)
        echter_client = httpx.AsyncClient
        monkeypatch.setattr(
            "src.extraction.extractor.httpx.AsyncClient",
            lambda *a, **kw: echter_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )
        speicher = _Speicher()
        datei = SimpleNamespace(
            id=uuid4(), body_id=uuid4(), download_url=start, access_url=None, mime_type="application/pdf", file_name=""
        )

        assert await TextExtractor(speicher)._process_file(datei) is False

        assert ("abruf", ziel) not in ablauf
        assert speicher.updates[-1]["status"] == "skipped"
        assert speicher.updates[-1]["error"].startswith("robots.txt sperrt")

    async def test_ausnahme_der_quelle_gilt_fuer_das_ziel(
        self, echte_robots: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_gate.seed("https://rat.example.de/", ERLAUBT_ALLES)
        robots_gate.seed("https://cdn.example.org/", SPERRT_ALLES)
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start, ziel = "https://rat.example.de/getfile.asp?id=1", "https://cdn.example.org/dokumente/a.pdf"
        transport = _server(
            {
                start: httpx.Response(302, headers={"Location": ziel}),
                ziel: httpx.Response(200, content=b"%PDF-1.4 x"),
            },
            ablauf,
        )
        extractor = TextExtractor(_Speicher())
        override = SourceFetchOptions.from_sync_config(
            {"robots_override": {"scope": "files", "note": "Freigabe der Stelle, offizielle Anfrage läuft"}}
        ).robots_override
        before = redirects.check_and_pace(
            None, user_agent=UA, kind="files", override=override, pace=lambda url: host_pacer.wait(url, 1.0)
        )
        echter_client = httpx.AsyncClient
        monkeypatch.setattr(
            "src.extraction.extractor.httpx.AsyncClient",
            lambda *a, **kw: echter_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )
        geladen = await extractor._download_to_file(start, {}, before)
        try:
            assert geladen.head.startswith(b"%PDF")
        finally:
            geladen.discard()
        assert ("warten", ziel) in ablauf and ("abruf", ziel) in ablauf


class TestScraper:
    async def test_folgt_nicht_auf_gesperrtes_ziel(self, echte_robots: None, monkeypatch: pytest.MonkeyPatch) -> None:
        robots_gate.seed("https://rat.example.de/", RobotsTxt.parse("User-agent: *\nDisallow: /intern/\n"))
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start, ziel = "https://rat.example.de/bi/si0040.asp", "https://rat.example.de/intern/login.asp"
        transport = _server({start: httpx.Response(302, headers={"Location": ziel})}, ablauf)

        async with PoliteFetcher(rate_limit_seconds=1.0, user_agent=UA) as fetcher:
            assert fetcher._client is not None
            await fetcher._client.aclose()
            fetcher._client = httpx.AsyncClient(transport=transport, headers={"User-Agent": UA})
            with pytest.raises(RobotsDisallowedError):
                await fetcher.fetch_text(start)

        assert ablauf == [("warten", start), ("abruf", start)]

    async def test_erlaubtes_ziel_im_takt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)
        start, ziel = "https://rat.example.de/bi/", "https://rat.example.de/bi/info.asp"
        transport = _server(
            {start: httpx.Response(301, headers={"Location": ziel}), ziel: httpx.Response(200, text="<html>")},
            ablauf,
        )
        async with PoliteFetcher(rate_limit_seconds=1.0, user_agent=UA) as fetcher:
            assert fetcher._client is not None
            await fetcher._client.aclose()
            fetcher._client = httpx.AsyncClient(transport=transport, headers={"User-Agent": UA})
            assert await fetcher.fetch_text(start) == "<html>"

        assert ablauf == [("warten", start), ("abruf", start), ("warten", ziel), ("abruf", ziel)]


# ---------------------------------------------------------------------------
# Sondierung (probe-ris)
# ---------------------------------------------------------------------------


class TestSondierung:
    async def test_weiterleitung_nur_nach_robots_txt_des_ziels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ablauf: list[tuple[str, str]] = []
        _reservierungen(monkeypatch, ablauf)

        def handler(request: httpx.Request) -> httpx.Response:
            ablauf.append(("abruf", str(request.url)))
            if request.url.host == "ris.example.org":
                if request.url.path == "/robots.txt":
                    return httpx.Response(200, text="User-agent: *\nAllow: /\n")
                return httpx.Response(302, headers={"Location": "https://portal.example.org/bi/"})
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /bi/\n")
            return httpx.Response(200, text="<html>gesperrt</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r = await rp.probe_url("https://ris.example.org/", client, user_agent=UA)

        abrufe = [url for art, url in ablauf if art == "abruf"]
        assert "https://portal.example.org/robots.txt" in abrufe, "robots.txt des Ziel-Hosts zuerst"
        assert "https://portal.example.org/bi/" not in abrufe, "gesperrtes Ziel nicht angefragt"
        assert r.landing_status == 302
        assert any("Weiterleitung nach https://portal.example.org/bi/ nicht gefolgt" in n for n in r.notes)
        assert r.requests == len(abrufe) <= rp.MAX_REQUESTS

    async def test_bekannter_groesserer_abstand_gilt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        intervalle: list[tuple[str, float]] = []

        async def wait(url: str, interval: float, **_kwargs: Any) -> bool:
            intervalle.append((host_of(url), interval))
            return True

        monkeypatch.setattr(host_pacer, "wait", wait)
        monkeypatch.setattr(rp.settings, "request_interval", 1.0)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await rp.probe_url(
                "https://RIS.example.org/", client, user_agent=UA, host_intervals={"ris.example.org": 4.0}
            )
            await rp.probe_url("https://anders.example.org/", client, user_agent=UA, host_intervals={})

        assert ("ris.example.org", 4.0) in intervalle
        assert all(abstand == 4.0 for host, abstand in intervalle if host == "ris.example.org")
        assert all(abstand == 1.0 for host, abstand in intervalle if host == "anders.example.org")

    async def test_vergleichsanfrage_an_die_403_adresse_ohne_weiterleitung(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rp.settings, "oparl_ua_probe_enabled", True)
        gesehen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            agent = request.headers.get("User-Agent", "")
            gesehen.append((request.url.path, agent))
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.path == "/":
                return httpx.Response(301, headers={"Location": "/bi/"})
            if "mandari" in agent:
                return httpx.Response(403)
            return httpx.Response(302, headers={"Location": "/bi/start.asp"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r = await rp.probe_url("https://ris.example.org/", client, user_agent=UA)

        neutrale = [pfad for pfad, agent in gesehen if "mandari" not in agent]
        assert neutrale == ["/bi/"], "eine Vergleichsanfrage an die Adresse mit 403, ohne Weiterleitung"
        assert r.ua_blocked is True
