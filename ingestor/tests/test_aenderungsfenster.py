# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weniger Abrufe je Lauf (Issue #1025): Änderungsfenster seit dem letzten vollständigen Abgleich und Listen, die es
bei der Quelle nicht gibt.

- Inkrementelle Läufe fragen je Kommune nach Änderungen seit dem Beginn ihres letzten vollständig erfolgreichen
  Abgleichs abzüglich Sicherheitsabstand (Standard 2 Stunden), nicht mehr nach allem aus sieben Tagen. Ein Lauf
  mit Lücke schreibt den Zeitpunkt nicht fort, nicht gespeicherte Objekte halten ihn bei ihrem ``modified``.
  Ohne Zeitpunkt, mit einem älteren oder bei Listen mit reinen Tagesstempeln bleibt es bei sieben Tagen.
- Antwortet eine optionale Liste schon auf der ersten Seite mit HTTP 404 oder einem OParl-Fehlerobjekt, wird sie
  vermerkt und bis zum Vollabgleich, höchstens einen Tag, nicht mehr angefragt. Das zählt als „nicht vorhanden“,
  nicht als Lücke. Bei Pflichtlisten bleibt es eine Lücke.

Antworten aus ``httpx.MockTransport`` (aufgezeichnetes Verhalten der Quellen nachgebildet), keine Abrufe.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

import httpx
import pytest
from mandari_oparl.utils import parse_datetime

from src.client.oparl_client import ListFetchError, ListMissingError, OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.storage.database import body_sync_started_of, missing_lists_of
from src.sync.orchestrator import (
    BodyItemCheck,
    DayStampLists,
    MissingLists,
    SyncOrchestrator,
    fallback_modified_since,
    modified_since_for,
    sync_complete,
)
from src.sync.processor import OParlProcessor

HOST = "ris.example.org"
BASE = f"https://{HOST}/oparl"
BODY = f"{BASE}/body/1"
SOURCE = f"{BASE}/system"
LISTEN = ("organization", "person", "meeting", "paper")
FEHLEROBJEKT = {"type": "https://schema.oparl.org/1.1/Error", "message": "Requested class doesn't exist."}


# ---------------------------------------------------------------------------
# Nachgebildete Quelle und Speicher
# ---------------------------------------------------------------------------


class Quelle:
    """
    OParl-Schnittstelle mit Listen je Kommune; filtert mit ``modified_since`` (Objekte ab dem Zeitpunkt) und
    blättert in Seiten zu zwei Objekten. Je Pfad lässt sich eine Antwort fest einstellen.
    """

    def __init__(self, geaendert: datetime) -> None:
        self.anfragen: list[httpx.URL] = []
        self.listen: dict[str, list[dict[str, Any]]] = {}
        for name in LISTEN:
            typ = name.capitalize()
            self.listen[f"/oparl/body/1/{name}"] = [
                {
                    "id": f"{BASE}/{name}/{i}",
                    "type": f"https://schema.oparl.org/1.1/{typ}",
                    "name": f"{typ} {i}",
                    "modified": geaendert.isoformat(),
                }
                for i in range(3)
            ]
        #: Pfad -> feste Antwort (Statuscode oder JSON) für jede Anfrage
        self.fest: dict[str, Any] = {}
        #: Pfad -> Statuscode nur für Anfragen mit modified_since
        self.mit_filter: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request.url)
        pfad = request.url.path
        params = dict(request.url.params)
        if "modified_since" in params and pfad in self.mit_filter:
            return httpx.Response(self.mit_filter[pfad])
        if pfad in self.fest:
            antwort = self.fest[pfad]
            return httpx.Response(antwort) if isinstance(antwort, int) else httpx.Response(200, json=antwort)
        objekte = self.listen.get(pfad)
        if objekte is None:
            return httpx.Response(404)
        seit = params.get("modified_since")
        if seit:
            grenze = parse_datetime(seit)
            assert grenze is not None and grenze.tzinfo is not None
            objekte = [o for o in objekte if "modified" not in o or parse_datetime(o["modified"]) >= grenze]
        seite = int(params.get("page", "1"))
        teil = objekte[(seite - 1) * 2 : seite * 2]
        links: dict[str, str] = {}
        if seite * 2 < len(objekte):
            links["next"] = f"https://{HOST}{pfad}?{urlencode({**params, 'page': str(seite + 1)})}"
        return httpx.Response(200, json={"data": teil, "links": links})

    def anfragen_je_liste(self) -> Counter[str]:
        return Counter(url.path.rsplit("/", 1)[1] for url in self.anfragen)

    def filter_der_anfragen(self, liste: str) -> list[str | None]:
        return [
            (parse_qs(urlparse(str(url)).query).get("modified_since") or [None])[0]
            for url in self.anfragen
            if url.path.endswith(f"/{liste}")
        ]


