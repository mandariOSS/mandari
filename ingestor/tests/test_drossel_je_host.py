# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drossel je Host über alle Quellen und Prozesse (src/client/host_pacing.py, mandari_oparl.pacing).

Bisher drosselte jeder Client für sich: ``request_interval`` galt je Abgleichslauf, ohne ihn wartete jeder
von bis zu 20 Abrufplätzen nur 0,05 s. Zwei Quellen auf demselben Host, Daemon und Einzelabgleich oder
Ingestor und Django fragten unabhängig voneinander an. Jetzt reservieren alle über einen gemeinsamen
Zeitstempel je Host. Keine Abrufe fremder Server, kein echtes Redis: ein Ersatz bildet das Lua-Skript nach.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mandari_oparl.pacing import DEFAULT_INTERVAL, LocalSchedule, interval_for, key

from src.client.host_pacing import REDIS_RETRY_SECONDS, HostPacer, host_pacer
from src.client.oparl_client import OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings


class _Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0
        self.geschlafen: list[float] = []

    def __call__(self) -> float:
        return self.jetzt

    async def sleep(self, sekunden: float) -> None:
        self.geschlafen.append(round(sekunden, 3))


class _RedisErsatz:
    """Bildet RESERVE_SCRIPT nach: ein Zeitstempel je Schlüssel, Uhr des „Servers“ für alle Prozesse."""

    def __init__(self, uhr: _Uhr) -> None:
        self.uhr = uhr
        self.werte: dict[str, int] = {}
        self.aufrufe: list[str] = []

    async def __call__(self, keys: list[str], args: list[int]) -> int:
        schluessel, (intervall, hoechstens) = keys[0], args
        self.aufrufe.append(schluessel)
        jetzt = int(self.uhr() * 1000)
        start = max(jetzt, self.werte.get(schluessel, 0))
        warten = start - jetzt
        if hoechstens >= 0 and warten > hoechstens:
            return -1
        self.werte[schluessel] = start + intervall
        return warten


def _prozess(uhr: _Uhr, redis: Any) -> HostPacer:
    pacer = HostPacer("redis://ersatz", clock=uhr, sleep=uhr.sleep)
    pacer._script = redis
    return pacer


class TestReservierung:
    async def test_zwei_prozesse_teilen_den_takt_je_host(self):
        uhr = _Uhr()
        redis = _RedisErsatz(uhr)
        daemon, django = _prozess(uhr, redis), _prozess(uhr, redis)

        assert await daemon.wait("https://rat.example.de/oparl/system", 1.0)
        assert await django.wait("https://RAT.example.de/dokumente/a.pdf", 1.0)
        assert await daemon.wait("https://rat.example.de/oparl/papers", 1.0)
        # erste Anfrage sofort, dann je eine Sekunde später – auch über Prozesse hinweg
        assert uhr.geschlafen == [1.0, 2.0]
        assert set(redis.aufrufe) == {key("rat.example.de")}

    async def test_andere_hosts_warten_nicht(self):
        uhr = _Uhr()
        pacer = _prozess(uhr, _RedisErsatz(uhr))
        for host in ("a.example", "b.example", "c.example"):
            assert await pacer.wait(f"https://{host}/x", 1.0)
        assert uhr.geschlafen == []

    async def test_hoechstwartezeit_reserviert_nichts(self):
        uhr = _Uhr()
        redis = _RedisErsatz(uhr)
        pacer = _prozess(uhr, redis)
        for _ in range(3):
            await pacer.wait("https://rat.example.de/x", 1.0)
        stand = dict(redis.werte)
        assert await pacer.wait("https://rat.example.de/x", 1.0, max_wait=2.5) is False
        assert redis.werte == stand
        assert await pacer.wait("https://rat.example.de/x", 1.0, max_wait=3.0) is True

    async def test_abstand_null_drosselt_nicht(self):
        uhr = _Uhr()
        redis = _RedisErsatz(uhr)
        pacer = _prozess(uhr, redis)
        assert await pacer.wait("https://rat.example.de/x", 0)
        assert redis.aufrufe == []

    async def test_ohne_redis_drosselt_der_prozess_und_versucht_es_spaeter_erneut(self, caplog):
        uhr = _Uhr()

        async def kaputt(keys: list[str], args: list[int]) -> int:
            raise ConnectionError("redis weg")

        pacer = _prozess(uhr, kaputt)
        assert await pacer.wait("https://rat.example.de/x", 1.0)
        assert await pacer.wait("https://rat.example.de/x", 1.0)
        assert uhr.geschlafen == [1.0]
        assert "nur im Prozess" in caplog.text

        # Nach der Sperrfrist wird Redis erneut versucht
        redis = _RedisErsatz(uhr)
        uhr.jetzt += REDIS_RETRY_SECONDS + 1
        pacer._script = redis
        await pacer.wait("https://rat.example.de/x", 1.0)
        assert redis.aufrufe == [key("rat.example.de")]


