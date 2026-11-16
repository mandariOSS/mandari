# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antworten fremder Server, die ganz in den Speicher kommen, haben eine Obergrenze und eine Gesamtfrist.

Die robots.txt jedes Hosts und jede Seite der Scraper liest der Ingestor über ``redirects.read_limited``: gestreamt,
höchstens eine feste Menge entpackter Bytes, höchstens eine Kompressionslage. ``redirects.Deadline`` begrenzt die
Zeit, die ein Abruf insgesamt im Netz verbringt; das Zeitlimit von ``httpx`` gilt nur je Lesevorgang.

Keine Abrufe fremder Server: Die Antworten kommen als Strom aus ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from mandari_oparl.robots import KIND_API, MAX_BYTES

from src.client import redirects, robots
from src.client.robots import RobotsGate
from src.scrapers import politeness
from src.scrapers.politeness import PoliteFetcher

MIB = 1024 * 1024
URL = "https://rat.example.de/bi/si0040.asp"


class _Strom(httpx.AsyncByteStream):
    """Antwort als Strom in Stücken; zählt, wie viele Stücke gelesen wurden."""

    def __init__(self, stuecke: list[bytes], pause: float = 0.0) -> None:
        self.stuecke = stuecke
        self.pause = pause
        self.gelesen = 0
        self.geschlossen = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for stueck in self.stuecke:
            if self.pause:
                await asyncio.sleep(self.pause)
            self.gelesen += 1
            yield stueck

    async def aclose(self) -> None:
        self.geschlossen = True


def _in_stuecken(daten: bytes, groesse: int = redirects.READ_CHUNK_BYTES) -> list[bytes]:
    """Stücke, wie sie aus dem Netz kommen; gelesen wird in Stücken von ``READ_CHUNK_BYTES``."""
    return [daten[start : start + groesse] for start in range(0, len(daten), groesse)]


@functools.cache
def _gzip_nullen(megabyte: int) -> bytes:
    """gzip von ``megabyte`` MiB Nullbytes, erzeugt ohne den entpackten Inhalt am Stück zu halten."""
    packer = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    block = bytes(MIB)
    teile = [packer.compress(block) for _ in range(megabyte)]
    return b"".join([*teile, packer.flush()])


async def _lesen(antwort: httpx.Response, grenze: int, *, frist: float | None = None) -> bytes:
    """Wie die Aufrufer: gestreamt senden, dann mit Obergrenze lesen."""

    async def keine_weiterleitung(_ziel: str) -> None:
        raise AssertionError("keine Weiterleitung erwartet")

    deadline = redirects.Deadline(frist) if frist is not None else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: antwort)) as client:
        response = await redirects.send(
            client,
            client.build_request("GET", URL),
            before_redirect=keine_weiterleitung,
            stream=True,
            deadline=deadline,
        )
        return await redirects.read_limited(response, grenze, deadline=deadline)


# --- read_limited ------------------------------------------------------------------------------------------


async def test_gepackte_antwort_endet_an_der_grenze() -> None:
    gepackt = _gzip_nullen(256)
    assert len(gepackt) < 400 * 1024, "wenige hundert Kilobyte, die entpackt 256 MiB ergäben"
    strom = _Strom(_in_stuecken(gepackt))
    assert len(strom.stuecke) >= 3

    tracemalloc.start()
    try:
        inhalt = await _lesen(httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=strom), MIB)
        _, spitze = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(inhalt) == MIB
    # Entpackt wurde nur bis zur Grenze: Die Spitze bleibt weit unter den 256 MiB des ganzen Inhalts
    assert spitze < 8 * MIB, f"Speicherspitze {spitze / MIB:.1f} MiB"
    assert strom.gelesen < len(strom.stuecke), "der Rest der Antwort bleibt ungelesen"
    assert strom.geschlossen


async def test_ungepackte_antwort_endet_an_der_grenze() -> None:
    strom = _Strom([b"a" * redirects.READ_CHUNK_BYTES] * 50)

    inhalt = await _lesen(httpx.Response(200, stream=strom), 100_000)

    assert inhalt == b"a" * 100_000
    assert strom.gelesen <= 4 and strom.geschlossen