class Speicher:
    """Was ``_sync_body`` vom Speicher braucht; hält Zeitpunkt und Vermerke wie ``sync_config``."""

    def __init__(self) -> None:
        self.begonnen: dict[str, datetime] = {}
        #: Vermerkte fehlende Listen -> Zeitpunkt der letzten Prüfung
        self.fehlend: dict[str, datetime | None] = {}
        self.modified: dict[str, datetime | None] = {}
        self.vermerke: list[tuple[str, bool]] = []
        #: Kennungen, deren Speichern mit einer Ausnahme scheitert (Deadlock, Constraint)
        self.fehler: set[str] = set()
        #: Kennungen, deren Bezüge sich nicht auflösen lassen (Mitgliedschaft ohne Person, TOP ohne Sitzung)
        self.unaufloesbar: set[str] = set()
        #: Listen mit reinen Tagesstempeln (``day_stamp_lists``)
        self.tagesstempel: set[str] = set()

    async def upsert_body(self, body: Any, source_id: UUID) -> UUID:
        return UUID(int=1)

    async def get_body_last_sync(self, body_id: UUID) -> datetime | None:
        return None

    async def update_body_sync_time(self, body_id: UUID) -> None:
        return None

    async def get_body_sync_started(self, source_id: UUID, body_external_id: str) -> datetime | None:
        return self.begonnen.get(body_external_id)

    async def set_body_sync_started(self, source_id: UUID, body_external_id: str, started_at: datetime | None) -> None:
        if started_at is None:
            self.begonnen.pop(body_external_id, None)
        else:
            self.begonnen[body_external_id] = started_at

    async def set_list_day_stamped(self, source_url: str, list_url: str, day_stamped: bool) -> None:
        if day_stamped:
            self.tagesstempel.add(list_url)
        else:
            self.tagesstempel.discard(list_url)

    async def set_list_missing(
        self, source_url: str, list_url: str, missing: bool, checked_at: datetime | None = None
    ) -> None:
        self.vermerke.append((list_url, missing))
        if missing:
            self.fehlend[list_url] = checked_at or datetime.now(UTC)
        else:
            self.fehlend.pop(list_url, None)

    async def batch_check_entities_exist(self, entity_type: str, external_ids: list[str]) -> dict[str, Any]:
        return {eid: self.modified.get(eid) for eid in external_ids}

    async def get_entity_content_hashes(self, entity_type: str, external_ids: list[str]) -> dict[str, str | None]:
        return {}

    async def get_person_ids_by_external_ids(self, ids: list[str]) -> dict[str, UUID]:
        return {}

    async def get_organization_ids_by_external_ids(self, ids: list[str]) -> dict[str, UUID]:
        return {}


def _orchestrator(speicher: Speicher) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = True

    async def speichern(entity: Any, body_id: UUID, entity_type: str, body_name: str | None = None) -> bool:
        if entity.external_id in speicher.fehler:
            raise RuntimeError("deadlock detected")
        if entity.external_id in speicher.unaufloesbar:
            return False
        speicher.modified[entity.external_id] = entity.oparl_modified
        return True

    orchestrator._store_entity = speichern  # type: ignore[method-assign]
    return orchestrator


