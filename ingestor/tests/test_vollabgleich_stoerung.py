# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vollabgleich bei Störungen der Quelle (Issue #1523).

Nachgebildet sind die Befunde vom 20.11.2026:

- Stadt Bochum (SD.NET RIM): Eine einzelne Seite der Sitzungsliste antwortet jede Nacht nicht innerhalb von
  60 Sekunden. Nach fünf Fehlversuchen öffnet der Schutzschalter des Hosts, und die parallel laufenden Listen brachen
  sofort mit „Circuit breaker open“ ab. Der nächste Vollabgleich begann sie wieder bei Seite 1 (Vorlagen: Seite 52
  von 2.600). Jetzt wartet ein Abruf einmal bis „halb offen“, und eine gestörte Liste setzt beim nächsten Mal an der
  zuletzt gelesenen Seite fort.
- Stadt Hamm (Zeitscheiben): Die Scheibe mit dem Massenstempel der Quelle passt in kein Fenster; der Vollabgleich
  begann sie jede Nacht bei Seite 1. Jetzt setzt er innerhalb der Scheibe fort. Die Beratungsliste scheitert seit
  Wochen an derselben Tagesscheibe; nach ``SLOW_LIST_STUCK_AFTER`` Versuchen gilt sie als festgefahren und wird
  gemeldet.
- Sync-Protokoll: Ein Vollabgleich mit Lücke steht als ``partial``, nicht als ``success``.

Antworten aus ``httpx.MockTransport``, keine Abrufe, keine Datenbank.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from src.client.oparl_client import FetchResult, FetchWindowClosedError, ListFetchError, OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.metrics import metrics
from src.storage.database import (
    SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY,
    SYNC_CONFIG_SLOW_LISTS_KEY,
    _put_entry,
    full_sync_progress_of,
    full_sync_slice_pages_of,
    slow_lists_of,
)
from src.sync.fortsetzung import ResumeRun
from src.sync.orchestrator import (
    SYNC_STATUS_FAILED,
    SYNC_STATUS_PARTIAL,
    SYNC_STATUS_SUCCESS,
    MissingLists,
    SyncResult,
    TimedLists,
    sync_log_status,
)
from src.sync.slices import (
    SLICE_PAGE_MAX_AGE,
    SLOW_LIST_STUCK_AFTER,
    SLOW_LIST_STUCK_RETRY,
    BodySlices,
    SlicePage,
    SliceRun,
    SliceWindow,
    SlowList,
    slow_backoff,
)
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    BASE,
    HOST,
    Quelle,
    _ohne_nebenlaeufe,
    _orchestrator,
)
from tests.test_fortsetzung_vollabgleich import (
    MEETING,
    ORGANIZATION,
    PAPER,
    PERSON,
    FortsetzungsSpeicher,
    _anfragen,
    _koeln,
    _nacht,
    _seitennummer,
)
from tests.test_zeitscheiben import BODY as HAMM_BODY
from tests.test_zeitscheiben import SCHEIBEN, SOURCE, HammQuelle, ScheibenSpeicher, _kommune
from tests.test_zeitscheiben import _client as _hamm_client
from tests.test_zeitscheiben import _lauf as _hamm_lauf


class _HaengendeSeite(Quelle):
    """Die Vorlagenliste antwortet auf die Seite ``seite`` nicht rechtzeitig (``ReadTimeout``), solange ``haengt``."""

    def __init__(self, geaendert: datetime, seite: int = 4) -> None:
        super().__init__(geaendert)
        self.seite = seite
        self.haengt = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (
            self.haengt
            and request.url.path.endswith("/paper")
            and request.url.params.get("page") == str(self.seite)
            and "modified_since" not in request.url.params
        ):
            self.anfragen.append(request.url)
            raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def _haengend(alt: datetime) -> _HaengendeSeite:
    quelle = _HaengendeSeite(alt)
    quelle.listen.update(_koeln(alt).listen)
    return quelle


@asynccontextmanager
async def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0, **kwargs) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client


# ---------------------------------------------------------------------------
# K5a: Listenfehler mit der zuletzt gelesenen Seite, Fortsetzung nach Störung
# ---------------------------------------------------------------------------


async def test_zeitueberschreitung_nennt_die_zuletzt_gelesene_seite() -> None:
    quelle = _haengend(datetime.now(UTC) - timedelta(days=30))
    async with _client(quelle) as client:
        with pytest.raises(ListFetchError) as fehler:
            async for _ in client.fetch_list(PAPER):
                pass
    assert fehler.value.interrupted and fehler.value.timed_out
    assert fehler.value.last_page_url is not None and _seitennummer(fehler.value.last_page_url) == 3
    assert fehler.value.last_page_ids == (f"{BASE}/paper/4", f"{BASE}/paper/5")