@pytest.mark.parametrize(
    ("kopf", "packen"),
    [
        ("gzip", gzip.compress),
        ("x-gzip", gzip.compress),
        ("identity, gzip", gzip.compress),
        # deflate mit zlib-Hülle (laut Standard) und ohne (manche Server)
        ("deflate", zlib.compress),
        ("deflate", lambda daten: zlib.compress(daten)[2:-4]),
    ],
)
async def test_eine_lage_wird_entpackt(kopf: str, packen: Callable[[bytes], bytes]) -> None:
    text = "User-agent: *\nDisallow: /geschützt\n".encode() * 200
    strom = _Strom(_in_stuecken(packen(text), 500))

    assert await _lesen(httpx.Response(200, headers={"Content-Encoding": kopf}, stream=strom), MIB) == text


@pytest.mark.parametrize("kopf", ["gzip, gzip", "gzip, deflate", "br", "zstd"])
async def test_mehrere_lagen_und_fremde_verfahren_sind_ein_abruffehler(kopf: str) -> None:
    zweifach = gzip.compress(gzip.compress(bytes(4 * MIB)))
    strom = _Strom(_in_stuecken(zweifach))

    with pytest.raises(httpx.DecodingError):
        await _lesen(httpx.Response(200, headers={"Content-Encoding": kopf}, stream=strom), MIB)

    assert strom.gelesen == 0, "abgelehnt vor dem ersten gelesenen Stück"
    assert strom.geschlossen


async def test_fehlerhafte_daten_sind_ein_abruffehler() -> None:
    strom = _Strom([b"kein gzip, nur Text" * 10])

    with pytest.raises(httpx.DecodingError):
        await _lesen(httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=strom), MIB)
    assert strom.geschlossen


async def test_tropfende_antwort_endet_mit_der_gesamtfrist() -> None:
    # Jedes Stück kommt rechtzeitig für ein Zeitlimit je Lesevorgang, die Antwort als Ganzes nie
    strom = _Strom([b"x"] * 200, pause=0.02)

    with pytest.raises(httpx.ReadTimeout):
        await _lesen(httpx.Response(200, stream=strom), MIB, frist=0.2)

    assert strom.gelesen < 50
    assert strom.geschlossen


async def test_gesamtfrist_zaehlt_nur_die_zeit_im_netz() -> None:
    frist = redirects.Deadline(0.2)
    request = httpx.Request("GET", URL)

    async with frist.spend(request):
        await asyncio.sleep(0.01)
    # Warten auf den Takt je Host zwischen zwei Anfragen geht nicht von der Frist ab
    await asyncio.sleep(0.3)
    async with frist.spend(request):
        await asyncio.sleep(0.01)

    with pytest.raises(httpx.ReadTimeout):
        async with frist.spend(request):
            await asyncio.sleep(1)


# --- robots.txt --------------------------------------------------------------------------------------------


def _client(antwort: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(antwort))


async def test_robots_txt_wird_nur_bis_zur_auswertungsgrenze_gelesen(echte_robots: None) -> None:
    regeln = b"User-agent: *\nDisallow: /geschuetzt\n"
    strom = _Strom(_in_stuecken(gzip.compress(regeln + b"#" * (8 * MAX_BYTES))))
    gesehen: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=strom)

    gate = RobotsGate()
    gelesen: list[int] = []
    echtes_lesen = redirects.read_limited

    async def mitzaehlen(response: httpx.Response, limit: int, **kwargs: Any) -> bytes:
        inhalt = await echtes_lesen(response, limit, **kwargs)
        gelesen.append(len(inhalt))
        return inhalt

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(redirects, "read_limited", mitzaehlen)
        async with _client(antwort) as client:
            gesperrt = await gate.decide(client, "https://rat.example.de/geschuetzt/1", user_agent="x/1", kind=KIND_API)
            frei = await gate.decide(client, "https://rat.example.de/offen", user_agent="x/1", kind=KIND_API)

    assert gelesen == [MAX_BYTES]
    assert not gesperrt.allowed and frei.allowed
    # Nur gzip angeboten: Das entpackt der Ingestor selbst, mit Obergrenze
    assert gesehen[0].headers["accept-encoding"] == "gzip"