def _kommune(**listen: str) -> dict[str, Any]:
    daten: dict[str, Any] = {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}
    for name in LISTEN:
        daten[name] = f"{BODY}/{name}"
    daten.update(listen)
    return daten


@asynccontextmanager
async def _client(quelle: Quelle) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        yield client


async def _lauf(
    quelle: Quelle,
    speicher: Speicher,
    *,
    full: bool = False,
    kommune: dict[str, Any] | None = None,
    options: SourceFetchOptions | None = None,
) -> dict[str, Any]:
    quelle.anfragen.clear()
    listen = MissingLists(SOURCE, dict(speicher.fehlend))
    async with _client(quelle) as client:
        return await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(),
            source_id=UUID(int=9),
            full=full,
            options=options,
            missing_lists=listen,
            day_stamps=DayStampLists(SOURCE, set(speicher.tagesstempel)),
        )


@pytest.fixture(autouse=True)
def _ohne_nebenlaeufe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keine Texterkennung, kein Suchindex; Befund „filtert nicht“ des Hosts je Test leer."""
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "oparl_max_retries", 1)
    vorher = OParlClient.get_modified_since_unsupported()
    listen_vorher = set(OParlClient._list_modified_since_unsupported)
    OParlClient._modified_since_unsupported.clear()
    OParlClient._list_modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(vorher)
    OParlClient._list_modified_since_unsupported.clear()
    OParlClient._list_modified_since_unsupported.update(listen_vorher)


class _OhneBefund:
    """Client-Ersatz für ``sync_complete``: kein Host gesperrt oder gestört."""

    def host_findings(self) -> list[Any]:
        return []


def _bisheriges_fenster() -> str:
    """``modified_since`` des bisherigen Fensters, wie der Client es sendet (naive Ortszeit mit ``+00:00``)."""
    return fallback_modified_since().replace(tzinfo=UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Änderungsfenster
# ---------------------------------------------------------------------------


def test_fenster_ab_beginn_des_letzten_vollstaendigen_abgleichs():
    beginn = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    jetzt = beginn + timedelta(minutes=10)
    assert modified_since_for(beginn, timedelta(hours=2), now=jetzt) == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    # Gespeicherter Zeitpunkt ohne Zone gilt als UTC
    assert modified_since_for(beginn.replace(tzinfo=None), timedelta(0), now=jetzt) == beginn
    # Ein Zeitpunkt in der Zukunft (falsche Uhr) zählt als jetzt
    assert modified_since_for(jetzt + timedelta(days=1), timedelta(hours=2), now=jetzt) == jetzt - timedelta(hours=2)


def test_ohne_zeitpunkt_wie_bisher_sieben_tage_ab_mitternacht():
    jetzt = datetime(2026, 10, 15, 14, 30).astimezone()
    seit = modified_since_for(None, timedelta(hours=2), now=jetzt)
    assert seit.tzinfo is None  # naive Ortszeit wie bisher
    assert seit == datetime(2026, 10, 8, 0, 0)


@pytest.mark.parametrize(("wert", "minuten"), [(None, 120), (30, 30), (0, 0), (-5, 120), (True, 120), ("60", 120)])
def test_sicherheitsabstand_je_quelle(wert, minuten):
    config = {} if wert is None else {"modified_since_margin_minutes": wert}
    assert SourceFetchOptions.from_sync_config(config).modified_since_margin() == timedelta(minutes=minuten)


def test_standard_des_sicherheitsabstands_aus_den_einstellungen(monkeypatch):
    monkeypatch.setattr(settings, "modified_since_margin_minutes", 45)
    assert SourceFetchOptions().modified_since_margin() == timedelta(minutes=45)


def test_zeitpunkt_und_vermerke_aus_sync_config():
    config = {
        "body_sync_started": {BODY: "2026-10-15T10:00:00+00:00", "x": "kaputt"},
        "missing_lists": {f"{BODY}/membership": "2026-10-15T10:00:00+00:00", f"{BODY}/file": "kaputt", "": "x"},
    }
    assert body_sync_started_of(config, BODY) == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert body_sync_started_of(config, "x") is None
    assert body_sync_started_of(config, "fehlt") is None
    assert body_sync_started_of(None, BODY) is None
    # Ungültiger Zeitpunkt: vermerkt, aber neu zu prüfen
    assert missing_lists_of(config) == {
        f"{BODY}/membership": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
        f"{BODY}/file": None,
    }
    assert missing_lists_of({"missing_lists": "x"}) == {}
    assert missing_lists_of({"missing_lists": [f"{BODY}/membership"]}) == {}


async def test_lauf_ohne_aenderungen_fragt_je_liste_genau_eine_seite_ab():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()

    # Erster Lauf ohne Zeitpunkt: sieben Tage, alle drei Objekte je Liste auf zwei Seiten
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    assert quelle.anfragen_je_liste() == dict.fromkeys(LISTEN, 2)
    beginn = speicher.begonnen[BODY]
    assert vorher <= beginn <= datetime.now(UTC)

    # Zweiter Lauf: seit Beginn des ersten abzüglich zwei Stunden, nichts geändert, je Liste eine Seite
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    assert quelle.anfragen_je_liste() == dict.fromkeys(LISTEN, 1)
    erwartet = (beginn - timedelta(hours=2)).isoformat(timespec="seconds")
    assert set(quelle.filter_der_anfragen("paper")) == {erwartet}
    # Der zweite Lauf ist vollständig und wird der neue Zeitpunkt
    assert speicher.begonnen[BODY] > beginn


async def test_sicherheitsabstand_der_quelle_gilt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    beginn = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    options = SourceFetchOptions.from_sync_config({"modified_since_margin_minutes": 30})
    await _lauf(quelle, speicher, options=options)
    assert set(quelle.filter_der_anfragen("meeting")) == {(beginn - timedelta(minutes=30)).isoformat()}


async def test_lauf_mit_luecke_schreibt_den_zeitpunkt_nicht_fort():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    quelle.fest["/oparl/body/1/paper"] = 500
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == ["paper"]
    assert speicher.begonnen[BODY] == beginn

    # Der nächste Lauf fragt weiter ab dem alten Zeitpunkt
    del quelle.fest["/oparl/body/1/paper"]
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("paper")) == {(beginn - timedelta(hours=2)).isoformat(timespec="seconds")}
    assert speicher.begonnen[BODY] > beginn


async def test_unlesbare_mitgliedschaften_brechen_die_kommune_nicht_ab():
    """Bisher endete der Lauf an einer Mitgliedschaftsliste mit Fehler; Sitzungen und Vorlagen blieben stehen."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.fest["/oparl/body/1/membership"] = 500
    speicher = Speicher()
    stats = await _lauf(quelle, speicher, kommune=_kommune(membership=f"{BODY}/membership"))
    assert stats["incomplete"] == ["membership"]
    assert stats["meetings"] == 3 and stats["papers"] == 3
    assert BODY not in speicher.begonnen


