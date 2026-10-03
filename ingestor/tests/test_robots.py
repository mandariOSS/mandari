# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt nach RFC 9309: Auswertung (mandari_oparl.robots), Zwischenspeicher und Abruf (src/client/robots.py),
Prüfung vor OParl-Abrufen (OParlClient) und vor Datei-Downloads (TextExtractor). Keine Abrufe fremder Server:
alle Antworten kommen aus httpx.MockTransport.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mandari_oparl.crawler import INFO_URL, PRODUCT_TOKEN, user_agent
from mandari_oparl.robots import (
    KIND_API,
    KIND_FILES,
    STATE_PARSED,
    STATE_UNAVAILABLE,
    STATE_UNREACHABLE,
    RobotsTxt,
    robots_override,
    robots_override_problem,
)

from src.client.oparl_client import ERROR_KIND_ROBOTS_BLOCKED, ERROR_KIND_SERVER_ERROR_SERIES, OParlClient
from src.client.robots import RobotsGate, robots_gate
from src.client.source_options import SourceFetchOptions
from src.config import DEFAULT_USER_AGENT
from src.extraction.extractor import TextExtractor
from src.sync.orchestrator import source_backoff_until

# ---------------------------------------------------------------------------
# Auswertung (RFC 9309)
# ---------------------------------------------------------------------------


def _allowed(text: str, path: str, token: str = PRODUCT_TOKEN) -> bool:
    return RobotsTxt.parse(text).decide(path, tokens=(token,)).allowed


class TestAuswertung:
    def test_platzhalter_und_pfadende_sperren_nur_dokumente(self):
        robots = "User-Agent: *\nDisallow: /*.pdf$\n"
        assert _allowed(robots, "/oparl/v1/body/1/paper?page=2")
        assert not _allowed(robots, "/dokumente/vorlage.pdf")
        assert not _allowed(robots, "/a/b/c/anlage.pdf")
        # $ verankert das Ende: Abfrage oder weitere Zeichen nach .pdf passen nicht
        assert _allowed(robots, "/dokumente/vorlage.pdf?download=1")
        assert _allowed(robots, "/dokumente/vorlage.pdfx")
        # Pfade vergleichen Groß-/Kleinschreibung
        assert _allowed(robots, "/dokumente/VORLAGE.PDF")

    def test_platzhalter_mitten_im_muster(self):
        robots = "User-agent: *\nDisallow: /bi/*/getfile\n"
        assert not _allowed(robots, "/bi/2026/getfile?id=1")
        assert _allowed(robots, "/bi/getfile")

    def test_laengste_regel_gewinnt_unabhaengig_von_der_reihenfolge(self):
        robots = "User-agent: *\nAllow: /oparl/\nDisallow: /\n"
        assert _allowed(robots, "/oparl/system")
        assert not _allowed(robots, "/intern/")
        robots = "User-agent: *\nDisallow: /oparl/files\nAllow: /oparl/\n"
        assert not _allowed(robots, "/oparl/files/1")
        assert _allowed(robots, "/oparl/papers")

    def test_bei_gleicher_laenge_gewinnt_allow(self):
        assert _allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")
        assert _allowed("User-agent: *\nAllow: /page\nDisallow: /page\n", "/page")

    def test_eigene_gruppe_vor_stern_und_gruppen_werden_zusammengefasst(self):
        robots = (
            "User-agent: *\nDisallow: /\n\n"
            "User-agent: Mandari-Ingestor\nDisallow: /privat/\n\n"
            "User-agent: andere\nUser-agent: mandari-ingestor\nDisallow: /intern/\n"
        )
        assert _allowed(robots, "/oparl/system")
        assert not _allowed(robots, "/privat/x")
        assert not _allowed(robots, "/intern/x")
        assert not _allowed(robots, "/oparl/system", token="fremder-bot")

    def test_leere_eigene_gruppe_erlaubt_alles(self):
        assert _allowed("User-agent: *\nDisallow: /\n\nUser-agent: mandari-ingestor\nDisallow:\n", "/x")

    def test_kommentare_sonstige_felder_und_regeln_ohne_gruppe(self):
        robots = (
            "Disallow: /ohne-gruppe\n"
            "# Kommentar\n"
            "User-agent: * # alle\n"
            "Crawl-delay: 10\n"
            "Sitemap: https://rat.example.de/sitemap.xml\n"
            "Disallow: /bi/ # Bürgerinfo\n"
        )
        assert _allowed(robots, "/ohne-gruppe")
        assert not _allowed(robots, "/bi/si0040.asp")

    def test_prozentkodierung_wird_vereinheitlicht(self):
        robots = "User-agent: *\nDisallow: /dokumente/%7Ealt/\nDisallow: /straße/\n"
        assert not _allowed(robots, "/dokumente/~alt/1.pdf")
        assert not _allowed(robots, "/stra%C3%9Fe/1")

    def test_robots_txt_selbst_ist_immer_erlaubt(self):
        assert _allowed("User-agent: *\nDisallow: /\n", "/robots.txt")

    def test_html_statt_robots_txt_enthaelt_keine_regeln(self):
        # Zugangsprüfung, die auch für /robots.txt eine HTML-Seite mit 200 liefert
        robots = RobotsTxt.from_response(200, b"<html><body>Zugriff pruefen</body></html>")
        assert robots.state == STATE_PARSED
        assert robots.decide("/oparl/system").allowed

    @pytest.mark.parametrize("status", [401, 403, 404, 406, 410, 301])
    def test_4xx_gilt_als_nicht_vorhanden(self, status):
        robots = RobotsTxt.from_response(status, b"User-agent: *\nDisallow: /\n")
        assert robots.state == STATE_UNAVAILABLE
        assert robots.decide("/alles").allowed

    @pytest.mark.parametrize("status", [None, 500, 503, 408, 429])
    def test_5xx_ratenlimit_und_netzfehler_stellen_zurueck_statt_zu_sperren(self, status):
        robots = RobotsTxt.from_response(status, b"User-agent: *\nAllow: /\n")
        assert robots.state == STATE_UNREACHABLE
        decision = robots.decide("/oparl/system")
        assert not decision.allowed
        # Nicht erreichbar ist eine Störung, keine Sperre: zurückstellen, nicht überspringen
        assert decision.unreachable and not decision.blocked
        assert decision.status_code == status
        assert "nicht erreichbar" in decision.reason and "zurückgestellt" in decision.reason
        assert "sperrt" not in decision.reason and "untersagt" not in decision.reason

    def test_entscheidende_regel_steht_im_grund(self):
        decision = RobotsTxt.parse("User-agent: *\nDisallow: /*.pdf$\n").decide("/a.pdf")
        assert decision.rule == "Disallow: /*.pdf$"
        assert "Disallow: /*.pdf$" in decision.reason
        assert decision.blocked and not decision.unreachable


