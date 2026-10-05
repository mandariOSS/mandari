# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Nicht erreichbar ist nicht gelöscht“ (Issue #556).

Eine Löschmarkierung entsteht nur aus einem Löschsignal der Quelle (OParl ``deleted: true``) oder, bei
Scraper-Quellen ohne Löschsignal, aus dem Fehlen nach einem vollständig erfolgreichen Vollabgleich. Je
Fehlerfall ein Test, dass daraus keine Markierung folgt: nicht erreichbar, Zeitüberschreitung, 4xx, 5xx,
Sperr- oder Bot-Schutz-Seite, Hinweisseite, abgebrochene Paginierung (Detailseiten-Budget, Folgeseite ohne
Liste) und Teilantwort (Seite einer Sitzung nicht lesbar). Dazu die Bremse und die Aktualität je Quelle.

Scraper-Läufe gehen durch den echten ``PoliteFetcher`` (MockTransport) und den SessionNet-Adapter gegen
die Fixtures der Samtgemeinde-Instanz; nur Speicher und Orchestrator sind Attrappen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

import httpx
import pytest

from src.client.oparl_client import ListFetchError, OParlClient
from src.config import settings
from src.scrapers import politeness
from src.scrapers import runner as runner_module
from src.scrapers.politeness import PoliteFetcher
from src.scrapers.runner import ScraperSyncRunner
from src.scrapers.sessionnet import is_calendar_page, is_sessionnet_page, parse_calendar
from src.storage.database import DatabaseStorage
from src.sync.orchestrator import SyncOrchestrator, sync_complete
from src.sync.processor import OParlProcessor
from tests.test_sessionnet_mehrere_koerperschaften import (
    BASE_URL,
    FIXTURES,
    SEPTEMBER,
    FakeOrchestrator,
    FakeSource,
    SamtgemeindeFetcher,
    body_url,
    make_adapter,
)

KOERPERSCHAFT_1 = uuid5(UUID(int=0), body_url(1))
GREMIUM_1 = f"{BASE_URL}kp0040.asp?__kgrnr=1"
#: Gremium im Bestand, das die Quelle nicht mehr führt
GREMIUM_ALT = f"{BASE_URL}kp0040.asp?__kgrnr=99"
#: Sitzung im Fenster, die der Kalender nicht mehr nennt
SITZUNG_ALT = f"{BASE_URL}si0057.asp?__ksinr=1999"
SITZUNG_1003 = f"{BASE_URL}si0057.asp?__ksinr=1003"

#: Prüfseite eines Bot-Schutzes, mit Status 200 ausgeliefert
SPERRSEITE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>"
)
#: Hinweisseite ohne SessionNet-Aufbau (Wartung), ebenfalls mit Status 200
HINWEISSEITE = "<html><body><h1>Wartungsarbeiten</h1><p>Bitte später erneut versuchen.</p></body></html>"
#: Proof-of-Work-Prüfseite (Status 200), die die angefragte Adresse samt üblichem Installationspfad in ihr
#: Formular übernimmt: Der Produktname steht darin, das SessionNet-Layout fehlt
SPERRSEITE_MIT_PFAD = (
    "<!DOCTYPE html><html><head><title>Sicherheitsprüfung</title></head><body>"
    "<form method='post' action='/sessionnet/sessionnetbi/si0040.php?__cjahr=2026&amp;__cmonat=9'>"
    "<altcha-widget challengeurl='/altcha/challenge'></altcha-widget><button>Weiter</button></form>"
    "</body></html>"
)
#: Hinweisseite im SessionNet-Layout ohne den Inhalt der angefragten Seite (Status 200)
SESSIONNET_HINWEISSEITE = (
    "<!DOCTYPE html><html><head><title>SessionNet | Hinweis</title></head>"
    "<body id='smc_body' class='smc-body'><div id='page-content'><p class='smc-hinweis'>"
    "Die Anwendung ist zurzeit nicht verfügbar.</p></div></body></html>"
)