async def test_vollabgleich_ist_ein_gueltiger_zeitpunkt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = Speicher()
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == []
    assert speicher.begonnen[BODY] >= vorher
    assert all(f is None for f in quelle.filter_der_anfragen("paper"))


def test_fenster_nie_weiter_zurueck_als_sieben_tage():
    """Ein alter Zeitpunkt (Kommune lange unvollständig) fragt nie mehr ab als das bisherige Fenster."""
    jetzt = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
    bisher = fallback_modified_since(jetzt)
    assert modified_since_for(None, timedelta(hours=2), now=jetzt) == bisher
    # 30 Tage alt: das bisherige Fenster, unverändert als naive Ortszeit wie ohne Zeitpunkt
    assert modified_since_for(jetzt - timedelta(days=30), timedelta(hours=2), now=jetzt) == bisher
    # Ein großer Sicherheitsabstand reicht ebenfalls nicht weiter zurück
    assert modified_since_for(jetzt, timedelta(days=8), now=jetzt) == bisher
    # Innerhalb der sieben Tage gilt der Zeitpunkt
    drei_tage = modified_since_for(jetzt - timedelta(days=3), timedelta(hours=2), now=jetzt)
    assert drei_tage == jetzt - timedelta(days=3, hours=2)
    assert drei_tage.tzinfo is not None and drei_tage > bisher.replace(tzinfo=UTC)