class TestAusnahme:
    def test_ausnahme_braucht_vermerk(self):
        config = {"robots_override": {"scope": "files"}}
        assert robots_override(config) is None
        assert "Vermerk" in (robots_override_problem(config) or "")
        assert robots_override({"robots_override": {"scope": "files", "note": "ja"}}) is None

    def test_gueltige_ausnahme(self):
        config = {"robots_override": {"scope": "files", "note": "Freigabe der Stelle per E-Mail, Anfrage läuft"}}
        override = robots_override(config)
        assert override is not None and override.covers(KIND_FILES) and not override.covers(KIND_API)
        assert robots_override_problem(config) is None
        assert SourceFetchOptions.from_sync_config(config).robots_override == override

    def test_bereich_standard_und_unbekannt(self):
        alle = robots_override({"robots_override": {"note": "Freigabe der Stelle liegt schriftlich vor"}})
        assert alle is not None and alle.covers(KIND_API) and alle.covers(KIND_FILES)
        config = {"robots_override": {"scope": "pdf", "note": "Freigabe der Stelle liegt schriftlich vor"}}
        assert robots_override(config) is None
        assert "Bereich" in (robots_override_problem(config) or "")


def test_user_agent_mit_infoseite_und_kontakt():
    assert user_agent("1.2.3") == "mandari-ingestor/1.2.3 (+https://mandari.de/crawler/; support@mandari.de)"
    assert INFO_URL in DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------
# Zwischenspeicher und Abruf
# ---------------------------------------------------------------------------


class _Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


