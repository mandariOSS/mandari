# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Listen, deren Folgeseiten immer dieselben Einträge liefern (Issue #1088).

Der Sammelserver von OWL-IT (SessionNet-OParl) liefert in der Kommunenliste auf jeder Folgeseite (``?page=2``,
``?page=3``, …) dieselben Einträge mit einem neuen ``links.next``, ohne ``totalPages`` oder ``last``. Die
Kreis-Erkennung aus #869 prüft nur Seitenadressen und greift bei immer neuen Adressen nicht; Objektlisten haben keine
Seitengrenze und liefen endlos im Takt des Hosts. Jetzt bricht ``OParlClient.fetch_list`` ab, wenn zwei Seiten in
Folge keine Kennung bringen, die nicht schon auf ihrer vorigen Seite stand. Seiten ohne Kennungen zählen nicht.

Antworten aus ``httpx.MockTransport``, keine Abrufe fremder Server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.client.oparl_client import REPEATED_PAGES_ERROR, ListFetchError, OParlClient
from src.sync.orchestrator import MAX_BODY_PAGES, missed_bodies_note, sync_complete
from tests.test_quellenanlage import (  # noqa: F401 - _ohne_pause: autouse-Fixture
    BASE,
    LISTE,
    SYSTEM,
    Quelle,
    _abgleich,
    _client,
    _ids,
    _kommunen_lesen,
    _ohne_pause,
    _seite_url,
    kommune,
)

PAPER = f"{BASE}/body/1/paper"
#: Nach so vielen Seiten antwortet auch eine endlose Liste mit HTTP 404: Ein Rückschritt hängt den Test nicht auf
GRENZE = 50
BEGINN = datetime(2020, 1, 1, tzinfo=UTC)
HOST = "ris.example.org"


def vorlage(nummer: int) -> dict[str, Any]:
    return {"id": f"{BASE}/paper/{nummer}", "type": "https://schema.oparl.org/1.1/Paper", "name": f"Vorlage {nummer}"}


def vorlagen(*nummern: int) -> list[dict[str, Any]]:
    return [vorlage(n) for n in nummern]


class Seiten:
    """
    Objektliste ``PAPER``: Seite n trägt ``inhalt(n)``, ``links.next`` zeigt auf ``?page=n+1`` bis Seite ``letzte``
    (ohne ``letzte`` immer weiter, wie bei OWL-IT). Weitere Parameter (Filter einer Zeitscheibe) zählen nicht.
    """

    def __init__(self, inhalt: Callable[[int], list[Any]], letzte: int | None = None) -> None:
        self.inhalt = inhalt
        self.letzte = letzte
        self.anfragen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(str(request.url))
        nummer = int(request.url.params.get("page", "1"))
        if request.url.path != urlparse(PAPER).path or nummer > (self.letzte or GRENZE):
            return httpx.Response(404)
        links: dict[str, str] = {}
        if self.letzte is None or nummer < self.letzte:
            links["next"] = f"{PAPER}?page={nummer + 1}"
        return httpx.Response(200, json={"data": self.inhalt(nummer), "links": links})


#: Die drei Wege durch ``fetch_list``: ganze Liste, ab vorliegender erster Seite, Zeitscheibe
WEGE = ["liste", "folgeseite", "zeitscheibe"]


def _lesen(client: OParlClient, weg: str) -> AsyncIterator[list[dict[str, Any]]]:
    if weg == "folgeseite":
        # Die erste Seite hat der Aufrufer schon, der Vergleich beginnt mit der ersten selbst gelesenen Seite
        return client.fetch_list(PAPER, next_page=f"{PAPER}?page=2")
    if weg == "zeitscheibe":
        return client.fetch_list(
            PAPER, modified_since=BEGINN, modified_until=BEGINN + timedelta(days=30), strict_filter=True
        )
    return client.fetch_list(PAPER)


def _seitennummer(url: str) -> int:
    return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])


#: Folgeseiten ohne neue Kennung gegenüber ihrer vorigen Seite
IM_KREIS: dict[str, Callable[[int], list[Any]]] = {
    "dieselben_eintraege": lambda n: vorlagen(1, 2, 3),
    "andere_reihenfolge": lambda n: vorlagen(1, 2, 3) if n % 2 else vorlagen(3, 2, 1),
    "teilmenge": lambda n: vorlagen(1, 2, 3) if n == 1 else vorlagen(2, 3),
}


@pytest.mark.parametrize("fall", sorted(IM_KREIS))
@pytest.mark.parametrize("weg", WEGE)
async def test_folgeseiten_mit_immer_denselben_eintraegen_brechen_nach_drei_seiten_ab(weg: str, fall: str) -> None:
    quelle = Seiten(IM_KREIS[fall])
    client = _client(quelle)
    seiten: list[list[str]] = []
    try:
        with pytest.raises(ListFetchError) as excinfo:
            async for page in _lesen(client, weg):
                seiten.append(_ids(page))
    finally:
        await client._client.aclose()

    # Höchstens drei Seitenanfragen: die erste gelesene Seite und zwei Wiederholungen in Folge
    assert len(quelle.anfragen) == 3
    assert len(seiten) == 3
    erste = 2 if weg == "folgeseite" else 1
    assert [_seitennummer(a) for a in quelle.anfragen] == [erste, erste + 1, erste + 2]
    # Abbruch wie bei #869: die nicht mehr gelesene Folgeseite, eigener Fehlertext, Liste als abgebrochen vermerkt
    assert excinfo.value.list_url == PAPER
    assert _seitennummer(excinfo.value.page_url) == erste + 3
    assert excinfo.value.result.error == REPEATED_PAGES_ERROR
    assert "dieselben Einträge" in str(excinfo.value)
    assert client.host_health[HOST].failed_lists == [PAPER]