#: Antworten mit Status 200, die keine lesbare Seite sind (der PoliteFetcher liefert sie aus)
SEITEN_MIT_STATUS_200 = {
    "sperrseite": SPERRSEITE,
    "sperrseite_mit_pfad": SPERRSEITE_MIT_PFAD,
    "hinweisseite": HINWEISSEITE,
    "sessionnet_hinweisseite": SESSIONNET_HINWEISSEITE,
}

FEHLERFAELLE = [
    "nicht_erreichbar",
    "zeitueberschreitung",
    "401",
    "403",
    "404",
    "410",
    "429",
    "500",
    "503",
    "sperrseite",
    "sperrseite_mit_pfad",
    "hinweisseite",
    "sessionnet_hinweisseite",
]


def _antwort(fall: str, request: httpx.Request) -> httpx.Response:
    if fall == "nicht_erreichbar":
        raise httpx.ConnectError("Verbindung abgelehnt", request=request)
    if fall == "zeitueberschreitung":
        raise httpx.ReadTimeout("keine Antwort", request=request)
    if fall in SEITEN_MIT_STATUS_200:
        return httpx.Response(200, text=SEITEN_MIT_STATUS_200[fall])
    return httpx.Response(int(fall), text="<html><body>Fehler</body></html>")


def _seite(name: str, cpanr: str | None = None) -> Callable[[httpx.Request], bool]:
    def passt(request: httpx.Request) -> bool:
        if not request.url.path.endswith(f"/{name}.asp"):
            return False
        return cpanr is None or request.url.params.get("__cpanr") == cpanr

    return passt


# ---------------------------------------------------------------------------
# Scraper-Lauf mit echtem PoliteFetcher
# ---------------------------------------------------------------------------


class Quelle(FakeSource):
    """Samtgemeinde-Quelle ohne Drossel; ``scraper`` ergänzt die Konfiguration."""

    def __init__(self, scraper_state: dict[str, Any] | None = None, **scraper: Any) -> None:
        super().__init__(scraper_state)
        self.sync_config["scraper"].update({"rate_limit_seconds": 0, **scraper})


class Orchestrator(FakeOrchestrator):
    """Wie im Läufer-Test; hält Fehler der Quelle fest, statt den Test abzubrechen."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[str] = []

    async def _record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.failures.append(error)


@pytest.fixture
def abruf(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Echter PoliteFetcher gegen die Fixtures. ``abruf["stoerung"] = (passt, fall)`` ersetzt die Antworten
    der passenden Seiten durch den Fehlerfall, ``abruf["antwort"]`` (optional) durch eine eigene Antwort.
    """
    zustand: dict[str, Any] = {"stoerung": None, "antwort": None}
    seiten = SamtgemeindeFetcher()

    async def handler(request: httpx.Request) -> httpx.Response:
        stoerung = zustand["stoerung"]
        if stoerung is not None and stoerung[0](request):
            if zustand["antwort"] is not None:
                return zustand["antwort"](request)
            return _antwort(stoerung[1], request)
        text = await seiten.fetch_text(str(request.url))
        return httpx.Response(200, text=text) if text is not None else httpx.Response(404)

    class Abruf(PoliteFetcher):
        async def __aenter__(self) -> Abruf:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
            return self

    class Fenster(runner_module.CrawlWindow):
        @classmethod
        def from_days(cls, days_back: int, days_ahead: int, today: Any = None) -> runner_module.CrawlWindow:
            return SEPTEMBER

    async def ohne_pause(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner_module, "PoliteFetcher", Abruf)
    monkeypatch.setattr(runner_module, "CrawlWindow", Fenster)
    # Wiederholungen nach 5xx nicht abwarten (nur im Modul des Abrufs, nicht global)
    monkeypatch.setattr(politeness, "asyncio", SimpleNamespace(sleep=ohne_pause, Lock=asyncio.Lock))
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "scraper_tombstone_full_crawls", 1)
    monkeypatch.setattr(settings, "scraper_tombstone_max_missing", 10)
    return zustand


