# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fortsetzung eines Vollabgleichs, den das Zeitfenster der Quelle abbricht (``src/sync/fortsetzung.py``).

Nachgebildet ist die Stadt Köln (Abrufe nur von 23 bis 1 Uhr): Der Vollabgleich liest Gremien, Personen und Sitzungen
ganz, die lange Liste der Vorlagen bricht am Fensterende ab. Bisher begann der nächste Vollabgleich wieder bei Seite 1
und kam nie weiter; jetzt setzt er an der zuletzt gelesenen Seite fort und holt die schon gelesenen Listen nur mit
``modified_since``. Antworten aus ``httpx.MockTransport``, keine Abrufe, keine Datenbank.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from src.client.oparl_client import OParlClient
from src.client.source_options import SourceFetchOptions
from src.storage.database import SYNC_CONFIG_FULL_SYNC_RESUME_KEY, _put_entry, full_sync_resume_of
from src.sync import orchestrator as orchestrator_module
from src.sync.fortsetzung import CHANGES, FULL, RESUME, RESUME_MAX_AGE, ResumeEntry, ResumeRun
from src.sync.orchestrator import MissingLists, TimedLists
from src.sync.slices import SliceRun
from tests.test_abruffenster import LISTE as KOMMUNENLISTE
from tests.test_abruffenster import _orchestrator as _quellen_orchestrator
from tests.test_abruffenster import _Speicher as _QuellenSpeicher
from tests.test_abruffenster import fenster_um_jetzt
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    BASE,
    BODY,
    SOURCE,
    Quelle,
    Speicher,
    _kommune,
    _ohne_nebenlaeufe,
    _orchestrator,
)

PAPER = f"{BODY}/paper"
MEETING = f"{BODY}/meeting"
ORGANIZATION = f"{BODY}/organization"
PERSON = f"{BODY}/person"


class FortsetzungsSpeicher(Speicher):
    """Speicher mit ``sync_config`` für die Fortsetzung (wie ``DatabaseStorage.set_full_sync_resume``)."""

    def __init__(self) -> None:
        super().__init__()
        self.config: dict[str, Any] = {}
        self.aenderungen: list[dict[str, Any]] = []

    async def set_full_sync_resume(self, source_url: str, changes: dict[str, Any]) -> None:
        self.aenderungen.append(dict(changes))
        for list_url, entry in changes.items():
            _put_entry(self.config, SYNC_CONFIG_FULL_SYNC_RESUME_KEY, list_url, entry)


def _koeln(alt: datetime) -> Quelle:
    """Gremien, Personen je zwei Seiten, Sitzungen eine Seite, Vorlagen fünf Seiten (je zwei Objekte)."""
    quelle = Quelle(geaendert=alt)
    quelle.listen["/oparl/body/1/meeting"] = quelle.listen["/oparl/body/1/meeting"][:2]
    quelle.listen["/oparl/body/1/paper"] = [
        {
            "id": f"{BASE}/paper/{i}",
            "type": "https://schema.oparl.org/1.1/Paper",
            "name": f"Vorlage {i}",
            "modified": alt.isoformat(),
        }
        for i in range(10)
    ]
    return quelle


@asynccontextmanager
async def _client(quelle: Quelle, fensterende: Callable[[], bool] | None) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        if fensterende is not None:
            client.deadline_passed = fensterende  # type: ignore[method-assign]
        yield client


async def _nacht(
    quelle: Quelle, speicher: FortsetzungsSpeicher, fensterende: Callable[[], bool] | None = None
) -> tuple[dict[str, Any], ResumeRun]:
    """Vollabgleich der Kommune wie im Betrieb: Fortsetzung aus ``sync_config``, danach gespeichert."""
    run = ResumeRun.build(SOURCE, full_sync_resume_of(speicher.config))
    quelle.anfragen.clear()
    orchestrator = _orchestrator(speicher)
    async with _client(quelle, fensterende) as client:
        stats = await orchestrator._sync_body(
            client=client,
            body_data=_kommune(),
            source_id=UUID(int=9),
            full=True,
            options=SourceFetchOptions(),
            missing_lists=MissingLists(SOURCE, {}),
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
            slices=SliceRun.build(SOURCE, None, {}, {}),
            resume=run,
        )
    await orchestrator._settle_resume(run, complete=not stats["incomplete"])
    return stats, run


