# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vollabgleich in Zeitscheiben und langsame Listen (Issue #1033).

Nachgebildet ist SD.NET RIM wie bei der Stadt Hamm (gemessen am 15.10.2026):

- Die ungefilterten Listen ``body/1/paper`` und ``body/1/meeting`` brechen nach 60 Sekunden mit HTTP 504 ab.
- Mit ``modified_since`` und ``modified_until`` antworten dieselben Listen; der Filter bleibt in ``links.next``.
- Eine gefilterte Liste ohne Treffer kommt als HTTP 404 mit Code 802 (Issue #1034).
- ``body/1/consultation?modified_since=…`` läuft in Zeitüberschreitungen (dreimal 60 Sekunden je Lauf).

Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from mandari_oparl.utils import parse_datetime

from src.client.filter_notes import FilterNotes
from src.client.oparl_client import ListFetchError, ListMissingError, OParlClient
from src.client.source_options import FullSyncSlices, FullSyncWindow, SourceFetchOptions
from src.scheduler.takt import default_full_sync_window
from src.storage.database import (
    SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY,
    SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY,
    SYNC_CONFIG_SLOW_LISTS_KEY,
    _put_entry,
    full_sync_progress_of,
    slice_filter_ignored_of,
    slow_lists_of,
)
from src.sync.orchestrator import (
    MissingLists,
    SyncOrchestrator,
    SyncResult,
    TimedLists,
    full_sync_finished,
    merge_slice_details,
    sync_complete,
)
from src.sync.slices import (
    SLICE_TOLERANCE,
    SLOW_LIST_SLICE,
    SliceRun,
    SliceWindow,
    SlowList,
    day_floor,
    full_windows,
    slow_backoff,
    slow_windows,
)
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    Speicher,
    _ohne_nebenlaeufe,
    _orchestrator,
)
from tests.test_loeschung_nur_vollstaendig import _speicher_mit

HOST = "oparl.hamm.example"
BASE = f"https://{HOST}/oparl"
BODY = f"{BASE}/body/1"
SOURCE = f"{BASE}/system"
#: Antwort von SD.NET RIM auf eine gefilterte Liste ohne Treffer (Stadt Hamm, 15.10.2026, HTTP 404)
KEINE_TREFFER = {
    "error": "Die angeforderte Ressource wurde nicht gefunden.",
    "code": 802,
    "type": "SD.NET RIM Webservice",
}
SCHEIBEN = {"days": 365, "since": "2020-01-01"}
FEHLEROBJEKT = {"type": "https://schema.oparl.org/1.1/Error", "message": "Interner Fehler"}
BEGINN = datetime(2020, 1, 1, tzinfo=UTC)
FILTER = ("modified_since", "modified_until")
TYPEN = {
    "organization": "Organization",
    "person": "Person",
    "meeting": "Meeting",
    "paper": "Paper",
    "consultation": "Consultation",
}


def _jetzt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stempel(jetzt: datetime) -> list[datetime]:
    """Änderungszeitpunkte der Objekte: vor ``since``, in mehreren Jahresscheiben, gestern und vor einer Stunde."""
    return [
        datetime(2015, 6, 1, 9, 30, tzinfo=UTC),
        datetime(2020, 3, 15, 10, 0, tzinfo=UTC),
        datetime(2022, 7, 1, 8, 0, tzinfo=UTC),
        jetzt - timedelta(days=1),
        jetzt - timedelta(hours=1),
    ]


class HammQuelle:
    """SD.NET RIM wie bei der Stadt Hamm (siehe Moduldokumentation), Seiten zu zwei Objekten."""

    def __init__(self, jetzt: datetime | None = None) -> None:
        jetzt = jetzt or _jetzt()
        self.anfragen: list[httpx.URL] = []
        self.listen: dict[str, list[dict[str, Any]]] = {}
        for name, typ in TYPEN.items():
            self.listen[name] = [
                {
                    "id": f"{BASE}/{name}/{i}",
                    "type": f"https://schema.oparl.org/1.1/{typ}",
                    "name": f"{typ} {i}",
                    "modified": stempel.isoformat(),
                }
                for i, stempel in enumerate(_stempel(jetzt))
            ]
        #: Diese Listen brechen ohne Filter mit HTTP 504 ab
        self.ungefiltert_504 = {"paper", "meeting"}
        #: Diese Listen laufen mit Filter in eine Zeitüberschreitung (ab ``langsam_ab``, sonst immer)
        self.langsam: set[str] = set()
        self.langsam_ab: datetime | None = None
        #: Liste -> Statuscode für Scheiben, deren modified_since ab diesem Zeitpunkt liegt
        self.fehler_ab: dict[str, tuple[datetime, int]] = {}
        #: Liste -> OParl-Fehlerobjekt mit HTTP 200 für Scheiben, deren modified_since ab diesem Zeitpunkt liegt
        self.fehlerobjekt_ab: dict[str, datetime] = {}
        #: Diese Listen lehnen jeden Filter mit HTTP 401 ab (wie die Stadt Münster), ohne Filter antworten sie
        self.lehnt_filter_ab: set[str] = set()
        #: Die Quelle beachtet modified_until nicht
        self.ignoriert_until = False
        #: Objekte ohne modified liefert die Quelle in jeder gefilterten Antwort mit
        self.ohne_modified_immer = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request.url)
        name = request.url.path.rsplit("/", 1)[1]
        params = dict(request.url.params)
        objekte = self.listen.get(name)
        if objekte is None:
            return httpx.Response(404, json=KEINE_TREFFER)
        gefiltert = any(params.get(key) for key in FILTER)
        if gefiltert and name in self.lehnt_filter_ab:
            return httpx.Response(401)
        if not gefiltert and name in self.ungefiltert_504:
            return httpx.Response(504)
        seit = parse_datetime(params["modified_since"]) if params.get("modified_since") else None
        bis = parse_datetime(params["modified_until"]) if params.get("modified_until") else None
        if name in self.langsam and (self.langsam_ab is None or (seit is not None and seit >= self.langsam_ab)):
            raise httpx.ReadTimeout("timed out", request=request)
        if name in self.fehler_ab and seit is not None and seit >= self.fehler_ab[name][0]:
            return httpx.Response(self.fehler_ab[name][1])
        if name in self.fehlerobjekt_ab and seit is not None and seit >= self.fehlerobjekt_ab[name]:
            return httpx.Response(200, json=FEHLEROBJEKT)
        if self.ignoriert_until:
            bis = None
        treffer = []
        for objekt in objekte:
            stempel = parse_datetime(objekt["modified"]) if objekt.get("modified") else None
            if stempel is None:
                if not gefiltert or self.ohne_modified_immer:
                    treffer.append(objekt)
                continue
            if (seit is None or stempel >= seit) and (bis is None or stempel <= bis):
                treffer.append(objekt)
        if gefiltert and not treffer:
            return httpx.Response(404, json=KEINE_TREFFER)
        seite = int(params.get("page", "1"))
        teil = treffer[(seite - 1) * 2 : seite * 2]
        links: dict[str, str] = {}
        if seite * 2 < len(treffer):
            links["next"] = f"https://{HOST}{request.url.path}?{urlencode({**params, 'page': str(seite + 1)})}"
        return httpx.Response(200, json={"data": teil, "links": links})

    def anfragen_an(self, liste: str) -> list[httpx.URL]:
        return [url for url in self.anfragen if url.path.endswith(f"/{liste}")]


