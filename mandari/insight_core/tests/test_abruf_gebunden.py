# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei fremden Servern: geprüfte Adresse, begrenztes Lesen.

- Der geschützte Client verbindet nur mit der Adresse, die die Zielprüfung bestanden hat: Der Verbindungsaufbau löst
  den Namen selbst auf und prüft; ein Name, der nicht auflösbar ist oder (auch nur teilweise) ins interne Netz zeigt,
  bekommt keine Verbindung.
- Antworten werden gestreamt und höchstens bis zur Grenze des Aufrufers gelesen (robots.txt, Personenfotos); mehrfach
  kodierte Antworten weisen wir ab.
- Personenfotos gehen über den geschützten Client, schon bei der ersten Anfrage.
"""

from __future__ import annotations

import gzip
import socket
import tracemalloc
from typing import Any

import httpcore
import httpx
import pytest
from mandari_oparl.robots import MAX_BYTES, Decision

from insight_core.models import OParlBody, OParlPerson, OParlSource
from insight_core.services import host_pacing, person_photos, redirects, robots, safe_fetch

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

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return gefragt


def _verbindungen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Verbindungsversuche aufzeichnen, ohne dass eine echte Verbindung entsteht."""
    verbunden: list[tuple[str, int]] = []

    def connect_tcp(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        verbunden.append((host, port))
        raise httpcore.ConnectError("Test: keine echte Verbindung")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    return verbunden


# --- Bindung an die geprüfte Adresse -------------------------------------------------------------------


def test_verbindung_geht_an_die_geprüfte_adresse(monkeypatch: pytest.MonkeyPatch) -> None:
    _aufloesung(monkeypatch, [OEFFENTLICH, "2606:2800:21f:cb07:6820:80da:af6b:8b2c"])
    verbunden = _verbindungen(monkeypatch)

    with safe_fetch.guarded_client() as client, pytest.raises(httpx.ConnectError):
        client.get("http://ris.example.org/datei.pdf")

    assert verbunden == [(OEFFENTLICH, 80)]  # die geprüfte Adresse, IPv4 zuerst; nicht der Name


def test_zweite_aufloesung_ins_interne_netz_bekommt_keine_verbindung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Prüfung vor der Anfrage sieht eine öffentliche Adresse, der Verbindungsaufbau danach eine interne."""
    _aufloesung(monkeypatch, [OEFFENTLICH], ["127.0.0.1"])
    verbunden = _verbindungen(monkeypatch)

    with safe_fetch.guarded_client() as client, pytest.raises(safe_fetch.BlockedDestinationError) as fehler:
        client.get("http://wechselhaft.example.org/geheim")

    assert verbunden == []
    assert str(fehler.value.request.url) == "http://wechselhaft.example.org/geheim"


def test_nicht_aufloesbarer_name_bekommt_keine_verbindung(monkeypatch: pytest.MonkeyPatch) -> None:
    _aufloesung(monkeypatch, [])
    verbunden = _verbindungen(monkeypatch)

    with safe_fetch.guarded_client() as client, pytest.raises(safe_fetch.BlockedDestinationError):
        client.get("http://unbekannt.example.org/")

    assert verbunden == []


def test_eigener_host_bleibt_ausgenommen(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.SITE_URL = "https://mandari.example.org"
    gefragt = _aufloesung(monkeypatch, ["10.0.0.5"])
    verbunden = _verbindungen(monkeypatch)

    with safe_fetch.guarded_client() as client, pytest.raises(httpx.ConnectError):
        client.get("http://mandari.example.org/datei.pdf")

    # Ohne eigene Auflösung und ohne Bindung: Die Verbindung geht wie bisher an den Namen
    assert (gefragt, verbunden) == ([], [("mandari.example.org", 80)])


def test_eigener_transport_ersetzt_die_bindung() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))

    with safe_fetch.guarded_client(transport=transport) as client:
        assert client.get(f"http://{OEFFENTLICH}/").content == b"ok"


# --- Begrenztes Lesen ----------------------------------------------------------------------------------


def _antwort(inhalt: bytes, **kopf: str) -> httpx.Response:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=inhalt, headers=kopf))
    )
    return client.send(client.build_request("GET", "https://ris.example.org/x"), stream=True)


def test_lesen_endet_an_der_grenze() -> None:
    assert redirects.read_limited(_antwort(b"a" * 1000), 1000) == b"a" * 1000
    with pytest.raises(redirects.ResponseTooLargeError):
        redirects.read_limited(_antwort(b"a" * 1001), 1000)
    assert redirects.read_limited(_antwort(b"a" * 5000), 1000, truncate=True) == b"a" * 1000


def test_stark_komprimierte_antwort_wird_nicht_ganz_entpackt() -> None:
    gepackt = gzip.compress(bytes(200 * 1024 * 1024))
    assert len(gepackt) < 300 * 1024
    antwort = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(gepackt), headers={"Content-Encoding": "gzip"})
        )
    ).send(httpx.Request("GET", "https://ris.example.org/x"), stream=True)

    tracemalloc.start()
    with pytest.raises(redirects.ResponseTooLargeError):
        redirects.read_limited(antwort, 1024 * 1024)
    _aktuell, spitze = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert spitze < 20 * 1024 * 1024


def _kodiert(inhalt: bytes, kodierung: str) -> httpx.Response:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(inhalt), headers={"Content-Encoding": kodierung}
            )
        )
    ).send(httpx.Request("GET", "https://ris.example.org/x"), stream=True)


@pytest.mark.parametrize("kodierung", ["gzip, gzip", "br", "zstd"])
def test_mehrfach_und_fremd_kodierte_antworten_werden_abgewiesen(kodierung: str) -> None:
    with pytest.raises(httpx.DecodingError):
        redirects.read_limited(_kodiert(b"x", kodierung), 1000)


def test_gzip_und_deflate_werden_entpackt() -> None:
    import zlib

    text = b"User-agent: *\nDisallow: /intern\n" * 100
    assert redirects.read_limited(_kodiert(gzip.compress(text), "gzip"), 10_000) == text
    assert redirects.read_limited(_kodiert(zlib.compress(text), "deflate"), 10_000) == text
    assert redirects.read_limited(_kodiert(gzip.compress(text), "gzip"), 100, truncate=True) == text[:100]
    with pytest.raises(httpx.DecodingError):
        redirects.read_limited(_kodiert(b"kein gzip", "gzip"), 1000)


def test_robots_txt_wird_nur_bis_zur_grenze_gelesen(monkeypatch: pytest.MonkeyPatch, echte_robots: Any) -> None:
    gross = b"User-agent: *\nDisallow: /intern\n" + b"# Kommentar\n" * 500_000
    assert len(gross) > 4 * MAX_BYTES
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=gross))
    monkeypatch.setattr(safe_fetch, "guarded_client", lambda **kwargs: httpx.Client(transport=transport, **kwargs))
    monkeypatch.setattr("insight_core.services.host_pacing.wait", lambda *a, **k: True)

    status, body = robots._fetch("https://ris.example.org/datei.pdf", robots.USER_AGENT)

    assert status == 200
    assert len(body) == MAX_BYTES and body.startswith(b"User-agent: *")


# --- Personenfotos -------------------------------------------------------------------------------------


def test_fotoabruf_nutzt_den_geschuetzten_client() -> None:
    with person_photos._client() as client:
        assert isinstance(client._transport, safe_fetch.PinnedTransport)
        assert client.event_hooks["request"] == [safe_fetch._check_request]


@pytest.mark.django_db
def test_zu_grosses_foto_wird_nicht_ganz_gelesen(monkeypatch: pytest.MonkeyPatch) -> None:
    source = OParlSource.objects.create(name="Foto-RIS", url="https://ris.foto.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.foto.example/oparl/body/1", source=source, name="Fotostadt"
    )
    person = OParlPerson.objects.create(
        external_id="https://ris.foto.example/oparl/person/1",
        body=body,
        name="Anna Foto",
        raw_json={"image": "https://ris.foto.example/fotos/anna.jpg"},
    )
    gross = bytes(person_photos.MAX_BYTES + 1)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=gross, headers={"Content-Type": "image/jpeg"})
    )
    monkeypatch.setattr(robots, "check", lambda *a, **k: Decision(True, "parsed"))
    monkeypatch.setattr(host_pacing, "wait", lambda *a, **k: True)

    with httpx.Client(transport=transport) as client:
        ergebnis = person_photos.fetch_person_photo(person, client=client)

    person.refresh_from_db()
    assert ergebnis == "error"
    assert person.photo_error == "Leere oder zu große Datei"
