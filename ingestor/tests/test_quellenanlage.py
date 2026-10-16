# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenlisten über alle Seiten (Issue #869) und Neuanlage inaktiv bzw. ungelistet (Teil von Issue #555).

Bisher las die Erkennung einer Quelle, deren Adresse eine Kommunenliste ist, nur deren erste Seite; die Kommunen auf
den Folgeseiten (``links.next``) wurden nie abgeglichen. Jetzt folgt sie der Liste wie jeder anderen: im Takt, und ein
Fehler auf einer Folgeseite bricht ab und macht den Abgleich unvollständig. Dazu: Neue Quellen sind inaktiv, eine
vorhandene Quelle behält ``is_active`` (auch abgeschaltet), neu entdeckte Kommunen sind nicht gelistet.

Antworten aus ``httpx.MockTransport``, keine Abrufe fremder Server. Die Speicher-Tests laufen gegen PostgreSQL
(``INGESTOR_TEST_DATABASE_URL``, in der CI gesetzt; lokal ohne Datenbank übersprungen).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from mandari_oparl import IdBases
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.client.oparl_client import ListFetchError, OParlClient
from src.config import settings
from src.storage.database import ENRICHMENT_FIELDS, DatabaseStorage
from src.storage.models import Base, OParlBody, OParlSource
from src.sync.orchestrator import SyncOrchestrator, missed_bodies_note, sync_complete
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
    OParlClient._modified_since_unsupported.clear()


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

    async def get_source_by_url(self, url: str) -> Any:
        return SimpleNamespace(url=url, sync_config={}, user_agent=None, last_successful_full_sync=None)

    async def get_modified_since_unsupported_hosts(self) -> set[str]:
        return set()

    async def add_modified_since_unsupported_hosts(self, source_url: str, hosts: set[str]) -> None:
        return None

    async def upsert_source(self, **kwargs: Any) -> UUID:
        return SOURCE_ID

    async def update_source_sync_time(self, source_id: UUID, full_sync: bool = False, complete: bool = False) -> None:
        self.vollstaendig.append(complete)

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.fehler.append(error)


async def _abgleich(monkeypatch: pytest.MonkeyPatch, quelle: Quelle) -> tuple[Any, Speicher, list[str]]:
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
    result = await orchestrator.sync_body_url(LISTE)
    return result, speicher, abgeglichen


async def test_abgleich_gleicht_kommunen_aller_seiten_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    result, speicher, abgeglichen = await _abgleich(monkeypatch, Quelle())

    assert result.success and result.errors == []
    assert sorted(abgeglichen) == sorted(ALLE)
    assert result.bodies_synced == 5
    assert speicher.vollstaendig == [True]


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
    """Sitzung, die die gesendeten Anweisungen festhält und eine feste Kennung zurückgibt."""

    def __init__(self, gesendet: list[Any]) -> None:
        self.gesendet = gesendet

    async def __aenter__(self) -> _Mitschnitt:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> Any:
        self.gesendet.append(stmt)
        return SimpleNamespace(scalar_one=lambda: SOURCE_ID, scalar_one_or_none=lambda: None)

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


async def test_neue_kommune_ungelistet_vorhandene_behaelt_ihren_status() -> None:
    gesendet: list[Any] = []
    storage = DatabaseStorage.__new__(DatabaseStorage)
    storage.events_enabled = False
    storage._body_uuid_cache = {}
    storage.get_session = lambda: _Mitschnitt(gesendet)  # type: ignore[method-assign]
    body = OParlProcessor().process_body(kommune(1), kommune(1)["id"])
    body.nested_entities = []

    await storage.upsert_body(body, SOURCE_ID)

    sql, params = _sql(gesendet[0])
    assert params["is_listed"] is False
    assert "is_listed" not in sql.split("ON CONFLICT", 1)[1]


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


def test_spalte_is_listed_wie_in_django() -> None:
    """Der Ingestor kennt ``is_listed`` (NOT NULL, Default true wie in Django) und setzt es nur beim Anlegen."""
    spalte = OParlBody.__table__.c.is_listed
    assert spalte.nullable is False
    assert spalte.server_default is not None
    assert "is_listed" in ENRICHMENT_FIELDS
