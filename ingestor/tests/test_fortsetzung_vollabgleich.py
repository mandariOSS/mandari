# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fortsetzung eines Vollabgleichs, den das Zeitfenster der Quelle abbricht (``src/sync/fortsetzung.py``).

Nachgebildet ist die Stadt Köln (Abrufe nur von 23 bis 1 Uhr): Der Vollabgleich liest Gremien, Personen und Sitzungen
ganz, die lange Liste der Vorlagen bricht am Fensterende ab. Bisher begann der nächste Vollabgleich wieder bei Seite 1
und kam nie weiter; jetzt holt er die Änderungen seit dem ersten Lauf nach, setzt an der zuletzt gelesenen Seite fort
und liest schon gelesene Listen nur mit ``modified_since``. Antworten aus ``httpx.MockTransport``, keine Abrufe, keine
Datenbank.
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

from src.client.oparl_client import FetchWindowClosedError, OParlClient
from src.client.source_options import SourceFetchOptions
from src.storage.database import SYNC_CONFIG_FULL_SYNC_RESUME_KEY, _put_entry, full_sync_resume_of
from src.sync import orchestrator as orchestrator_module
from src.sync.fortsetzung import CHANGES, FULL, MAX_PAGE_IDS, RESUME, RESUME_MAX_AGE, ResumeEntry, ResumeRun
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
CONSULTATION = f"{BODY}/consultation"


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


def _vorlage(i: int, modified: datetime) -> dict[str, Any]:
    return {
        "id": f"{BASE}/paper/{i}",
        "type": "https://schema.oparl.org/1.1/Paper",
        "name": f"Vorlage {i}",
        "modified": modified.isoformat(),
    }


def _koeln(alt: datetime) -> Quelle:
    """Gremien, Personen je zwei Seiten, Sitzungen eine Seite, Vorlagen fünf Seiten (je zwei Objekte)."""
    quelle = Quelle(geaendert=alt)
    quelle.listen["/oparl/body/1/meeting"] = quelle.listen["/oparl/body/1/meeting"][:2]
    quelle.listen["/oparl/body/1/paper"] = [_vorlage(i, alt) for i in range(10)]
    return quelle


@asynccontextmanager
async def _client(
    quelle: Quelle, fensterende: Callable[[], bool] | None, list_params: dict[str, str] | None = None
) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0, list_params=list_params) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        if fensterende is not None:
            client.deadline_passed = fensterende  # type: ignore[method-assign]
        yield client


async def _nacht(
    quelle: Quelle,
    speicher: FortsetzungsSpeicher,
    fensterende: Callable[[], bool] | None = None,
    kommune: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ResumeRun]:
    """Vollabgleich der Kommune wie im Betrieb: Fortsetzung aus ``sync_config``, danach gespeichert."""
    run = ResumeRun.build(SOURCE, full_sync_resume_of(speicher.config))
    quelle.anfragen.clear()
    orchestrator = _orchestrator(speicher)
    async with _client(quelle, fensterende) as client:
        stats = await orchestrator._sync_body(
            client=client,
            body_data=kommune or _kommune(),
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


def _seitennummer(url: str) -> int:
    return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])


def _anfragen(quelle: Quelle, liste: str) -> list[tuple[int, bool]]:
    """Anfragen an eine Liste: (Seite, mit Filter)."""
    return [
        (_seitennummer(str(url)), "modified_since" in url.params)
        for url in quelle.anfragen
        if url.path.endswith(f"/{liste}")
    ]


def _bis_seite_drei(quelle: Quelle) -> Callable[[], bool]:
    """Das Fenster schließt nach drei Seiten der Vorlagen."""
    return lambda: quelle.anfragen_je_liste()["paper"] >= 3