async def test_fehlende_folgeseite_ist_keine_stoerung() -> None:
    """HTTP 404 auf einer Folgeseite sagt etwas über die Liste, nicht über die Quelle: kein Abbruch zum Fortsetzen."""
    quelle = _koeln(datetime.now(UTC) - timedelta(days=30))
    quelle.listen["/oparl/body/1/paper"] = quelle.listen["/oparl/body/1/paper"][:4]
    weiter = quelle.__call__

    def ohne_seite_zwei(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(404)
        return weiter(request)

    async with _client(ohne_seite_zwei) as client:
        with pytest.raises(ListFetchError) as fehler:
            async for _ in client.fetch_list(PAPER):
                pass
    assert not fehler.value.interrupted


async def test_gestoerte_liste_setzt_in_der_naechsten_nacht_fort() -> None:
    """
    Bochum: Seite 4 der Vorlagen hängt. Früher fiel der Stand der Liste weg (``note_failed``), und der nächste
    Vollabgleich las sie wieder ab Seite 1; jetzt gilt die Störung wie das Fensterende.
    """
    alt = datetime.now(UTC) - timedelta(days=30)
    quelle = _haengend(alt)
    speicher = FortsetzungsSpeicher()

    stats, run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == ["paper"]
    assert PAPER not in run.failed and PAPER in run.cut
    assert run.finished == {ORGANIZATION, PERSON, MEETING}
    stand = speicher.config["full_sync_resume"]
    assert _seitennummer(stand[PAPER]["page"]) == 3
    assert stand[PAPER]["ids"] == [f"{BASE}/paper/4", f"{BASE}/paper/5"]
    assert all(stand[liste]["done"] is True for liste in (ORGANIZATION, PERSON, MEETING))

    # Nächste Nacht antwortet die Seite: Änderungen, Prüfung der Fortsetzungsseite, dann ab Seite 3
    quelle.haengt = False
    stats, run = await _nacht(quelle, speicher)
    assert stats["incomplete"] == []
    assert _anfragen(quelle, "paper") == [(1, True), (3, False), (3, False), (4, False), (5, False)]
    assert "full_sync_resume" not in speicher.config


async def test_offener_schutzschalter_beim_ersten_abruf_haelt_den_stand() -> None:
    """Ein Abbruch ohne gelesene Folgeseite behält eine frühere Fortsetzungsseite, statt sie zu verwerfen."""
    run = ResumeRun.build(SOURCE, {})
    run.note_cut(PAPER, None)
    assert run.active() and PAPER in run.cut and PAPER not in run.failed


# ---------------------------------------------------------------------------
# K5b: bei offenem Schutzschalter auf „halb offen“ warten
# ---------------------------------------------------------------------------


def _schutzschalter_quelle() -> Callable[[httpx.Request], httpx.Response]:
    def antwort(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/kaputt"):
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [], "links": {}})

    return antwort


async def _schalter_oeffnen(client: OParlClient) -> None:
    for i in range(settings.circuit_breaker_failure_threshold):
        ergebnis = await client.fetch(f"https://{HOST}/kaputt/{i}", use_cache=False)
        assert ergebnis.retries_exhausted and not ergebnis.circuit_open