class ScheibenSpeicher(Speicher):
    """Speicher mit ``sync_config`` für Fortschritt, langsame Listen und Befund (wie ``DatabaseStorage``)."""

    def __init__(self) -> None:
        super().__init__()
        self.config: dict[str, Any] = {}
        #: Speichern einer Aussetzung scheitert (Datenbank nicht erreichbar)
        self.aussetzung_scheitert = False

    async def set_full_sync_progress(self, source_url: str, list_url: str, next_start: datetime | None) -> None:
        wert = None if next_start is None else next_start.astimezone(UTC).isoformat(timespec="seconds")
        _put_entry(self.config, SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY, list_url, wert)

    async def set_slow_list(self, source_url: str, list_url: str, entry: dict[str, Any] | None) -> None:
        if self.aussetzung_scheitert and entry is not None:
            raise RuntimeError("Datenbank nicht erreichbar")
        _put_entry(self.config, SYNC_CONFIG_SLOW_LISTS_KEY, list_url, entry)

    async def set_slice_filter_ignored(self, source_url: str, finding: dict[str, Any] | None) -> None:
        if finding is None:
            self.config.pop(SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY, None)
        else:
            self.config[SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY] = finding


def _kommune(*listen: str) -> dict[str, Any]:
    daten: dict[str, Any] = {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Hamm"}
    for name in listen or ("organization", "person", "meeting", "paper"):
        daten[name] = f"{BODY}/{name}"
    return daten


@asynccontextmanager
async def _client(
    quelle: HammQuelle, frist: datetime | None = None, notizen: FilterNotes | None = None
) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0, fetch_deadline=frist, filter_notes=notizen) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        yield client


async def _lauf(
    quelle: HammQuelle,
    speicher: ScheibenSpeicher,
    *,
    full: bool = True,
    scheiben: dict[str, Any] | None = None,
    kommune: dict[str, Any] | None = None,
    frist: datetime | None = None,
    ohne_scheiben: bool = False,
) -> dict[str, Any]:
    """
    Abgleich der Kommune wie im Betrieb: Zustand aus ``sync_config``, Zeitscheiben nur im Vollabgleich (mit
    ``ohne_scheiben`` eine Quelle ohne ``full_sync_slices``).
    """
    options = SourceFetchOptions.from_sync_config({} if ohne_scheiben else {"full_sync_slices": scheiben or SCHEIBEN})
    run = SliceRun.build(
        SOURCE,
        options.full_sync_slices if full else None,
        full_sync_progress_of(speicher.config),
        slow_lists_of(speicher.config),
    )
    quelle.anfragen.clear()
    async with _client(quelle, frist, speicher.filtervermerke) as client:
        return await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(),
            source_id=UUID(int=9),
            full=full,
            options=options,
            missing_lists=MissingLists(SOURCE, dict(speicher.fehlend)),
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
            slices=run,
        )


def _grenzen(url: httpx.URL) -> tuple[datetime | None, datetime | None]:
    params = dict(url.params)
    seit = parse_datetime(params["modified_since"]) if params.get("modified_since") else None
    bis = parse_datetime(params["modified_until"]) if params.get("modified_until") else None
    return seit, bis


class _OhneBefund:
    def host_findings(self) -> list[Any]:
        return []


_ECHTE_ZEIT = datetime


class _ZeitTyp(type):
    """Mit fester Uhr im Modul bleibt ``isinstance(wert, datetime)`` für echte Zeitpunkte wahr."""

    def __instancecheck__(cls, wert: object) -> bool:
        return isinstance(wert, _ECHTE_ZEIT)


def _feste_uhr(monkeypatch: pytest.MonkeyPatch, jetzt: datetime, ortszeit: str) -> None:
    """
    ``datetime.now()`` in Orchestrator und Zeitscheiben liefert ``jetzt``, ohne Zone als naive Ortszeit des Ingestors
    (``ortszeit``: in der CI UTC, im Container ``Europe/Berlin``).
    """
    zone = ZoneInfo(ortszeit)

    class FesteUhr(_ECHTE_ZEIT, metaclass=_ZeitTyp):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return jetzt.astimezone(tz) if tz is not None else jetzt.astimezone(zone).replace(tzinfo=None)

    for modul in ("src.sync.orchestrator", "src.sync.slices"):
        monkeypatch.setattr(f"{modul}.datetime", FesteUhr)


#: Feste Uhr um die kritischen Stunden: Zwischen 22 Uhr (Sommerzeit) bzw. 23 Uhr (Winterzeit) und 24 Uhr UTC ist in
#: Berlin schon der nächste Tag. Fensteranfang (vier Tage zurück) am 14./15.10. (Sommerzeit), am 23./24.10.
#: (Tagesscheiben über den Wechsel auf die Winterzeit am 25.10.2026), am 25./26.10. (Tag des Wechsels) und am
#: 27./28.10. (Winterzeit, das Fenster von sieben Tagen liegt noch in der Sommerzeit)
FESTE_UHRZEITEN = [
    datetime(2026, 10, tag, tzinfo=UTC) + timedelta(hours=stunde, minutes=minute)
    for tag in (18, 27, 29, 31)
    for stunde, minute in ((21, 59), (22, 30), (23, 59), (24, 1))
]


# ---------------------------------------------------------------------------
# Einstellung full_sync_slices
# ---------------------------------------------------------------------------


def test_einstellung_wird_gelesen():
    scheiben = SourceFetchOptions.from_sync_config({"full_sync_slices": {"days": 30, "since": "2010-01-01"}})
    assert scheiben.full_sync_slices == FullSyncSlices(since=datetime(2010, 1, 1, tzinfo=UTC), days=30)
    assert SourceFetchOptions.from_sync_config({}).full_sync_slices is None
    # Ohne days gelten 30 Tage; ein Zeitpunkt mit Zone zählt in UTC
    ohne_tage = FullSyncSlices.parse({"since": "2010-01-01T00:00:00+02:00"})
    assert ohne_tage == FullSyncSlices(since=datetime(2009, 12, 31, 22, 0, tzinfo=UTC), days=30)


@pytest.mark.parametrize(
    "wert",
    [
        None,
        "2010-01-01",
        {},
        {"days": 30},
        {"days": 0, "since": "2010-01-01"},
        {"days": 367, "since": "2010-01-01"},
        {"days": True, "since": "2010-01-01"},
        {"days": "30", "since": "2010-01-01"},
        {"days": 30, "since": "kaputt"},
        {"days": 30, "since": 2010},
        {"days": 30, "since": "2999-01-01"},
    ],
)
def test_ungueltige_einstellung_gilt_als_nicht_gesetzt(wert):
    assert FullSyncSlices.parse(wert) is None


