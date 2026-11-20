# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenabgleich mit der Schnittstelle der Kommune (Issue #1525): ``totalElements`` der Hauptlisten gegen den Bestand.

- Erste Seite je Liste über den Client des Ingestors, ein Versuch je Liste, ``skip_lists`` und vermerkte fehlende
  Listen ausgelassen, Fehler als Befund.
- Kein Abruf außerhalb des Abruffensters, in der Schonung oder bei Scraper-Quellen.
- Ablage je Kommune, mit ``--kommune`` zu den übrigen dazu.
- Daemon: höchstens einmal täglich je Quelle, auch wenn der Abgleich scheitert.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from src.client.oparl_client import FetchResult
from src.sync import datenabgleich

JETZT = datetime(2026, 11, 20, 14, tzinfo=UTC)
BASIS = "https://ris.example.org/oparl/v1.1/body/1"


class Client:
    """Nachbildung des OParlClient: Antworten je Adresse, merkt Abrufe und Optionen."""

    def __init__(self, antworten: Mapping[str, FetchResult], optionen: dict[str, Any]) -> None:
        self.antworten = antworten
        self.optionen = optionen
        self.abrufe: list[str] = []
        self.max_retries = 3

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
        assert use_cache is False
        self.abrufe.append(url)
        return self.antworten.get(url) or FetchResult(url=url, data=None, status_code=404, error="HTTP 404")


def _seite(total: int | None) -> FetchResult:
    pagination = {} if total is None else {"totalElements": total, "elementsPerPage": 25}
    return FetchResult(url="", data={"data": [], "pagination": pagination, "links": {}}, status_code=200)