def _seiten(quelle: Quelle, liste: str) -> list[int]:
    return [
        int(parse_qs(urlparse(str(url)).query).get("page", ["1"])[0])
        for url in quelle.anfragen
        if url.path.endswith(f"/{liste}")
    ]


async def test_abgebrochener_vollabgleich_setzt_in_der_naechsten_nacht_fort() -> None:
    alt = datetime.now(UTC) - timedelta(days=30)
    quelle = _koeln(alt)
    speicher = FortsetzungsSpeicher()

    # Nacht 1: Das Fenster schließt nach drei Seiten der Vorlagen
    stats, run = await _nacht(quelle, speicher, lambda: quelle.anfragen_je_liste()["paper"] >= 3)

    assert stats["incomplete"] == ["paper"]
    assert _seiten(quelle, "paper") == [1, 2, 3]
    assert run.finished == {ORGANIZATION, PERSON, MEETING}
    stand = speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert set(stand) == {ORGANIZATION, PERSON, MEETING, PAPER}
    assert all(stand[liste]["done"] is True for liste in (ORGANIZATION, PERSON, MEETING))
    # Fortgesetzt wird ab der zuletzt ganz gelesenen Seite (eine Seite Überlappung)
    assert _seiten_nummer(stand[PAPER]["page"]) == 3

    # Nacht 2: Gelesene Listen nur mit modified_since, die Vorlagen ab Seite 3 bis zum Ende
    stats, run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    for liste in ("organization", "person", "meeting"):
        assert all(filter_ is not None for filter_ in quelle.filter_der_anfragen(liste)), liste
    assert _seiten(quelle, "paper") == [3, 4, 5]
    assert quelle.filter_der_anfragen("paper") == [None, None, None]
    # Vollständig: Der Durchgang ist fertig, die Fortsetzung gelöscht
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.config
    assert speicher.aenderungen[-1] == dict.fromkeys(sorted({ORGANIZATION, PERSON, MEETING, PAPER}))

    # Nacht 3 (nächster fälliger Vollabgleich): wieder alles ganz, ohne Filter
    stats, run = await _nacht(quelle, speicher)
    assert stats["incomplete"] == []
    assert _seiten(quelle, "paper") == [1, 2, 3, 4, 5]
    assert quelle.filter_der_anfragen("meeting") == [None]


async def test_ohne_abbruch_schreibt_der_vollabgleich_keine_fortsetzung() -> None:
    quelle = _koeln(datetime.now(UTC) - timedelta(days=30))
    speicher = FortsetzungsSpeicher()

    stats, run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    assert run.finished == {ORGANIZATION, PERSON, MEETING, PAPER}
    assert speicher.aenderungen == [] and speicher.config == {}


async def test_abbruch_vor_der_ersten_seite_beginnt_die_liste_von_vorn() -> None:
    """Endet das Fenster, bevor eine Seite der Vorlagen gelesen ist, gibt es für sie keine Fortsetzungsseite."""
    quelle = _koeln(datetime.now(UTC) - timedelta(days=30))
    speicher = FortsetzungsSpeicher()

    stats, run = await _nacht(quelle, speicher, lambda: quelle.anfragen_je_liste()["meeting"] >= 1)

    assert "paper" in stats["incomplete"]
    assert run.cut.get(PAPER) is None
    assert PAPER not in speicher.config.get(SYNC_CONFIG_FULL_SYNC_RESUME_KEY, {})


def _seiten_nummer(url: str) -> int:
    return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])


# ---------------------------------------------------------------------------
# Zustand der Fortsetzung
# ---------------------------------------------------------------------------


def _eintrag(at: datetime, page: str | None = None, done: bool = False) -> dict[str, Any]:
    return ResumeEntry(at=at, page=page, done=done).to_entry()