@pytest.fixture
def schnell_halb_offen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schutzschalter wie im Betrieb, aber nach 0,2 Sekunden halb offen."""
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_recovery_timeout", 0.2)
    monkeypatch.setattr(settings, "circuit_breaker_half_open_wait", 90.0)


@pytest.mark.usefixtures("schnell_halb_offen")
async def test_offener_schutzschalter_wartet_auf_halb_offen() -> None:
    async with _client(_schutzschalter_quelle()) as client:
        await _schalter_oeffnen(client)
        # Parallel laufende Liste desselben Hosts: wartet, statt sofort abzubrechen, und kommt durch
        ergebnis = await client.fetch(f"https://{HOST}/oparl/body/1/paper", use_cache=False)
        assert ergebnis.error is None and ergebnis.status_code == 200


@pytest.mark.usefixtures("schnell_halb_offen")
async def test_ohne_warten_bricht_der_abruf_sofort_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "circuit_breaker_half_open_wait", 0.0)
    async with _client(_schutzschalter_quelle()) as client:
        await _schalter_oeffnen(client)
        ergebnis = await client.fetch(f"https://{HOST}/oparl/body/1/paper", use_cache=False)
    assert ergebnis.circuit_open and not ergebnis.retries_exhausted
    assert ListFetchError(PAPER, PAPER, ergebnis).interrupted


@pytest.mark.usefixtures("schnell_halb_offen")
async def test_kein_warten_ueber_das_fensterende_hinaus() -> None:
    frist = datetime.now(UTC) + timedelta(seconds=0.4)
    async with _client(_schutzschalter_quelle(), fetch_deadline=frist) as client:
        await _schalter_oeffnen(client)
        ergebnis = await client.fetch(f"https://{HOST}/oparl/body/1/paper", use_cache=False)
    assert ergebnis.circuit_open


@pytest.mark.usefixtures("schnell_halb_offen")
async def test_kein_warten_wenn_der_schalter_lange_offen_bleibt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "circuit_breaker_recovery_timeout", 600.0)
    async with _client(_schutzschalter_quelle()) as client:
        await _schalter_oeffnen(client)
        ergebnis = await client.fetch(f"https://{HOST}/oparl/body/1/paper", use_cache=False)
    assert ergebnis.circuit_open


# ---------------------------------------------------------------------------
# K5c/K6b: Fortschritt innerhalb einer Zeitscheibe
# ---------------------------------------------------------------------------


class _SeitenSpeicher(ScheibenSpeicher):
    """Mit ``sync_config["full_sync_slice_page"]`` wie ``DatabaseStorage.set_full_sync_slice_page``."""

    async def set_full_sync_slice_page(self, source_url: str, list_url: str, entry: dict[str, Any] | None) -> None:
        _put_entry(self.config, SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY, list_url, entry)


#: Massenstempel: neun Vorlagen mit demselben modified in der Scheibe 2020 (Seiten zu zwei Objekten: fünf Seiten)
MASSENSTEMPEL = datetime(2020, 3, 15, 10, 0, tzinfo=UTC)
HAMM_PAPER = f"{HAMM_BODY}/paper"


def _hamm_mit_massenstempel() -> HammQuelle:
    quelle = HammQuelle()
    quelle.listen["paper"] = [
        {
            "id": f"https://oparl.hamm.example/oparl/paper/m{i}",
            "type": "https://schema.oparl.org/1.1/Paper",
            "name": f"Vorlage m{i}",
            "modified": MASSENSTEMPEL.isoformat(),
        }
        for i in range(9)
    ]
    return quelle


def _scheibe_2020(url: httpx.URL) -> bool:
    return url.path.endswith("/paper") and str(url.params.get("modified_since", "")).startswith("2020-01-01")


def _seiten_2020(quelle: HammQuelle) -> list[int]:
    return [int(url.params.get("page", "1")) for url in quelle.anfragen if _scheibe_2020(url)]


async def _hamm_nacht(
    quelle: HammQuelle, speicher: _SeitenSpeicher, fensterende: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """Vollabgleich in Zeitscheiben wie im Betrieb: Fortschritt und Stand in der Scheibe aus ``sync_config``."""
    options = SourceFetchOptions.from_sync_config({"full_sync_slices": SCHEIBEN})
    run = SliceRun.build(
        SOURCE,
        options.full_sync_slices,
        full_sync_progress_of(speicher.config),
        slow_lists_of(speicher.config),
        full_sync_slice_pages_of(speicher.config),
    )
    quelle.anfragen.clear()
    async with _hamm_client(quelle, None, speicher.filtervermerke) as client:
        if fensterende is not None:
            client.deadline_passed = fensterende  # type: ignore[method-assign]
        return await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=_kommune("organization", "person", "paper"),
            source_id=UUID(int=9),
            full=True,
            options=options,
            missing_lists=MissingLists(SOURCE, dict(speicher.fehlend)),
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
            slices=run,
        )


async def test_scheibe_setzt_nach_dem_fensterende_an_der_letzten_seite_fort() -> None:
    quelle = _hamm_mit_massenstempel()
    speicher = _SeitenSpeicher()

    # Nacht 1: Das Fenster schließt nach Seite 3 der Scheibe 2020
    stats = await _hamm_nacht(quelle, speicher, lambda: len([u for u in quelle.anfragen if _scheibe_2020(u)]) >= 3)
    assert "paper" in stats["incomplete"]
    assert _seiten_2020(quelle) == [1, 2, 3]
    stand = SlicePage.from_entry(speicher.config[SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY][HAMM_PAPER])
    assert stand is not None and stand.since == datetime(2020, 1, 1, tzinfo=UTC)
    assert parse_qs(urlparse(stand.page).query)["page"] == ["3"]
    assert stand.ids == ("https://oparl.hamm.example/oparl/paper/m4", "https://oparl.hamm.example/oparl/paper/m5")
    # Der Fortschritt über Scheiben bleibt am Beginn der Scheibe
    assert full_sync_progress_of(speicher.config)[HAMM_PAPER] == datetime(2020, 1, 1, tzinfo=UTC)

    # Nacht 2: Prüfung der Seite 3, dann ab Seite 3 weiter statt wieder ab Seite 1
    stats = await _hamm_nacht(quelle, speicher)
    assert stats["incomplete"] == []
    assert _seiten_2020(quelle) == [3, 3, 4, 5]
    assert {f"https://oparl.hamm.example/oparl/paper/m{i}" for i in range(9)} <= set(speicher.modified)
    assert SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY not in speicher.config


async def test_verschobene_scheibe_kommt_ganz() -> None:
    quelle = _hamm_mit_massenstempel()
    speicher = _SeitenSpeicher()
    await _hamm_nacht(quelle, speicher, lambda: len([u for u in quelle.anfragen if _scheibe_2020(u)]) >= 3)

    # Vier Vorlagen verlassen die Scheibe (neu gestempelt): alles rückt zwei Seiten nach vorn
    jetzt = datetime.now(UTC).replace(microsecond=0)
    for vorlage in quelle.listen["paper"][:4]:
        vorlage["modified"] = jetzt.isoformat()
    stats = await _hamm_nacht(quelle, speicher)
    assert stats["incomplete"] == []
    assert _seiten_2020(quelle) == [3, 1, 2, 3]
    assert SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY not in speicher.config


def test_stand_in_der_scheibe_gilt_nur_fuer_genau_diese_scheibe() -> None:
    jetzt = datetime.now(UTC).replace(microsecond=0)
    scheibe = SliceWindow(datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    stand = SlicePage(since=scheibe.since, until=scheibe.until, page=f"{HAMM_PAPER}?page=3", at=jetzt, ids=("a",))
    assert stand.matches(scheibe, jetzt)
    assert not stand.matches(SliceWindow(scheibe.since, None), jetzt)
    assert not stand.matches(scheibe, jetzt + SLICE_PAGE_MAX_AGE + timedelta(minutes=1))
    assert SlicePage.from_entry(stand.to_entry()) == stand
    assert SlicePage.from_entry({"page": "ohne Adresse", "at": jetzt.isoformat(), "since": "2020-01-01"}) is None
    assert SlicePage.from_entry({"page": f"{HAMM_PAPER}?page=3", "at": jetzt.isoformat()}) is None


async def test_scheibe_nach_anderem_fehler_kommt_ganz() -> None:
    """Liste im Kreis oder Teilantwort: Der Stand in der Scheibe fällt weg, statt immer wieder dort anzusetzen."""
    speicher = _SeitenSpeicher()
    scheibe = SliceWindow(datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    alt = SlicePage(scheibe.since, scheibe.until, f"{HAMM_PAPER}?page=3", datetime.now(UTC))
    speicher.config[SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY] = {HAMM_PAPER: alt.to_entry()}
    run = SliceRun.build(SOURCE, None, {}, {}, full_sync_slice_pages_of(speicher.config))
    slices = BodySlices(run)
    teilantwort = ListFetchError(
        HAMM_PAPER,
        f"{HAMM_PAPER}?page=5",
        FetchResult(url=f"{HAMM_PAPER}?page=5", data=None, status_code=200, error="Folgeseite ohne Liste"),
        f"{HAMM_PAPER}?page=4",
    )
    assert not teilantwort.interrupted
    await _orchestrator(speicher)._remember_slice_page(slices, HAMM_PAPER, scheibe, teilantwort)
    assert SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY not in speicher.config and run.pages == {}

    # Fensterende ohne gelesene Folgeseite: ein früherer Stand bleibt
    speicher.config[SYNC_CONFIG_FULL_SYNC_SLICE_PAGE_KEY] = {HAMM_PAPER: alt.to_entry()}
    run.pages[HAMM_PAPER] = alt
    await _orchestrator(speicher)._remember_slice_page(
        slices, HAMM_PAPER, scheibe, FetchWindowClosedError(HAMM_PAPER, f"{HAMM_PAPER}?page=3")
    )
    assert run.pages[HAMM_PAPER] == alt


# ---------------------------------------------------------------------------
# K6c: festgefahrene langsame Liste melden statt täglich dieselbe Tagesscheibe
# ---------------------------------------------------------------------------


def test_festgefahrene_liste_wartet_eine_woche() -> None:
    assert [slow_backoff(n) / timedelta(hours=1) for n in range(1, SLOW_LIST_STUCK_AFTER)][-1] == 24
    assert slow_backoff(SLOW_LIST_STUCK_AFTER) == SLOW_LIST_STUCK_RETRY
    assert slow_backoff(SLOW_LIST_STUCK_AFTER + 30) == SLOW_LIST_STUCK_RETRY
    eintrag = SlowList(
        failures=SLOW_LIST_STUCK_AFTER,
        until=datetime(2026, 11, 27, tzinfo=UTC),
        since=datetime(2026, 9, 27, 22, 0, tzinfo=UTC),
        reason="HTTP 504",
        stuck_since=datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert eintrag.stuck and SlowList.from_entry(eintrag.to_entry()) == eintrag
    assert "festgefahren" in eintrag.stuck_note(f"{HAMM_BODY}/consultation")
    assert "stuck_since" not in SlowList(1, eintrag.until, eintrag.since).to_entry()


async def test_festgefahrene_langsame_liste_wird_gemeldet(caplog: pytest.LogCaptureFixture) -> None:
    """Hamm: Die Beratungsliste scheitert seit Wochen an derselben Tagesscheibe."""
    quelle = HammQuelle()
    quelle.langsam.add("consultation")
    speicher = ScheibenSpeicher()
    kommune = _kommune("organization", "person", "meeting", "paper", "consultation")
    liste = f"{HAMM_BODY}/consultation"
    seit = datetime.now(UTC) - timedelta(days=50)
    faellig = SlowList(failures=SLOW_LIST_STUCK_AFTER - 1, until=datetime.now(UTC) - timedelta(minutes=1), since=seit)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY] = {liste: faellig.to_entry()}

    vorher = datetime.now(UTC)
    with caplog.at_level(logging.WARNING, logger="src.sync.orchestrator"):
        stats = await _hamm_lauf(quelle, speicher, full=False, kommune=kommune)
    eintrag = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][liste])
    assert eintrag is not None and eintrag.stuck and eintrag.stuck_since is not None
    assert eintrag.until - vorher >= SLOW_LIST_STUCK_RETRY - timedelta(minutes=1)
    assert sum("festgefahren" in fehler for fehler in stats["errors"]) == 1
    assert any("festgefahren" in record.getMessage() for record in caplog.records)
    assert stats["incomplete"] == []

    # Solange sie ausgesetzt ist: keine Anfrage, aber der Hinweis steht in jedem Sync-Protokoll
    caplog.clear()
    stats = await _hamm_lauf(quelle, speicher, full=False, kommune=kommune)
    assert quelle.anfragen_an("consultation") == []
    assert sum("festgefahren" in fehler for fehler in stats["errors"]) == 1
    assert not caplog.records  # gewarnt wird einmal, beim Festfahren

    # Nach der Woche wieder gescheitert: festgefahren seit dem ersten Mal
    abgelaufen = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][liste])
    assert abgelaufen is not None
    abgelaufen.until = datetime.now(UTC) - timedelta(minutes=1)
    speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][liste] = abgelaufen.to_entry()
    stats = await _hamm_lauf(quelle, speicher, full=False, kommune=kommune)
    weiter = SlowList.from_entry(speicher.config[SYNC_CONFIG_SLOW_LISTS_KEY][liste])
    assert weiter is not None and weiter.failures == SLOW_LIST_STUCK_AFTER + 1
    assert weiter.stuck_since == eintrag.stuck_since


# ---------------------------------------------------------------------------
# 6.4: Vollabgleich mit Lücke im Sync-Protokoll
# ---------------------------------------------------------------------------


def test_zustand_im_sync_protokoll() -> None:
    ergebnis = SyncResult(source_url=SOURCE, source_name="Bochum", success=True, full=True)
    assert sync_log_status(ergebnis) == SYNC_STATUS_SUCCESS
    ergebnis.full_incomplete = True
    assert sync_log_status(ergebnis) == SYNC_STATUS_PARTIAL
    ergebnis.success = False
    assert sync_log_status(ergebnis) == SYNC_STATUS_FAILED


def test_unvollstaendig_ist_kein_fehlgeschlagener_lauf() -> None:
    vorher = metrics.simple.sync_errors
    metrics.record_source_run("Bochum", "full", SYNC_STATUS_PARTIAL, 1.0, 3, 0.0)
    assert metrics.simple.sync_errors == vorher
    metrics.record_source_run("Bochum", "full", SYNC_STATUS_FAILED, 1.0, 3, 0.0)
    assert metrics.simple.sync_errors == vorher + (1 if metrics.enabled else 0)