def test_abstand_aus_der_quellenkonfiguration(echte_drossel: None):
    assert interval_for({}) == DEFAULT_INTERVAL == 1.0
    assert interval_for({"request_interval": 0.25}) == 1.0  # nie schneller als eine Anfrage je Sekunde
    assert interval_for({"request_interval": 2.5}) == 2.5
    assert interval_for({"request_interval": 600}) == DEFAULT_INTERVAL  # Tippfehler gilt als nicht gesetzt
    assert SourceFetchOptions.from_sync_config({"request_interval": 2}).request_interval == 2.0
    assert type(settings).model_fields["request_interval"].default == 1.0


def test_lokaler_rueckfall_reserviert_wie_das_skript():
    plan = LocalSchedule()
    assert plan.reserve("h", 0.0, 1.0, None) == 0.0
    assert plan.reserve("h", 0.2, 1.0, None) == pytest.approx(0.8)
    assert plan.reserve("h", 0.2, 1.0, 1.0) is None
    assert plan.reserve("anderer", 0.2, 1.0, None) == 0.0


# ---------------------------------------------------------------------------
# OParl-Client: zwei Quellen auf demselben Host
# ---------------------------------------------------------------------------


def _client(zeiten: list[float], **kwargs: Any) -> OParlClient:
    def handler(request: httpx.Request) -> httpx.Response:
        zeiten.append(time.monotonic())
        return httpx.Response(200, json={"id": str(request.url)})

    client = OParlClient(max_concurrent=5, **kwargs)
    client._semaphore = asyncio.Semaphore(5)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_zwei_quellen_auf_einem_host_teilen_sich_den_standardabstand(echte_drossel, monkeypatch):
    monkeypatch.setattr(settings, "request_interval", 0.05)
    zeiten: list[float] = []
    quelle_a, quelle_b = _client(zeiten), _client(zeiten)
    try:
        await asyncio.gather(
            *(quelle_a.fetch(f"https://rat.example.de/oparl/a/{i}", use_cache=False) for i in range(4)),
            *(quelle_b.fetch(f"https://rat.example.de/oparl/b/{i}", use_cache=False) for i in range(4)),
            # skip_wait umgeht die Drossel nicht
            quelle_b.fetch("https://rat.example.de/oparl/system", use_cache=False, skip_wait=True),
        )
    finally:
        for client in (quelle_a, quelle_b):
            assert client._client is not None
            await client._client.aclose()

    zeiten.sort()
    abstaende = [b - a for a, b in zip(zeiten, zeiten[1:], strict=False)]
    assert len(zeiten) == 9
    assert min(abstaende) >= 0.04
    assert zeiten[-1] - zeiten[0] >= 0.39


async def test_abstand_der_quelle_geht_vor(echte_drossel, monkeypatch):
    monkeypatch.setattr(settings, "request_interval", 5.0)
    zeiten: list[float] = []
    client = _client(zeiten, request_interval=0.01)
    try:
        await asyncio.gather(*(client.fetch(f"https://rat.example.de/x/{i}", use_cache=False) for i in range(3)))
    finally:
        assert client._client is not None
        await client._client.aclose()
    assert zeiten[-1] - zeiten[0] < 1.0
    assert client.effective_interval == 0.01


