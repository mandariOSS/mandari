# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weniger Abrufe je Lauf (Issue #1025): Änderungsfenster seit dem letzten vollständigen Abgleich und Listen, die es
bei der Quelle nicht gibt.

- Inkrementelle Läufe fragen je Kommune nach Änderungen seit dem Beginn ihres letzten vollständig erfolgreichen
  Abgleichs abzüglich Sicherheitsabstand (Standard 2 Stunden), nicht mehr nach allem aus sieben Tagen. Ein Lauf
  mit Lücke schreibt den Zeitpunkt nicht fort, nicht gespeicherte Objekte halten ihn bei ihrem ``modified``, außer
  die Quelle liefert sie trotz Filter. Ohne Zeitpunkt oder mit einem älteren bleibt es bei sieben Tagen. Das kurze
  Fenster gilt nur für Listen mit nachgewiesener Uhrzeit in ``modified``, die übrigen beginnen um 00:00 Uhr.
- Antwortet eine optionale Liste schon auf der ersten Seite mit HTTP 404 oder einem OParl-Fehlerobjekt, wird sie
  vermerkt und bis zum Vollabgleich, höchstens einen Tag, nicht mehr angefragt. Das zählt als „nicht vorhanden“,
  nicht als Lücke. Bei Pflichtlisten ist HTTP 404 eine Lücke; ein Fehlerobjekt gilt dort wie bisher als leer,
  solange eine andere Liste lesbar ist.

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

from src.client.filter_notes import FilterNotes
from src.client.oparl_client import ListFetchError, ListMissingError, OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.storage.database import body_sync_started_of, missing_lists_of, timed_lists_of
from src.sync.orchestrator import (
    SOURCE_TZ,
    BodyItemCheck,
    MissingLists,
    SyncOrchestrator,
    SyncResult,
    TimedLists,
    day_start_window,
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
        #: Grund je vermerkter Liste (Issue #1086)
        self.gruende: dict[str, str] = {}
        self.modified: dict[str, datetime | None] = {}
        self.vermerke: list[tuple[str, bool]] = []
        #: Kennungen, deren Speichern mit einer Ausnahme scheitert (Deadlock, Constraint)
        self.fehler: set[str] = set()
        #: Kennungen, deren Bezüge sich nicht auflösen lassen (Mitgliedschaft ohne Person, TOP ohne Sitzung)
        self.unaufloesbar: set[str] = set()
        #: Listen mit nachgewiesener Uhrzeit (``timed_lists``): die vier Pflichtlisten, wie bei einer Quelle nach den
        #: ersten Läufen, damit die Tests das kurze Fenster sehen
        self.uhrzeit: set[str] = {f"{BODY}/{name}" for name in LISTEN}
        #: Einträge im Sync-Log
        self.protokoll: list[dict[str, Any]] = []
        #: Vermerke „filtert nicht“ der Quelle; gelten wie in ``sync_config`` über die Läufe hinweg (Issue #1041)
        self.filtervermerke = FilterNotes()

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

    async def set_list_timed(self, source_url: str, list_url: str, timed: bool) -> None:
        if timed:
            self.uhrzeit.add(list_url)
        else:
            self.uhrzeit.discard(list_url)

    async def write_sync_log(self, **felder: Any) -> None:
        self.protokoll.append(felder)

    async def set_list_missing(
        self,
        source_url: str,
        list_url: str,
        missing: bool,
        checked_at: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        self.vermerke.append((list_url, missing))
        self.gruende.pop(list_url, None)
        if missing:
            self.fehlend[list_url] = checked_at or datetime.now(UTC)
            if reason:
                self.gruende[list_url] = reason
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
async def _client(quelle: Quelle, notizen: FilterNotes | None = None) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0, filter_notes=notizen) as client:
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
    listen = MissingLists(SOURCE, dict(speicher.fehlend), dict(speicher.gruende))
    async with _client(quelle, speicher.filtervermerke) as client:
        return await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(),
            source_id=UUID(int=9),
            full=full,
            options=options,
            missing_lists=listen,
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
        )


