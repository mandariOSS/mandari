# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei fremden Servern: geprüfte Adresse, begrenztes Lesen.

- Der geschützte Client verbindet nur mit der Adresse, die die Zielprüfung bestanden hat: Der Verbindungsaufbau löst
  den Namen selbst auf und prüft; ein Name, der nicht auflösbar ist oder (auch nur teilweise) ins interne Netz zeigt,
  bekommt keine Verbindung.
- Personenfotos werden gestreamt und höchstens bis zur Grenze gelesen.
- Personenfotos gehen über den geschützten Client, schon bei der ersten Anfrage.
"""

from __future__ import annotations

import socket
from typing import Any

import httpcore
import httpx
import pytest

from insight_core.models import OParlBody, OParlPerson, OParlSource
from insight_core.services import person_photos, safe_fetch

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


# --- Personenfotos -------------------------------------------------------------------------------------


def test_fotoabruf_nutzt_den_geschuetzten_client() -> None:
    with person_photos._photo_client() as client:
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

    with httpx.Client(transport=transport) as client:
        ergebnis = person_photos.fetch_person_photo(person, client=client)

    person.refresh_from_db()
    assert ergebnis == "error"
    assert person.photo_error == "Leere oder zu große Datei"