async def test_datei_download_zaehlt_mit(echte_drossel, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from src.extraction.extractor import DownloadedFile, TextExtractor

    reserviert: list[tuple[str, float]] = []

    async def wait(url: str, interval: float, **_kwargs: Any) -> bool:
        reserviert.append((url, interval))
        return True

    monkeypatch.setattr(host_pacer, "wait", wait)

    class _Speicher:
        async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
            return {}

        async def get_fetch_options_for_body(self, body_id: Any) -> SourceFetchOptions:
            return SourceFetchOptions.from_sync_config({"request_interval": 3})

        async def update_file_text(self, **kwargs: Any) -> None:
            return None

    extractor = TextExtractor(_Speicher())

    async def download(url: str, *_args: Any) -> DownloadedFile:
        ziel = tmp_path / "a.part"
        ziel.write_bytes(b"Text")
        return DownloadedFile(path=ziel, size=4, sha256=hashlib.sha256(b"Text").hexdigest(), head=b"Text")

    monkeypatch.setattr(extractor, "_download_to_file", download)
    datei = SimpleNamespace(
        id=uuid4(),
        body_id=uuid4(),
        download_url="https://rat.example.de/a.txt",
        access_url=None,
        mime_type="text/plain",
        file_name="a.txt",
    )
    await extractor._process_file(datei)
    assert ("https://rat.example.de/a.txt", 3.0) in reserviert


async def test_horizont_je_host_bleibt_kurz(echte_drossel, monkeypatch):
    """
    Grenze gleichzeitiger Anfragen je Host: Nicht jeder freie Abrufplatz reserviert einen eigenen Zeitpunkt.
    Ohne Grenze reichte der Takt bei zehn Plätzen neun Abstände in die Zukunft, und die Vorschau in Django fand
    während eines Abgleichs keinen freien Zeitpunkt.
    """
    monkeypatch.setattr(settings, "request_interval", 0.05)
    monkeypatch.setattr(settings, "host_max_concurrent", 2)
    gewartet: list[float] = []

    async def schlafen(sekunden: float) -> None:
        gewartet.append(sekunden)
        await asyncio.sleep(sekunden)

    monkeypatch.setattr(host_pacer, "_sleep", schlafen)
    aktiv = 0
    hoechstens = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal aktiv, hoechstens
        aktiv += 1
        hoechstens = max(hoechstens, aktiv)
        await asyncio.sleep(0.01)
        aktiv -= 1
        return httpx.Response(200, json={"id": str(request.url)})

    client = OParlClient(max_concurrent=10)
    client._semaphore = asyncio.Semaphore(10)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await asyncio.gather(*(client.fetch(f"https://rat.example.de/oparl/p/{i}", use_cache=False) for i in range(10)))
    finally:
        await client._client.aclose()

    assert hoechstens <= 2
    assert gewartet, "die Drossel hat gewartet"
    # Höchstens zwei Abstände voraus (plus Laufzeit), nicht neun
    assert max(gewartet) <= 2 * 0.05 + 0.03


async def _belegt(url: str) -> bool:
    """Ist die Grenze für den Host von ``url`` ausgeschöpft? (kein Platz binnen 50 ms)"""

    async def platz() -> None:
        async with host_pacer.limit(url, 1.0):
            pass

    try:
        await asyncio.wait_for(platz(), timeout=0.05)
    except TimeoutError:
        return True
    return False


async def test_andere_hosts_teilen_die_grenze_nicht(echte_drossel, monkeypatch):
    monkeypatch.setattr(settings, "host_max_concurrent", 1)
    async with host_pacer.limit("https://a.example/x", 1.0), host_pacer.limit("https://b.example/x", 1.0):
        assert await _belegt("https://A.example:443/y"), "derselbe Server in anderer Schreibweise teilt die Grenze"
        assert not await _belegt("https://c.example/y")
    assert not await _belegt("https://a.example/x")
    # Ohne Drossel keine Grenze
    async with host_pacer.limit("https://a.example/x", 0), host_pacer.limit("https://a.example/x", 0):
        pass


async def test_platz_zieht_bei_weiterleitung_zum_ziel_host(echte_drossel, monkeypatch):
    """
    Die Grenze gilt auch für das Ziel einer Weiterleitung (Issue #1027): Leiten mehrere Hosts auf denselben
    Datei-Host weiter, sind dort trotzdem höchstens ``host_max_concurrent`` Anfragen offen. Der Platz beim
    Ausgangshost wird dabei frei.
    """
    monkeypatch.setattr(settings, "host_max_concurrent", 1)
    async with host_pacer.limit("https://rat.example.de/a.pdf", 1.0) as platz:
        await platz.move_to("https://rat.example.de/b.pdf")  # derselbe Host: Platz bleibt
        assert await _belegt("https://rat.example.de/x")
        await platz.move_to("https://dateien.example.org/a.pdf")
        assert await _belegt("https://dateien.example.org/x"), "der Ziel-Host ist belegt"
        assert not await _belegt("https://rat.example.de/x"), "der Ausgangshost ist wieder frei"
    assert not await _belegt("https://dateien.example.org/x")


async def test_weiterleitungen_in_gegenrichtung_blockieren_nicht(echte_drossel, monkeypatch):
    """Ein Abruf hält nie Plätze bei zwei Hosts zugleich: a → b und b → a gleichzeitig laufen beide durch."""
    monkeypatch.setattr(settings, "host_max_concurrent", 1)
    monkeypatch.setattr(settings, "request_interval", 0.01)
    beide_gestartet = asyncio.Barrier(2)

    async def abruf(start: str, ziel: str) -> None:
        async with host_pacer.limit(start, 1.0) as platz:
            await beide_gestartet.wait()
            await platz.move_to(ziel)
            await asyncio.sleep(0)

    await asyncio.wait_for(
        asyncio.gather(
            abruf("https://a.example/x", "https://b.example/x"), abruf("https://b.example/y", "https://a.example/y")
        ),
        timeout=1,
    )


async def test_oparl_abruf_haelt_grenze_beim_ziel_host(echte_drossel, monkeypatch):
    """Zwei Quellen leiten auf denselben Host weiter: Dort ist mit Grenze 1 nie mehr als eine Anfrage offen."""
    monkeypatch.setattr(settings, "request_interval", 0.01)
    monkeypatch.setattr(settings, "host_max_concurrent", 1)
    monkeypatch.setattr(host_pacer, "_sleep", lambda _s: asyncio.sleep(0))
    offen: dict[str, int] = {}
    hoechstens: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host != "ziel.example.org":
            return httpx.Response(302, headers={"Location": f"https://ziel.example.org/{host}{request.url.path}"})
        offen[host] = offen.get(host, 0) + 1
        hoechstens[host] = max(hoechstens.get(host, 0), offen[host])
        await asyncio.sleep(0.01)
        offen[host] -= 1
        return httpx.Response(200, json={"id": str(request.url)})

    client = OParlClient(max_concurrent=10)
    client._semaphore = asyncio.Semaphore(10)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    client.fetch(f"https://{quelle}.example.de/oparl/p/{i}", use_cache=False)
                    for quelle in ("eins", "zwei", "drei")
                    for i in range(3)
                )
            ),
            timeout=10,
        )
    finally:
        await client._client.aclose()

    assert hoechstens == {"ziel.example.org": 1}