async def test_alter_zeitpunkt_fragt_hoechstens_sieben_tage_ab():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(days=30)
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("paper")) == {_bisheriges_fenster()}


async def test_fehler_beim_speichern_halten_das_fenster_beim_objekt():
    """
    Ein Objekt, das sich nicht speichern lässt (Deadlock, Constraint), fragt der nächste Lauf wieder ab: Das Fenster
    rückt höchstens bis zu seinem ``modified`` vor. Eine Lücke der Quelle ist es nicht.
    """
    geaendert = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    quelle = Quelle(geaendert=geaendert)
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    speicher.fehler.add(f"{BASE}/paper/1")

    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    assert stats["unstored"] == {"paper": 1}
    assert sync_complete([stats], [], _OhneBefund()) is True  # # kein erneuter Vollabgleich jede Nacht
    assert f"{BASE}/paper/1" not in speicher.modified
    assert speicher.begonnen[BODY] == geaendert

    # Der nächste Lauf fragt ab dem modified des Objekts (abzüglich Abstand) und speichert es
    speicher.fehler.clear()
    stats = await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("paper")) == {(geaendert - timedelta(hours=2)).isoformat()}
    assert f"{BASE}/paper/1" in speicher.modified
    assert "unstored" not in stats
    assert speicher.begonnen[BODY] > geaendert


async def test_fehler_ohne_modified_schreibt_den_zeitpunkt_nicht_fort():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    del quelle.listen["/oparl/body/1/paper"][1]["modified"]
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    speicher.fehler.add(f"{BASE}/paper/1")
    stats = await _lauf(quelle, speicher)
    assert stats["unstored"] == {"paper": 1}
    assert speicher.begonnen[BODY] == beginn


async def test_mitgliedschaft_ohne_person_haelt_das_fenster():
    geaendert = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=40))
    quelle.listen["/oparl/body/1/membership"] = [
        {
            "id": f"{BASE}/membership/1",
            "type": "https://schema.oparl.org/1.1/Membership",
            "person": f"{BASE}/person/unbekannt",
            "organization": f"{BASE}/organization/0",
            "modified": geaendert.isoformat(),
        }
    ]
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    speicher.unaufloesbar.add(f"{BASE}/membership/1")
    stats = await _lauf(quelle, speicher, kommune=_kommune(membership=f"{BODY}/membership"))
    assert stats["incomplete"] == [] and stats["unstored"] == {"membership": 1}
    assert speicher.begonnen[BODY] == geaendert


async def test_tagesordnungspunkt_ohne_sitzung_gilt_als_nicht_gespeichert():
    class Bestand:
        async def get_meeting_uuid(self, external_id: str) -> UUID | None:
            return None

    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = Bestand()  # type: ignore[assignment]
    punkt = OParlProcessor().process(
        {
            "id": f"{BASE}/agendaitem/1",
            "type": "https://schema.oparl.org/1.1/AgendaItem",
            "meeting": f"{BASE}/meeting/fehlt",
            "modified": "2026-10-15T10:00:00+02:00",
        },
        BODY,
    )
    assert punkt is not None
    assert await orchestrator._store_entity(punkt, UUID(int=1), "agendaitem") is False