async def test_seiten_ohne_kennungen_zaehlen_nicht() -> None:
    """Leere Seiten dazwischen setzen den Zähler nicht zurück: Verglichen wird mit der vorigen Seite mit Kennungen."""
    quelle = Seiten(lambda n: vorlagen(1, 2, 3) if n % 2 else [])
    client = _client(quelle)
    try:
        with pytest.raises(ListFetchError) as excinfo:
            [page async for page in client.fetch_list(PAPER)]
    finally:
        await client._client.aclose()

    assert [_seitennummer(a) for a in quelle.anfragen] == [1, 2, 3, 4, 5]
    assert excinfo.value.result.error == REPEATED_PAGES_ERROR


#: Listen, die trotz wiederholter Einträge vollständig durchlaufen (je sechs Seiten)
DURCHLAUF: dict[str, Callable[[int], list[Any]]] = {
    # Jede Seite wiederholt zwei Einträge der vorigen und bringt einen neuen (Seiten verschoben)
    "ueberlappung": lambda n: vorlagen(n, n + 1, n + 2),
    # Je eine voll wiederholte Seite, danach wieder neue Kennungen: nie zweimal in Folge
    "einzelne_wiederholungen": lambda n: vorlagen((n + 1) // 2 * 10, (n + 1) // 2 * 10 + 1),
    # Die letzten Seiten wiederholen sich, aber die Liste endet (kein links.next)
    "wiederholung_am_ende": lambda n: vorlagen(n) if n < 4 else vorlagen(4),
    # Einträge ohne Kennung (oder mit Kennung, die keine Zeichenkette ist) zählen nicht
    "ohne_kennungen": lambda n: [{"name": "ohne Kennung"}, {"id": 7, "name": "Zahl"}],
}


@pytest.mark.parametrize("fall", sorted(DURCHLAUF))
@pytest.mark.parametrize("weg", WEGE)
async def test_normale_liste_mit_wiederholten_eintraegen_laeuft_vollstaendig(weg: str, fall: str) -> None:
    quelle = Seiten(DURCHLAUF[fall], letzte=6)
    client = _client(quelle)
    try:
        pages = [page async for page in _lesen(client, weg)]
    finally:
        await client._client.aclose()

    erste = 2 if weg == "folgeseite" else 1
    assert [_seitennummer(a) for a in quelle.anfragen] == list(range(erste, 7))
    assert len(pages) == 7 - erste
    assert client.host_health[HOST].failed_lists == []


#: Kommunen auf jeder Seite der Kommunenliste von OWL-IT (Sammelserver, mehrere Kommunen)
OWL_IT = [kommune(n) for n in range(1, 29)]


class OwlIt(Quelle):
    """Kommunenliste wie bei OWL-IT: jede Seite dieselben 28 Kommunen, immer ein neues ``links.next``."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/oparl/bodies":
            return super().__call__(request)
        self.anfragen.append(str(request.url))
        nummer = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"data": OWL_IT, "links": {"next": _seite_url(nummer + 1)}})


@pytest.mark.parametrize("weg", [LISTE, SYSTEM, "adressliste"])
async def test_kommunenliste_von_owl_it_endet_unvollstaendig_statt_nach_zwanzig_seiten(weg: str) -> None:
    quelle = OwlIt()
    client = _client(quelle)
    missed: list[str] = []
    try:
        bodies = await _kommunen_lesen(client, weg, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == _ids(OWL_IT)
    # Liegt die erste Seite schon vor (Erkennung der Adresse), beginnt der Vergleich mit der zweiten
    gelesen = 3 if weg == SYSTEM else 4
    assert [a for a in quelle.anfragen if a != SYSTEM] == [_seite_url(n) for n in range(1, gelesen + 1)]
    assert gelesen < MAX_BODY_PAGES
    assert missed == [_seite_url(gelesen + 1)]
    assert client.host_health[HOST].failed_lists == [LISTE]
    assert sync_complete([{"incomplete": []}] * len(bodies), missed, client) is False


async def test_abgleich_mit_kommunenliste_von_owl_it_ist_unvollstaendig(monkeypatch: pytest.MonkeyPatch) -> None:
    result, speicher, abgeglichen = await _abgleich(monkeypatch, OwlIt())

    # Die 28 Kommunen werden abgeglichen, der Lauf gilt aber nicht als vollständig
    assert sorted(abgeglichen) == sorted(_ids(OWL_IT))
    assert speicher.vollstaendig == [False]
    assert result.errors == [missed_bodies_note([_seite_url(5)])]