@pytest.fixture(autouse=True)
def _ohne_nebenlaeufe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keine Texterkennung, kein Suchindex, ein Versuch je Anfrage."""
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "oparl_max_retries", 1)
    yield


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
    assert timed_lists_of({"timed_lists": {f"{BODY}/paper": "2026-10-15T10:00:00+00:00", "": "x"}}) == {f"{BODY}/paper"}
    assert timed_lists_of({"timed_lists": [f"{BODY}/paper"]}) == set()
    assert timed_lists_of(None) == set()


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


async def test_fehler_beim_speichern_im_vollabgleich_halten_das_fenster():
    """Wie inkrementell: Der nächste Lauf fragt das Objekt wieder ab, statt erst der nächste Vollabgleich."""
    geaendert = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    quelle = Quelle(geaendert=geaendert)
    speicher = Speicher()
    speicher.fehler.add(f"{BASE}/paper/1")

    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == [] and stats["unstored"] == {"paper": 1}
    assert stats["papers"] == 2
    assert speicher.begonnen[BODY] == geaendert


async def test_element_ohne_objekt_bricht_den_vollabgleich_nicht_ab():
    """Ein Listenelement, das kein Objekt ist, wird übersprungen wie bisher; es hält das Fenster wie eines ohne
    ``modified``, statt im Fehlerzweig die ganze Liste abzubrechen."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    quelle.listen["/oparl/body/1/paper"].insert(1, "kaputt")  # type: ignore[arg-type]
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert stats["papers"] == 3 and stats["unstored"] == {"paper": 1}
    assert speicher.begonnen[BODY] == beginn

    befund = BodyItemCheck()
    befund.note_unstored("paper", None)
    befund.note_unstored("paper", ["x"])
    assert befund.unstored == {"paper": 2} and befund.unstored_ids == {"paper": ["ohne Kennung", "ohne Kennung"]}
    assert befund.window_start(datetime.now(UTC)) is None


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


def _mitgliedschaft(nummer: int, geaendert: str) -> dict[str, Any]:
    return {
        "id": f"{BASE}/membership/{nummer}",
        "type": "https://schema.oparl.org/1.1/Membership",
        "person": f"{BASE}/person/fremd",
        "organization": f"{BASE}/organization/0",
        "modified": geaendert,
    }


async def test_waisen_vor_dem_fenster_halten_es_nicht():
    """
    Bezirksregierung Köln: membership läuft ohne Filter und liefert jedes Mal alle Mitgliedschaften, darunter solche
    mit einer Person aus einer anderen Quelle. Sie lassen sich nie speichern; hielten sie das Fenster bei ihrem alten
    ``modified``, bliebe die Kommune für immer bei sieben Tagen. Eine nicht speicherbare Mitgliedschaft aus dem
    Fenster hält es weiter.
    """
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.mit_filter["/oparl/body/1/membership"] = 404
    quelle.listen["/oparl/body/1/membership"] = [
        _mitgliedschaft(1, "2025-01-01T10:00:00+01:00"),
        _mitgliedschaft(2, "2026-01-01T10:00:00+01:00"),
    ]
    kommune = _kommune(membership=f"{BODY}/membership")
    speicher = Speicher()
    speicher.unaufloesbar.update({f"{BASE}/membership/1", f"{BASE}/membership/2", f"{BASE}/membership/3"})
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)

    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.filter_der_anfragen("membership")[1:] == [None]  # ohne Filter
    assert stats["incomplete"] == [] and stats["unstored"] == {"membership": 2}
    assert speicher.begonnen[BODY] >= vorher

    # Eine neue Waise aus dem Fenster hält es bei ihrem modified
    geaendert = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
    quelle.listen["/oparl/body/1/membership"].append(_mitgliedschaft(3, geaendert.isoformat()))
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["unstored"] == {"membership": 3}
    assert speicher.begonnen[BODY] == geaendert


