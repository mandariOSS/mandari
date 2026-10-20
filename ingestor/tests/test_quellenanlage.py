# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenlisten über alle Seiten (Issue #869) und Neuanlage inaktiv bzw. ungelistet (Teil von Issue #555).

Bisher las die Erkennung einer Quelle, deren Adresse eine Kommunenliste ist, nur deren erste Seite; die Kommunen auf
den Folgeseiten (``links.next``) wurden nie abgeglichen. Jetzt folgt sie der Liste wie jeder anderen: im Takt, und ein
Fehler auf einer Folgeseite bricht ab und macht den Abgleich unvollständig, ebenso die Seitengrenze und eine Liste,
die im Kreis läuft. Dazu: Neue Quellen sind inaktiv, eine vorhandene Quelle behält ``is_active`` (auch abgeschaltet),
neu entdeckte Kommunen sind nicht gelistet, außer bei der eigenen Session-Schnittstelle eines Mandanten.

Antworten aus ``httpx.MockTransport``, keine Abrufe fremder Server. Die Speicher-Tests laufen gegen PostgreSQL
(``INGESTOR_TEST_DATABASE_URL``, in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from mandari_oparl import IdBases
from rich.console import Console
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from src import main as cli
from src.client.oparl_client import ListFetchError, OParlClient
from src.config import settings
from src.storage.database import ENRICHMENT_FIELDS, DatabaseStorage
from src.storage.models import Base, OParlBody, OParlSource
from src.sync.orchestrator import MAX_BODY_PAGES, SyncOrchestrator, missed_bodies_note, sync_complete
from src.sync.processor import OParlProcessor

BASE = "https://ris.example.org/oparl"
SYSTEM = f"{BASE}/system"
LISTE = f"{BASE}/bodies"
SEITE_2 = f"{LISTE}?page=2"
SEITE_3 = f"{LISTE}?page=3"
#: Kommunen je Seite der Kommunenliste
SEITEN = {1: [1, 2], 2: [3, 4], 3: [5]}
SOURCE_ID = UUID(int=869)


def kommune(nummer: int) -> dict[str, Any]:
    return {"id": f"{BASE}/body/{nummer}", "type": "https://schema.oparl.org/1.1/Body", "name": f"Kommune {nummer}"}


def seite(nummer: int) -> dict[str, Any]:
    links = {"next": f"{LISTE}?page={nummer + 1}"} if nummer + 1 in SEITEN else {}
    return {"data": [kommune(n) for n in SEITEN[nummer]], "links": links}


class Quelle:
    """Kommunenliste mit drei Seiten (5 Kommunen) und System-Objekt; die zweite Seite lässt sich stören."""

    def __init__(self, folgeseite: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.folgeseite = folgeseite
        self.anfragen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.anfragen.append(url)
        if url == SYSTEM:
            return httpx.Response(
                200, json={"id": SYSTEM, "type": "https://schema.oparl.org/1.1/System", "body": LISTE}
            )
        if request.url.path == "/oparl/bodies":
            nummer = int(request.url.params.get("page", "1"))
            if nummer == 2 and self.folgeseite is not None:
                return self.folgeseite(request)
            return httpx.Response(200, json=seite(nummer))
        return httpx.Response(404)


#: Störungen einer Folgeseite: jeweils ein Abbruch der Liste wie bei allen anderen Listen
STOERUNGEN: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "500": lambda r: httpx.Response(500),
    "403": lambda r: httpx.Response(403, text="Forbidden"),
    "404": lambda r: httpx.Response(404),
    "fehlerobjekt": lambda r: httpx.Response(
        200, json={"type": "https://schema.oparl.org/1.1/Error", "message": "Requested class doesn't exist."}
    ),
    "anderes_json": lambda r: httpx.Response(200, json={"status": "blocked"}),
    "nicht_erreichbar": lambda r: (_ for _ in ()).throw(httpx.ConnectError("weg", request=r)),
}


@pytest.fixture(autouse=True)
def _ohne_pause(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiederholungen nach 5xx und Netzfehlern ohne Wartezeit, ohne Schutzschalter (nicht für die Datenbank-Tests)."""
    if "speicher" in request.fixturenames:
        return

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(settings, "circuit_breaker_enabled", False)


def _client(quelle: Callable[[httpx.Request], httpx.Response]) -> OParlClient:
    client = OParlClient(max_concurrent=2, wait_time=0)
    client._semaphore = asyncio.Semaphore(2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
    return client


def _orchestrator(storage: Any = None) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = storage
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = False
    return orchestrator


def _ids(bodies: list[dict[str, Any]]) -> list[str]:
    return [body["id"] for body in bodies]


ALLE = [kommune(n)["id"] for n in (1, 2, 3, 4, 5)]


# ---------------------------------------------------------------------------
# Kommunenliste über alle Seiten (Issue #869)
# ---------------------------------------------------------------------------


async def test_kommunenliste_mit_mehreren_seiten_liefert_alle_kommunen() -> None:
    quelle = Quelle()
    client = _client(quelle)
    missed: list[str] = []
    try:
        art, bodies, _version = await _orchestrator().auto_detect_url(client, LISTE, missed)
    finally:
        await client._client.aclose()

    assert art == "body_list"
    assert _ids(bodies) == ALLE
    assert missed == []
    # Die erste Seite nur einmal (die Erkennung hat sie schon), dann jede Folgeseite genau einmal
    assert quelle.anfragen == [LISTE, SEITE_2, SEITE_3]


async def test_system_mit_kommunenliste_ueber_mehrere_seiten() -> None:
    quelle = Quelle()
    client = _client(quelle)
    missed: list[str] = []
    try:
        art, bodies, _version = await _orchestrator().auto_detect_url(client, SYSTEM, missed)
    finally:
        await client._client.aclose()

    assert art == "system"
    assert _ids(bodies) == ALLE
    assert missed == []
    assert quelle.anfragen == [SYSTEM, LISTE, SEITE_2, SEITE_3]


async def test_kommunenliste_als_eintrag_einer_liste_von_adressen() -> None:
    """``body`` als Liste von Adressen: Ist ein Eintrag eine Kommunenliste, zählen auch ihre Folgeseiten."""
    quelle = Quelle()
    client = _client(quelle)
    missed: list[str] = []
    eingebettet = {"id": f"{BASE}/body/eingebettet", "type": "https://schema.oparl.org/1.1/Body"}
    try:
        bodies = await _orchestrator()._fetch_bodies(client, [LISTE, eingebettet], missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == [*ALLE, eingebettet["id"]]
    assert missed == []


@pytest.mark.parametrize("fall", sorted(STOERUNGEN))
@pytest.mark.parametrize("adresse", [LISTE, SYSTEM])
async def test_fehler_auf_folgeseite_bricht_ab_und_macht_den_abgleich_unvollstaendig(fall: str, adresse: str) -> None:
    quelle = Quelle(STOERUNGEN[fall])
    client = _client(quelle)
    missed: list[str] = []
    try:
        _art, bodies, _version = await _orchestrator().auto_detect_url(client, adresse, missed)
    finally:
        await client._client.aclose()

    # Die Kommunen der ersten Seite bleiben im Abgleich, nach der gestörten Seite fragt er nicht weiter
    assert _ids(bodies) == ALLE[:2]
    assert SEITE_3 not in quelle.anfragen
    assert missed == [SEITE_2]
    assert client.host_health["ris.example.org"].failed_lists == [LISTE]
    assert sync_complete([{"incomplete": []}] * len(bodies), missed, client) is False


async def test_fehler_auf_der_ersten_seite_der_kommunenliste_geht_an_den_aufrufer() -> None:
    """Ohne eine lesbare Kommune kein Abgleich: wie bisher ein Fehler der Quelle, keine stille Lücke."""

    def kaputt(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SYSTEM:
            return Quelle()(request)
        return httpx.Response(500)

    client = _client(kaputt)
    try:
        with pytest.raises(ListFetchError):
            await _orchestrator().auto_detect_url(client, SYSTEM, [])
    finally:
        await client._client.aclose()


async def test_ohne_vermerk_der_luecke_kein_stiller_abbruch() -> None:
    """Wer ``missed`` nicht übergibt, bekommt den Abbruch einer Folgeseite als Fehler statt einer kurzen Liste."""
    client = _client(Quelle(STOERUNGEN["500"]))
    try:
        with pytest.raises(ListFetchError) as excinfo:
            await _orchestrator().auto_detect_url(client, LISTE)
    finally:
        await client._client.aclose()
    assert excinfo.value.page_url == SEITE_2


async def test_folgeseiten_ab_vorliegender_erster_seite() -> None:
    """``fetch_list(next_page=…)`` fragt die erste Seite nicht noch einmal an; eine Folgeseite ohne Liste bricht ab."""
    quelle = Quelle(STOERUNGEN["fehlerobjekt"])
    client = _client(quelle)
    try:
        with pytest.raises(ListFetchError) as excinfo:
            [page async for page in client.fetch_list(LISTE, next_page=SEITE_2)]
    finally:
        await client._client.aclose()
    assert quelle.anfragen == [SEITE_2]
    assert excinfo.value.list_url == LISTE

    quelle = Quelle()
    client = _client(quelle)
    try:
        pages = [page async for page in client.fetch_list(LISTE, next_page=SEITE_2)]
    finally:
        await client._client.aclose()
    assert [_ids(page) for page in pages] == [ALLE[2:4], ALLE[4:]]
    assert quelle.anfragen == [SEITE_2, SEITE_3]


def _seite_url(nummer: int) -> str:
    return LISTE if nummer == 1 else f"{LISTE}?page={nummer}"


class LangeListe:
    """
    Kommunenliste mit ``seiten`` Seiten zu je einer Kommune und System-Objekt.

    ``kreis_auf``: ``links.next`` der letzten Seite zeigt zurück auf diese Seite (fehlerhafte Quelle).
    """

    def __init__(self, seiten: int, kreis_auf: int | None = None) -> None:
        self.seiten = seiten
        self.kreis_auf = kreis_auf
        self.anfragen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.anfragen.append(url)
        if url == SYSTEM:
            return httpx.Response(
                200, json={"id": SYSTEM, "type": "https://schema.oparl.org/1.1/System", "body": LISTE}
            )
        nummer = int(request.url.params.get("page", "1"))
        if request.url.path != "/oparl/bodies" or nummer > self.seiten:
            return httpx.Response(404)
        links: dict[str, str] = {}
        if nummer < self.seiten:
            links["next"] = _seite_url(nummer + 1)
        elif self.kreis_auf is not None:
            links["next"] = _seite_url(self.kreis_auf)
        return httpx.Response(200, json={"data": [kommune(nummer)], "links": links})


def _kommunen(von: int, bis: int) -> list[str]:
    return [kommune(n)["id"] for n in range(von, bis + 1)]


@pytest.mark.parametrize("adresse", [LISTE, SYSTEM])
async def test_kommunenliste_endet_an_der_seitengrenze_und_ist_dann_unvollstaendig(adresse: str) -> None:
    """Wie ``add_oparl_source`` höchstens ``MAX_BODY_PAGES`` Seiten; die nächste Seite steht in ``missed``."""
    quelle = LangeListe(MAX_BODY_PAGES + 5)
    client = _client(quelle)
    missed: list[str] = []
    try:
        _art, bodies, _version = await _orchestrator().auto_detect_url(client, adresse, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == _kommunen(1, MAX_BODY_PAGES)
    assert missed == [_seite_url(MAX_BODY_PAGES + 1)]
    assert _seite_url(MAX_BODY_PAGES + 1) not in quelle.anfragen
    assert sync_complete([{"incomplete": []}] * len(bodies), missed, client) is False


async def test_kommunenliste_mit_genau_der_hoechstzahl_an_seiten_ist_vollstaendig() -> None:
    quelle = LangeListe(MAX_BODY_PAGES)
    client = _client(quelle)
    missed: list[str] = []
    try:
        _art, bodies, _version = await _orchestrator().auto_detect_url(client, LISTE, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == _kommunen(1, MAX_BODY_PAGES)
    assert missed == []
    assert len(quelle.anfragen) == MAX_BODY_PAGES


@pytest.mark.parametrize("kreis_auf", [1, 2, 3])
@pytest.mark.parametrize("adresse", [LISTE, SYSTEM])
async def test_kommunenliste_im_kreis_bricht_ab_statt_endlos(adresse: str, kreis_auf: int) -> None:
    """Verweist ``links.next`` auf eine gelesene Seite (auch die erste oder sich selbst), endet das Lesen."""
    quelle = LangeListe(3, kreis_auf=kreis_auf)
    client = _client(quelle)
    missed: list[str] = []
    try:
        _art, bodies, _version = await _orchestrator().auto_detect_url(client, adresse, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == _kommunen(1, 3)
    assert missed == [_seite_url(kreis_auf)]
    # Jede Seite genau einmal
    assert [a for a in quelle.anfragen if a != SYSTEM] == [_seite_url(n) for n in (1, 2, 3)]
    assert sync_complete([{"incomplete": []}] * len(bodies), missed, client) is False


async def test_liste_im_kreis_bricht_ab_wie_eine_teilantwort() -> None:
    """Gilt für jede Liste: Ein Verweis auf eine schon gelesene Seite bricht mit ``ListFetchError`` ab."""
    quelle = LangeListe(2, kreis_auf=2)
    client = _client(quelle)
    seiten: list[list[str]] = []
    try:
        with pytest.raises(ListFetchError) as excinfo:
            async for page in client.fetch_list(LISTE):
                seiten.append(_ids(page))
    finally:
        await client._client.aclose()

    assert seiten == [_kommunen(1, 1), _kommunen(2, 2)]
    assert excinfo.value.page_url == _seite_url(2)
    assert "schon gelesene Seite" in str(excinfo.value)
    assert quelle.anfragen == [_seite_url(1), _seite_url(2)]


async def _kommunen_lesen(client: OParlClient, weg: str, missed: list[str]) -> list[dict[str, Any]]:
    """Die drei Wege zur Kommunenliste: Adresse der Liste, System-Objekt, Eintrag einer Liste von Adressen."""
    if weg == "adressliste":
        return await _orchestrator()._fetch_bodies(client, [LISTE], missed)
    _art, bodies, _version = await _orchestrator().auto_detect_url(client, weg, missed)
    return bodies


@pytest.mark.parametrize("weg", [LISTE, SYSTEM, "adressliste"])
async def test_erste_seite_im_kreis_auf_sich_selbst(weg: str) -> None:
    """Eine einseitige Kommunenliste, deren ``links.next`` auf sie selbst zeigt: eine Anfrage, keine Kommune doppelt."""
    quelle = LangeListe(1, kreis_auf=1)
    client = _client(quelle)
    missed: list[str] = []
    try:
        bodies = await _kommunen_lesen(client, weg, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == _kommunen(1, 1)
    assert missed == [LISTE]
    assert [a for a in quelle.anfragen if a != SYSTEM] == [LISTE]
    assert client.host_health["ris.example.org"].failed_lists == [LISTE]
    assert sync_complete([{"incomplete": []}] * len(bodies), missed, client) is False


async def test_folgeseite_gleich_der_ersten_seite_ohne_anfrage() -> None:
    """``fetch_list(next_page=…)`` mit der Adresse der Liste selbst bricht ab, bevor eine Anfrage hinausgeht."""
    quelle = LangeListe(1, kreis_auf=1)
    client = _client(quelle)
    try:
        with pytest.raises(ListFetchError) as excinfo:
            [page async for page in client.fetch_list(LISTE, next_page=LISTE)]
    finally:
        await client._client.aclose()

    assert quelle.anfragen == []
    assert excinfo.value.page_url == LISTE
    assert "schon gelesene Seite" in str(excinfo.value)


@pytest.mark.parametrize("weg", [LISTE, SYSTEM, "adressliste"])
async def test_kommune_auf_zwei_seiten_zaehlt_einmal(weg: str) -> None:
    """Verschieben sich die Seiten zwischen zwei Anfragen, steht eine Kommune doppelt in der Liste: zählt einmal."""

    def verschoben(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SYSTEM or request.url.params.get("page") is None:
            return Quelle()(request)
        # Seite 2 beginnt mit der letzten Kommune der ersten Seite (eine Kommune davor kam neu hinzu)
        return httpx.Response(200, json={"data": [kommune(n) for n in (2, 3, 4, 5)], "links": {}})

    client = _client(verschoben)
    missed: list[str] = []
    try:
        bodies = await _kommunen_lesen(client, weg, missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == ALLE
    assert missed == []


async def test_dieselbe_kommunenliste_unter_zwei_adressen_zaehlt_einmal() -> None:
    quelle = Quelle()
    client = _client(quelle)
    missed: list[str] = []
    try:
        bodies = await _orchestrator()._fetch_bodies(client, [LISTE, LISTE, kommune(1)], missed)
    finally:
        await client._client.aclose()

    assert _ids(bodies) == ALLE
    assert missed == []


async def test_seitengrenze_nennt_die_nicht_gelesene_folgeseite() -> None:
    """``max_pages`` endet nicht mehr still: ``page_limit_lists`` nennt die Folgeseite, bis die Liste ganz läuft."""
    client = _client(LangeListe(5))
    try:
        pages = [page async for page in client.fetch_list(LISTE, max_pages=2)]
        assert len(pages) == 2
        assert client.page_limit_lists == {LISTE: _seite_url(3)}

        pages = [page async for page in client.fetch_list(LISTE)]
        assert len(pages) == 5
        assert client.page_limit_lists == {}

        # Endet die Liste genau an der Grenze, fehlt nichts
        pages = [page async for page in client.fetch_list(LISTE, max_pages=5)]
        assert len(pages) == 5
        assert client.page_limit_lists == {}
    finally:
        await client._client.aclose()


def test_eintrag_im_sync_log_nennt_die_luecke() -> None:
    assert missed_bodies_note([SEITE_2]) == f"Kommunen nicht vollständig gelesen, nicht lesbar: {SEITE_2}"
    viele = [f"{BASE}/body/{n}" for n in range(8)]
    assert missed_bodies_note(viele).endswith(f"{viele[4]} und 3 weitere")


class Speicher:
    """Was ``sync_body_url`` vom Speicher braucht; die Kommunen selbst gleicht ``_sync_body`` (Attrappe) ab."""

    def __init__(self) -> None:
        self.id_bases = IdBases()
        self.vollstaendig: list[bool] = []
        self.fehler: list[str] = []
        self.quelle: dict[str, Any] = {}

    async def get_source_by_url(self, url: str) -> Any:
        return SimpleNamespace(url=url, sync_config={}, user_agent=None, last_successful_full_sync=None)

    async def save_filter_notes(self, source_url: str, lists: Any, hosts: Any, *, drop_legacy: bool = False) -> None:
        return None

    async def upsert_source(self, **kwargs: Any) -> UUID:
        self.quelle = kwargs
        return SOURCE_ID

    async def update_source_sync_time(
        self, source_id: UUID, full_sync: bool = False, complete: bool = False, full_complete: bool | None = None
    ) -> None:
        self.vollstaendig.append(complete)

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.fehler.append(error)


async def _abgleich(
    monkeypatch: pytest.MonkeyPatch, quelle: Quelle, url: str = LISTE, **optionen: Any
) -> tuple[Any, Speicher, list[str]]:
    echter_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.client.oparl_client.httpx.AsyncClient",
        lambda *args, **kwargs: echter_client(transport=httpx.MockTransport(quelle), **kwargs),
    )
    speicher = Speicher()
    orchestrator = _orchestrator(speicher)
    abgeglichen: list[str] = []

    async def sync_body(**kwargs: Any) -> dict[str, Any]:
        abgeglichen.append(kwargs["body_data"]["id"])
        return {"incomplete": []}

    orchestrator._sync_body = sync_body  # type: ignore[method-assign]
    result = await orchestrator.sync_body_url(url, **optionen)
    return result, speicher, abgeglichen


async def test_abgleich_gleicht_kommunen_aller_seiten_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    result, speicher, abgeglichen = await _abgleich(monkeypatch, Quelle())

    assert result.success and result.errors == []
    assert sorted(abgeglichen) == sorted(ALLE)
    assert result.bodies_synced == 5
    assert speicher.vollstaendig == [True]


@pytest.mark.parametrize("filter_", ["kommune 3", f"{BASE}/body/3"])
async def test_sync_source_mit_filter_gleicht_nur_passende_kommunen_ab(
    monkeypatch: pytest.MonkeyPatch, filter_: str
) -> None:
    """
    ``sync --source <System> --filter`` läuft seit Issue #554 über den Einstieg des Daemons: nur passende Kommunen
    (Name ohne Groß-/Kleinschreibung oder Kennung), der Abgleich gilt nicht als vollständig, Name und Rohdaten der
    Quelle bleiben wie im Daemon (erste Kommune der ganzen Liste).
    """
    result, speicher, abgeglichen = await _abgleich(monkeypatch, Quelle(), SYSTEM, body_filter=filter_)

    assert result.success and result.errors == []
    assert abgeglichen == [f"{BASE}/body/3"]
    assert speicher.vollstaendig == [False]
    assert speicher.quelle["url"] == SYSTEM
    assert speicher.quelle["name"] == "Kommune 1"
    assert speicher.quelle["raw_json"] == {"bodies_count": 5}


async def test_abgleich_mit_abgebrochener_kommunenliste_ist_unvollstaendig(monkeypatch: pytest.MonkeyPatch) -> None:
    result, speicher, abgeglichen = await _abgleich(monkeypatch, Quelle(STOERUNGEN["500"]))

    # Die gelesenen Kommunen werden abgeglichen, der Lauf gilt aber nicht als vollständig
    assert sorted(abgeglichen) == ALLE[:2]
    assert speicher.vollstaendig == [False]
    assert result.errors == [missed_bodies_note([SEITE_2])]


# ---------------------------------------------------------------------------
# Neuanlage inaktiv bzw. ungelistet (Teil von Issue #555), ohne Datenbank
# ---------------------------------------------------------------------------


class _Mitschnitt:
    """Sitzung, die die gesendeten Anweisungen festhält und eine feste Kennung bzw. ``sync_config`` zurückgibt."""

    def __init__(self, gesendet: list[Any], sync_config: dict[str, Any] | None = None) -> None:
        self.gesendet = gesendet
        self.sync_config = sync_config

    async def __aenter__(self) -> _Mitschnitt:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> Any:
        self.gesendet.append(stmt)
        return SimpleNamespace(scalar_one=lambda: SOURCE_ID, scalar_one_or_none=lambda: self.sync_config)

    async def commit(self) -> None:
        return None


def _sql(stmt: Any) -> tuple[str, dict[str, Any]]:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


async def test_neue_quelle_inaktiv_vorhandene_behaelt_ihren_status() -> None:
    gesendet: list[Any] = []
    storage = DatabaseStorage.__new__(DatabaseStorage)
    storage.get_session = lambda: _Mitschnitt(gesendet)  # type: ignore[method-assign]

    await storage.upsert_source(url=SYSTEM, name="Musterstadt")

    sql, params = _sql(gesendet[0])
    assert params["is_active"] is False
    # ON CONFLICT ändert is_active nicht: Eine abgeschaltete Quelle wird nie wieder aktiv
    assert "is_active" not in sql.split("ON CONFLICT", 1)[1]


#: ``sync_config`` der Quelle und ob ihre neu entdeckten Kommunen gleich gelistet sind
LISTUNG_NACH_QUELLE = [
    (None, False),
    ({}, False),
    ({"source_type": "oparl", "events_enabled": False}, False),
    ({"session_tenant": ""}, False),
    # Eigene Session-Schnittstelle: Der Mandant hat die Veröffentlichung im Bürgerportal selbst eingeschaltet
    ({"source_type": "oparl", "session_tenant": "musterstadt"}, True),
]


@pytest.mark.parametrize(("sync_config", "gelistet"), LISTUNG_NACH_QUELLE)
async def test_neue_kommune_ungelistet_vorhandene_behaelt_ihren_status(
    sync_config: dict[str, Any] | None, gelistet: bool
) -> None:
    gesendet: list[Any] = []
    storage = DatabaseStorage.__new__(DatabaseStorage)
    storage.events_enabled = False
    storage._body_uuid_cache = {}
    storage.get_session = lambda: _Mitschnitt(gesendet, sync_config)  # type: ignore[method-assign]
    body = OParlProcessor().process_body(kommune(1), kommune(1)["id"])
    body.nested_entities = []

    await storage.upsert_body(body, SOURCE_ID)

    einfuegen = [stmt for stmt in gesendet if isinstance(stmt, Insert)]
    assert len(einfuegen) == 1
    sql, params = _sql(einfuegen[0])
    assert params["is_listed"] is gelistet
    assert "is_listed" not in sql.split("ON CONFLICT", 1)[1]


# ---------------------------------------------------------------------------
# add-source nennt den tatsächlichen Status (Teil von Issue #555)
# ---------------------------------------------------------------------------


def _registrierte_quelle(is_active: bool) -> SimpleNamespace:
    return SimpleNamespace(
        url=SYSTEM, is_active=is_active, consecutive_failures=0, last_error_kind=None, last_error_at=None
    )


def _add_source(monkeypatch: pytest.MonkeyPatch, vorher: SimpleNamespace | None, nachher: SimpleNamespace) -> str:
    """``add-source`` mit Attrappe des Orchestrators; ``vorher``/``nachher``: die Quelle vor bzw. nach dem Anlegen."""
    stand = [vorher, nachher]

    class Speicher:
        async def get_source_by_url(self, url: str) -> SimpleNamespace | None:
            return stand.pop(0)

    class Orchestrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = Speicher()

        async def __aenter__(self) -> Orchestrator:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def add_source(self, url: str, name: str | None = None) -> UUID:
            return SOURCE_ID

    ausgabe = io.StringIO()
    monkeypatch.setattr(cli, "SyncOrchestrator", Orchestrator)
    monkeypatch.setattr(cli, "console", Console(file=ausgabe, width=300))
    result = CliRunner().invoke(cli.app, ["add-source", SYSTEM])
    assert result.exit_code == 0, result.output
    assert stand == []
    return " ".join(ausgabe.getvalue().split())


def test_add_source_neue_quelle_ist_inaktiv_und_darf_einmal_von_hand_laufen(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _add_source(monkeypatch, None, _registrierte_quelle(False))

    assert "Neue Quelle, Status: inaktiv" in text
    assert f"mandari-ingestor sync --source {SYSTEM}" in text


def test_add_source_vorhandene_aktive_quelle_behaelt_ihren_status(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _add_source(monkeypatch, _registrierte_quelle(True), _registrierte_quelle(True))

    assert "Status unverändert: aktiv" in text
    assert "sync --source" not in text


def test_add_source_raet_bei_abgeschalteter_quelle_vom_handabruf_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sync --source`` prüft ``is_active`` nicht: Für eine abgeschaltete Quelle (etwa Köln) kein Handabruf-Rezept."""
    text = _add_source(monkeypatch, _registrierte_quelle(False), _registrierte_quelle(False))

    assert "Status unverändert: inaktiv" in text
    assert "Bitte nicht von Hand abgleichen" in text
    assert "mandari-ingestor sync --source" not in text


# ---------------------------------------------------------------------------
# Dasselbe gegen PostgreSQL
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_quellen_{uuid.uuid4().hex[:12]}"


def _engine() -> AsyncEngine:
    url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})