async def test_abgebrochener_vollabgleich_setzt_in_der_naechsten_nacht_fort() -> None:
    alt = datetime.now(UTC) - timedelta(days=30)
    quelle = _koeln(alt)
    speicher = FortsetzungsSpeicher()

    # Nacht 1: abgebrochen nach Seite 3 der Vorlagen
    stats, run = await _nacht(quelle, speicher, _bis_seite_drei(quelle))

    assert stats["incomplete"] == ["paper"]
    assert _anfragen(quelle, "paper") == [(1, False), (2, False), (3, False)]
    assert run.finished == {ORGANIZATION, PERSON, MEETING}
    stand = speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert set(stand) == {ORGANIZATION, PERSON, MEETING, PAPER}
    assert all(stand[liste]["done"] is True for liste in (ORGANIZATION, PERSON, MEETING))
    # Fortgesetzt wird ab der zuletzt ganz gelesenen Seite (eine Seite Überlappung), mit ihren Kennungen
    assert _seitennummer(stand[PAPER]["page"]) == 3
    assert stand[PAPER]["ids"] == [f"{BASE}/paper/4", f"{BASE}/paper/5"]
    beginn = stand[PAPER]["at"]
    assert all(stand[liste]["at"] == beginn for liste in stand)

    # Nacht 2: Gelesene Listen nur mit Filter. Die Vorlagen: Prüfung der Fortsetzungsseite, Änderungen seit Nacht 1
    # (keine), dann ab Seite 3 bis zum Ende
    stats, run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    for liste in ("organization", "person", "meeting"):
        anfragen = _anfragen(quelle, liste)
        assert anfragen and all(gefiltert for _seite, gefiltert in anfragen), liste
    assert _anfragen(quelle, "paper") == [(3, False), (1, True), (3, False), (4, False), (5, False)]
    assert quelle.filter_der_anfragen("paper")[1] == datetime.fromisoformat(beginn).isoformat()
    # Vollständig: Der Durchgang ist fertig, die Fortsetzung gelöscht
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.config
    assert speicher.aenderungen[-1] == dict.fromkeys(sorted({ORGANIZATION, PERSON, MEETING, PAPER}))

    # Nacht 3 (nächster fälliger Vollabgleich): wieder alles ganz, ohne Filter
    stats, run = await _nacht(quelle, speicher)
    assert stats["incomplete"] == []
    assert _anfragen(quelle, "paper") == [(n, False) for n in range(1, 6)]
    assert quelle.filter_der_anfragen("meeting") == [None]


async def test_aenderung_auf_einer_schon_gelesenen_seite_kommt_mit() -> None:
    """Prüfbefund: Ändert sich zwischen zwei Nächten eine Vorlage vor der Fortsetzungsseite, geht sie nicht verloren."""
    alt = datetime.now(UTC) - timedelta(days=30)
    quelle = _koeln(alt)
    speicher = FortsetzungsSpeicher()
    await _nacht(quelle, speicher, _bis_seite_drei(quelle))
    assert speicher.modified[f"{BASE}/paper/0"] == alt

    # Tagsüber ändert die Quelle Vorlage 0 (Seite 1)
    geaendert = datetime.now(UTC).replace(microsecond=0)
    quelle.listen["/oparl/body/1/paper"][0] = _vorlage(0, geaendert)
    stats, _run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    assert speicher.modified[f"{BASE}/paper/0"] == geaendert


async def test_verschobene_liste_kommt_ganz() -> None:
    """Prüfbefund: Trägt die Fortsetzungsseite keine gemerkte Kennung mehr, liest der Lauf die Liste von vorn."""
    alt = datetime.now(UTC) - timedelta(days=30)
    quelle = _koeln(alt)
    speicher = FortsetzungsSpeicher()
    await _nacht(quelle, speicher, _bis_seite_drei(quelle))

    # Die Quelle entfernt vier Vorlagen am Anfang ohne Löschvermerk: alles rückt zwei Seiten nach vorn
    quelle.listen["/oparl/body/1/paper"] = quelle.listen["/oparl/body/1/paper"][4:]
    stats, _run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    # Prüfung der Seite 3, dann die ganze Liste ab Seite 1 (ohne eigenen Änderungsabgleich: sie kommt ja ganz)
    assert _anfragen(quelle, "paper") == [(3, False), (1, False), (2, False), (3, False)]
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.config


async def test_fehlende_liste_gilt_nicht_als_gelesen() -> None:
    """Prüfbefund: Antwortet eine Liste im laufenden Durchgang mit 404, ist sie nicht „gelesen“."""
    quelle = _koeln(datetime.now(UTC) - timedelta(days=30))
    speicher = FortsetzungsSpeicher()
    speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY] = {
        PAPER: ResumeEntry(at=datetime.now(UTC), page=f"{PAPER}?page=4").to_entry()
    }
    kommune = _kommune(consultation=CONSULTATION)  # die Quelle antwortet auf die Beratungen mit 404

    stats, run = await _nacht(quelle, speicher, kommune=kommune)

    assert stats["missing_lists"] == [CONSULTATION]
    assert CONSULTATION not in run.finished
    assert CONSULTATION not in run.changes(complete=False)