def test_scheiben_nur_fuer_genannte_listen():
    nur = FullSyncSlices.parse({"since": "2010-01-01", "lists": ["paper", "meeting", "papers", 3]})
    assert nur is not None and nur.lists == frozenset({"paper", "meeting"})
    assert nur.covers("paper") and not nur.covers("organization")
    # Ohne gültigen Namen gelten alle Listen, nie keine (sonst käme die ungefilterte Liste)
    alle = FullSyncSlices.parse({"since": "2010-01-01", "lists": ["papers"]})
    assert alle is not None and alle.lists is None and alle.covers("organization")
    assert FullSyncSlices.parse({"since": "2010-01-01", "lists": "paper"}).lists is None  # type: ignore[union-attr]
    assert not alle.covers("legislativeterm")


# ---------------------------------------------------------------------------
# Scheiben
# ---------------------------------------------------------------------------


def test_scheiben_von_alt_nach_neu_ohne_luecke():
    config = FullSyncSlices(since=datetime(2010, 1, 1, tzinfo=UTC), days=30)
    jetzt = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    scheiben = full_windows(config, None, jetzt)
    # Zuerst alles bis since, zuletzt alles ab der letzten Grenze
    assert scheiben[0] == SliceWindow(None, config.since)
    assert scheiben[-1].until is None
    for vorher, nachher in zip(scheiben, scheiben[1:], strict=False):
        assert vorher.until == nachher.since  # lückenlos
    for scheibe in scheiben[1:-1]:
        assert scheibe.until is not None and scheibe.since is not None
        assert scheibe.until - scheibe.since == timedelta(days=30)
        assert scheibe.until <= jetzt
    assert jetzt - scheiben[-1].since < timedelta(days=30)  # type: ignore[operator]
    # 2010-01-01 bis 2026-10-16: 6133 Tage, 204 volle Scheiben, dazu die erste und die letzte
    assert len(scheiben) == 206


def test_fortschritt_setzt_an_der_letzten_vollstaendigen_scheibe_an():
    config = FullSyncSlices(since=datetime(2010, 1, 1, tzinfo=UTC), days=30)
    jetzt = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    stand = datetime(2016, 4, 3, tzinfo=UTC)
    scheiben = full_windows(config, stand, jetzt)
    assert scheiben[0] == SliceWindow(stand, stand + timedelta(days=30))
    # Fortschritt vor since (Einstellung geändert) oder in der Zukunft: von vorn
    assert full_windows(config, datetime(2009, 1, 1, tzinfo=UTC), jetzt)[0] == SliceWindow(None, config.since)
    assert full_windows(config, jetzt + timedelta(days=1), jetzt)[0] == SliceWindow(None, config.since)


def test_objekt_ausserhalb_der_scheibe():
    scheibe = SliceWindow(datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
    assert scheibe.outside({"modified": "2020-01-15T10:00:00+01:00"}) is None
    # Toleranz von einem Tag (Zeitzonen, Tagesstempel)
    assert scheibe.outside({"modified": "2020-02-01T23:00:00+00:00"}) is None
    assert scheibe.outside({"modified": "2019-12-31"}) is None
    assert scheibe.outside({"modified": "2020-02-03T00:00:00+00:00"}) == datetime(2020, 2, 3, tzinfo=UTC)
    assert scheibe.outside({"modified": "2019-12-30T00:00:00Z"}) == datetime(2019, 12, 30, tzinfo=UTC)
    assert scheibe.outside({"name": "ohne modified"}) is None
    assert SliceWindow(None, datetime(2020, 1, 1, tzinfo=UTC)).outside({"modified": "1999-01-01"}) is None
    assert timedelta(days=1) == SLICE_TOLERANCE


def test_aussetzung_waechst_bis_24_stunden():
    assert [slow_backoff(n) / timedelta(hours=1) for n in range(1, 9)] == [1, 2, 4, 8, 16, 24, 24, 24]


def test_langsame_liste_in_tagesscheiben():
    jetzt = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    scheiben = slow_windows(datetime(2026, 10, 13, 22, 0, tzinfo=UTC), jetzt)
    assert [s.since for s in scheiben] == [datetime(2026, 10, d, 22, 0, tzinfo=UTC) for d in (13, 14, 15)]
    assert scheiben[-1].until is None
    assert all(s.until - s.since == SLOW_LIST_SLICE for s in scheiben[:-1])  # type: ignore[operator]
    # Tagesbeginn: der frühere von 00:00 Uhr UTC und 00:00 Uhr Ortszeit
    assert day_floor(datetime(2026, 10, 15, 13, 0, tzinfo=UTC)) == datetime(2026, 10, 14, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stempel", "tagesbeginn"),
    [
        # Sommerzeit: Ab 22 Uhr UTC ist in Berlin schon der nächste Tag, dann ist 00:00 Uhr UTC früher
        ("2026-10-18T21:59Z", "2026-10-17T22:00Z"),
        ("2026-10-18T22:30Z", "2026-10-18T00:00Z"),
        ("2026-10-18T23:59Z", "2026-10-18T00:00Z"),
        ("2026-10-19T00:01Z", "2026-10-18T22:00Z"),
        # 25.10.2026: Der Tag beginnt in Berlin noch in der Sommerzeit, der nächste in der Winterzeit (23 Uhr UTC)
        ("2026-10-25T21:59Z", "2026-10-24T22:00Z"),
        ("2026-10-25T22:30Z", "2026-10-24T22:00Z"),
        ("2026-10-25T23:59Z", "2026-10-25T00:00Z"),
        ("2026-10-26T00:01Z", "2026-10-25T23:00Z"),
        # Schon abgerundet heißt nicht fertig: Vor 00:00 Uhr UTC liegt 00:00 Uhr Berlin desselben Tages
        ("2026-10-18T00:00Z", "2026-10-17T22:00Z"),
        ("2026-10-27T00:00Z", "2026-10-26T23:00Z"),
    ],
)
def test_tagesbeginn_um_mitternacht_und_ueber_den_wechsel_auf_die_winterzeit(stempel, tagesbeginn):
    assert day_floor(datetime.fromisoformat(stempel)) == datetime.fromisoformat(tagesbeginn)


def test_eintraege_aus_sync_config():
    config = {
        "full_sync_progress": {f"{BODY}/paper": "2016-04-03T00:00:00+00:00", f"{BODY}/meeting": "kaputt", "": "x"},
        "slow_lists": {
            f"{BODY}/consultation": {"failures": 2, "until": "2026-10-16T10:00:00+00:00", "since": "2026-10-09"},
            f"{BODY}/file": "kaputt",
        },
        "full_sync_filter_ignored": {"checked_at": "2026-10-16T01:00:00+00:00", "detail": "x"},
    }
    assert full_sync_progress_of(config) == {f"{BODY}/paper": datetime(2016, 4, 3, tzinfo=UTC)}
    assert list(slow_lists_of(config)) == [f"{BODY}/consultation"]
    assert slice_filter_ignored_of(config) == {"checked_at": "2026-10-16T01:00:00+00:00", "detail": "x"}
    assert full_sync_progress_of(None) == {} and slow_lists_of({"slow_lists": []}) == {}
    langsam = SlowList.from_entry(slow_lists_of(config)[f"{BODY}/consultation"])
    assert langsam is not None and langsam.failures == 2 and langsam.since == datetime(2026, 10, 9, tzinfo=UTC)
    assert SlowList.from_entry({"failures": 0, "until": "2026-10-16", "since": "2026-10-09"}) is None
    assert SlowList.from_entry({"failures": True, "until": "2026-10-16", "since": "2026-10-09"}) is None


# ---------------------------------------------------------------------------
# Client: Zeitscheiben nie ohne Filter
# ---------------------------------------------------------------------------


async def test_zeitscheibe_ohne_rueckfall_auf_die_ungefilterte_liste():
    for status in (400, 401, 403):
        anfragen: list[httpx.URL] = []

        def antwort(request: httpx.Request, status: int = status) -> httpx.Response:
            anfragen.append(request.url)  # noqa: B023 - je Durchlauf eine eigene Liste
            return httpx.Response(status)

        quelle_seit = datetime(2020, 1, 1, tzinfo=UTC)
        async with OParlClient(max_concurrent=1, wait_time=0) as client:
            assert client._client is not None
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
            with pytest.raises(ListFetchError):
                async for _ in client.fetch_list(f"{BODY}/paper", modified_since=quelle_seit, strict_filter=True):
                    pass
        # Bei 403 folgt nur die Vergleichsanfrage mit neutralem Client, ebenfalls mit Filter
        assert anfragen and all(url.params.get("modified_since") for url in anfragen)
        assert len(anfragen) == (2 if status == 403 else 1)
        assert client.filter_notes.lists == {} and client.filter_notes.hosts == {}


async def test_zeitscheibe_haengt_verlorene_filter_an_folgeseiten():
    """Verliert ``links.next`` den Filter (ALLRIS), bekommt die Folgeseite ihn wieder; nie eine Seite ohne Filter."""
    anfragen: list[httpx.URL] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"data": [{"id": "b"}], "links": {}})
        return httpx.Response(200, json={"data": [{"id": "a"}], "links": {"next": f"{BODY}/paper?page=2"}})

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
        seiten = [
            seite
            async for seite in client.fetch_list(
                f"{BODY}/paper",
                modified_since=datetime(2020, 1, 1, tzinfo=UTC),
                modified_until=datetime(2020, 2, 1, tzinfo=UTC),
                strict_filter=True,
            )
        ]
        assert seiten == [[{"id": "a"}], [{"id": "b"}]]
        # Der verlorene Filter macht weder den Server noch die Liste zu „ohne Filter“
        assert client.filter_notes.lists == {} and client.filter_notes.hosts == {}
    erwartet = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
    assert all(_grenzen(url) == erwartet for url in anfragen)

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        with pytest.raises(ValueError, match="ohne Filter"):
            async for _ in client.fetch_list(f"{BODY}/paper", strict_filter=True):
                pass