async def _schema(anlegen: bool) -> None:
    engine = _engine()
    async with engine.begin() as conn:
        if anlegen:
            await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
        else:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    await engine.dispose()


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    if not DATABASE_URL:
        if os.environ.get("CI"):
            pytest.fail("INGESTOR_TEST_DATABASE_URL fehlt: In der CI müssen diese Tests laufen.")
        pytest.skip("braucht PostgreSQL (INGESTOR_TEST_DATABASE_URL)")
    asyncio.run(_schema(anlegen=True))
    yield
    asyncio.run(_schema(anlegen=False))


@pytest.fixture
async def speicher(schema: None) -> AsyncIterator[DatabaseStorage]:
    storage = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=False)
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    yield storage
    await storage.close()


async def _spalte(storage: DatabaseStorage, stmt: Any) -> Any:
    async with storage.get_session() as session:
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.integration
async def test_quelle_bleibt_inaktiv_und_wird_nie_wieder_aktiv(speicher: DatabaseStorage) -> None:
    aktiv = select(OParlSource.is_active).where(OParlSource.url == SYSTEM)
    source_id = await speicher.upsert_source(SYSTEM, "Musterstadt")
    assert await _spalte(speicher, aktiv) is False

    # Im Admin aktiviert: Ein erneutes Anlegen ändert nichts
    async with speicher.get_session() as session:
        await session.execute(update(OParlSource).where(OParlSource.id == source_id).values(is_active=True))
        await session.commit()
    assert await speicher.upsert_source(SYSTEM, "Musterstadt neu") == source_id
    assert await _spalte(speicher, aktiv) is True

    # Abgeschaltet: bleibt abgeschaltet, auch über den Abgleich (der ebenfalls upsert_source aufruft)
    async with speicher.get_session() as session:
        await session.execute(update(OParlSource).where(OParlSource.id == source_id).values(is_active=False))
        await session.commit()
    await speicher.upsert_source(SYSTEM, "Musterstadt")
    assert await _spalte(speicher, aktiv) is False