class _OhneSeiteNeun(Quelle):
    """Die Vorlagenliste kennt die Seite 9 nicht (mehr): HTTP 404."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/paper") and request.url.params.get("page") == "9":
            self.anfragen.append(request.url)
            return httpx.Response(404)
        return super().__call__(request)


async def test_fortsetzungsseite_mit_fehler_beginnt_beim_naechsten_mal_von_vorn() -> None:
    """Gibt es die Fortsetzungsseite nicht mehr (404), fällt der Eintrag weg; der nächste Lauf liest die Liste ganz."""
    jetzt = datetime.now(UTC)
    quelle = _OhneSeiteNeun(geaendert=jetzt - timedelta(days=30))
    quelle.listen.update(_koeln(jetzt - timedelta(days=30)).listen)
    speicher = FortsetzungsSpeicher()
    speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY] = {
        MEETING: ResumeEntry(at=jetzt, done=True).to_entry(),
        PAPER: ResumeEntry(
            at=jetzt, page=f"https://ris.example.org{PAPER.split('ris.example.org')[1]}?page=9"
        ).to_entry(),
    }

    stats, run = await _nacht(quelle, speicher)

    assert "paper" in stats["incomplete"]
    assert PAPER in run.failed
    stand = speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert PAPER not in stand and stand[MEETING]["done"] is True


async def test_abbruch_nach_der_ersten_seite_ist_keine_fortsetzung() -> None:
    """Prüfbefund: Auch mit ``list_params`` meldet der Client die erste Seite nie als Fortsetzungsseite."""
    quelle = _koeln(datetime.now(UTC) - timedelta(days=30))
    async with _client(quelle, lambda: quelle.anfragen_je_liste()["paper"] >= 1, list_params={"size": "2"}) as client:
        with pytest.raises(FetchWindowClosedError) as erste:
            [page async for page in client.fetch_list(PAPER)]
    assert erste.value.last_page_url is None and erste.value.last_page_ids == ()

    quelle.anfragen.clear()
    async with _client(quelle, lambda: quelle.anfragen_je_liste()["paper"] >= 2, list_params={"size": "2"}) as client:
        with pytest.raises(FetchWindowClosedError) as zweite:
            [page async for page in client.fetch_list(PAPER)]
    assert zweite.value.last_page_url is not None and _seitennummer(zweite.value.last_page_url) == 2
    assert zweite.value.last_page_ids == (f"{BASE}/paper/2", f"{BASE}/paper/3")


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
    assert run.cut.get(PAPER) == (None, ())
    # Der Durchgang beginnt trotzdem: Die gelesenen Listen werden nur noch nachgeführt, die Vorlagen kommen von vorn
    stand = speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert PAPER not in stand
    assert stand[ORGANIZATION]["done"] is True and stand[PERSON]["done"] is True


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
    quelle.listen.update(_koeln(jetzt - timedelta(days=30)).listen)
    # Drei Personen, nach dem Beginn des Durchgangs geändert: Der Änderungsabgleich braucht zwei Seiten
    for person in quelle.listen["/oparl/body/1/person"]:
        person["modified"] = (jetzt - timedelta(hours=1)).isoformat()
    speicher = FortsetzungsSpeicher()
    speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY] = {
        PERSON: ResumeEntry(at=jetzt - timedelta(days=1), done=True).to_entry(),
        PAPER: ResumeEntry(at=jetzt - timedelta(days=1), page=f"{PAPER}?page=4").to_entry(),
    }

    stats, _run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    # Erst der Änderungsabgleich (zweite Seite abgelehnt), dann die ganze Liste ohne Filter
    assert _anfragen(quelle, "person") == [(1, True), (2, True), (1, False), (2, False)]
    # Vorlagen ohne gemerkte Kennungen: keine Prüfung der Seite, Änderungen, dann ab Seite 4
    assert _anfragen(quelle, "paper") == [(1, True), (4, False), (5, False)]
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.config


# ---------------------------------------------------------------------------
# Zustand der Fortsetzung
# ---------------------------------------------------------------------------


def _eintrag(at: datetime, page: str | None = None, done: bool = False, ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return ResumeEntry(at=at, page=page, done=done, ids=ids).to_entry()


def test_plan_je_liste_und_aenderungsfenster() -> None:
    jetzt = datetime.now(UTC)
    gestern = jetzt - timedelta(days=1)
    run = ResumeRun.build(
        SOURCE,
        {MEETING: _eintrag(gestern, done=True), PAPER: _eintrag(gestern, page=f"{PAPER}?page=7", ids=("a", "b"))},
        jetzt,
        margin=timedelta(hours=2),
    )
    assert run.plan(MEETING) == (CHANGES, ResumeEntry(at=gestern.replace(microsecond=0), done=True))
    how, entry = run.plan(PAPER)
    assert how == RESUME and entry is not None and entry.page == f"{PAPER}?page=7" and entry.ids == ("a", "b")
    assert run.plan(PERSON) == (FULL, None)
    # Änderungen seit dem ersten Lauf der Liste abzüglich Sicherheitsabstand
    assert run.since(MEETING) == gestern.replace(microsecond=0) - timedelta(hours=2)
    assert run.since(PERSON) is None


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
    run.note_cut(PAPER, f"{PAPER}?page=2", ["x"])
    aenderungen = run.changes(complete=False)
    # Neuer Eintrag mit dem Beginn dieses Laufs; die gelesene Sitzungsliste bleibt unverändert
    assert aenderungen[PAPER] == _eintrag(jetzt, page=f"{PAPER}?page=2", ids=("x",))
    assert MEETING not in aenderungen


def test_abbruch_ohne_gelesene_seite_behaelt_die_fruehere_fortsetzung() -> None:
    jetzt = datetime.now(UTC)
    beginn = jetzt - timedelta(days=2)
    run = ResumeRun.build(SOURCE, {PAPER: _eintrag(beginn, page=f"{PAPER}?page=7", ids=("x",))}, jetzt)

    run.note_cut(PAPER, None)

    assert run.changes(complete=False) == {PAPER: _eintrag(beginn, page=f"{PAPER}?page=7", ids=("x",))}


def test_erste_seite_ist_keine_fortsetzung() -> None:
    run = ResumeRun.build(SOURCE, {})
    run.note_cut(PAPER, PAPER)
    # Der Abbruch beginnt den Durchgang, die Liste aber ohne Fortsetzungsseite
    assert run.active()
    assert run.changes(complete=False) == {PAPER: None}


def test_stand_vor_dem_letzten_vollabgleich_gilt_nicht() -> None:
    """Scheiterte das Löschen nach einem vollständigen Durchgang, gilt der alte Stand im nächsten nicht."""
    jetzt = datetime.now(UTC)
    vor_zwei_tagen = jetzt - timedelta(days=2)
    run = ResumeRun.build(
        SOURCE,
        {MEETING: _eintrag(vor_zwei_tagen, done=True), PAPER: _eintrag(jetzt, page=f"{PAPER}?page=3")},
        jetzt,
        not_before=jetzt - timedelta(days=1),
    )
    assert run.plan(MEETING) == (FULL, None)
    assert run.plan(PAPER)[0] == RESUME
    # Der alte Eintrag fällt beim nächsten Speichern weg
    assert run.changes(complete=False)[MEETING] is None


def test_eintraege_behalten_den_beginn_ihres_ersten_laufs() -> None:
    jetzt = datetime.now(UTC)
    gestern = jetzt - timedelta(days=1)
    run = ResumeRun.build(
        SOURCE,
        {MEETING: _eintrag(gestern, done=True), PAPER: _eintrag(gestern, page=f"{PAPER}?page=3")},
        jetzt,
    )

    run.note_finished(MEETING)
    run.note_cut(PAPER, f"{PAPER}?page=6", [f"{BASE}/paper/{i}" for i in range(MAX_PAGE_IDS + 5)])
    run.note_finished(PERSON)

    aenderungen = run.changes(complete=False)
    assert aenderungen[MEETING] == _eintrag(gestern, done=True)
    assert aenderungen[PERSON] == _eintrag(jetzt, done=True)
    assert aenderungen[PAPER] is not None and aenderungen[PAPER]["at"] == _eintrag(gestern, done=True)["at"]
    assert len(aenderungen[PAPER]["ids"]) == MAX_PAGE_IDS
    assert run.changes(complete=True) == {MEETING: None, PAPER: None}


def test_gescheiterte_liste_verliert_ihren_eintrag() -> None:
    jetzt = datetime.now(UTC)
    run = ResumeRun.build(SOURCE, {PAPER: _eintrag(jetzt, page=f"{PAPER}?page=3"), MEETING: _eintrag(jetzt, done=True)})
    run.note_failed(PAPER)
    assert run.changes(complete=False) == {PAPER: None}


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
    speicher = _Quellenspeicher({"sync_window": fenster_um_jetzt(offen=True), "modified_since_margin_minutes": 30})
    orchestrator = _quellen_orchestrator(speicher)
    erhalten: list[ResumeRun | None] = []
    ergebnis: dict[str, Any] = {"incomplete": ["paper"]}

    async def sync_body(**kwargs: Any) -> dict[str, Any]:
        resume = kwargs["resume"]
        erhalten.append(resume)
        if resume is not None and ergebnis["incomplete"]:
            resume.note_finished(MEETING)
            resume.note_cut(PAPER, f"{PAPER}?page=3", [f"{BASE}/paper/4"])
        return dict(ergebnis)

    orchestrator._sync_body = sync_body  # type: ignore[method-assign]
    monkeypatch.setattr(
        orchestrator_module, "sync_complete", lambda results, missed, client: not results[0]["incomplete"]
    )

    # Nacht 1: abgebrochen, Fortsetzung gespeichert
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=True)
    stand = speicher.sync_config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY]
    assert stand[MEETING]["done"] is True and stand[PAPER]["page"] == f"{PAPER}?page=3"

    # Nacht 2: Der Lauf bekommt den Stand samt Sicherheitsabstand, wird vollständig, die Fortsetzung endet
    ergebnis["incomplete"] = []
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=True)
    zweite = erhalten[1]
    assert zweite is not None and zweite.margin == timedelta(minutes=30)
    how, entry = zweite.plan(PAPER)
    assert how == RESUME and entry is not None and entry.page == f"{PAPER}?page=3"
    assert SYNC_CONFIG_FULL_SYNC_RESUME_KEY not in speicher.sync_config

    # Änderungsabgleich: keine Fortsetzung
    await orchestrator.sync_body_url(KOMMUNENLISTE, full=False)
    assert erhalten[2] is None


async def test_nachfuehrung_liest_alle_gefilterten_seiten_und_uebernimmt_alles() -> None:
    """
    Prüfbefund (CodeRabbit): Die Nachführung einer gelesenen Liste bricht nicht nach fünf Seiten ohne Änderung ab und
    übernimmt jedes gelieferte Objekt, auch mit gleichem ``modified`` (Korrektur am selben Tag).
    """
    jetzt = datetime.now(UTC).replace(microsecond=0)
    quelle = _koeln(jetzt - timedelta(days=30))
    stempel = jetzt - timedelta(hours=1)
    quelle.listen["/oparl/body/1/meeting"] = [
        {
            "id": f"{BASE}/meeting/{i}",
            "type": "https://schema.oparl.org/1.1/Meeting",
            "name": f"Sitzung {i}" + (" (korrigiert)" if i == 13 else ""),
            "modified": stempel.isoformat(),
        }
        for i in range(14)
    ]
    speicher = FortsetzungsSpeicher()
    # Alle Sitzungen schon mit diesem Stand gespeichert: inkrementell sähe der Lauf nur unveränderte Seiten
    speicher.modified.update({f"{BASE}/meeting/{i}": stempel for i in range(14)})
    speicher.config[SYNC_CONFIG_FULL_SYNC_RESUME_KEY] = {
        MEETING: ResumeEntry(at=jetzt - timedelta(days=1), done=True).to_entry(),
        PAPER: ResumeEntry(at=jetzt - timedelta(days=1), page=f"{PAPER}?page=4").to_entry(),
    }

    stats, _run = await _nacht(quelle, speicher)

    assert stats["incomplete"] == []
    assert _anfragen(quelle, "meeting") == [(n, True) for n in range(1, 8)]
    assert stats["meetings"] == 14