async def test_zeitscheibe_meldet_fehlende_liste_ohne_ungefilterte_nachfrage():
    anfragen: list[httpx.URL] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url)
        return httpx.Response(404)

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
        with pytest.raises(ListMissingError):
            async for _ in client.fetch_list(
                f"{BODY}/file", modified_until=BEGINN, strict_filter=True, report_missing=True
            ):
                pass
    assert len(anfragen) == 1 and anfragen[0].params.get("modified_until")


@pytest.mark.parametrize(
    ("antwort", "zeitueberschreitung"),
    [("timeout", True), (504, True), (500, False), ("verbindung", False)],
)
async def test_zeitueberschreitung_wird_erkannt(antwort, zeitueberschreitung):
    def transport(request: httpx.Request) -> httpx.Response:
        if antwort == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if antwort == "verbindung":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(antwort)

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with pytest.raises(ListFetchError) as fehler:
            async for _ in client.fetch_list(f"{BODY}/consultation", modified_since=BEGINN):
                pass
    assert fehler.value.timed_out is zeitueberschreitung


# ---------------------------------------------------------------------------
# Vollabgleich in Zeitscheiben (Hamm)
# ---------------------------------------------------------------------------


async def test_vollabgleich_in_zeitscheiben_ruft_keine_ungefilterte_liste_ab():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    stats = await _lauf(quelle, speicher)

    assert stats["incomplete"] == [] and stats["errors"] == []
    # Keine einzige Anfrage ohne Filter, also kein HTTP 504
    assert quelle.anfragen and all(any(url.params.get(key) for key in FILTER) for url in quelle.anfragen)
    # Alles übernommen, auch das Objekt vor since (erste Scheibe) und das von vor einer Stunde (letzte)
    for name in ("organization", "person", "meeting", "paper"):
        assert {f"{BASE}/{name}/{i}" for i in range(5)} <= set(speicher.modified)
    assert stats["papers"] == 5 and stats["meetings"] == 5
    # Durchgang fertig: Fortschritt gelöscht, der nächste Vollabgleich beginnt wieder bei since
    assert SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY not in speicher.config
    zusammenfassung = stats["full_sync_slices"]
    eintrag = zusammenfassung["lists"][f"{BODY}/paper"]
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    assert eintrag == {"type": "paper", "slices_read": len(scheiben), "finished": True, "next_slice_from": None}
    assert zusammenfassung["without_modified"] == {}
    assert "ohne modified" in zusammenfassung["note"]
    assert sync_complete([stats], [], _OhneBefund()) is True


async def test_scheiben_kommen_von_alt_nach_neu():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    await _lauf(quelle, speicher, kommune=_kommune("paper"))
    grenzen = [_grenzen(url) for url in quelle.anfragen_an("paper") if url.params.get("page") in (None, "1")]
    assert grenzen[0] == (None, BEGINN)
    assert grenzen[-1][1] is None
    anfaenge = [seit for seit, _bis in grenzen[1:]]
    assert anfaenge == sorted(anfaenge) and anfaenge[0] == BEGINN


async def test_abgebrochener_vollabgleich_setzt_an_der_letzten_vollstaendigen_scheibe_fort():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    bruch = scheiben[3].since
    assert bruch is not None
    quelle.fehler_ab["paper"] = (bruch, 500)

    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == ["paper"]
    # Fortschritt: Vorlagen bis zur Scheibe vor dem Fehler, alle anderen Listen an ihrer letzten Grenze
    fortschritt = full_sync_progress_of(speicher.config)
    assert fortschritt[f"{BODY}/paper"] == bruch
    assert fortschritt[f"{BODY}/meeting"] == scheiben[-1].since
    assert stats["full_sync_slices"]["lists"][f"{BODY}/paper"]["finished"] is False

    # Nächster Vollabgleich (nächste Nacht): beginnt bei der Scheibe ab dem Bruch, nicht wieder bei since
    del quelle.fehler_ab["paper"]
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    vorlagen = [_grenzen(url) for url in quelle.anfragen_an("paper") if url.params.get("page") in (None, "1")]
    assert vorlagen[0][0] == bruch
    assert all(seit is not None and seit >= bruch for seit, _bis in vorlagen)
    # Fertige Listen fragen nur ihre letzte Scheibe noch einmal an
    sitzungen = [_grenzen(url) for url in quelle.anfragen_an("meeting") if url.params.get("page") in (None, "1")]
    assert sitzungen == [(scheiben[-1].since, None)]
    assert SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY not in speicher.config