def test_plan_je_liste() -> None:
    jetzt = datetime.now(UTC)
    run = ResumeRun.build(
        SOURCE,
        {MEETING: _eintrag(jetzt, done=True), PAPER: _eintrag(jetzt, page=f"{PAPER}?page=7")},
        jetzt,
    )
    assert run.plan(MEETING) == (CHANGES, None)
    assert run.plan(PAPER) == (RESUME, f"{PAPER}?page=7")
    assert run.plan(PERSON) == (FULL, None)


@pytest.mark.parametrize(
    "roh",
    [None, "x", {}, {"at": "kein Datum", "done": True}, {"at": "2026-11-17T22:00:00+00:00"}, {"page": "relativ"}],
)
def test_ungueltige_eintraege_zaehlen_nicht(roh: Any) -> None:
    run = ResumeRun.build(SOURCE, {PAPER: roh}, datetime(2026, 11, 18, tzinfo=UTC))
    assert run.entries == {}
    # Beim nächsten Speichern fallen sie weg
    assert run.changes(complete=False) == {PAPER: None}


def test_alte_eintraege_gelten_nicht_und_werden_geloescht() -> None:
    jetzt = datetime.now(UTC)
    alt = jetzt - RESUME_MAX_AGE - timedelta(minutes=1)
    run = ResumeRun.build(
        SOURCE, {PAPER: _eintrag(alt, page=f"{PAPER}?page=9"), MEETING: _eintrag(jetzt, done=True)}, jetzt
    )

    assert run.plan(PAPER) == (FULL, None)
    run.note_cut(PAPER, f"{PAPER}?page=2")
    aenderungen = run.changes(complete=False, now=jetzt)
    # Neuer Eintrag mit neuem Zeitpunkt; die gelesene Sitzungsliste behält ihren
    assert aenderungen[PAPER] == _eintrag(jetzt, page=f"{PAPER}?page=2")
    assert MEETING not in aenderungen


def test_abbruch_ohne_gelesene_seite_behaelt_die_fruehere_fortsetzung() -> None:
    jetzt = datetime.now(UTC)
    beginn = jetzt - timedelta(days=2)
    run = ResumeRun.build(SOURCE, {PAPER: _eintrag(beginn, page=f"{PAPER}?page=7")}, jetzt)

    run.note_cut(PAPER, None)

    assert run.changes(complete=False, now=jetzt) == {PAPER: _eintrag(beginn, page=f"{PAPER}?page=7")}


def test_erste_seite_ist_keine_fortsetzung() -> None:
    run = ResumeRun.build(SOURCE, {})
    run.note_cut(PAPER, PAPER)
    assert not run.active()
    assert run.changes(complete=False) == {}


def test_gelesene_listen_behalten_den_zeitpunkt_ihres_ersten_eintrags() -> None:
    jetzt = datetime.now(UTC)
    gestern = jetzt - timedelta(days=1)
    run = ResumeRun.build(
        SOURCE, {MEETING: _eintrag(gestern, done=True), PAPER: _eintrag(gestern, page=f"{PAPER}?page=3")}, jetzt
    )

    run.note_finished(MEETING)
    run.note_cut(PAPER, f"{PAPER}?page=6")
    run.note_finished(PERSON)

    assert run.changes(complete=False, now=jetzt) == {
        MEETING: _eintrag(gestern, done=True),
        PAPER: _eintrag(gestern, page=f"{PAPER}?page=6"),
        PERSON: _eintrag(jetzt, done=True),
    }
    assert run.changes(complete=True) == {MEETING: None, PAPER: None}


# ---------------------------------------------------------------------------
# Lauf der Quelle (sync_body_url): Fortsetzung nur im Vollabgleich ohne Zeitscheiben, gespeichert nach dem Lauf
# ---------------------------------------------------------------------------


