# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei fremden Servern: Ziel, Größe und robots.txt.

- Datei-Downloads gehen nur an öffentliche Adressen; geprüft wird die Adresse, mit der die Verbindung entsteht
  (eine Namensauflösung je Verbindung). Nicht auflösbare Namen, interne Adressen und Weiterleitungen dorthin enden
  ohne Anfrage, die Datei gilt als übersprungen.
- Dateien werden unkomprimiert angefordert; mehrfach kodierte Antworten gelten als zu groß.
- Regeln der robots.txt werden ohne regulären Ausdruck abgeglichen, in linearer Zeit.
"""

from __future__ import annotations

import socket
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpcore
import httpx
import pytest
from mandari_oparl import ziel
from mandari_oparl.robots import Rule

from src.config import settings
from src.extraction import extractor
from src.extraction.extractor import TextExtractor

OEFFENTLICH = "93.184.215.14"


def _aufloesung(monkeypatch: pytest.MonkeyPatch, *antworten: list[str]) -> list[str]:
    """``getaddrinfo`` liefert je Aufruf die nächste Antwort (die letzte bleibt); zurück kommen die gefragten Namen."""
    gefragt: list[str] = []
    folge = list(antworten)

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        gefragt.append(host)
        adressen = folge.pop(0) if len(folge) > 1 else folge[0]
        if not adressen:
            raise socket.gaierror("nicht auflösbar")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (adresse, 0)) for adresse in adressen]

    monkeypatch.setattr(ziel.socket, "getaddrinfo", getaddrinfo)
    return gefragt


# --- Zielprüfung ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adresse",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1", "0.0.0.0", "::ffff:127.0.0.1"],
)
def test_interne_adressen_sind_gesperrt(adresse: str) -> None:
    assert ziel.allowed_address(adresse) is False
    with pytest.raises(ziel.BlockedDestinationError):
        ziel.checked_address(adresse)


def test_private_netze_nur_mit_schalter_loopback_nie() -> None:
    assert ziel.allowed_address("10.0.0.5", allow_private=True) is True
    assert ziel.allowed_address("127.0.0.1", allow_private=True) is False
    assert ziel.allowed_address("169.254.169.254", allow_private=True) is False
    assert ziel.checked_address(OEFFENTLICH) == OEFFENTLICH


def test_name_wird_einmal_aufgeloest_und_alle_adressen_muessen_zulaessig_sein(monkeypatch: pytest.MonkeyPatch) -> None:
    gefragt = _aufloesung(monkeypatch, [OEFFENTLICH, "2606:2800:21f:cb07:6820:80da:af6b:8b2c"])
    assert ziel.checked_address("ris.example.org") == OEFFENTLICH  # IPv4 zuerst
    assert gefragt == ["ris.example.org"]

    _aufloesung(monkeypatch, [OEFFENTLICH, "10.0.0.5"])
    with pytest.raises(ziel.BlockedDestinationError):
        ziel.checked_address("gemischt.example.org")

    _aufloesung(monkeypatch, [])
    with pytest.raises(ziel.BlockedDestinationError, match="nicht auflösbar"):
        ziel.checked_address("unbekannt.example.org")


def test_ausgenommene_hosts_werden_nicht_geprueft(monkeypatch: pytest.MonkeyPatch) -> None:
    gefragt = _aufloesung(monkeypatch, ["10.0.0.5"])

    assert ziel.checked_address("Mandari.", allowed_hosts=["mandari", " "]) is None
    assert gefragt == []


@pytest.mark.asyncio
async def test_verbindung_geht_an_die_geprüfte_adresse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Erst öffentlich, beim zweiten Mal intern: Es gibt nur eine Auflösung, verbunden wird mit ihrem Ergebnis."""
    gefragt = _aufloesung(monkeypatch, [OEFFENTLICH], ["127.0.0.1"])
    verbunden: list[tuple[str, int]] = []

    async def connect_tcp(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        verbunden.append((host, port))
        raise httpcore.ConnectError("Test: keine echte Verbindung")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)

    async with httpx.AsyncClient(transport=ziel.PinnedAsyncTransport()) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://ris.example.org/datei.pdf")

    assert verbunden == [(OEFFENTLICH, 80)]
    assert gefragt == ["ris.example.org"]


@pytest.mark.asyncio
async def test_internes_ziel_ohne_verbindung(monkeypatch: pytest.MonkeyPatch) -> None:
    _aufloesung(monkeypatch, ["127.0.0.1"])
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", lambda *a, **k: pytest.fail("keine Verbindung erlaubt"))

    async with httpx.AsyncClient(transport=ziel.PinnedAsyncTransport()) as client:
        with pytest.raises(ziel.BlockedDestinationError) as fehler:
            await client.get("http://intern.example.org/geheim")

    assert str(fehler.value.request.url) == "http://intern.example.org/geheim"
    assert not isinstance(fehler.value, ziel.UnresolvableHostError)


@pytest.mark.asyncio
async def test_nicht_aufloesbarer_name_bleibt_im_transport_ein_voruebergehender_fehler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aufloesung(monkeypatch, [])
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", lambda *a, **k: pytest.fail("keine Verbindung erlaubt"))

    async with httpx.AsyncClient(transport=ziel.PinnedAsyncTransport()) as client:
        with pytest.raises(ziel.UnresolvableHostError) as fehler:
            await client.get("http://ris.example.org/datei.pdf")

    # Wie ein Verbindungsfehler: Aufrufer stellen zurück, statt das Ziel als intern zu werten
    assert isinstance(fehler.value, httpx.ConnectError)
    assert str(fehler.value.request.url) == "http://ris.example.org/datei.pdf"


