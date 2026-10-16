# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SD.NET RIM: leere gefilterte Listen kommen als HTTP 404 mit Code 802 (Issue #1034).

SD.NET RIM (Sternberg) beantwortet eine gefilterte Liste ohne Treffer mit HTTP 404 und dem Fehlerobjekt
``{"error": "Die angeforderte Ressource wurde nicht gefunden.", "code": 802, "type": "SD.NET RIM Webservice"}``
statt mit einer leeren Liste. Aufgezeichnet am 15.10.2026 bei der Stadt Hamm:

- ``body/1/organization?modified_since=2026-10-08T00:00:00``: 404 mit diesem Fehlerobjekt
- ``body/1/organization?modified_since=2020-01-01T00:00:00``: 200, 315 Einträge
- ``body/1/membership`` ohne Filter: 200, 1.159 Einträge, mit Filter und ohne Treffer: 404 wie oben

Mit Filter auf der ersten Seite ist das eine leere, vollständige Liste. Ohne Filter, ohne dieses Fehlerobjekt und auf
Folgeseiten bleibt HTTP 404, was es war. Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from src.client.oparl_client import (
    MODIFIED_SINCE_INCONCLUSIVE,
    MODIFIED_SINCE_SUPPORTED,
    ListFetchError,
    ListMissingError,
    OParlClient,
    _has_list_filter,
)
from src.client.oparl_compat import sdnet_empty_list
from src.sync.orchestrator import MissingLists, TimedLists, sync_complete
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    BASE,
    BODY,
    HOST,
    LISTEN,
    SOURCE,
    Quelle,
    Speicher,
    _client,
    _kommune,
    _ohne_nebenlaeufe,
    _orchestrator,
)

#: Antwort von SD.NET RIM auf eine gefilterte Liste ohne Treffer (Stadt Hamm, 15.10.2026, HTTP 404)
KEINE_TREFFER = {
    "error": "Die angeforderte Ressource wurde nicht gefunden.",
    "code": 802,
    "type": "SD.NET RIM Webservice",
}
MITGLIEDSCHAFTEN = f"{BODY}/membership"


class SdnetQuelle(Quelle):
    """
    SD.NET RIM wie in Hamm: filtert mit ``modified_since``, antwortet auf eine gefilterte Liste ohne Treffer aber mit
    HTTP 404 und ``KEINE_TREFFER``. Listen, die es nicht gibt, beantwortet die Nachbildung ebenso, auch ohne Filter.
    """

    def __call__(self, request: httpx.Request) -> httpx.Response:
        antwort = super().__call__(request)
        gefiltert = "modified_since" in request.url.params
        leer = antwort.status_code == 200 and gefiltert and antwort.json().get("data") == []
        if antwort.status_code == 404 or leer:
            return httpx.Response(404, json=KEINE_TREFFER)
        return antwort


def _mitgliedschaften(geaendert: datetime) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{BASE}/membership/{i}",
            "type": "https://schema.oparl.org/1.1/Membership",
            "modified": geaendert.isoformat(),
        }
        for i in range(3)
    ]


async def _lauf(
    quelle: Quelle, speicher: Speicher, *, full: bool = False, kommune: dict[str, Any] | None = None
) -> tuple[dict[str, Any], OParlClient]:
    """Abgleich einer Kommune wie ``test_aenderungsfenster._lauf``, mit dem Client für den Befund je Host."""
    quelle.anfragen.clear()
    async with _client(quelle, speicher.filtervermerke) as client:
        stats = await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(),
            source_id=UUID(int=9),
            full=full,
            missing_lists=MissingLists(SOURCE, dict(speicher.fehlend)),
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
        )
    return stats, client


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------


def test_fehlerobjekt_von_sdnet_wird_erkannt():
    assert sdnet_empty_list(KEINE_TREFFER)
    assert sdnet_empty_list({**KEINE_TREFFER, "code": "802"})
    assert not sdnet_empty_list({**KEINE_TREFFER, "code": 801})
    assert not sdnet_empty_list({**KEINE_TREFFER, "type": "Anderes RIS"})
    assert not sdnet_empty_list({**KEINE_TREFFER, "error": "Interner Fehler"})
    assert not sdnet_empty_list({"type": "https://schema.oparl.org/1.1/Error", "message": "Not found"})
    assert not sdnet_empty_list(None)
    assert not sdnet_empty_list([KEINE_TREFFER])


@pytest.mark.parametrize(
    ("adresse", "gefiltert"),
    [
        (f"{BODY}/paper?modified_since=2026-10-08T00:00:00", True),
        (f"{BODY}/paper?modified_until=2026-10-08T00:00:00&page=2", True),
        (f"{BODY}/paper?created_since=2026-10-08T00:00:00", True),
        (f"{BODY}/paper?page=2", False),
        (f"{BODY}/paper?modified_since=", False),
        (f"{BODY}/paper", False),
    ],
)
def test_filter_in_der_adresse(adresse, gefiltert):
    assert _has_list_filter(adresse) is gefiltert


