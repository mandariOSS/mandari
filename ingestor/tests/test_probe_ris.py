"""
Zensus-Werkzeug probe_ris (Issue #114): Fingerprint je Hersteller aus eingefrorenen
Fixtures, robots-Verdict (erlaubt/gesperrt/nicht vorhanden), Gate-Erkennung, 403 auf
unseren User-Agent, OParl-Autodiscovery und das Anfragebudget – alles ohne Netz.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.probe import ris_probe as rp

FIXTURES = Path(__file__).parent / "fixtures" / "ris"
UA = "mandari-ingestor/1.0 (+https://mandari.de/crawler)"


@pytest.mark.parametrize(
    ("datei", "erwartet"),
    [
        ("sessionnet.html", "sessionnet"),
        ("allris3.html", "allris3"),
        ("allris4.html", "allris4"),
        ("sternberg_rim.html", "sternberg_rim"),
        ("rubin.html", "rubin"),
        ("regisafe.html", "regisafe"),
        ("komuna.html", "komuna"),
        ("unbekannt.html", "unbekannt"),
    ],
)
def test_fingerprint_je_hersteller(datei: str, erwartet: str) -> None:
    html = (FIXTURES / datei).read_text(encoding="utf-8")
    assert rp.fingerprint(html, "https://ris.example.org/") == erwartet


def test_fingerprint_rubin_ueber_host() -> None:
    assert rp.fingerprint("<html><div id='app'></div></html>", "https://stadt.gremien.info/") == "rubin"


def test_robots_verdicts() -> None:
    assert rp.robots_verdict(None, UA, ["/"])[0] == "nicht_vorhanden"
    assert rp.robots_verdict("User-agent: *\nAllow: /\n", UA, ["/", "/oparl/system"])[0] == "erlaubt"
    verdict, detail = rp.robots_verdict("User-agent: *\nDisallow: /\n", UA, ["/", "/oparl/system"])
    assert verdict == "gesperrt" and "alles gesperrt" in detail
    # Sperre nur für unser Produkt-Token
    verdict, _ = rp.robots_verdict("User-agent: mandari-ingestor\nDisallow: /\nUser-agent: *\nAllow: /\n", UA, ["/"])
    assert verdict == "gesperrt"


def test_gate_erkennung() -> None:
    assert rp.detect_gate(503, "<title>Just a moment...</title>") == "browser_verification"
    assert rp.detect_gate(200, "<script src='altcha.js'></script>") == "proof_of_work"
    assert rp.detect_gate(403, "<h1>Access denied</h1>") == "waf_forbidden"
    assert rp.detect_gate(200, "<html>Willkommen</html>") is None
    assert rp.detect_gate(403, "", {"cf-mitigated": "challenge"}) == "browser_verification"


def test_oparl_kandidaten_hersteller_zuerst() -> None:
    k = rp.oparl_candidates("https://buergerinfo.example.org/bi/", "sessionnet")
    assert k[0] == "https://buergerinfo.example.org/bi/oparl/1.0/system.asp"
    assert "https://buergerinfo.example.org/oparl/system" in k
    assert len(k) == len(set(k))


class FakeRis:
    """Antwortet wie eine Kommune: robots, Startseite, OParl-Endpunkt; zählt Anfragen."""

    def __init__(
        self,
        *,
        html: str,
        robots: str | None = "User-agent: *\nAllow: /\n",
        oparl_path: str | None = None,
        oparl_body: dict | None = None,
        landing_status: int = 200,
        block_ua: bool = False,
    ) -> None:
        self.html, self.robots, self.oparl_path, self.oparl_body = html, robots, oparl_path, oparl_body
        self.landing_status, self.block_ua = landing_status, block_ua
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.path, request.headers.get("User-Agent", "")))
        pfad = request.url.path
        if pfad == "/robots.txt":
            return httpx.Response(200, text=self.robots) if self.robots is not None else httpx.Response(404)
        if self.oparl_path and pfad == self.oparl_path:
            return httpx.Response(200, json=self.oparl_body or {})
        if pfad.startswith(("/oparl", "/bi/oparl", "/public/oparl", "/webservice")):
            return httpx.Response(404)
        if self.block_ua and "mandari" in request.headers.get("User-Agent", ""):
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(self.landing_status, text=self.html)


async def _probe(fake: FakeRis, url: str = "https://ris.example.org/") -> rp.ProbeResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        return await rp.probe_url(url, client, user_agent=UA)


@pytest.mark.asyncio
async def test_probe_findet_oparl_und_schlaegt_config_vor() -> None:
    fake = FakeRis(
        html=(FIXTURES / "rubin.html").read_text(encoding="utf-8"),
        oparl_path="/oparl/system",
        oparl_body={
            "id": "https://stadt.gremien.info/oparl/system",
            "type": "https://schema.oparl.org/1.0/System",
            "body": "https://stadt.gremien.info/oparl/body",
        },
    )
    r = await _probe(fake, "https://stadt.gremien.info/")

    assert r.vendor == "rubin"
    assert r.oparl_endpoint == "https://stadt.gremien.info/oparl/system"
    assert r.oparl_version == "1.0"
    assert r.sync_config == {"type": "oparl", "system_url": r.oparl_endpoint, "oparl_version": "1.0"}
    assert r.requests <= rp.MAX_REQUESTS


@pytest.mark.asyncio
async def test_probe_meldet_ua_sperre_ohne_umgehung() -> None:
    fake = FakeRis(html=(FIXTURES / "sessionnet.html").read_text(encoding="utf-8"), block_ua=True)
    r = await _probe(fake)

    assert r.landing_status == 403
    assert r.ua_blocked is True
    assert r.vendor == "sessionnet", "Hersteller aus der Vergleichsantwort erkannt"
    assert r.sync_config is None and any("Sperre" in n for n in r.notes)
    neutrale = [ua for _, ua in fake.requests if "mandari" not in ua]
    assert len(neutrale) == 1, "genau eine Vergleichsanfrage mit neutralem Client"
    assert r.requests <= rp.MAX_REQUESTS


@pytest.mark.asyncio
async def test_probe_gate_und_robots_gesperrt() -> None:
    fake = FakeRis(html="<title>Just a moment...</title>", robots="User-agent: *\nDisallow: /\n", landing_status=503)
    r = await _probe(fake)

    assert r.gate == "browser_verification"
    assert r.robots == "gesperrt"
    assert r.sync_config is None


@pytest.mark.asyncio
async def test_probe_sessionnet_ohne_oparl_schlaegt_scraper_vor() -> None:
    fake = FakeRis(html=(FIXTURES / "sessionnet.html").read_text(encoding="utf-8"))
    r = await _probe(fake, "https://buergerinfo.example.org/bi/")

    assert r.vendor == "sessionnet" and r.oparl_endpoint is None
    assert r.sync_config == {"scraper": "sessionnet", "base_url": "https://buergerinfo.example.org/bi"}
    assert r.requests <= rp.MAX_REQUESTS
    assert len(fake.requests) == r.requests


@pytest.mark.asyncio
async def test_probe_ignoriert_oparl_fehlerobjekt() -> None:
    fake = FakeRis(
        html="<html>x</html>",
        oparl_path="/oparl/v1/system",
        oparl_body={"error": "Requested class doesn't exist", "type": "https://schema.oparl.org/1.1/Error"},
    )
    r = await _probe(fake)
    assert r.oparl_endpoint is None


def test_batch_klassifikation() -> None:
    a = rp.ProbeResult(url="https://a", vendor="sessionnet", robots="erlaubt")
    b = rp.ProbeResult(url="https://b", vendor="rubin", oparl_endpoint="https://b/oparl/system")
    c = rp.ProbeResult(url="https://c", vendor="rubin", oparl_endpoint="https://c/oparl/system")
    d = rp.ProbeResult(url="https://d", vendor="regisafe", robots="gesperrt")
    e = rp.ProbeResult(url="https://e", vendor="unbekannt", gate="proof_of_work")

    listen = rp.classify_batch([a, b, c, d, e], registered_urls={"https://c/oparl/system"})

    assert [x["url"] for x in listen["ohne_oparl_robots_frei"]] == ["https://a"]
    assert [x["url"] for x in listen["oparl_vorhanden_nicht_registriert"]] == ["https://b"]


def test_csv_zeilen_und_json() -> None:
    r = rp.ProbeResult(url="https://a", vendor="sessionnet", robots="erlaubt", sync_config={"scraper": "sessionnet"})
    zeile = rp.to_csv_rows([r])[0]
    assert zeile["hersteller"] == "sessionnet" and json.loads(zeile["vorschlag"]) == {"scraper": "sessionnet"}
    assert json.loads(json.dumps(r.to_dict()))["url"] == "https://a"