async def test_listen_mit_tagesstempeln_bleiben_bei_sieben_tagen():
    """
    Trägt eine Liste nur das Datum (00:00 Uhr), fände ein Fenster ab „Beginn minus zwei Stunden“ ab etwa 02:00 Uhr
    nichts mehr vom selben Tag. Diese Liste läuft weiter mit sieben Tagen, die übrigen mit dem kurzen Fenster.
    """
    heute = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    quelle = Quelle(geaendert=datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3))
    for objekt in quelle.listen["/oparl/body/1/meeting"]:
        objekt["modified"] = heute.isoformat()
    speicher = Speicher()

    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == []
    assert stats["day_stamped_lists"] == [f"{BODY}/meeting"]
    assert speicher.tagesstempel == {f"{BODY}/meeting"}
    beginn = speicher.begonnen[BODY]  # die Kommune rückt trotzdem vor

    # Inkrementell: Sitzungen mit sieben Tagen, Vorgänge mit dem kurzen Fenster
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("meeting")) == {_bisheriges_fenster()}
    assert set(quelle.filter_der_anfragen("paper")) == {(beginn - timedelta(hours=2)).isoformat(timespec="seconds")}

    # Eine ruhige Woche: Kein Lauf sieht Sitzungen, der Vermerk bleibt trotzdem und mit ihm die sieben Tage
    for objekt in quelle.listen["/oparl/body/1/meeting"]:
        objekt["modified"] = (heute - timedelta(days=10)).isoformat()
    for _ in range(2):
        stats = await _lauf(quelle, speicher)
        assert "day_stamped_lists" not in stats
        assert set(quelle.filter_der_anfragen("meeting")) == {_bisheriges_fenster()}
    assert speicher.tagesstempel == {f"{BODY}/meeting"}

    # Sieht ein Lauf dort Uhrzeiten, wird der Vermerk gelöscht; danach gilt auch dort das kurze Fenster
    for objekt in quelle.listen["/oparl/body/1/meeting"]:
        objekt["modified"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await _lauf(quelle, speicher)
    assert speicher.tagesstempel == set()
    beginn = speicher.begonnen[BODY]
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("meeting")) == {(beginn - timedelta(hours=2)).isoformat(timespec="seconds")}


def test_tagesstempel_je_liste():
    befund = BodyItemCheck()
    for stempel in ("2026-10-15T00:00:00+02:00", "2026-10-14", None, "kaputt"):
        befund.note_stamp("tage", {"modified": stempel})
    befund.note_stamp("gemischt", {"modified": "2026-10-15T00:00:00+02:00"})
    befund.note_stamp("gemischt", {"modified": "2026-10-15T09:12:00+02:00"})
    befund.note_stamp("leer", {})
    assert befund.day_stamped_lists() == ["tage"]
    assert befund.timed_lists() == ["gemischt"]


# ---------------------------------------------------------------------------
# Listen, die es bei der Quelle nicht gibt
# ---------------------------------------------------------------------------


def test_vermerk_gilt_einen_tag():
    jetzt = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    listen = MissingLists(
        SOURCE,
        {
            "neu": jetzt - timedelta(hours=1),
            "alt": jetzt - timedelta(days=1),
            "ohne": None,
            "zukunft": jetzt + timedelta(hours=3),
        },
    )
    assert listen.skip("neu", jetzt)
    assert not listen.skip("alt", jetzt)
    assert not listen.skip("ohne", jetzt)
    assert not listen.skip("zukunft", jetzt)  # falsche Uhr: lieber prüfen
    assert not listen.skip("unbekannt", jetzt)