# ---------------------------------------------------------------------------
# Inkrementeller Lauf
# ---------------------------------------------------------------------------


async def test_inkrementeller_lauf_ohne_aenderungen_eine_anfrage_je_liste():
    """Hamm: Der erste inkrementelle Lauf brach bei membership ab, Sitzungen und Vorlagen kamen nicht mehr an."""
    quelle = SdnetQuelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.listen["/oparl/body/1/membership"] = _mitgliedschaften(datetime.now(UTC) - timedelta(days=3))
    kommune = _kommune(membership=MITGLIEDSCHAFTEN)
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    for _ in range(2):
        vorher = speicher.begonnen[BODY]
        stats, client = await _lauf(quelle, speicher, kommune=kommune)

        # Erfolg ohne Fehler, Lücke oder Vermerk
        assert stats["incomplete"] == [] and stats["errors"] == []
        assert "missing_lists" not in stats and "empty_required_lists" not in stats
        assert speicher.fehlend == {} and speicher.vermerke == []
        assert stats["organizations"] == stats["meetings"] == stats["papers"] == stats["memberships"] == 0

        # Eine Anfrage je Liste, jede mit Filter; keine Liste läuft danach ohne Filter
        assert quelle.anfragen_je_liste() == dict.fromkeys([*LISTEN, "membership"], 1)
        assert all(f is not None for name in [*LISTEN, "membership"] for f in quelle.filter_der_anfragen(name))
        assert client.filter_notes.lists == {} and client.filter_notes.hosts == {}

        # Keine Zählung als Fehler am Host, der Lauf ist vollständig und das Fenster rückt vor
        gesundheit = client.host_health[HOST]
        assert gesundheit.failed_lists == [] and gesundheit.consecutive_server_errors == 0
        assert gesundheit.ok_requests == len(LISTEN) + 1
        assert client.stats.errors == 0 and client.host_findings() == []
        assert sync_complete([stats], [], client) is True
        assert speicher.begonnen[BODY] > vorher


async def test_aenderungen_kommen_weiter_an():
    """Hat die Liste Treffer, liefert SD.NET sie wie bisher mit HTTP 200; leer bleiben nur die übrigen."""
    quelle = SdnetQuelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.listen["/oparl/body/1/paper"][0]["modified"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)

    stats, _ = await _lauf(quelle, speicher)
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert stats["papers"] == 1 and stats["meetings"] == 0
    assert quelle.anfragen_je_liste() == dict.fromkeys(LISTEN, 1)


# ---------------------------------------------------------------------------
# Wie bisher: ohne Filter, ohne Fehlerobjekt, Folgeseiten
# ---------------------------------------------------------------------------


async def test_404_ohne_filter_bleibt_fehlende_liste_bzw_luecke():
    """Vollabgleich: Ohne Filter heißt dieselbe Antwort „gibt es nicht“, optional vermerkt, Pflichtliste eine Lücke."""
    quelle = SdnetQuelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    del quelle.listen["/oparl/body/1/meeting"]
    kommune = _kommune(membership=MITGLIEDSCHAFTEN)  # membership gibt es in der Nachbildung nicht
    speicher = Speicher()

    stats, client = await _lauf(quelle, speicher, full=True, kommune=kommune)
    assert set(speicher.fehlend) == {MITGLIEDSCHAFTEN}
    assert stats["missing_lists"] == [MITGLIEDSCHAFTEN]
    assert stats["incomplete"] == ["meeting"]
    assert sum("Pflichtliste" in fehler for fehler in stats["errors"]) == 1
    assert BODY not in speicher.begonnen
    assert quelle.filter_der_anfragen("meeting") == [None]
    assert client.filtered_empty_lists == set()

    # Im Client: ohne Filter ein Listenfehler bzw. eine fehlende Liste wie bisher
    async with _client(quelle) as client:
        with pytest.raises(ListFetchError):
            async for _ in client.fetch_list(f"{BODY}/meeting"):
                pass
        with pytest.raises(ListMissingError) as fehlt:
            async for _ in client.fetch_list(MITGLIEDSCHAFTEN, report_missing=True):
                pass
        assert fehlt.value.not_found