def _mit_bestand(orchestrator: Orchestrator) -> Orchestrator:
    """Der Bestand kennt je ein Gremium und eine Sitzung, die die Quelle nicht mehr nennt."""
    orchestrator.storage.active[("organization", KOERPERSCHAFT_1)] = {GREMIUM_1, GREMIUM_ALT}
    orchestrator.storage.active[("meeting", KOERPERSCHAFT_1)] = {SITZUNG_1003, SITZUNG_ALT}
    return orchestrator


async def _lauf(quelle: Quelle | None = None, *, full: bool = True) -> tuple[Orchestrator, Any]:
    orchestrator = _mit_bestand(Orchestrator())
    result = await ScraperSyncRunner(orchestrator, quelle or Quelle()).run(full=full)  # type: ignore[arg-type]
    return orchestrator, result


async def test_vollstaendiger_vollabgleich_markiert_was_fehlt(abruf: dict[str, Any]) -> None:
    orchestrator, result = await _lauf()

    assert result.success, result.errors
    assert sorted(orchestrator.marked) == sorted([GREMIUM_ALT, SITZUNG_ALT])
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["complete"] is True
    assert last_run["failed_pages"] == 0
    assert last_run["incomplete"] == {}
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": True}]


@pytest.mark.parametrize("fall", FEHLERFAELLE)
async def test_kalender_nicht_lesbar_markiert_keine_sitzung(abruf: dict[str, Any], fall: str) -> None:
    abruf["stoerung"] = (_seite("si0040", cpanr="1"), fall)

    orchestrator, result = await _lauf()

    assert SITZUNG_ALT not in orchestrator.marked
    assert SITZUNG_1003 not in orchestrator.marked
    # Die Gremienliste kam vollständig: Dort schließt der Abgleich weiter
    assert GREMIUM_ALT in orchestrator.marked
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["complete"] is False
    assert last_run["incomplete"]["meeting"].startswith("Kalender 09/2026")
    assert last_run["failed_pages"] >= 1
    # Durchgelaufen, aber nicht vollständig: last_sync ja, Aktualität nein
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": False}]
    assert result.success


@pytest.mark.parametrize("fall", FEHLERFAELLE)
async def test_gremienliste_nicht_lesbar_markiert_kein_gremium(abruf: dict[str, Any], fall: str) -> None:
    abruf["stoerung"] = (_seite("gr0040", cpanr="1"), fall)

    orchestrator, _result = await _lauf()

    assert GREMIUM_ALT not in orchestrator.marked
    assert SITZUNG_ALT in orchestrator.marked
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["complete"] is False
    assert "organization" in last_run["incomplete"]
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": False}]


@pytest.mark.parametrize("fall", ["500", "nicht_erreichbar", "sperrseite", "sperrseite_mit_pfad"])
async def test_teilantwort_sitzungsseite_nicht_lesbar(abruf: dict[str, Any], fall: str) -> None:
    """Der Kalender nennt die Sitzung, ihre Seite ist nicht lesbar: Es gibt sie, sie fehlt nicht."""
    abruf["stoerung"] = (lambda r: r.url.path.endswith("/si0057.asp") and r.url.params["__ksinr"] == "1003", fall)

    orchestrator, _result = await _lauf()

    assert SITZUNG_1003 not in orchestrator.marked
    # Die Liste selbst war vollständig: die verschwundene Sitzung wird markiert
    assert SITZUNG_ALT in orchestrator.marked
    assert orchestrator.storage.state["last_run"]["complete"] is False
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": False}]


async def test_abgebrochene_paginierung_durch_budget(abruf: dict[str, Any]) -> None:
    """Endet der Crawl am Detailseiten-Budget, bleiben spätere Sitzungen ungesehen: kein Schluss daraus."""
    orchestrator, _result = await _lauf(Quelle(max_detail_pages=1))

    assert SITZUNG_ALT not in orchestrator.marked
    assert SITZUNG_1003 not in orchestrator.marked
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["incomplete"]["meeting"] == "Detailseiten-Budget erreicht"
    assert last_run["complete"] is False