@pytest.fixture
def lauf(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Bestand und Ablage ohne Datenbank; ``lauf.clients`` sammelt die erzeugten Clients."""
    zustand = SimpleNamespace(clients=[], abgelegt=[], kommunen=[], bestand={})

    async def kommunen(_storage: Any, _source_id: Any) -> list[Mapping[str, Any]]:
        return list(zustand.kommunen)

    async def bestand(_storage: Any, body_id: Any) -> dict[str, tuple[int, int]]:
        return dict(zustand.bestand.get(body_id, dict.fromkeys(datenabgleich.LISTEN, (0, 0))))

    async def speichern(_storage: Any, _source: Any, eintraege: dict[str, Any], *, ersetzen: bool) -> None:
        zustand.abgelegt.append((eintraege, ersetzen))

    monkeypatch.setattr(datenabgleich, "_kommunen", kommunen)
    monkeypatch.setattr(datenabgleich, "_bestand", bestand)
    monkeypatch.setattr(datenabgleich, "_speichern", speichern)

    def fabrik(antworten: Mapping[str, FetchResult]) -> Any:
        def neu(**optionen: Any) -> Client:
            client = Client(antworten, optionen)
            zustand.clients.append(client)
            return client

        return neu

    zustand.fabrik = fabrik
    return zustand


def _quelle(**felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "id": uuid4(),
        "name": "Beispielstadt",
        "url": "https://ris.example.org/oparl/v1.1/system",
        "user_agent": None,
        "sync_config": {},
        "is_active": True,
        "consecutive_failures": 0,
        "last_error_at": None,
        "last_error_kind": None,
    }
    werte.update(felder)
    return SimpleNamespace(**werte)


def _kommune(nummer: int = 1, **raw: Any) -> dict[str, Any]:
    basis = f"https://ris.example.org/oparl/v1.1/body/{nummer}"
    listen = {
        "paper": f"{basis}/paper",
        "meeting": f"{basis}/meeting",
        "consultation": f"{basis}/consultation",
        "agendaItem": f"{basis}/agendaitem",
        "file": f"{basis}/file",
    }
    return {"id": uuid4(), "external_id": basis, "name": f"Kommune {nummer}", "raw_json": {**listen, **raw}}


async def test_erste_seite_je_liste_gegen_den_bestand(lauf: SimpleNamespace) -> None:
    kommune = _kommune()
    lauf.kommunen = [kommune]
    lauf.bestand = {
        kommune["id"]: {
            "paper": (1835, 1900),
            "meeting": (900, 900),
            "consultation": (10, 10),
            "agendaitem": (0, 0),
            "file": (7, 7),
        }
    }
    quelle = _quelle(
        user_agent="vereinbart/1.0",
        sync_config={"skip_lists": ["file"], "missing_lists": {f"{BASIS}/agendaitem": {"reason": "HTTP 404"}}},
    )
    antworten = {
        f"{BASIS}/paper": _seite(64982),
        f"{BASIS}/meeting": _seite(None),
        f"{BASIS}/consultation": FetchResult(url="", data=None, status_code=504, error="HTTP 504", timed_out=True),
    }

    ergebnis = await datenabgleich.gleiche_ab(object(), quelle, now=JETZT, client_factory=lauf.fabrik(antworten))

    (client,) = lauf.clients
    assert client.abrufe == [f"{BASIS}/paper", f"{BASIS}/meeting", f"{BASIS}/consultation"], "skip/vermerkt aus"
    assert client.max_retries == 1
    assert client.optionen["user_agent"] == "vereinbart/1.0"
    assert client.optionen["max_concurrent"] == 1
    assert ergebnis.abrufe == 3
    assert ergebnis.kommunen == {
        BASIS: {
            "at": "2026-11-20T14:00:00+00:00",
            "name": "Kommune 1",
            "listen": {
                "paper": {"quelle": 64982, "bestand": 1835, "mit_geloeschten": 1900},
                "meeting": {"quelle": None, "bestand": 900, "mit_geloeschten": 900},
                "consultation": {"fehler": "HTTP 504", "bestand": 10, "mit_geloeschten": 10},
            },
        }
    }
    assert lauf.abgelegt == [(ergebnis.kommunen, True)]


async def test_more_rubin_ohne_beratungs_und_top_liste(lauf: SimpleNamespace) -> None:
    """OParl 1.0 (more! rubin): Der Body nennt nur Vorlagen und Sitzungen."""
    kommune = {
        "id": uuid4(),
        "external_id": "https://x.gremien.info/oparl/body/1",
        "name": "Rubin",
        "raw_json": {
            "paper": "https://x.gremien.info/oparl/body/1/paper",
            "meeting": "https://x.gremien.info/oparl/body/1/meeting",
        },
    }
    lauf.kommunen = [kommune]

    ergebnis = await datenabgleich.gleiche_ab(object(), _quelle(), now=JETZT, client_factory=lauf.fabrik({}))

    assert set(ergebnis.kommunen["https://x.gremien.info/oparl/body/1"]["listen"]) == {"paper", "meeting"}


@pytest.mark.parametrize(
    ("felder", "hinweis"),
    [
        ({"sync_config": {"sync_window": {"start": "23:00", "end": "01:00"}}}, "außerhalb des Abruffensters"),
        ({"consecutive_failures": 5, "last_error_at": JETZT - timedelta(minutes=5)}, "Schonung"),
        ({"sync_config": {"source_type": "scraper:sessionnet"}}, "Scraper-Quelle"),
    ],
)
async def test_kein_abruf_ausserhalb_der_regeln(lauf: SimpleNamespace, felder: dict[str, Any], hinweis: str) -> None:
    lauf.kommunen = [_kommune()]

    ergebnis = await datenabgleich.gleiche_ab(object(), _quelle(**felder), now=JETZT, client_factory=lauf.fabrik({}))

    assert lauf.clients == []
    assert hinweis in ergebnis.hinweis
    assert ergebnis.kommunen == {}
    assert lauf.abgelegt == []


async def test_im_fenster_mit_ende(lauf: SimpleNamespace) -> None:
    """Im Abruffenster (Köln: 23–01 Uhr) gilt sein Ende als Ende des Abrufs."""
    lauf.kommunen = [_kommune()]
    nachts = datetime(2026, 11, 20, 22, 30, tzinfo=UTC)  # 23:30 Uhr Ortszeit
    quelle = _quelle(sync_config={"sync_window": {"start": "23:00", "end": "01:00"}})

    await datenabgleich.gleiche_ab(object(), quelle, now=nachts, client_factory=lauf.fabrik({}))

    assert lauf.clients[0].optionen["fetch_deadline"] == datetime(2026, 11, 21, 0, 0, tzinfo=UTC)


async def test_dateien_mehrerer_kommunen_nur_als_hinweis(lauf: SimpleNamespace) -> None:
    """Eine Datei steht im Bestand nur bei einer Kommune der Quelle: kein Alarm aus der Dateiliste."""
    lauf.kommunen = [_kommune(1), _kommune(2)]
    antworten = {f"https://ris.example.org/oparl/v1.1/body/{n}/file": _seite(10) for n in (1, 2)}

    ergebnis = await datenabgleich.gleiche_ab(
        object(), _quelle(), kommune="kommune 1", now=JETZT, client_factory=lauf.fabrik(antworten)
    )

    assert ergebnis.kommunen[BASIS]["listen"]["file"]["nur_hinweis"] is True
    assert "nur_hinweis" not in ergebnis.kommunen[BASIS]["listen"]["paper"]


async def test_abruf_endet_mit_dem_fenster(lauf: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wartet ein Abruf (Takt, Weiterleitung) über das Fensterende hinaus, endet der Abgleich als Teilabbruch."""
    import asyncio

    from src.client.source_options import SourceFetchOptions

    class Langsam(Client):
        async def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
            await asyncio.sleep(5)
            return await super().fetch(url, use_cache)

    def fabrik(**optionen: Any) -> Client:
        client = Langsam({}, optionen)
        lauf.clients.append(client)
        return client

    ende = datetime.now(UTC) + timedelta(seconds=0.2)
    monkeypatch.setattr(SourceFetchOptions, "fetch_deadline", lambda self, now=None, full_deadline=None: ende)
    lauf.kommunen = [_kommune()]

    ergebnis = await datenabgleich.gleiche_ab(object(), _quelle(), now=JETZT, client_factory=fabrik)

    assert lauf.clients[0].optionen["fetch_deadline"] == ende
    assert ergebnis.abrufe == 0
    assert "Abruffenster endete" in ergebnis.hinweis
    assert ergebnis.kommunen[BASIS]["abgebrochen"] == "Abruffenster endete"
    assert lauf.abgelegt == [(ergebnis.kommunen, False)], "Teilergebnis ersetzt die übrigen Kommunen nicht"


async def test_kommune_filtert_und_ergaenzt(lauf: SimpleNamespace) -> None:
    lauf.kommunen = [_kommune(1), _kommune(2)]

    ergebnis = await datenabgleich.gleiche_ab(
        object(), _quelle(), kommune="kommune 2", now=JETZT, client_factory=lauf.fabrik({})
    )

    assert list(ergebnis.kommunen) == ["https://ris.example.org/oparl/v1.1/body/2"]
    assert lauf.abgelegt[0][1] is False, "zu den übrigen dazu"


def test_faellig_hoechstens_taeglich() -> None:
    gestern = (JETZT - timedelta(hours=24)).isoformat()
    heute = (JETZT - timedelta(hours=2)).isoformat()

    assert datenabgleich.faellig(_quelle(), JETZT)
    assert datenabgleich.faellig(_quelle(sync_config={datenabgleich.SCHLUESSEL: {BASIS: {"at": gestern}}}), JETZT)
    assert not datenabgleich.faellig(_quelle(sync_config={datenabgleich.SCHLUESSEL: {BASIS: {"at": heute}}}), JETZT)
    assert not datenabgleich.faellig(_quelle(is_active=False), JETZT)
    assert not datenabgleich.faellig(_quelle(sync_config={"source_type": "scraper:allris3"}), JETZT)


def test_total_elements() -> None:
    assert datenabgleich.total_elements({"pagination": {"totalElements": 12}}) == 12
    assert datenabgleich.total_elements({"pagination": {"totalElements": "12"}}) is None
    assert datenabgleich.total_elements({"pagination": {"totalElements": True}}) is None
    assert datenabgleich.total_elements({"data": []}) is None
    assert datenabgleich.total_elements(None) is None


async def test_daemon_versucht_hoechstens_taeglich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheitert der Abgleich, zählt der Versuch trotzdem: kein erneuter Versuch nach jedem Lauf."""
    from src.scheduler import SyncScheduler

    versuche: list[Any] = []

    async def scheitert(_storage: Any, source: Any, _now: Any = None) -> None:
        versuche.append(source.id)

    monkeypatch.setattr(datenabgleich, "im_daemon", scheitert)
    uhr = SimpleNamespace(jetzt=JETZT)
    planer = SyncScheduler(clock=lambda: uhr.jetzt, orchestrator=SimpleNamespace(storage=object()), heartbeat=False)
    quelle = _quelle()

    await planer._datenabgleich(quelle)
    await planer._datenabgleich(quelle)
    uhr.jetzt += timedelta(hours=12)
    await planer._datenabgleich(quelle)
    assert versuche == [quelle.id]

    uhr.jetzt += timedelta(hours=12)
    await planer._datenabgleich(quelle)
    assert versuche == [quelle.id, quelle.id]


async def test_daemon_faengt_fehler_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    async def kaputt(*_: Any, **__: Any) -> Any:
        raise RuntimeError("Datenbank weg")

    monkeypatch.setattr(datenabgleich, "gleiche_ab", kaputt)

    assert await datenabgleich.im_daemon(object(), _quelle(), JETZT) is None


def test_befehl_zeigt_die_tabelle(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from rich.console import Console
    from typer.testing import CliRunner

    from src import main as cli

    quelle = _quelle()
    aufrufe: list[dict[str, Any]] = []

    class Speicher:
        async def get_all_sources(self) -> list[SimpleNamespace]:
            return [quelle]

    class FakeOrchestrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = Speicher()

        async def __aenter__(self) -> FakeOrchestrator:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def gleiche_ab(_storage: Any, source: Any, **optionen: Any) -> datenabgleich.Ergebnis:
        aufrufe.append(optionen)
        return datenabgleich.Ergebnis(
            quelle=source.name,
            abrufe=2,
            kommunen={
                BASIS: {
                    "at": "2026-11-20T14:00:00+00:00",
                    "name": "Kommune [1]",
                    "listen": {"paper": {"quelle": 64982, "bestand": 1835}, "meeting": {"fehler": "HTTP 504"}},
                }
            },
        )

    ausgabe = io.StringIO()
    monkeypatch.setattr(cli, "SyncOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli, "console", Console(file=ausgabe, width=200))
    monkeypatch.setattr(datenabgleich, "gleiche_ab", gleiche_ab)

    result = CliRunner().invoke(cli.app, ["datenabgleich", "--kommune", "1", "--nur-anzeigen"])

    assert result.exit_code == 0, result.output
    assert aufrufe == [{"kommune": "1", "speichern": False}]
    text = ausgabe.getvalue()
    zeile = next(z for z in text.splitlines() if "Kommune [1]" in z)
    assert "64982 / 1835" in zeile
    assert "HTTP 504" in zeile
    assert "2 Abrufe; nicht abgelegt" in text
