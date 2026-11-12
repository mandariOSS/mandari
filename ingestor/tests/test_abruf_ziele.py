# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei fremden Servern: Ziel und Größe.

- Datei-Downloads gehen nur an öffentliche Adressen; geprüft wird die Adresse, mit der die Verbindung entsteht
  (eine Namensauflösung je Verbindung). Nicht auflösbare Namen, interne Adressen und Weiterleitungen dorthin enden
  ohne Anfrage, die Datei gilt als übersprungen.
- Dateien werden unkomprimiert angefordert; mehrfach kodierte Antworten gelten als zu groß.
"""

from __future__ import annotations

import socket
import uuid
from types import SimpleNamespace
from typing import Any

import httpcore
import httpx
import pytest
from mandari_oparl import ziel

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
    """Jede echte Verbindung lässt den Test scheitern."""
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", lambda *a, **k: pytest.fail("keine Verbindung erlaubt"))


@pytest.mark.asyncio
async def test_datei_mit_interner_adresse_wird_uebersprungen(ohne_takt: None) -> None:
    """Kein Abruf bei einem Ziel im internen Netz; die Datei gilt als übersprungen."""
    storage = _Storage()

    ergebnis = await TextExtractor(storage)._process_file(_datei("http://127.0.0.1:9200/_cat/indices"))

    assert ergebnis is False
    # Übersprungen, nicht gescheitert: Ein Ziel im internen Netz ist kein vorübergehender Ausfall der Quelle
    assert [eintrag["status"] for eintrag in storage.texte] == ["skipped"]


@pytest.mark.asyncio
async def test_download_fordert_unkomprimiert_an_und_weist_mehrfach_kodiertes_ab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    with pytest.raises(extractor.FileTooLargeError):
        await TextExtractor(_Storage())._download("https://ris.example.org/a.pdf")

    assert gesehen[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_download_bricht_an_der_groessengrenze_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    echter_client = httpx.AsyncClient

    def antwort(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 4096))

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        ohne_transport = {k: v for k, v in kwargs.items() if k != "transport"}
        return echter_client(transport=httpx.MockTransport(antwort), **ohne_transport)

    monkeypatch.setattr("src.extraction.extractor.httpx.AsyncClient", client)
    extraktor = TextExtractor(_Storage())
    extraktor.max_size_bytes = 1024

    with pytest.raises(extractor.FileTooLargeError):
        await extraktor._download("https://ris.example.org/a.pdf")