async def test_robots_txt_in_zwei_lagen_gilt_als_nicht_erreichbar(echte_robots: None) -> None:
    def antwort(request: httpx.Request) -> httpx.Response:
        inhalt = gzip.compress(gzip.compress(b"User-agent: *\nAllow: /\n"))
        return httpx.Response(200, headers={"Content-Encoding": "gzip, gzip"}, stream=_Strom([inhalt]))

    async with _client(antwort) as client:
        entscheidung = await RobotsGate().decide(client, "https://rat.example.de/a", user_agent="x/1", kind=KIND_API)

    # Zurückstellen statt „alles erlaubt“: Eine nicht lesbare Datei gibt den Host nicht frei
    assert entscheidung.unreachable


async def test_tropfende_robots_txt_gilt_als_nicht_erreichbar(
    echte_robots: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(robots, "ROBOTS_DEADLINE", 0.2)
    strom = _Strom([b"#"] * 200, pause=0.02)

    async with _client(lambda request: httpx.Response(200, stream=strom)) as client:
        entscheidung = await RobotsGate().decide(client, "https://rat.example.de/a", user_agent="x/1", kind=KIND_API)

    assert entscheidung.unreachable
    assert strom.geschlossen


@pytest.mark.parametrize(("status", "erlaubt", "zurueckgestellt"), [(404, True, False), (503, False, True)])
async def test_robots_txt_ohne_erfolg_wird_nicht_gelesen(
    echte_robots: None, status: int, erlaubt: bool, zurueckgestellt: bool
) -> None:
    strom = _Strom([b"<html>" + b"x" * 1000] * 100)

    async with _client(lambda request: httpx.Response(status, stream=strom)) as client:
        entscheidung = await RobotsGate().decide(client, "https://rat.example.de/a", user_agent="x/1", kind=KIND_API)

    assert (entscheidung.allowed, entscheidung.unreachable) == (erlaubt, zurueckgestellt)
    assert strom.gelesen == 0 and strom.geschlossen


# --- Seiten der Scraper ------------------------------------------------------------------------------------


async def _seite(
    antwort: Callable[[httpx.Request], httpx.Response], **optionen: Any
) -> tuple[str | None, PoliteFetcher]:
    fetcher = PoliteFetcher(rate_limit_seconds=0.0, max_retries=1, **optionen)
    fetcher._client = _client(antwort)
    try:
        return await fetcher.fetch_text(URL), fetcher
    finally:
        await fetcher._client.aclose()


async def test_seite_gepackt_mit_zeichensatz_der_antwort() -> None:
    html = "<html><body>Bürgerschaft, Sitzung am 3. März</body></html>"
    gesehen: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "text/html; charset=ISO-8859-1"},
            stream=_Strom(_in_stuecken(gzip.compress(html.encode("iso-8859-1")), 20)),
        )

    text, fetcher = await _seite(antwort)

    assert text == html
    assert fetcher.last_content == html.encode("iso-8859-1")
    assert (fetcher.last_status, fetcher.last_url) == (200, URL)
    assert gesehen[0].headers["accept-encoding"] == "gzip"


async def test_seite_ueber_der_groessengrenze_wird_nicht_ausgewertet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "MAX_PAGE_BYTES", 64 * 1024)
    strom = _Strom(_in_stuecken(_gzip_nullen(256)))

    def antwort(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=strom)

    text, fetcher = await _seite(antwort)

    assert text is None and fetcher.last_content is None
    assert strom.gelesen < len(strom.stuecke), "nach der Grenze wird nicht weitergelesen"


async def test_seite_genau_an_der_grenze_wird_ausgewertet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "MAX_PAGE_BYTES", 1000)

    text, _ = await _seite(lambda request: httpx.Response(200, stream=_Strom([b"a" * 600, b"b" * 400])))

    assert text == "a" * 600 + "b" * 400


async def test_seite_in_zwei_lagen_gilt_als_nicht_abrufbar() -> None:
    def antwort(request: httpx.Request) -> httpx.Response:
        inhalt = gzip.compress(gzip.compress(b"<html></html>"))
        return httpx.Response(200, headers={"Content-Encoding": "gzip, gzip"}, stream=_Strom([inhalt]))

    text, fetcher = await _seite(antwort)

    assert text is None and fetcher.last_status is None


async def test_tropfende_seite_endet_mit_der_gesamtfrist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(politeness, "PAGE_DEADLINE", 0.2)
    strom = _Strom([b"<p>"] * 200, pause=0.02)

    text, fetcher = await _seite(lambda request: httpx.Response(200, stream=strom))

    assert text is None and fetcher.last_status is None
    assert strom.gelesen < 50 and strom.geschlossen