async def test_fehlende_liste_bis_zum_vollabgleich_nicht_mehr_angefragt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.fest["/oparl/body/1/agendaItem"] = FEHLEROBJEKT  # gibt es nicht (Fehlerobjekt mit HTTP 200)
    kommune = _kommune(membership=f"{BODY}/membership", agendaItem=f"{BODY}/agendaItem")  # membership: 404
    speicher = Speicher()

    # Erster Lauf: beide Listen mit Filter, dann einmal ohne, dann vermerkt; kein Fehler, keine Lücke
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert set(speicher.fehlend) == {f"{BODY}/membership", f"{BODY}/agendaItem"}
    assert stats["missing_lists"] == sorted(speicher.fehlend)
    assert quelle.anfragen_je_liste()["membership"] == 2
    assert BODY in speicher.begonnen  # vollständig

    # Inkrementelle Läufe fragen sie nicht mehr an
    for _ in range(2):
        stats = await _lauf(quelle, speicher, kommune=kommune)
        zaehler = quelle.anfragen_je_liste()
        assert zaehler["membership"] == 0 and zaehler["agendaItem"] == 0
        assert stats["incomplete"] == []

    # Der Vollabgleich prüft wieder: Die Mitgliedschaften gibt es jetzt, die Tagesordnungspunkte weiter nicht
    quelle.listen["/oparl/body/1/membership"] = []
    stats = await _lauf(quelle, speicher, full=True, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 1
    assert quelle.anfragen_je_liste()["agendaItem"] == 1
    assert set(speicher.fehlend) == {f"{BODY}/agendaItem"}
    assert stats["incomplete"] == []

    # Danach fragt der inkrementelle Lauf die Mitgliedschaften wieder an
    await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 1


async def test_404_nur_mit_filter_ist_keine_fehlende_liste():
    """Bezirksregierung Köln: membership?modified_since=… antwortet 404, ohne Filter kommt die Liste."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    quelle.listen["/oparl/body/1/membership"] = [
        {
            "id": f"{BASE}/membership/1",
            "type": "https://schema.oparl.org/1.1/Membership",
            "modified": "2025-01-01T10:00:00+01:00",
        }
    ]
    quelle.mit_filter["/oparl/body/1/membership"] = 404
    kommune = _kommune(membership=f"{BODY}/membership")
    speicher = Speicher()

    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["incomplete"] == [] and stats["memberships"] == 1
    assert speicher.fehlend == {}
    assert quelle.filter_der_anfragen("membership")[0] is not None
    assert quelle.filter_der_anfragen("membership")[1:] == [None]

    # Im selben Prozess ohne die gefilterte Anfrage, die anderen Listen weiter mit Filter
    await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.filter_der_anfragen("membership") == [None]
    assert all(f is not None for f in quelle.filter_der_anfragen("paper"))


async def test_alle_listen_404_ist_eine_stoerung_und_kein_vermerk():
    quelle = Quelle(geaendert=datetime.now(UTC))
    for name in LISTEN:
        quelle.fest[f"/oparl/body/1/{name}"] = 404
    speicher = Speicher()
    # membership und agendaItem gibt es nicht (optional), die Pflichtlisten antworten ebenfalls mit 404
    stats = await _lauf(quelle, speicher, kommune=_kommune(membership=f"{BODY}/membership", agendaItem=f"{BODY}/a"))
    assert speicher.fehlend == {}
    assert "lists" in stats["incomplete"]
    assert BODY not in speicher.begonnen


async def test_client_meldet_fehlende_liste_nur_auf_wunsch():
    quelle = Quelle(geaendert=datetime.now(UTC))
    quelle.fest["/oparl/body/1/agendaItem"] = FEHLEROBJEKT
    async with _client(quelle) as client:
        with pytest.raises(ListMissingError) as fehlt:
            async for _ in client.fetch_list(f"{BODY}/membership", report_missing=True):
                pass
        assert fehlt.value.reason == "HTTP 404"
        with pytest.raises(ListMissingError):
            async for _ in client.fetch_list(f"{BODY}/agendaItem", report_missing=True):
                pass
        # Ohne report_missing wie bisher: 404 ist ein Listenfehler, ein Fehlerobjekt eine leere Liste
        with pytest.raises(ListFetchError):
            async for _ in client.fetch_list(f"{BODY}/membership"):
                pass
        assert [page async for page in client.fetch_list(f"{BODY}/agendaItem")] == []
        # Kein Fehlvermerk am Host
        assert client.host_health[HOST].failed_lists == [f"{BODY}/membership"]


async def test_404_loest_den_schutzschalter_nicht_aus(monkeypatch):
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    quelle = Quelle(geaendert=datetime.now(UTC))
    async with _client(quelle) as client:
        for i in range(settings.circuit_breaker_failure_threshold + 2):
            ergebnis = await client.fetch(f"{BASE}/gibt-es-nicht/{i}", use_cache=False)
            assert ergebnis.status_code == 404
        ergebnis = await client.fetch(f"{BODY}/paper", use_cache=False)
        assert ergebnis.status_code == 200 and ergebnis.error is None


async def test_pflichtliste_mit_404_oder_fehlerobjekt_bleibt_eine_luecke():
    """Sitzungen und Vorlagen fehlten sonst nach einem kurzen 404 bis zum nächsten Vollabgleich (bis zu 28 Tage)."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    quelle.fest["/oparl/body/1/meeting"] = 404
    quelle.fest["/oparl/body/1/paper"] = FEHLEROBJEKT
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    stats = await _lauf(quelle, speicher)
    assert sorted(stats["incomplete"]) == ["meeting", "paper"]
    assert sum("Pflichtliste" in fehler for fehler in stats["errors"]) == 2
    assert speicher.fehlend == {}
    assert speicher.begonnen[BODY] == beginn

    # Der nächste Lauf fragt beide wieder an, mit Filter ab dem alten Zeitpunkt
    del quelle.fest["/oparl/body/1/meeting"], quelle.fest["/oparl/body/1/paper"]
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == [] and stats["meetings"] == 3 and stats["papers"] == 3
    erwartet = (beginn - timedelta(hours=2)).isoformat(timespec="seconds")
    assert set(quelle.filter_der_anfragen("meeting")) == {erwartet}


async def test_vermerkte_liste_wird_einmal_am_tag_geprueft():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    kommune = _kommune(membership=f"{BODY}/membership")  # 404
    liste = f"{BODY}/membership"
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)

    # Vor einer Stunde geprüft: nicht angefragt
    speicher.fehlend[liste] = datetime.now(UTC) - timedelta(hours=1)
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 0 and stats["incomplete"] == []

    # Vor mehr als einem Tag geprüft: wieder angefragt (mit Filter, dann ohne), weiter vermerkt, neuer Zeitpunkt
    vorher = datetime.now(UTC)
    speicher.fehlend[liste] = vorher - timedelta(days=1, minutes=5)
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 2
    assert stats["incomplete"] == [] and stats["missing_lists"] == [liste]
    geprueft = speicher.fehlend[liste]
    assert geprueft is not None and geprueft >= vorher.replace(microsecond=0)

    # Gibt es die Liste wieder, fragt die Prüfung ab dem bisherigen Fenster (sieben Tage) und löscht den Vermerk
    speicher.fehlend[liste] = datetime.now(UTC) - timedelta(days=2)
    quelle.listen["/oparl/body/1/membership"] = [
        {
            "id": f"{BASE}/membership/1",
            "type": "https://schema.oparl.org/1.1/Membership",
            "modified": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        }
    ]
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.filter_der_anfragen("membership") == [_bisheriges_fenster()]
    assert stats["memberships"] == 1 and speicher.fehlend == {}


async def test_liste_ohne_filter_nur_wenn_sie_ohne_filter_lesbar_war():
    """Fehlt die Liste auch ohne Filter, sagte der 404 auf den Filter nichts: Kommt sie wieder, läuft sie mit Filter."""
    quelle = Quelle(geaendert=datetime.now(UTC))
    liste = f"{BODY}/membership"
    seit = datetime.now(UTC) - timedelta(hours=3)
    async with _client(quelle) as client:
        with pytest.raises(ListMissingError):
            async for _ in client.fetch_list(liste, modified_since=seit, report_missing=True):
                pass
        assert liste not in OParlClient._list_modified_since_unsupported
        quelle.listen["/oparl/body/1/membership"] = []
        quelle.anfragen.clear()
        assert [page async for page in client.fetch_list(liste, modified_since=seit, report_missing=True)] == []
        assert quelle.filter_der_anfragen("membership") == [seit.isoformat(timespec="seconds")]