class _Quellenspeicher(_QuellenSpeicher):
    def __init__(self, sync_config: dict[str, Any]) -> None:
        super().__init__(sync_config)
        self.aenderungen: list[dict[str, Any]] = []

    async def set_full_sync_resume(self, source_url: str, changes: dict[str, Any]) -> None:
        self.aenderungen.append(dict(changes))
        for list_url, entry in changes.items():
            _put_entry(self.sync_config, SYNC_CONFIG_FULL_SYNC_RESUME_KEY, list_url, entry)


def _kommunenliste(monkeypatch: pytest.MonkeyPatch) -> None:
    def ris(request: httpx.Request) -> httpx.Response:
        kommune = {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Köln"}
        return httpx.Response(200, json={"data": [kommune], "links": {}})

    echter_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.client.oparl_client.httpx.AsyncClient",
        lambda *args, **kwargs: echter_client(transport=httpx.MockTransport(ris), **kwargs),
    )


async def test_lauf_der_quelle_speichert_und_beendet_die_fortsetzung(monkeypatch: pytest.MonkeyPatch) -> None:
    _kommunenliste(monkeypatch)
    speicher = _Quellenspeicher({"sync_window": fenster_um_jetzt(offen=True)})
    orchestrator = _quellen_orchestrator(speicher)
    erhalten: list[ResumeRun | None] = []
    ergebnis: dict[str, Any] = {"incomplete": ["paper"]}

    async def sync_body(**kwargs: Any) -> dict[str, Any]:
        resume = kwargs["resume"]
        erhalten.append(resume)
        if resume is not None and ergebnis["incomplete"]:
            resume.note_finished(MEETING)
            resume.note_cut(PAPER, f"{PAPER}?page=3")
        return dict(ergebnis)

    orchestrator._sync_body = sync_body  # type: ignore[method-assign]
    monkeypatch.setattr(
        orchestrator_module, "sync_complete", lambda results, missed, client: not results[0]["incomplete"]
    )

    # Nacht 1: abgebrochen, Fortsetzung gespeichert
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=True)
    stand = speicher.sync_config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert stand[MEETING]["done"] is True and stand[PAPER]["page"] == f"{PAPER}?page=3"

    # Nacht 2: Der Lauf bekommt den Stand, wird vollständig, die Fortsetzung endet
    ergebnis["incomplete"] = []
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=True)
    assert erhalten[1] is not None and erhalten[1].plan(PAPER) == (RESUME, f"{PAPER}?page=3")
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.sync_config

    # Änderungsabgleich: keine Fortsetzung
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=False)
    assert erhalten[2] is None


class _KoelnMitAbsage(Quelle):
    """Lehnt gefilterte Folgeseiten der Personen mit HTTP 401 ab, wie die Stadt Köln Anfang November 2026."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.endswith("/person") and "modified_since" in params and params.get("page", "1") != "1":
            self.anfragen.append(request.url)
            return httpx.Response(401)
        return super().__call__(request)


async def test_scheitert_der_aenderungsabgleich_einer_gelesenen_liste_kommt_sie_ganz() -> None:
    jetzt = datetime.now(UTC)
    quelle = _KoelnMitAbsage(geaendert=jetzt - timedelta(days=30))
    neu = _koeln(jetzt - timedelta(days=30))
    quelle.listen.update(neu.listen)
    # Drei Personen, gestern geändert: Der Änderungsabgleich braucht zwei Seiten
    for person in quelle.listen["/oparl/body/1/person"]:
        person["modified"] = (jetzt - timedelta(days=1)).isoformat()
    speicher = FortsetzungsSpeicher()
    speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY] = {
        PERSON: _eintrag(jetzt - timedelta(days=1), done=True),
        PAPER: _eintrag(jetzt - timedelta(days=1), page=f"{PAPER}?page=4"),
    }

    stats, run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    filter_personen = quelle.filter_der_anfragen("person")
    # Erst der Änderungsabgleich (zweite Seite abgelehnt), dann die ganze Liste ohne Filter
    assert filter_personen[:2] == [filter_personen[0], filter_personen[0]] and filter_personen[0] is not None
    assert filter_personen[2:] == [None, None]
    assert _seiten(quelle, "paper") == [4, 5]
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.config