async def test_zeitfenster_endet_mitten_im_vollabgleich():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    speicher.config[SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY] = {f"{BODY}/paper": "2023-01-01T00:00:00+00:00"}
    stats = await _lauf(quelle, speicher, kommune=_kommune("paper"), frist=datetime.now(UTC) - timedelta(seconds=1))
    assert stats["incomplete"] == ["paper"]
    assert quelle.anfragen == []
    assert full_sync_progress_of(speicher.config) == {f"{BODY}/paper": datetime(2023, 1, 1, tzinfo=UTC)}


async def test_quelle_ignoriert_modified_until():
    """Statt jede Scheibe die ganze Liste zu liefern: erkannt an der ersten Seite, Abbruch mit klarer Meldung."""
    quelle = HammQuelle()
    quelle.ignoriert_until = True
    speicher = ScheibenSpeicher()
    stats = await _lauf(quelle, speicher)

    assert set(stats["incomplete"]) == {"organization", "person", "meeting", "paper"}
    meldungen = " ".join(stats["errors"])
    assert "Vollabgleich in Zeitscheiben abgebrochen" in meldungen and "ignoriert modified_until" in meldungen
    # Keine zweite Scheibe: Jede Liste fragt höchstens die Seiten ihrer ersten Scheibe an, die meisten gar nichts
    assert all(not url.params.get("modified_since") for url in quelle.anfragen)
    assert len(quelle.anfragen) <= 4
    befund = slice_filter_ignored_of(speicher.config)
    assert befund is not None and "modified_until" in befund["detail"]
    assert SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY not in speicher.config
    # Nichts aus der falschen Antwort übernommen
    assert speicher.modified == {}


async def test_befund_setzt_den_vollabgleich_in_zeitscheiben_aus():
    options = SourceFetchOptions.from_sync_config({"full_sync_slices": SCHEIBEN})
    speicher = ScheibenSpeicher()
    orchestrator = _orchestrator(speicher)
    befund = {"checked_at": datetime.now(UTC).isoformat(), "list": f"{BODY}/paper", "detail": "Quelle ignoriert x"}
    full, run, meldung = await orchestrator._slice_run(SOURCE, options, True, {"full_sync_filter_ignored": befund})
    assert full is False and run.config is None
    assert meldung is not None and "ausgesetzt" in meldung and "Quelle ignoriert x" in meldung

    # Nach 30 Tagen versucht er es wieder und löscht den alten Befund
    speicher.config[SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY] = befund
    alt = {**befund, "checked_at": (datetime.now(UTC) - timedelta(days=31)).isoformat()}
    full, run, meldung = await orchestrator._slice_run(SOURCE, options, True, {"full_sync_filter_ignored": alt})
    assert full is True and run.config == options.full_sync_slices and meldung is None
    assert SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY not in speicher.config

    # Ohne Zeitscheiben oder inkrementell bleibt alles, wie es ist
    ohne = SourceFetchOptions()
    assert (await orchestrator._slice_run(SOURCE, ohne, True, {"full_sync_filter_ignored": befund}))[0] is True
    full, run, meldung = await orchestrator._slice_run(SOURCE, options, False, None)
    assert full is False and run.config is None and meldung is None


async def test_objekte_ohne_modified_werden_sichtbar():
    quelle = HammQuelle()
    quelle.listen["paper"].append({"id": f"{BASE}/paper/ohne", "type": "https://schema.oparl.org/1.1/Paper"})
    speicher = ScheibenSpeicher()

    # Die Quelle liefert sie in keiner Scheibe: nicht erfasst, der Hinweis steht im Protokoll
    stats = await _lauf(quelle, speicher, kommune=_kommune("paper"))
    assert f"{BASE}/paper/ohne" not in speicher.modified
    assert stats["full_sync_slices"]["without_modified"] == {}
    assert "ohne modified" in stats["full_sync_slices"]["note"]

    # Die Quelle liefert sie in jeder Scheibe mit: einmal gezählt
    quelle.ohne_modified_immer = True
    stats = await _lauf(quelle, speicher, kommune=_kommune("paper"))
    assert stats["full_sync_slices"]["without_modified"] == {"paper": 1}
    assert f"{BASE}/paper/ohne" in speicher.modified


async def test_nur_genannte_listen_in_zeitscheiben():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    stats = await _lauf(quelle, speicher, scheiben={**SCHEIBEN, "lists": ["paper", "meeting"]})
    assert stats["incomplete"] == []
    # Gremien und Personen wie bisher ohne Filter (bei Hamm schnell), Vorlagen und Sitzungen nur in Scheiben
    assert quelle.anfragen_an("organization")
    assert all(not any(url.params.get(key) for key in FILTER) for url in quelle.anfragen_an("organization"))
    assert all(any(url.params.get(key) for key in FILTER) for url in quelle.anfragen_an("paper"))
    assert set(stats["full_sync_slices"]["lists"]) == {f"{BODY}/paper", f"{BODY}/meeting"}


async def test_vermerkte_fehlende_liste_bekommt_keine_zeitscheiben():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    speicher.fehlend[f"{BODY}/consultation"] = datetime.now(UTC) - timedelta(days=3)
    stats = await _lauf(quelle, speicher, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == []
    assert quelle.anfragen_an("consultation") == []


def test_befund_mehrerer_kommunen_wird_zusammengefasst():
    ergebnis = SyncResult(source_url=SOURCE, source_name="Hamm", success=True)
    eins = {
        "full_sync_slices": {"lists": {"a": {}}, "without_modified": {"paper": 2}, "note": "n"},
        "suspended_lists": {"x": "2026-10-16T10:00:00+00:00"},
    }
    zwei = {"full_sync_slices": {"lists": {"b": {}}, "without_modified": {"paper": 1, "meeting": 1}, "note": "n"}}
    merge_slice_details(ergebnis.slice_details, eins)
    merge_slice_details(ergebnis.slice_details, zwei)
    merge_slice_details(ergebnis.slice_details, {"errors": []})
    assert ergebnis.slice_details == {
        "full_sync_slices": {"lists": {"a": {}, "b": {}}, "without_modified": {"paper": 3, "meeting": 1}, "note": "n"},
        "suspended_lists": {"x": "2026-10-16T10:00:00+00:00"},
    }


# ---------------------------------------------------------------------------
# Langsame optionale Listen
# ---------------------------------------------------------------------------


async def test_langsame_liste_wird_mit_wachsendem_abstand_ausgesetzt():
    """Hamm: consultation läuft mit Filter in Zeitüberschreitungen. Statt jeden Lauf: ausgesetzt, der Lauf bleibt
    vollständig, und das Fenster der Kommune rückt vor."""
    quelle = HammQuelle()
    quelle.langsam.add("consultation")
    speicher = ScheibenSpeicher()
    kommune = _kommune("organization", "person", "meeting", "paper", "consultation")

    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert stats["incomplete"] == []
    assert sync_complete([stats], [], _OhneBefund()) is True
    assert speicher.begonnen[BODY] >= vorher  # Fenster der Kommune rückt vor
    eintrag = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"])
    assert eintrag is not None and eintrag.failures == 1 and eintrag.reason == "Zeitüberschreitung"
    assert timedelta(minutes=59) < eintrag.until - vorher <= timedelta(hours=1, minutes=1)
    assert f"{BODY}/consultation" in stats["suspended_lists"]
    assert any("ausgesetzt bis" in fehler for fehler in stats["errors"])
    # Fensteranfang der Liste: das Fenster dieses Laufs (sieben Tage, ohne Zeitpunkt der Kommune)
    assert eintrag.since <= vorher - timedelta(days=7)

    # Nächster Lauf in der Aussetzung: keine Anfrage an die Liste
    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert quelle.anfragen_an("consultation") == []
    assert stats["incomplete"] == [] and f"{BODY}/consultation" in stats["suspended_lists"]
    assert not any("ausgesetzt bis" in fehler for fehler in stats["errors"])

    # Nach der Aussetzung: wieder eine Zeitüberschreitung, jetzt zwei Stunden
    abgelaufen = SlowList(failures=1, until=datetime.now(UTC) - timedelta(minutes=1), since=eintrag.since)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"] = abgelaufen.to_entry()
    jetzt = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert len(quelle.anfragen_an("consultation")) == 1  # erste Tagesscheibe, ein Versuch (Tests: ein Versuch)
    zweiter = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"])
    assert zweiter is not None and zweiter.failures == 2
    assert timedelta(hours=1, minutes=59) < zweiter.until - jetzt <= timedelta(hours=2, minutes=1)
    assert zweiter.since == day_floor(eintrag.since)
    assert stats["incomplete"] == []


async def test_langsame_liste_wird_in_tagesscheiben_nachgeholt():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    # Fenster der Kommune: kurz (letzter vollständiger Abgleich vor einer Stunde); die Liste braucht mehr
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    seit = datetime.now(UTC) - timedelta(days=3)
    faellig = SlowList(failures=3, until=datetime.now(UTC) - timedelta(minutes=1), since=seit)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY] = {f"{BODY}/consultation": faellig.to_entry()}
    kommune = _kommune("paper", "consultation")

    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert stats["incomplete"] == [] and "suspended_lists" not in stats
    grenzen = [_grenzen(url) for url in quelle.anfragen_an("consultation") if url.params.get("page") in (None, "1")]
    # Ab dem Fensteranfang der Liste, auf den Tagesbeginn abgerundet, in Scheiben zu einem Tag
    assert grenzen[0][0] == day_floor(seit)
    assert all(bis - seit_ == SLOW_LIST_SLICE for seit_, bis in grenzen[:-1])  # type: ignore[operator]
    assert grenzen[-1][1] is None and len(grenzen) >= 4
    # Ganz gelesen: Aussetzung beendet, Änderungen von gestern und vor einer Stunde übernommen
    assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config
    assert {f"{BASE}/consultation/3", f"{BASE}/consultation/4"} <= set(speicher.modified)