async def test_scraper_halten_grenze_beim_ziel_host(echte_drossel, monkeypatch):
    """
    Auch Scraper zählen in der Grenze gleichzeitiger Anfragen je Host, am Ziel einer Weiterleitung ebenso (Issue
    #1036): Drei Quellen leiten auf denselben Host weiter, dort ist mit Grenze 1 nie mehr als eine Anfrage offen.
    """
    from src.scrapers.politeness import PoliteFetcher

    monkeypatch.setattr(settings, "host_max_concurrent", 1)
    monkeypatch.setattr(host_pacer, "_sleep", lambda _s: asyncio.sleep(0))
    offen = 0
    hoechstens = 0
    gesehen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal offen, hoechstens
        gesehen.append(str(request.url))
        if request.url.host != "ziel.example.org":
            return httpx.Response(302, headers={"Location": f"https://ziel.example.org/{request.url.host}/"})
        offen += 1
        hoechstens = max(hoechstens, offen)
        await asyncio.sleep(0.01)
        offen -= 1
        return httpx.Response(200, text="<html>Sitzung</html>")

    async def quelle(name: str) -> None:
        async with PoliteFetcher(rate_limit_seconds=0.01, source_name=name) as fetcher:
            assert fetcher._client is not None
            await fetcher._client.aclose()
            fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
            for i in range(3):
                assert await fetcher.fetch_text(f"https://{name}.example.de/seite/{i}") == "<html>Sitzung</html>"

    await asyncio.wait_for(asyncio.gather(*(quelle(name) for name in ("eins", "zwei", "drei"))), timeout=10)

    assert len([adresse for adresse in gesehen if "ziel.example.org" in adresse]) == 9
    assert hoechstens == 1