async def test_zaehler_bleiben_bei_luecke_stehen(abruf: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein unvollständiger Lauf zählt weder hoch noch setzt er zurück."""
    monkeypatch.setattr(settings, "scraper_tombstone_full_crawls", 3)
    abruf["stoerung"] = (_seite("si0040"), "503")
    zaehler = {"meeting": {SITZUNG_ALT: 2}, "organization": {GREMIUM_ALT: 1}}

    orchestrator, _result = await _lauf(Quelle(scraper_state={"missing": zaehler}))

    assert orchestrator.marked == []
    missing = orchestrator.storage.state["missing"]
    assert missing["meeting"] == {SITZUNG_ALT: 2}  # unverändert, nicht 3
    assert missing["organization"] == {GREMIUM_ALT: 2}  # Gremienliste vollständig: zählt weiter


async def test_bremse_bei_massenhaft_fehlenden(abruf: dict[str, Any]) -> None:
    orchestrator = Orchestrator()
    alte = {f"{BASE_URL}kp0040.asp?__kgrnr={900 + n}" for n in range(11)}
    orchestrator.storage.active[("organization", KOERPERSCHAFT_1)] = {GREMIUM_1, *alte}
    orchestrator.storage.active[("meeting", KOERPERSCHAFT_1)] = {SITZUNG_ALT}

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert not alte & set(orchestrator.marked)
    assert orchestrator.marked == [SITZUNG_ALT]  # andere Typen bleiben unberührt
    assert orchestrator.storage.state["last_run"]["tombstone_braked"] == {"organization": 11}


async def test_abgebrochener_crawl_ist_kein_abgleich(abruf: dict[str, Any]) -> None:
    """Bricht der Crawl ab, markiert er nichts und gilt nicht als Abgleich (Fehlerstatus bleibt stehen)."""
    orchestrator = _mit_bestand(Orchestrator())

    async def bricht_ab(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("Speicher nicht erreichbar")

    orchestrator._store_entity = bricht_ab  # type: ignore[method-assign]
    result = await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert result.success is False
    assert orchestrator.marked == []
    assert orchestrator.failures and orchestrator.failures[0].startswith("Crawl-Fehler")
    assert orchestrator.storage.source_syncs == []
    assert orchestrator.storage.synced_bodies == []


async def test_inkrementeller_lauf_markiert_nie(abruf: dict[str, Any]) -> None:
    orchestrator, _result = await _lauf(full=False)

    assert orchestrator.marked == []
    assert orchestrator.storage.source_syncs == [{"full": False, "complete": True}]


#: Vorlagenseite im SessionNet-Layout ohne Betreff: nicht auswertbar, aber keine Störung
VORLAGE_OHNE_BETREFF = "<html><body class='smc-body'><div id='page-content'></div></body></html>"


@pytest.mark.parametrize(
    ("fall", "vollstaendig"),
    [
        ("404", True),
        ("410", True),
        ("ohne_betreff", True),
        ("403", False),
        ("500", False),
        ("nicht_erreichbar", False),
        ("sperrseite_mit_pfad", False),
    ],
)
async def test_toter_verweis_auf_vorlage_ist_keine_stoerung(
    abruf: dict[str, Any], fall: str, vollstaendig: bool
) -> None:
    """
    Nennt eine Tagesordnung eine Vorlage, deren Seite die Quelle selbst mit 404/410 beantwortet (toter
    Verweis) oder die keinen Betreff trägt, hat der Lauf trotzdem alles gelesen, was die Quelle anbietet:
    Die Aktualität rückt vor. Eine gestörte Vorlagenseite (Fehler, Sperrseite) lässt sie stehen.
    """
    vorlage_5001 = f"{BASE_URL}vo0050.asp?__kvonr=5001"

    def antwort(request: httpx.Request) -> httpx.Response:
        if fall == "ohne_betreff":
            return httpx.Response(200, text=VORLAGE_OHNE_BETREFF)
        return _antwort(fall, request)

    abruf["stoerung"] = (lambda r: str(r.url) == vorlage_5001, fall)
    abruf["antwort"] = antwort

    orchestrator, result = await _lauf()

    last_run = orchestrator.storage.state["last_run"]
    assert result.success, result.errors
    assert last_run["complete"] is vollstaendig
    assert last_run["incomplete"] == {}
    assert last_run["gone_pages"] == (1 if fall in ("404", "410") else 0)
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": vollstaendig}]
    # Der Löschabgleich hängt nicht daran: Listen vollständig, die Vorlage zählt als gesehen
    assert sorted(orchestrator.marked) == sorted([GREMIUM_ALT, SITZUNG_ALT])


# ---------------------------------------------------------------------------
# Erkennung lesbarer SessionNet-Seiten
# ---------------------------------------------------------------------------


def test_fixtures_sind_sessionnet_seiten() -> None:
    seiten = sorted(FIXTURES.parent.glob("*/*.html"))
    assert seiten
    for seite in seiten:
        assert is_sessionnet_page(seite.read_text(encoding="utf-8")), seite.name
    kalender = sorted(FIXTURES.parent.glob("*/si0040*.html"))
    assert kalender
    for seite in kalender:
        assert is_calendar_page(seite.read_text(encoding="utf-8")), seite.name


@pytest.mark.parametrize("fall", sorted(SEITEN_MIT_STATUS_200))
def test_sperr_und_hinweisseiten_sind_kein_kalender(fall: str) -> None:
    html = SEITEN_MIT_STATUS_200[fall]
    assert not is_calendar_page(html)
    assert is_sessionnet_page(html) is (fall == "sessionnet_hinweisseite")


def test_monat_ohne_sitzungen_ist_lesbarer_kalender() -> None:
    """Ein Monat ohne Sitzungen hat die Tageszeilen ohne Eintrag: lesbar, die Liste ist leer."""
    tage = "".join(
        f"<tr><td class='smc-t-cn991 smc_fct_day'><span class='weekday'>{tag}</span></td>"
        "<td data-label='Sitzung' class='smc-t-cn991 silink'></td></tr>"
        for tag in range(1, 31)
    )
    html = (
        "<html><body class='smc-body'><table id='smc_page_si0040_contenttable1' class='smc-table'>"
        f"<tbody>{tage}</tbody></table></body></html>"
    )
    assert is_sessionnet_page(html)
    assert is_calendar_page(html)
    assert parse_calendar(html) == []


async def test_variante_nicht_an_sperrseite_erkannt() -> None:
    """Eine Prüfseite unter dem .asp-Pfad nennt „sessionnet“, ist aber keine Seite der Instanz."""

    class Abruf(SamtgemeindeFetcher):
        async def fetch_text(self, url: str) -> str | None:
            self.requests.append(url)
            if url.endswith("si0040.asp"):
                return SPERRSEITE_MIT_PFAD
            return (FIXTURES / "si0040_cpanr1_2026-09.html").read_text(encoding="utf-8")

    adapter = make_adapter(None, Abruf())
    adapter.config.variant = None

    await adapter.detect_variant()

    assert adapter.urls.ext == "php"


# ---------------------------------------------------------------------------
# PoliteFetcher: jeder Fehlerfall ist „keine Seite“, nie „leere Seite“
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fall", [f for f in FEHLERFAELLE if f not in SEITEN_MIT_STATUS_200])
async def test_politefetcher_liefert_bei_fehlern_keine_seite(monkeypatch: pytest.MonkeyPatch, fall: str) -> None:
    async def ohne_pause(_seconds: float) -> None:
        return None

    fetcher = PoliteFetcher(rate_limit_seconds=0)
    monkeypatch.setattr(politeness, "asyncio", SimpleNamespace(sleep=ohne_pause, Lock=asyncio.Lock))
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _antwort(fall, r)))
    try:
        assert await fetcher.fetch_text(f"{BASE_URL}si0040.asp") is None
    finally:
        await fetcher._client.aclose()


# ---------------------------------------------------------------------------
# OParl: abgebrochene Paginierung und Teilantworten
# ---------------------------------------------------------------------------

LIST_URL = "https://ris.example.org/oparl/body/1/meeting"
MEETING = "https://ris.example.org/oparl/meeting/{}"


async def _oparl_client(respond: Callable[[httpx.Request], httpx.Response]) -> OParlClient:
    client = OParlClient(max_concurrent=2)
    client._semaphore = asyncio.Semaphore(2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return client


@pytest.fixture
def _oparl_ohne_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(settings, "circuit_breaker_enabled", False)
    OParlClient._modified_since_unsupported.clear()


def _liste_mit_folgeseite(folgeseite: Callable[[httpx.Request], httpx.Response]) -> Callable[..., httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return folgeseite(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": MEETING.format(1), "type": "https://schema.oparl.org/1.1/Meeting", "name": "Rat"},
                    {"id": MEETING.format(2), "deleted": True},
                ],
                "links": {"next": LIST_URL + "?page=2"},
            },
        )

    return respond


TEILANTWORTEN: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "fehlerobjekt": lambda r: httpx.Response(
        200, json={"type": "https://schema.oparl.org/1.1/Error", "message": "Requested class doesn't exist."}
    ),
    "anderes_json": lambda r: httpx.Response(200, json={"status": "blocked"}),
    "null": lambda r: httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
    "leer": lambda r: httpx.Response(200, content=b""),
    "sperrseite": lambda r: httpx.Response(200, text=SPERRSEITE),
    "403": lambda r: httpx.Response(403, text="Forbidden"),
    "500": lambda r: httpx.Response(500),
    "nicht_erreichbar": lambda r: (_ for _ in ()).throw(httpx.ConnectError("weg", request=r)),
}


@pytest.mark.usefixtures("_oparl_ohne_pause")
@pytest.mark.parametrize("fall", sorted(TEILANTWORTEN))
async def test_oparl_folgeseite_ohne_liste_ist_abbruch(fall: str) -> None:
    client = await _oparl_client(_liste_mit_folgeseite(TEILANTWORTEN[fall]))
    pages: list[list[dict[str, Any]]] = []
    try:
        with pytest.raises(ListFetchError) as excinfo:
            async for page in client.fetch_list(LIST_URL):
                pages.append(page)
    finally:
        await client._client.aclose()

    assert len(pages) == 1
    assert excinfo.value.page_url == LIST_URL + "?page=2"
    assert client.host_health["ris.example.org"].failed_lists == [LIST_URL]


@pytest.mark.usefixtures("_oparl_ohne_pause")
async def test_oparl_fehlerobjekt_auf_der_ersten_seite_bleibt_leere_liste() -> None:
    """OParl 1.0: Ein Fehlerobjekt auf der ersten Seite heißt „Liste gibt es hier nicht“, kein Abbruch."""
    client = await _oparl_client(TEILANTWORTEN["fehlerobjekt"])
    try:
        pages = [page async for page in client.fetch_list(LIST_URL)]
    finally:
        await client._client.aclose()
    assert pages == []


@pytest.mark.usefixtures("_oparl_ohne_pause")
@pytest.mark.parametrize("fall", sorted(TEILANTWORTEN))
async def test_oparl_abgleich_markiert_nur_per_loeschsignal(fall: str) -> None:
    """Bricht die Liste ab, markiert der Abgleich nur, was die Quelle als gelöscht meldet."""
    client = await _oparl_client(_liste_mit_folgeseite(TEILANTWORTEN[fall]))
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.processor = OParlProcessor()
    marked: list[str] = []
    stored: list[str] = []

    async def mark(item: dict[str, Any], entity_type: str, es_deletions: Any) -> bool:
        marked.append(item["id"])
        return True

    async def store(processed: Any, *args: Any) -> bool:
        stored.append(processed.external_id)
        return True

    orchestrator._mark_deleted = mark  # type: ignore[method-assign]
    orchestrator._store_entity = store  # type: ignore[method-assign]
    try:
        with pytest.raises(ListFetchError):
            await orchestrator._sync_entity_type(
                client=client,
                list_url=LIST_URL,
                entity_type="meeting",
                body_id=UUID(int=7),
                body_external_id="https://ris.example.org/oparl/body/1",
                body_name="Beispielstadt",
                full=True,
                es_deletions={},
            )
    finally:
        await client._client.aclose()

    assert marked == [MEETING.format(2)]  # nur das Löschsignal
    assert stored == [MEETING.format(1)]


# ---------------------------------------------------------------------------
# Vollständigkeit und Aktualität je Quelle
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, findings: list[Any] | None = None) -> None:
        self._findings = findings or []

    def host_findings(self) -> list[Any]:
        return self._findings


def test_vollstaendig_nur_ohne_luecke() -> None:
    ganz = [{"errors": []}, {"errors": ["Text extraction: x"], "incomplete": []}]
    assert sync_complete(ganz, [], _Client()) is True  # Fehler der Texterkennung zählen nicht
    assert sync_complete([{"incomplete": ["paper"]}], [], _Client()) is False
    assert sync_complete([{"errors": ["boom"], "incomplete": ["body"]}], [], _Client()) is False
    assert sync_complete(ganz, ["https://ris.example.org/oparl/body/2"], _Client()) is False
    assert sync_complete(ganz, [], _Client(findings=[object()])) is False


async def test_sync_body_haelt_luecken_je_typ_fest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.processor = OParlProcessor()
    orchestrator._parallel_mode = True
    body_id = UUID(int=9)

    class Speicher:
        async def upsert_body(self, processed: Any, source_id: Any) -> UUID:
            return body_id

        async def get_body_last_sync(self, _body_id: UUID) -> None:
            return None

        async def update_body_sync_time(self, _body_id: UUID) -> None:
            return None

    async def entity_sync(**kwargs: Any) -> int:
        if kwargs["entity_type"] == "paper":
            raise ListFetchError(kwargs["list_url"] or "", "", SimpleNamespace(error="HTTP 500", error_kind=None))  # type: ignore[arg-type]
        return 0

    orchestrator.storage = Speicher()  # type: ignore[assignment]
    orchestrator._sync_entity_type = entity_sync  # type: ignore[method-assign]
    body = {
        "id": "https://ris.example.org/oparl/body/1",
        "type": "https://schema.oparl.org/1.1/Body",
        "name": "Beispielstadt",
        "paper": "https://ris.example.org/oparl/body/1/paper",
        "meeting": "https://ris.example.org/oparl/body/1/meeting",
    }

    stats = await orchestrator._sync_body(client=None, body_data=body, source_id=UUID(int=1), full=True)  # type: ignore[arg-type]

    assert stats["incomplete"] == ["paper"]
    assert sync_complete([stats], [], _Client()) is False


class _Sitzung:
    """Attrappe einer Datenbanksitzung für ``update_source_sync_time``."""

    def __init__(self, source: Any) -> None:
        self.source = source

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self.source

    async def commit(self) -> None:
        return None


def _speicher_mit(source: Any) -> DatabaseStorage:
    storage = DatabaseStorage.__new__(DatabaseStorage)

    def get_session() -> Any:
        class _Kontext:
            async def __aenter__(self) -> _Sitzung:
                return _Sitzung(source)

            async def __aexit__(self, *args: Any) -> None:
                return None

        return _Kontext()

    storage.get_session = get_session  # type: ignore[method-assign]
    return storage


@pytest.mark.parametrize(
    ("full", "complete", "erwartet"),
    [
        (False, False, (True, False, False, False)),
        (True, False, (True, True, False, False)),
        (False, True, (True, False, True, False)),
        (True, True, (True, True, True, True)),
    ],
)
async def test_aktualitaet_nur_nach_vollstaendigem_abgleich(
    full: bool, complete: bool, erwartet: tuple[bool, bool, bool, bool]
) -> None:
    source = SimpleNamespace(
        last_sync=None,
        last_full_sync=None,
        last_successful_sync=None,
        last_successful_full_sync=None,
        last_error="alt",
        last_error_at=None,
        last_error_kind=None,
        consecutive_failures=3,
    )
    await _speicher_mit(source).update_source_sync_time(UUID(int=1), full_sync=full, complete=complete)

    gesetzt = (
        source.last_sync is not None,
        source.last_full_sync is not None,
        source.last_successful_sync is not None,
        source.last_successful_full_sync is not None,
    )
    assert gesetzt == erwartet
    assert source.consecutive_failures == 0