def test_nicht_gespeichert_vor_dem_fenster():
    fenster = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    befund = BodyItemCheck()
    # Zwölf Stunden vor dem Fenster: kann eine falsch angegebene Zeitzone sein, hält das Fenster
    befund.note_unstored("paper", {"id": "a", "modified": "2026-10-14T20:00:00+00:00"}, fenster)
    assert befund.oldest_unstored == datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
    # Mehr als einen Tag davor: Die Quelle liefert es ohne Filter, es hält das Fenster nicht
    befund.note_unstored("paper", {"id": "b", "modified": "2026-10-10T20:00:00+00:00"}, fenster)
    assert befund.unstored_before_window == 1
    assert befund.oldest_unstored == datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
    # Das bisherige Fenster (naiv) gilt als UTC, wie es der Client sendet
    befund.note_unstored("paper", {"id": "c", "modified": "2026-10-01T00:00:00+00:00"}, datetime(2026, 10, 8))
    assert befund.unstored_before_window == 2
    # Im Vollabgleich (ohne Fenster) hält jedes Objekt mit modified
    befund.note_unstored("paper", {"id": "d", "modified": "2026-01-01T00:00:00+00:00"})
    assert befund.oldest_unstored == datetime(2026, 1, 1, tzinfo=UTC)
    # Kennungen fürs Log, höchstens fünf je Typ
    for nummer in range(5):
        befund.note_unstored("paper", {"id": f"x{nummer}"}, fenster)
    assert befund.unstored == {"paper": 9}
    assert befund.unstored_ids == {"paper": ["a", "b", "c", "d", "x0"]}


async def test_nicht_gespeicherte_im_log_und_sync_log(capsys):
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    speicher.unaufloesbar.update({f"{BASE}/paper/0", f"{BASE}/paper/2"})
    stats = await _lauf(quelle, speicher)
    assert stats["unstored"] == {"paper": 2}
    ausgabe = " ".join(capsys.readouterr().out.split())
    assert f"paper: {BASE}/paper/0, {BASE}/paper/2" in ausgabe

    orchestrator = _orchestrator(speicher)
    ergebnis = SyncResult(source_url=SOURCE, source_name="Musterstadt", success=True)
    ergebnis.unstored.update(stats["unstored"])
    ruhig = SyncResult(source_url=f"{SOURCE}/2", source_name="Ruhig", success=True)
    await orchestrator.write_results_to_synclog([ergebnis, ruhig], datetime.now(UTC))
    details = speicher.protokoll[0]["details"]
    assert details["Musterstadt"]["unstored"] == {"paper": 2}
    assert "unstored" not in details["Ruhig"]


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


def _gesendet(zeitpunkt: datetime) -> str:
    """``modified_since``, wie der Client es sendet (naive Zeitpunkte mit ``+00:00``)."""
    return (zeitpunkt if zeitpunkt.tzinfo is not None else zeitpunkt.replace(tzinfo=UTC)).isoformat(timespec="seconds")


def test_fenster_ab_tagesbeginn():
    jetzt = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
    # Tagsüber: 00:00 Uhr Ortszeit (22:00 UTC am Vortag) liegt vor 00:00 UTC
    tagsueber = day_start_window(datetime(2026, 10, 15, 8, 0, tzinfo=UTC), now=jetzt)
    assert tagsueber == datetime(2026, 10, 14, 22, 0, tzinfo=UTC)
    # Nach Mitternacht Ortszeit, vor Mitternacht UTC: 00:00 UTC liegt früher
    nachts = day_start_window(datetime(2026, 10, 14, 23, 30, tzinfo=UTC), now=jetzt)
    assert nachts == datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    # Winterzeit
    winter = day_start_window(datetime(2026, 12, 1, 8, 0, tzinfo=UTC), now=datetime(2026, 12, 1, 9, 0, tzinfo=UTC))
    assert winter == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
    # Das bisherige Fenster bleibt, wie es ist; weiter zurück als sieben Tage geht es nie
    bisher = fallback_modified_since(jetzt)
    assert day_start_window(bisher, now=jetzt) == bisher
    assert day_start_window(bisher.replace(tzinfo=UTC) + timedelta(hours=1), now=jetzt) == bisher