@pytest.mark.integration
async def test_neu_entdeckte_kommune_ist_nicht_gelistet(speicher: DatabaseStorage) -> None:
    source_id = await speicher.upsert_source(SYSTEM, "Musterstadt")
    processor = OParlProcessor()
    body_id = await speicher.upsert_body(processor.process_body(kommune(1), kommune(1)["id"]), source_id)
    gelistet = select(OParlBody.is_listed).where(OParlBody.id == body_id)
    assert await _spalte(speicher, gelistet) is False

    # Im Admin gelistet: Der nächste Abgleich ändert das nicht
    async with speicher.get_session() as session:
        await session.execute(update(OParlBody).where(OParlBody.id == body_id).values(is_listed=True))
        await session.commit()
    speicher.clear_uuid_caches()
    await speicher.upsert_body(processor.process_body({**kommune(1), "name": "Umbenannt"}, kommune(1)["id"]), source_id)
    assert await _spalte(speicher, gelistet) is True
    assert await speicher.body_stores_files(body_id) is True


SESSION_SYSTEM = "https://mandari.example.org/session/musterstadt/api/oparl/"


@pytest.mark.integration
async def test_kommune_der_eigenen_session_schnittstelle_ist_gleich_gelistet(speicher: DatabaseStorage) -> None:
    """
    Wie beim Spiegel (``insight_sync/session_mirror.py``) erscheint die Kommune eines Session-Mandanten, der ins
    Bürgerportal veröffentlicht, ohne weiteren Schritt; die Kommune einer fremden Quelle bleibt ungelistet.
    """
    fremd = await speicher.upsert_source(SYSTEM, "Musterstadt")
    eigen = await speicher.upsert_source(SESSION_SYSTEM, "Sitzungsdienst Musterstadt")
    # So legt insight_service.register_source die Quelle an
    async with speicher.get_session() as session:
        await session.execute(
            update(OParlSource)
            .where(OParlSource.id == eigen)
            .values(is_active=True, sync_config={"source_type": "oparl", "session_tenant": "musterstadt"})
        )
        await session.commit()
    processor = OParlProcessor()
    eigene_kommune = {**kommune(1), "id": f"{SESSION_SYSTEM}bodies/1"}

    fremde_id = await speicher.upsert_body(processor.process_body(kommune(1), kommune(1)["id"]), fremd)
    eigene_id = await speicher.upsert_body(processor.process_body(eigene_kommune, eigene_kommune["id"]), eigen)

    assert await _spalte(speicher, select(OParlBody.is_listed).where(OParlBody.id == fremde_id)) is False
    eigene_gelistet = select(OParlBody.is_listed).where(OParlBody.id == eigene_id)
    assert await _spalte(speicher, eigene_gelistet) is True

    # Ausgeblendet (etwa nach einer Rücknahme, insight_service.retract_source): Der Abgleich listet sie nicht wieder
    async with speicher.get_session() as session:
        await session.execute(update(OParlBody).where(OParlBody.id == eigene_id).values(is_listed=False))
        await session.commit()
    speicher.clear_uuid_caches()
    await speicher.upsert_body(processor.process_body(eigene_kommune, eigene_kommune["id"]), eigen)
    assert await _spalte(speicher, eigene_gelistet) is False


def test_spalte_is_listed_wie_in_django() -> None:
    """Der Ingestor kennt ``is_listed`` (NOT NULL, Default true wie in Django) und setzt es nur beim Anlegen."""
    spalte = OParlBody.__table__.c.is_listed
    assert spalte.nullable is False
    assert spalte.server_default is not None
    assert "is_listed" in ENRICHMENT_FIELDS