def _transport(antworten: dict[str, Any], gesehen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        antwort = antworten.get(request.url.path)
        if callable(antwort):
            return antwort(request)
        if antwort is None:
            return httpx.Response(200, json={"id": str(request.url)})
        return antwort

    return httpx.MockTransport(handler)


class TestZwischenspeicher:
    async def test_abruf_mit_eigenem_user_agent_und_text_plain(self, echte_robots):
        gesehen: list[httpx.Request] = []

        def robots(request: httpx.Request) -> httpx.Response:
            # Wie manche Server: mit JSON-Accept 406 statt der Datei
            if "application/json" in request.headers.get("accept", ""):
                return httpx.Response(406)
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

        gate = RobotsGate()
        async with httpx.AsyncClient(
            transport=_transport({"/robots.txt": robots}, gesehen), headers={"Accept": "application/json"}
        ) as client:
            decision = await gate.decide(client, "https://rat.example.de/oparl/system", user_agent="x/1", kind=KIND_API)
        assert not decision.allowed
        assert gesehen[0].headers["accept"] == "text/plain"
        assert gesehen[0].headers["user-agent"] == "x/1"

    async def test_24_stunden_zwischenspeicher_je_host(self, echte_robots):
        gesehen: list[httpx.Request] = []
        uhr = _Uhr()
        gate = RobotsGate(clock=uhr)
        transport = _transport({"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /x\n")}, gesehen)
        async with httpx.AsyncClient(transport=transport) as client:
            for pfad in ("/a", "/b", "/x"):
                await gate.decide(client, f"https://rat.example.de{pfad}", user_agent=DEFAULT_USER_AGENT, kind=KIND_API)
            assert len(gesehen) == 1
            await gate.decide(client, "https://anderer.example.de/a", user_agent=DEFAULT_USER_AGENT, kind=KIND_API)
            assert len(gesehen) == 2
            uhr.jetzt += 24 * 3600 + 1
            await gate.decide(client, "https://rat.example.de/a", user_agent=DEFAULT_USER_AGENT, kind=KIND_API)
            assert len(gesehen) == 3

    async def test_nicht_erreichbar_behaelt_letzte_gueltige_datei(self, echte_robots):
        uhr = _Uhr()
        gate = RobotsGate(clock=uhr)
        antworten: dict[str, Any] = {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /x\n")}
        async with httpx.AsyncClient(transport=_transport(antworten, [])) as client:

            async def erlaubt(pfad: str) -> bool:
                url = f"https://rat.example.de{pfad}"
                return (await gate.decide(client, url, user_agent=DEFAULT_USER_AGENT, kind=KIND_API)).allowed

            assert await erlaubt("/a") and not await erlaubt("/x")
            uhr.jetzt += 24 * 3600 + 1
            antworten["/robots.txt"] = httpx.Response(503)
            # Abgelaufen, neuer Abruf scheitert: die letzte gültige Datei gilt weiter
            assert await erlaubt("/a") and not await erlaubt("/x")

    async def test_ohne_gueltige_datei_sperrt_nicht_erreichbar_und_versucht_spaeter_neu(self, echte_robots):
        uhr = _Uhr()
        gate = RobotsGate(clock=uhr)
        gesehen: list[httpx.Request] = []
        antworten: dict[str, Any] = {"/robots.txt": httpx.Response(500)}
        async with httpx.AsyncClient(transport=_transport(antworten, gesehen)) as client:
            url = "https://rat.example.de/a"
            assert not (await gate.decide(client, url, user_agent=DEFAULT_USER_AGENT, kind=KIND_API)).allowed
            uhr.jetzt += 60
            assert not (await gate.decide(client, url, user_agent=DEFAULT_USER_AGENT, kind=KIND_API)).allowed
            assert len(gesehen) == 1
            uhr.jetzt += 15 * 60
            antworten["/robots.txt"] = httpx.Response(404)
            assert (await gate.decide(client, url, user_agent=DEFAULT_USER_AGENT, kind=KIND_API)).allowed
            assert len(gesehen) == 2

    async def test_zwischenspeicher_je_user_agent(self, echte_robots):
        # Server mit Wortfilter: auch /robots.txt antwortet auf „crawler“ im User-Agent mit 403
        gesehen: list[httpx.Request] = []

        def robots(request: httpx.Request) -> httpx.Response:
            if "crawler" in request.headers.get("user-agent", ""):
                return httpx.Response(403)
            return httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\n")

        gate = RobotsGate()
        kurz = "mandari-ingestor/1.0 (+https://mandari.de; support@mandari.de)"
        async with httpx.AsyncClient(transport=_transport({"/robots.txt": robots}, gesehen)) as client:
            url = "https://rat.example.de/dokumente/a.pdf"
            mit_wort = await gate.decide(client, url, user_agent=DEFAULT_USER_AGENT, kind=KIND_FILES)
            eigene = await gate.decide(client, url, user_agent=kurz, kind=KIND_FILES)
        # Jede Kennung bekommt ihre eigene Antwort; die 403 der einen gilt nicht für die andere
        assert mit_wort.allowed and mit_wort.state == STATE_UNAVAILABLE
        assert not eigene.allowed and eigene.rule == "Disallow: /*.pdf$"
        assert [r.headers["user-agent"] for r in gesehen] == [DEFAULT_USER_AGENT, kurz]

    async def test_ausnahme_laedt_keine_robots_txt(self, echte_robots):
        gesehen: list[httpx.Request] = []
        override = robots_override({"robots_override": {"scope": "api", "note": "Freigabe der Stelle liegt vor"}})
        async with httpx.AsyncClient(transport=_transport({}, gesehen)) as client:
            decision = await RobotsGate().decide(
                client, "https://rat.example.de/a", user_agent=DEFAULT_USER_AGENT, kind=KIND_API, override=override
            )
        assert decision.allowed and decision.state == "override"
        assert gesehen == []


# ---------------------------------------------------------------------------
# OParl-Client
# ---------------------------------------------------------------------------


async def _client_fetch(antworten: dict[str, Any], url: str, **kwargs: Any) -> tuple[Any, list[str], OParlClient]:
    gesehen: list[httpx.Request] = []
    async with OParlClient(max_concurrent=1, wait_time=0, **kwargs) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=_transport(antworten, gesehen), headers={"Accept": "application/json"}
        )
        result = await client.fetch(url)
    return result, [r.url.path for r in gesehen], client