@pytest.mark.parametrize("ortszeit", ["UTC", "Europe/Berlin"])
@pytest.mark.parametrize("jetzt", FESTE_UHRZEITEN, ids=lambda t: f"{t:%d.%m.%H:%M}")
async def test_abbruch_einer_langsamen_liste_behaelt_ihren_fortschritt(monkeypatch, jetzt, ortszeit):
    """
    Die dritte Tagesscheibe läuft in die Zeitüberschreitung: Der Fensteranfang der Liste rückt auf sie vor.

    Mit fester Uhr (Issue #1066): Die erste Scheibe beginnt am Tagesbeginn des Fensteranfangs (``day_floor``). Liegt
    er zwischen 22 Uhr (Winterzeit 23 Uhr) und 24 Uhr UTC, ist das 00:00 Uhr UTC, sonst 00:00 Uhr Berlin. Der Test
    rechnet die Grenzen wie der Lauf; bisher nahm er an, die Scheiben lägen immer auf 00:00 Uhr Berlin.
    """
    _feste_uhr(monkeypatch, jetzt, ortszeit)
    quelle = HammQuelle(jetzt)
    speicher = ScheibenSpeicher()
    # Fenster der Kommune kurz (letzter vollständiger Abgleich vor einer Stunde): Die Liste beginnt an ihrem eigenen
    # Fensteranfang vor vier Tagen, auf den Tagesbeginn abgerundet
    speicher.begonnen[BODY] = jetzt - timedelta(hours=1)
    seit = jetzt - timedelta(days=4)
    beginn = day_floor(seit)
    faellig = SlowList(failures=4, until=jetzt - timedelta(minutes=1), since=seit)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY] = {f"{BODY}/consultation": faellig.to_entry()}
    # Die ersten beiden Tagesscheiben antworten, die dritte läuft in die Zeitüberschreitung
    quelle.langsam.add("consultation")
    quelle.langsam_ab = beginn + 2 * SLOW_LIST_SLICE

    stats = await _lauf(quelle, speicher, full=False, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == []
    grenzen = [_grenzen(url) for url in quelle.anfragen_an("consultation") if url.params.get("page") in (None, "1")]
    assert grenzen == [(beginn + n * SLOW_LIST_SLICE, beginn + (n + 1) * SLOW_LIST_SLICE) for n in range(3)]
    eintrag = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"])
    assert eintrag is not None
    assert eintrag.since == beginn + 2 * SLOW_LIST_SLICE
    # Die Liste kam voran: Die Reihe beginnt wieder bei einer Stunde statt bei 16
    assert eintrag.failures == 1
    assert eintrag.until == jetzt + timedelta(hours=1)


async def test_pflichtliste_mit_zeitueberschreitung_bleibt_eine_luecke():
    quelle = HammQuelle()
    quelle.langsam.add("paper")
    speicher = ScheibenSpeicher()
    stats = await _lauf(quelle, speicher, full=False)
    assert stats["incomplete"] == ["paper"]
    assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config
    assert BODY not in speicher.begonnen


async def test_ausgesetzte_liste_haelt_den_durchgang_offen():
    """Vollabgleich in Zeitscheiben: Eine ausgesetzte optionale Liste ist keine Lücke des Laufs, der Durchgang ist
    aber nicht fertig. Alle Listen behalten ihren Fortschritt, der Vollabgleich bleibt fällig."""
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    ausgesetzt = SlowList(failures=2, until=datetime.now(UTC) + timedelta(hours=1), since=datetime.now(UTC))
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY] = {f"{BODY}/consultation": ausgesetzt.to_entry()}
    speicher.config[SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY] = {f"{BODY}/consultation": "2022-01-01T00:00:00+00:00"}

    stats = await _lauf(quelle, speicher, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == []
    assert sync_complete([stats], [], _OhneBefund()) is True
    assert quelle.anfragen_an("consultation") == []
    assert stats["full_pending"] == [f"{BODY}/consultation"]
    assert full_sync_finished([stats]) is False
    # Vorlagen sind fertig, behalten aber ihre letzte Grenze: Der nächste Vollabgleich fragt nur die letzte Scheibe an
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    assert full_sync_progress_of(speicher.config) == {
        f"{BODY}/consultation": datetime(2022, 1, 1, tzinfo=UTC),
        f"{BODY}/paper": scheiben[-1].since,
    }
    assert stats["full_sync_slices"]["lists"][f"{BODY}/paper"]["finished"] is True


async def test_zeitueberschreitung_im_vollabgleich_setzt_die_optionale_liste_aus():
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    quelle.langsam.add("consultation")
    quelle.langsam_ab = scheiben[2].since

    stats = await _lauf(quelle, speicher, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == []
    assert full_sync_progress_of(speicher.config) == {
        f"{BODY}/consultation": scheiben[2].since,
        f"{BODY}/paper": scheiben[-1].since,
    }
    eintrag = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"])
    assert eintrag is not None and eintrag.failures == 1
    assert stats["full_pending"] == [f"{BODY}/consultation"] and full_sync_finished([stats]) is False

    # Nach der Aussetzung: Der nächste Vollabgleich setzt die Beratungen fort, der Durchgang wird fertig
    quelle.langsam.clear()
    abgelaufen = SlowList(failures=1, until=datetime.now(UTC) - timedelta(minutes=1), since=eintrag.since)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][f"{BODY}/consultation"] = abgelaufen.to_entry()
    stats = await _lauf(quelle, speicher, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == [] and "full_pending" not in stats and full_sync_finished([stats]) is True
    beratungen = [_grenzen(url) for url in quelle.anfragen_an("consultation") if url.params.get("page") in (None, "1")]
    assert beratungen[0][0] == scheiben[2].since
    assert SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY not in speicher.config
    assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config


async def test_ausgesetzte_liste_ohne_zeitscheiben_haelt_den_vollabgleich_faellig():
    """Auch ohne ``full_sync_slices``: Eine im Vollabgleich ausgesetzte Liste ist keine Lücke, der Vollabgleich zählt
    aber nicht als vollständig und wird im nächsten Fenster wiederholt."""
    quelle = HammQuelle()
    quelle.ungefiltert_504.clear()
    quelle.langsam.add("consultation")
    speicher = ScheibenSpeicher()
    stats = await _lauf(
        quelle, speicher, kommune=_kommune("organization", "person", "consultation"), ohne_scheiben=True
    )
    assert stats["incomplete"] == [] and sync_complete([stats], [], _OhneBefund()) is True
    assert all(not any(url.params.get(key) for key in FILTER) for url in quelle.anfragen)
    assert stats["full_pending"] == [f"{BODY}/consultation"] and full_sync_finished([stats]) is False
    assert "full_sync_slices" not in stats


@pytest.mark.parametrize(
    ("complete", "full_complete", "erwartet"),
    [(True, None, True), (True, True, True), (True, False, False), (False, True, False)],
)
async def test_vollabgleich_mit_ausgesetzter_liste_zaehlt_nicht_als_vollstaendig(complete, full_complete, erwartet):
    quelle = SimpleNamespace(
        last_sync=None,
        last_full_sync=None,
        last_successful_sync=None,
        last_successful_full_sync=None,
        last_error=None,
        last_error_at=None,
        last_error_kind=None,
        consecutive_failures=0,
    )
    await _speicher_mit(quelle).update_source_sync_time(
        UUID(int=1), full_sync=True, complete=complete, full_complete=full_complete
    )
    assert (quelle.last_successful_sync is not None) is complete
    assert (quelle.last_successful_full_sync is not None) is erwartet
    assert quelle.last_full_sync is not None


async def test_ungespeicherte_aussetzung_bleibt_eine_luecke():
    """Lässt sich die Aussetzung nicht speichern, gäbe es keinen eigenen Fensteranfang: Die Liste bleibt eine Lücke,
    das Fenster der Kommune rückt nicht vor."""
    quelle = HammQuelle()
    quelle.langsam.add("consultation")
    speicher = ScheibenSpeicher()
    speicher.aussetzung_scheitert = True
    stats = await _lauf(quelle, speicher, full=False, kommune=_kommune("paper", "consultation"))
    assert stats["incomplete"] == ["consultation"]
    assert "suspended_lists" not in stats
    assert not any("ausgesetzt bis" in fehler for fehler in stats["errors"])
    assert BODY not in speicher.begonnen
    assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config


async def test_fehlerobjekt_in_einer_scheibe_ist_keine_leere_scheibe():
    """Liefert die Quelle unter Last ein Fehlerobjekt statt der Scheibe, rückt der Fortschritt nicht darüber hinweg."""
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    bruch = scheiben[2].since
    assert bruch is not None
    quelle.fehlerobjekt_ab["paper"] = bruch

    stats = await _lauf(quelle, speicher, kommune=_kommune("paper"))
    assert stats["incomplete"] == ["paper"]
    assert any("Fehlerobjekt statt Liste" in fehler for fehler in stats["errors"])
    assert full_sync_progress_of(speicher.config) == {f"{BODY}/paper": bruch}
    assert stats["full_sync_slices"]["lists"][f"{BODY}/paper"]["finished"] is False


@pytest.mark.parametrize("liste", ["consultation", "paper"])
@pytest.mark.parametrize("antwort", ["fehlerobjekt", "404"])
async def test_fortgesetzte_liste_mit_fehler_auf_der_ersten_scheibe_des_laufs_bleibt_eine_luecke(liste, antwort):
    """Eine Liste mit gespeichertem Fortschritt haben frühere Scheiben nachgewiesen: Antwortet ihre erste Scheibe in
    diesem Lauf mit einem Fehlerobjekt oder HTTP 404 (ohne Code 802), fehlt sie nicht und ist auch nicht leer. Der
    Lauf hat eine Lücke, der Durchgang ist nicht fertig, und jeder Fortschritt bleibt."""
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    scheiben = full_windows(FullSyncSlices.parse(SCHEIBEN), None, datetime.now(UTC))  # type: ignore[arg-type]
    fortschritt = scheiben[2].since
    assert fortschritt is not None
    speicher.config[SYNC_CONFIG_FULL_SYNC_PROGRESS_KEY] = {f"{BODY}/{liste}": fortschritt.isoformat(timespec="seconds")}
    if antwort == "404":
        quelle.fehler_ab[liste] = (fortschritt, 404)
    else:
        quelle.fehlerobjekt_ab[liste] = fortschritt
    andere = "paper" if liste == "consultation" else "meeting"

    stats = await _lauf(quelle, speicher, kommune=_kommune(andere, liste))
    assert stats["incomplete"] == [liste]
    assert sync_complete([stats], [], _OhneBefund()) is False
    # Weder als fehlend vermerkt noch als leere Pflichtliste gewertet
    assert f"{BODY}/{liste}" not in stats.get("missing_lists", [])
    assert f"{BODY}/{liste}" not in speicher.fehlend
    assert "empty_required_lists" not in stats
    # Nur eine Anfrage an die Liste: ihre Scheibe ab dem Fortschritt, keine weitere
    anfragen = [_grenzen(url) for url in quelle.anfragen_an(liste)]
    assert [seit for seit, _bis in anfragen] == [fortschritt]
    # Fortschritt der Liste bleibt, die fertige andere Liste behält ihre letzte Grenze
    assert full_sync_progress_of(speicher.config) == {
        f"{BODY}/{liste}": fortschritt,
        f"{BODY}/{andere}": scheiben[-1].since,
    }
    assert stats["full_sync_slices"]["lists"][f"{BODY}/{liste}"]["finished"] is False


async def test_client_fehlerobjekt_auf_der_ersten_seite_einer_scheibe():
    def antwort(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FEHLEROBJEKT)

    async with OParlClient(max_concurrent=1, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
        with pytest.raises(ListFetchError, match="Fehlerobjekt statt Liste"):
            async for _ in client.fetch_list(
                f"{BODY}/paper", modified_since=BEGINN, modified_until=BEGINN + timedelta(days=30), strict_filter=True
            ):
                pass
        # Ohne Zeitscheibe bleibt es, wie es war: leer
        seiten = [seite async for seite in client.fetch_list(f"{BODY}/paper", modified_since=BEGINN)]
        assert seiten == []


# ---------------------------------------------------------------------------
# Nachholen langsamer Listen bei Quellen, die nicht in Scheiben filtern
# ---------------------------------------------------------------------------


def _faellig(speicher: ScheibenSpeicher, tage: int) -> datetime:
    """Langsame Beratungen mit abgelaufener Aussetzung, Fensteranfang vor ``tage`` Tagen; Fenster der Kommune kurz."""
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    seit = (datetime.now(UTC) - timedelta(days=tage)).replace(microsecond=0)
    faellig = SlowList(failures=2, until=datetime.now(UTC) - timedelta(minutes=1), since=seit)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY] = {f"{BODY}/consultation": faellig.to_entry()}
    return seit


async def test_nachholen_bei_quelle_die_den_filter_ablehnt():
    """Wie die Stadt Münster (HTTP 401 auf den Filter): keine Lücke, kein Befund, die Liste kommt ohne Scheiben."""
    quelle = HammQuelle()
    quelle.ungefiltert_504.clear()
    quelle.lehnt_filter_ab.add("consultation")
    speicher = ScheibenSpeicher()
    notizen = speicher.filtervermerke
    kommune = _kommune("organization", "person", "consultation")

    _faellig(speicher, 3)
    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert stats["incomplete"] == [] and "suspended_lists" not in stats
    assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config
    assert SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY not in speicher.config
    assert {f"{BASE}/consultation/3", f"{BASE}/consultation/4"} <= set(speicher.modified)
    beratungen = quelle.anfragen_an("consultation")
    # Eine Scheibe angefragt (401), dann wie jede Liste: mit Filter (401), ohne Filter gelesen
    assert sum(1 for url in beratungen if url.params.get("modified_until")) == 1
    assert any(not any(url.params.get(key) for key in FILTER) for url in beratungen)
    # Vermerkt ist nur diese Liste, nicht der Server (Issue #1041)
    assert set(notizen.lists) == {f"{BODY}/consultation"} and notizen.hosts == {}

    # Zweiter Lauf mit fälliger Liste: Die Liste gilt jetzt als „filtert nicht“, keine Scheibe mehr; die übrigen
    # Listen der Quelle fragt der Lauf weiter mit Filter an
    _faellig(speicher, 3)
    stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
    assert stats["incomplete"] == [] and SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config
    assert quelle.anfragen_an("consultation")
    assert all(not any(url.params.get(key) for key in FILTER) for url in quelle.anfragen_an("consultation"))
    assert all(url.params.get("modified_since") for url in quelle.anfragen_an("organization"))
    assert all(url.params.get("modified_since") for url in quelle.anfragen_an("person"))
    assert SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY not in speicher.config


async def test_nachholen_bei_quelle_ohne_modified_until():
    """Die Quelle beachtet ``modified_until`` nicht, die Liste muss mehr als zwei Tage nachholen: kein Abbruch, kein
    Befund gegen den Vollabgleich in Zeitscheiben, die Liste kommt ab ihrem Fensteranfang ohne Scheiben."""
    quelle = HammQuelle()
    quelle.ignoriert_until = True
    speicher = ScheibenSpeicher()
    kommune = _kommune("paper", "consultation")

    for _ in range(2):
        seit = _faellig(speicher, 4)
        # Ohne nachgewiesene Uhrzeit ab Tagesbeginn; der erste Lauf weist sie nach
        beginn = seit if f"{BODY}/consultation" in speicher.uhrzeit else day_floor(seit)
        stats = await _lauf(quelle, speicher, full=False, kommune=kommune)
        assert stats["incomplete"] == [] and "suspended_lists" not in stats
        assert not any("Zeitscheiben abgebrochen" in fehler for fehler in stats["errors"])
        assert SYNC_CONFIG_SLICE_FILTER_IGNORED_KEY not in speicher.config
        assert SYNC_CONFIG_SLOW_LISTS_KEY not in speicher.config
        assert {f"{BASE}/consultation/3", f"{BASE}/consultation/4"} <= set(speicher.modified)
        beratungen = quelle.anfragen_an("consultation")
        # Erste Tagesscheibe, dann ab ihrem Beginn nur mit modified_since
        assert _grenzen(beratungen[0]) == (beginn, beginn + SLOW_LIST_SLICE)
        assert all(_grenzen(url) == (beginn, None) for url in beratungen[1:])

    # Der Vollabgleich in Zeitscheiben ist nicht gesperrt
    options = SourceFetchOptions.from_sync_config({"full_sync_slices": SCHEIBEN})
    full, run, meldung = await _orchestrator(speicher)._slice_run(SOURCE, options, True, speicher.config)
    assert full is True and run.config is not None and meldung is None


# ---------------------------------------------------------------------------
# Zeitfenster des Vollabgleichs in Zeitscheiben
# ---------------------------------------------------------------------------


def _berlin(stunde: int) -> datetime:
    return datetime(2026, 10, 13, stunde, 0, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC)


def test_vollabgleich_in_zeitscheiben_endet_mit_dem_standardfenster():
    standard = default_full_sync_window(1)
    assert standard is not None
    scheiben = SourceFetchOptions.from_sync_config({"full_sync_slices": SCHEIBEN})

    # Ohne eigenes Fenster: das Standardfenster des Daemons, mit seinem Ende
    assert scheiben.sync_window(standard) == standard
    assert scheiben.full_sync_plan(True, now=_berlin(2), default_window=standard) == (True, _berlin(6))
    # Außerhalb des Fensters (Auslöser am Tag) nur Änderungsabgleich
    assert scheiben.full_sync_plan(True, now=_berlin(10), default_window=standard) == (False, None)
    # Ohne Fenster (Kommandozeile, geplanter Vollabgleich aus): kein Vollabgleich in Zeitscheiben ohne Ende
    assert scheiben.full_sync_plan(True, now=_berlin(2)) == (False, None)
    planned, frist = SyncOrchestrator._full_sync_plan(SOURCE, scheiben, True)
    assert planned is False and frist is None

    # Eigenes Fenster geht vor
    eigenes = SourceFetchOptions.from_sync_config(
        {"full_sync_slices": SCHEIBEN, "full_sync_window": {"start": "00:00", "end": "04:00"}}
    )
    assert eigenes.sync_window(standard) == FullSyncWindow.parse({"start": "00:00", "end": "04:00"})
    assert eigenes.full_sync_plan(True, now=_berlin(2), default_window=standard) == (True, _berlin(4))
    assert eigenes.full_sync_plan(True, now=_berlin(2)) == (True, _berlin(4))

    # Ohne Zeitscheiben bleibt es, wie es war: ohne eigenes Fenster ohne Ende
    ohne = SourceFetchOptions()
    assert ohne.sync_window(standard) is None
    assert ohne.full_sync_plan(True, now=_berlin(2), default_window=standard) == (True, None)
    assert ohne.full_sync_plan(True, now=_berlin(10)) == (True, None)