@pytest.mark.asyncio
async def test_dns_ausfall_sperrt_den_host_nicht_dauerhaft_und_stellt_die_datei_zurueck(
    monkeypatch: pytest.MonkeyPatch, ohne_takt: None
) -> None:
    _aufloesung(monkeypatch, [])
    storage = _Storage()

    ergebnis = await TextExtractor(storage)._process_file(_datei("http://ris.example.org/datei.pdf"))

    assert ergebnis is False
    assert [eintrag["status"] for eintrag in storage.texte] == ["pending"]


# --- Datei-Download des Ingestors ------------------------------------------------------------------------


class _Storage:
    def __init__(self) -> None:
        self.texte: list[dict[str, Any]] = []

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def update_file_text(self, **werte: Any) -> None:
        self.texte.append(werte)

    async def body_stores_files(self, body_id: Any) -> bool:
        return True


def _datei(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        body_id=uuid.uuid4(),
        download_url=url,
        access_url=None,
        mime_type="application/pdf",
        file_name="a.pdf",
    )


@pytest.fixture
def ohne_takt(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Kein Warten, Ablage im Temp-Verzeichnis, und jede echte Verbindung lässt den Test scheitern."""
    monkeypatch.setattr(settings, "oparl_files_root", str(tmp_path))
    monkeypatch.setattr(settings, "request_interval", 0)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", lambda *a, **k: pytest.fail("keine Verbindung erlaubt"))


@pytest.mark.asyncio
async def test_datei_mit_interner_adresse_wird_uebersprungen(ohne_takt: None) -> None:
    """Schon die robots.txt des internen Hosts wird nicht abgerufen; die Datei gilt als übersprungen."""
    storage = _Storage()

    ergebnis = await TextExtractor(storage)._process_file(_datei("http://127.0.0.1:9200/_cat/indices"))

    assert ergebnis is False
    # Übersprungen, nicht zurückgestellt: Ein Ziel im internen Netz ist kein vorübergehender Ausfall der Quelle
    assert [eintrag["status"] for eintrag in storage.texte] == ["skipped"]


@pytest.mark.asyncio
async def test_download_verbindet_auch_mit_robots_ausnahme_nur_mit_geprueften_adressen(
    monkeypatch: pytest.MonkeyPatch, ohne_takt: None
) -> None:
    """Mit einer robots-Ausnahme der Quelle entfällt die robots-Prüfung; der Download selbst prüft die Adresse."""

    async def erlaubt(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(allowed=True, unreachable=False, reason="")

    monkeypatch.setattr(extractor.robots_gate, "decide", erlaubt)
    storage = _Storage()

    ergebnis = await TextExtractor(storage)._process_file(_datei("http://10.1.2.3/akte.pdf"))

    assert ergebnis is False
    assert storage.texte[-1]["status"] == "skipped"
    assert storage.texte[-1]["error"].startswith("Blocked:")


@pytest.mark.asyncio
async def test_download_fordert_unkomprimiert_an_und_weist_mehrfach_kodiertes_ab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(settings, "oparl_files_root", str(tmp_path))
    gesehen: list[httpx.Request] = []
    echter_client = httpx.AsyncClient

    def antwort(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(
            200, stream=httpx.ByteStream(b"x" * 64), headers={"Content-Encoding": "identity, gzip , GZIP"}
        )

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        ohne_transport = {k: v for k, v in kwargs.items() if k != "transport"}
        return echter_client(transport=httpx.MockTransport(antwort), **ohne_transport)

    monkeypatch.setattr("src.extraction.extractor.httpx.AsyncClient", client)

    async def nichts(ziel_url: str) -> None:
        return None

    with pytest.raises(extractor.FileTooLargeError):
        await TextExtractor(_Storage())._download_to_file("https://ris.example.org/a.pdf", None, nichts)

    assert gesehen[0].headers["accept-encoding"] == "identity"
    # Die angefangene Datei bleibt nicht liegen
    assert list(tmp_path.rglob("*.part")) == []


# --- robots.txt ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("muster", "pfad", "passt"),
    [
        ("/intern", "/intern/a.pdf", True),
        ("/intern", "/oeffentlich", False),
        ("/*.pdf$", "/a/b.pdf", True),
        ("/*.pdf$", "/a/b.pdf?x=1", False),
        ("/*.pdf", "/a/b.pdf?x=1", True),
        ("/a*b*c", "/a-b-c-d", True),
        ("/a*b*c", "/a-c-b", False),
        ("/a*a$", "/a", False),
        ("/a*a$", "/aa", True),
        ("*", "/alles", True),
        ("/$", "/", True),
        ("/$", "/a", False),
        ("/a**b", "/a-b", True),
    ],
)
def test_regeln_passen_wie_bisher(muster: str, pfad: str, passt: bool) -> None:
    assert Rule.build(False, muster).matches(pfad) is passt


@pytest.mark.parametrize("muster", ["/" + "*a" * 20 + "$", "/" + "*" * 20 + "z", "/" + "*a*b" * 10])
def test_viele_sterne_bleiben_schnell(muster: str) -> None:
    regel = Rule.build(False, muster)
    pfad = "/" + "a" * 2000 + "c"

    start = time.perf_counter()
    for _ in range(200):
        regel.matches(pfad)

    assert time.perf_counter() - start < 1.0