async def test_listen_ohne_uhrzeit_ab_tagesbeginn():
    """
    Trägt eine Liste nur das Datum (00:00 Uhr), fände ein Fenster ab „Beginn minus zwei Stunden“ ab etwa 02:00 Uhr
    nichts mehr vom selben Tag. Das kurze Fenster bekommen nur Listen, deren Änderungen nachweislich eine Uhrzeit
    tragen; die übrigen fragt der Lauf ab 00:00 Uhr an, auch nach einer ruhigen Woche ohne Änderungen.
    """
    heute = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    quelle = Quelle(geaendert=datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3))
    sitzungen = quelle.listen["/oparl/body/1/meeting"]
    for objekt in sitzungen:
        objekt["modified"] = heute.isoformat()
    speicher = Speicher()
    speicher.uhrzeit.clear()

    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == []
    assert stats["day_stamped_lists"] == [f"{BODY}/meeting"]
    assert speicher.uhrzeit == {f"{BODY}/organization", f"{BODY}/person", f"{BODY}/paper"}
    beginn = speicher.begonnen[BODY]  # die Kommune rückt trotzdem vor

    # Inkrementell: Sitzungen ab Tagesbeginn, Vorlagen mit dem kurzen Fenster
    await _lauf(quelle, speicher)
    kurz = beginn - timedelta(hours=2)
    assert set(quelle.filter_der_anfragen("meeting")) == {_gesendet(day_start_window(kurz))}
    assert set(quelle.filter_der_anfragen("paper")) == {_gesendet(kurz)}

    # Eine ruhige Woche: Kein Lauf sieht Sitzungen, die Liste bleibt beim Fenster ab Tagesbeginn
    for objekt in sitzungen:
        objekt["modified"] = (heute - timedelta(days=10)).isoformat()
    for _ in range(2):
        kurz = speicher.begonnen[BODY] - timedelta(hours=2)
        stats = await _lauf(quelle, speicher)
        assert "day_stamped_lists" not in stats
        assert set(quelle.filter_der_anfragen("meeting")) == {_gesendet(day_start_window(kurz))}
    assert f"{BODY}/meeting" not in speicher.uhrzeit

    # Eine Sitzung von heute mit Tagesstempel (00:00 Uhr Ortszeit) findet der nächste Lauf
    ortszeit = datetime.now(SOURCE_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    neu = {"id": f"{BASE}/meeting/9", "type": "https://schema.oparl.org/1.1/Meeting", "modified": ortszeit.isoformat()}
    sitzungen.append(neu)
    stats = await _lauf(quelle, speicher)
    assert stats["meetings"] == 1 and stats["day_stamped_lists"] == [f"{BODY}/meeting"]

    # Tragen die Änderungen dort Uhrzeiten, bekommt die Liste das kurze Fenster
    neu["modified"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await _lauf(quelle, speicher)
    assert f"{BODY}/meeting" in speicher.uhrzeit
    kurz = speicher.begonnen[BODY] - timedelta(hours=2)
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("meeting")) == {_gesendet(kurz)}

    # Gemischte Stempel: Sieht der Vollabgleich dort eine Änderung nur mit Datum, verliert sie es wieder
    sitzungen.append({**neu, "id": f"{BASE}/meeting/8", "modified": ortszeit.isoformat()})
    stats = await _lauf(quelle, speicher, full=True)
    assert f"{BODY}/meeting" in stats["day_stamped_lists"]
    assert f"{BODY}/meeting" not in speicher.uhrzeit


def test_tagesstempel_je_liste():
    befund = BodyItemCheck(recent_since=datetime(2026, 10, 8, tzinfo=UTC))
    for stempel in ("2026-10-15T00:00:00+02:00", "2026-10-14", None, "kaputt"):
        befund.note_stamp("tage", {"modified": stempel})
    # 00:00 Uhr Ortszeit, in UTC angegeben (Sommer- und Winterzeit)
    befund.note_stamp("utc", {"modified": "2026-10-14T22:00:00Z"})
    befund.note_stamp("utc", {"modified": "2026-12-01T23:00:00+00:00"})
    befund.note_stamp("gemischt", {"modified": "2026-10-15T00:00:00+02:00"})
    befund.note_stamp("gemischt", {"modified": "2026-10-15T09:12:00+02:00"})
    befund.note_stamp("uhrzeit", {"modified": "2026-10-15T09:12:00+02:00"})
    befund.note_stamp("uhrzeit", {"modified": "2026-10-14T23:00:00Z"})  # 01:00 Uhr Ortszeit
    # Altdaten vor den letzten sieben Tagen zählen nicht
    befund.note_stamp("uhrzeit", {"modified": "2020-01-01T00:00:00+01:00"})
    befund.note_stamp("alt", {"modified": "2020-01-01T00:00:00+01:00"})
    befund.note_stamp("leer", {})
    assert befund.day_stamped_lists() == ["gemischt", "tage", "utc"]
    assert befund.timed_lists() == ["uhrzeit"]


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

    # Im nächsten Lauf ohne die gefilterte Anfrage (Vermerk der Quelle), die anderen Listen weiter mit Filter
    assert set(speicher.filtervermerke.lists) == {f"{BODY}/membership"}
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


async def test_pflichtliste_mit_404_bleibt_eine_luecke():
    """
    Sitzungen und Vorlagen fehlten sonst nach einem kurzen 404 bis zum nächsten Vollabgleich (bis zu 28 Tage). Ein
    Fehlerobjekt auf einer Pflichtliste gilt wie bisher als leere Liste (Warnung), solange andere Listen lesbar sind.
    """
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    quelle.fest["/oparl/body/1/meeting"] = 404
    quelle.fest["/oparl/body/1/paper"] = FEHLEROBJEKT
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == ["meeting"]
    assert sum("Pflichtliste" in fehler for fehler in stats["errors"]) == 1
    assert stats["empty_required_lists"] == [f"{BODY}/paper"]
    assert speicher.fehlend == {}
    assert speicher.begonnen[BODY] == beginn

    # Der nächste Lauf fragt beide wieder an, mit Filter ab dem alten Zeitpunkt
    del quelle.fest["/oparl/body/1/meeting"], quelle.fest["/oparl/body/1/paper"]
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == [] and stats["meetings"] == 3 and stats["papers"] == 3
    erwartet = (beginn - timedelta(hours=2)).isoformat(timespec="seconds")
    assert set(quelle.filter_der_anfragen("meeting")) == {erwartet}


async def test_pflichtliste_mit_fehlerobjekt_gilt_als_leer():
    """
    OParl 1.0 (more! rubin) antwortet auf Klassen, die es nicht gibt, mit „Requested class doesn't exist.“. Auf dev
    galt das als leere Liste; eine Lücke daraus hielte das Fenster und wiederholte den Vollabgleich jede Nacht.
    """
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    quelle.fest["/oparl/body/1/organization"] = FEHLEROBJEKT
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)

    for lauf in range(2):
        vorher = datetime.now(UTC)
        stats = await _lauf(quelle, speicher)
        assert stats["incomplete"] == [] and stats["errors"] == []
        assert stats["empty_required_lists"] == [f"{BODY}/organization"]
        assert stats["organizations"] == 0 and stats["papers"] == (3 if lauf == 0 else 0)
        assert speicher.fehlend == {}  # nie vermerkt, jeder Lauf fragt sie an
        assert quelle.anfragen_je_liste()["organization"] == 2  # mit Filter, dann ohne
        assert sync_complete([stats], [], _OhneBefund()) is True
        assert speicher.begonnen[BODY] >= vorher


async def test_nur_fehlerobjekte_ist_eine_luecke():
    quelle = Quelle(geaendert=datetime.now(UTC))
    for name in LISTEN:
        quelle.fest[f"/oparl/body/1/{name}"] = FEHLEROBJEKT
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == ["lists"]
    assert len(stats["errors"]) == len(LISTEN)
    assert "empty_required_lists" not in stats
    assert speicher.begonnen[BODY] == beginn


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
        assert liste not in client.filter_notes.lists
        quelle.listen["/oparl/body/1/membership"] = []
        quelle.anfragen.clear()
        assert [page async for page in client.fetch_list(liste, modified_since=seit, report_missing=True)] == []
        assert quelle.filter_der_anfragen("membership") == [seit.isoformat(timespec="seconds")]