@pytest.mark.parametrize(
    "antwort",
    [
        httpx.Response(404),
        httpx.Response(404, text="<html><body>404 Not Found</body></html>"),
        httpx.Response(404, json={**KEINE_TREFFER, "code": 801}),
        httpx.Response(404, json={**KEINE_TREFFER, "type": "Anderes RIS"}),
        httpx.Response(404, json={"type": "https://schema.oparl.org/1.1/Error", "message": "Not found"}),
    ],
    ids=["ohne-inhalt", "html", "anderer-code", "andere-herkunft", "oparl-fehlerobjekt"],
)
async def test_404_mit_filter_ohne_sdnet_fehlerobjekt_wie_bisher(antwort):
    """Ohne das Fehlerobjekt bleibt es beim Nachfragen ohne Filter (Issue #1025)."""
    objekt = {"id": f"{BASE}/membership/1", "type": "https://schema.oparl.org/1.1/Membership"}
    anfragen: list[httpx.URL] = []

    def quelle(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url)
        if "modified_since" in request.url.params:
            return antwort
        return httpx.Response(200, json={"data": [objekt], "links": {}})

    async with _client(quelle) as client:
        seiten = [
            seite
            async for seite in client.fetch_list(
                MITGLIEDSCHAFTEN, modified_since=datetime.now(UTC) - timedelta(hours=3), report_missing=True
            )
        ]
    assert seiten == [[objekt]]
    assert len(anfragen) == 2 and "modified_since" not in anfragen[1].params
    assert set(client.filter_notes.lists) == {MITGLIEDSCHAFTEN}
    assert client.filtered_empty_lists == set()


async def test_404_auf_folgeseite_bleibt_ein_listenfehler():
    liste = f"{BODY}/paper"
    seit = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    objekte = [{"id": f"{BASE}/paper/{i}", "type": "https://schema.oparl.org/1.1/Paper"} for i in range(2)]

    def quelle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(404, json=KEINE_TREFFER)
        weiter = str(httpx.URL(liste, params={"modified_since": seit, "page": "2"}))
        return httpx.Response(200, json={"data": objekte, "links": {"next": weiter}})

    async with _client(quelle) as client:
        seiten: list[list[dict[str, Any]]] = []
        with pytest.raises(ListFetchError) as fehler:
            async for seite in client.fetch_list(liste, modified_since=datetime.fromisoformat(seit)):
                seiten.append(seite)
    assert seiten == [objekte]
    assert "page=2" in fehler.value.page_url
    assert client.host_health[HOST].failed_lists == [liste]


async def test_filter_aus_den_listenparametern_zaehlt():
    """Ein Filter aus ``sync_config["list_params"]`` (``modified_until``) macht 404 mit Code 802 zur leeren Liste."""
    anfragen: list[httpx.URL] = []

    def quelle(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url)
        return httpx.Response(404, json=KEINE_TREFFER)

    async with _client(quelle) as client:
        client.list_params = {"modified_until": "2020-01-01T00:00:00+00:00"}
        assert [seite async for seite in client.fetch_list(f"{BODY}/paper", report_missing=True)] == []
    assert len(anfragen) == 1
    assert client.filtered_empty_lists == {f"{BODY}/paper"}


# ---------------------------------------------------------------------------
# Prüfung von modified_since und Vermerk fehlender Listen
# ---------------------------------------------------------------------------


async def test_pruefung_wertet_keine_treffer_als_filter():
    """Nach dem Vollabgleich: „keine Treffer“ heißt, die Quelle filtert; nicht „ignored“, „unsupported“ oder offen."""
    quelle = SdnetQuelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    async with _client(quelle) as client:
        befund = await client.probe_modified_since(f"{BODY}/paper", datetime.now(UTC) - timedelta(days=7))
    assert befund == MODIFIED_SINCE_SUPPORTED
    assert len(quelle.anfragen) == 1

    # HTTP 404 ohne das Fehlerobjekt bleibt ohne Aussage
    andere = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    andere.mit_filter["/oparl/body/1/paper"] = 404
    async with _client(andere) as client:
        befund = await client.probe_modified_since(f"{BODY}/paper", datetime.now(UTC) - timedelta(days=7))
    assert befund == MODIFIED_SINCE_INCONCLUSIVE


async def test_vermerkte_liste_bleibt_bei_keinen_treffern_vermerkt():
    """
    Die tägliche Prüfung einer vermerkten Liste fragt mit Filter (sieben Tage). „Keine Treffer“ sagt nicht, ob es die
    Liste gibt: Der Vermerk bleibt und gilt wieder einen Tag, statt jeden Lauf neu anzufragen.
    """
    quelle = SdnetQuelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    kommune = _kommune(membership=MITGLIEDSCHAFTEN)
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    vorher = datetime.now(UTC)
    speicher.fehlend[MITGLIEDSCHAFTEN] = vorher - timedelta(days=1, minutes=5)

    stats, _ = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 1
    assert stats["incomplete"] == [] and stats["errors"] == []
    geprueft = speicher.fehlend[MITGLIEDSCHAFTEN]
    assert geprueft is not None and geprueft >= vorher

    # Der nächste Lauf fragt sie nicht an
    stats, _ = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 0

    # Gibt es Änderungen, kommen sie mit der nächsten Prüfung, und der Vermerk wird gelöscht
    speicher.fehlend[MITGLIEDSCHAFTEN] = datetime.now(UTC) - timedelta(days=2)
    quelle.listen["/oparl/body/1/membership"] = _mitgliedschaften(datetime.now(UTC) - timedelta(days=2))
    stats, _ = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["memberships"] == 3 and speicher.fehlend == {}