class TestOParlClient:
    async def test_gesperrte_schnittstelle_wird_nicht_abgerufen(self, echte_robots):
        antworten = {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /oparl/\n")}
        result, pfade, client = await _client_fetch(antworten, "https://rat.example.de/oparl/system")
        assert result.data is None
        assert result.error_kind == ERROR_KIND_ROBOTS_BLOCKED
        assert "Disallow: /oparl/" in (result.error or "")
        assert pfade == ["/robots.txt"]
        assert client.error_kind == ERROR_KIND_ROBOTS_BLOCKED
        befund = client.host_findings()[0]
        assert "robots.txt sperrt" in befund.describe()

    async def test_dokumentsperre_laesst_schnittstelle_frei(self, echte_robots):
        antworten = {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\n")}
        result, pfade, client = await _client_fetch(antworten, "https://rat.example.de/oparl/system")
        assert result.data == {"id": "https://rat.example.de/oparl/system"}
        assert pfade == ["/robots.txt", "/oparl/system"]
        assert client.error_kind is None

    async def test_ausnahme_fuer_die_schnittstelle(self, echte_robots):
        antworten = {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")}
        override = robots_override({"robots_override": {"scope": "api", "note": "Freigabe der Stelle liegt vor"}})
        result, pfade, _ = await _client_fetch(
            antworten, "https://rat.example.de/oparl/system", robots_override=override
        )
        assert result.data is not None
        assert pfade == ["/oparl/system"]

    @pytest.mark.parametrize("status", [503, 429])
    async def test_nicht_erreichbare_robots_txt_ist_stoerung_keine_sperre(self, echte_robots, status):
        antworten = {"/robots.txt": httpx.Response(status)}
        result, pfade, client = await _client_fetch(antworten, "https://rat.example.de/oparl/system")
        assert result.data is None
        assert pfade == ["/robots.txt"], "keine Anfrage an die Schnittstelle"
        assert result.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
        assert client.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
        befund = client.host_findings()[0]
        text = befund.describe()
        assert f"nicht erreichbar (HTTP {status})" in text and "zurückgestellt" in text
        assert "untersagt" not in text and "Freigabe" not in text and "sperrt" not in text
        # Kurze Schonung wie bei einer 5xx-Serie statt eines Tages wie bei einer Sperre
        jetzt = datetime.now(UTC)
        quelle = SimpleNamespace(consecutive_failures=1, last_error_at=jetzt, last_error_kind=client.error_kind)
        bis = source_backoff_until(quelle, jetzt)
        assert bis is not None and bis - jetzt <= timedelta(hours=1)

    async def test_netzfehler_bei_robots_txt_ist_stoerung(self, echte_robots):
        def kein_netz(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("keine Verbindung", request=request)

        result, pfade, client = await _client_fetch({"/robots.txt": kein_netz}, "https://rat.example.de/oparl/system")
        assert pfade == ["/robots.txt"]
        assert client.error_kind == ERROR_KIND_SERVER_ERROR_SERIES
        assert "Netzfehler" in client.host_findings()[0].describe()

    async def test_teilsperre_ist_kein_quellenbefund(self, echte_robots):
        antworten = {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /oparl/files\n")}
        gesehen: list[httpx.Request] = []
        async with OParlClient(max_concurrent=1, wait_time=0) as client:
            assert client._client is not None
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=_transport(antworten, gesehen))
            assert (await client.fetch("https://rat.example.de/oparl/papers")).data is not None
            assert (await client.fetch("https://rat.example.de/oparl/files")).error_kind == ERROR_KIND_ROBOTS_BLOCKED
            # Andere Listen kamen durch: keine Schonung der ganzen Quelle
            assert client.error_kind is None


# ---------------------------------------------------------------------------
# Datei-Downloads (Textextraktion)
# ---------------------------------------------------------------------------


class _Speicher:
    def __init__(self, override_config: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        self.updates: list[dict[str, Any]] = []
        self.override_config = override_config or {}
        self.headers = headers or {}
        self.override_abfragen = 0
        self.beansprucht: list[Any] = []
        self.wartend: list[Any] = []

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return self.headers

    async def get_robots_override_for_body(self, body_id: Any) -> Any:
        self.override_abfragen += 1
        return robots_override(self.override_config)

    async def update_file_text(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    async def file_downloads_enabled_for_body(self, body_id: Any) -> bool:
        return True

    async def get_pending_files(self, body_id: Any, **_kwargs: Any) -> list[Any]:
        self.beansprucht.append(body_id)
        return list(self.wartend)


def _datei(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), body_id=uuid4(), download_url=url, access_url=None, mime_type="application/pdf", file_name="a.pdf"
    )


class TestDateien:
    async def test_gesperrte_datei_wird_uebersprungen(self, echte_robots, monkeypatch):
        robots_gate.seed("https://rat.example.de/", RobotsTxt.parse("User-agent: *\nDisallow: /*.pdf$\n"))
        speicher = _Speicher()
        extractor = TextExtractor(speicher)

        async def kein_download(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("gesperrte Datei darf nicht geladen werden")

        monkeypatch.setattr(extractor, "_download", kein_download)
        assert await extractor._process_file(_datei("https://rat.example.de/dokumente/vorlage.pdf")) is False
        assert speicher.updates[0]["status"] == "skipped"
        assert speicher.updates[0]["error"].startswith("robots.txt")

    async def test_ausnahme_fuer_dateien_laedt_trotz_sperre(self, echte_robots, monkeypatch):
        robots_gate.seed("https://rat.example.de/", RobotsTxt.parse("User-agent: *\nDisallow: /*.pdf$\n"))
        config = {"robots_override": {"scope": "files", "note": "Freigabe der Stelle, offizielle Anfrage läuft"}}
        extractor = TextExtractor(_Speicher(config))
        geladen: list[str] = []

        async def download(url: str, *_args: Any) -> bytes:
            geladen.append(url)
            return b"Text"

        monkeypatch.setattr(extractor, "_download", download)
        await extractor._process_file(_datei("https://rat.example.de/dokumente/vorlage.pdf"))
        assert geladen == ["https://rat.example.de/dokumente/vorlage.pdf"]


class _Uhrwerk:
    def __init__(self) -> None:
        self.jetzt = 5000.0

    def __call__(self) -> float:
        return self.jetzt


class TestDateienNichtErreichbar:
    async def test_datei_bleibt_wartend_und_body_wird_zurueckgestellt(self, echte_robots, monkeypatch):
        robots_gate.seed("https://rat.example.de/", RobotsTxt.from_response(503))
        speicher = _Speicher()
        uhr = _Uhrwerk()
        extractor = TextExtractor(speicher, clock=uhr)

        async def kein_download(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("ohne erreichbare robots.txt wird nichts geladen")

        monkeypatch.setattr(extractor, "_download", kein_download)
        datei = _datei("https://rat.example.de/dokumente/vorlage.pdf")
        speicher.wartend = [datei]
        assert await extractor.extract_pending_files(datei.body_id) == 0
        # Nicht „skipped“: die Datei bleibt in der Warteschlange und kommt später wieder dran
        assert speicher.updates == [{"file_id": datei.id, "status": "pending"}]
        assert speicher.beansprucht == [datei.body_id]

        # Bis zum neuen Versuch beansprucht der Worker für diesen Body nichts
        uhr.jetzt += 60
        assert await extractor.extract_pending_files(datei.body_id) == 0
        assert speicher.beansprucht == [datei.body_id]
        uhr.jetzt += 15 * 60
        await extractor.extract_pending_files(datei.body_id)
        assert speicher.beansprucht == [datei.body_id, datei.body_id]

    async def test_neue_ausnahme_wirkt_ohne_neustart(self, echte_robots, monkeypatch):
        robots_gate.seed("https://rat.example.de/", RobotsTxt.parse("User-agent: *\nDisallow: /*.pdf$\n"))
        speicher = _Speicher()
        uhr = _Uhrwerk()
        extractor = TextExtractor(speicher, clock=uhr)
        geladen: list[str] = []

        async def download(url: str, *_args: Any) -> bytes:
            geladen.append(url)
            return b"Text"

        monkeypatch.setattr(extractor, "_download", download)
        datei = _datei("https://rat.example.de/dokumente/vorlage.pdf")
        await extractor._process_file(datei)
        assert speicher.updates[-1]["status"] == "skipped" and geladen == []

        # Ausnahme wird gesetzt (robots_override reiht neu ein); der laufende Worker sieht sie nach Ablauf
        speicher.override_config = {"robots_override": {"scope": "files", "note": "Freigabe der Stelle liegt vor"}}
        uhr.jetzt += 10
        await extractor._process_file(datei)
        assert geladen == [], "innerhalb der Zwischenspeicherzeit gilt der alte Stand"
        uhr.jetzt += 300
        await extractor._process_file(datei)
        assert geladen == ["https://rat.example.de/dokumente/vorlage.pdf"]

    async def test_robots_txt_mit_user_agent_der_quelle(self, echte_robots, monkeypatch):
        # Wortfilter: der Standard bekäme 403 (gilt als „nicht vorhanden“), die Kennung der Quelle die echte Datei
        gesehen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            agent = request.headers.get("user-agent", "")
            gesehen.append(agent)
            if request.url.path == "/robots.txt":
                if "crawler" in agent:
                    return httpx.Response(403)
                return httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\n")
            raise AssertionError("gesperrte Datei darf nicht geladen werden")

        echter_client = httpx.AsyncClient

        def client_mit_transport(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return echter_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("src.client.robots.httpx.AsyncClient", client_mit_transport)
        kennung = "mandari-ingestor/1.0 (+https://mandari.de; support@mandari.de)"
        speicher = _Speicher(headers={"user-agent": kennung})
        extractor = TextExtractor(speicher)
        assert await extractor._process_file(_datei("https://rat.example.de/dokumente/vorlage.pdf")) is False
        assert speicher.updates[-1]["status"] == "skipped"
        assert gesehen == [kennung]


class TestScraperNichtErreichbar:
    async def test_fetcher_meldet_stoerung_statt_sperre(self, echte_robots):
        from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError, RobotsUnreachableError

        robots_gate.seed("https://rat.example.de/", RobotsTxt.from_response(502))
        async with PoliteFetcher(rate_limit_seconds=0) as fetcher:
            with pytest.raises(RobotsUnreachableError) as fehler:
                await fetcher.fetch_text("https://rat.example.de/bi/si0040.asp")
        assert not isinstance(fehler.value, RobotsDisallowedError)
        assert "nicht erreichbar" in str(fehler.value)
