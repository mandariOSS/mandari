# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Listen, die eine Quelle nicht freigibt (Issue #1086).

Die Stadt Bochum (SD.NET RIM, ``bochum.ratsinfomanagement.net/webservice/oparl/v1.1``) beantwortet ``body/1/person``
und ``body/1/membership`` mit HTTP 403, mit und ohne ``modified_since``, auch für den neutralen Vergleichs-Client
(Produktion, 19.10.2026). Bisher stand die Liste danach als „filtert nicht“ in ``unfiltered_lists``, der Lauf galt
als unvollständig, und das Änderungsfenster rückte nie vor.

Jetzt gilt eine solche Liste als „nicht freigegeben“: vermerkt wie eine fehlende Liste (mit Grund), täglich neu
geprüft, keine Lücke, auch bei Pflichtlisten. Ist nur unser User-Agent gesperrt, bleibt es eine Sperre und eine
Lücke. Antworten aus ``httpx.MockTransport`` (Verhalten der Quelle nachgebildet), keine Abrufe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from src.client.oparl_client import (
    ERROR_KIND_UA_BLOCKED,
    FORBIDDEN_LIST_REASON,
    NEUTRAL_USER_AGENT,
    HostHealth,
    ListFetchError,
    ListMissingError,
    OParlClient,
)
from src.config import settings
from src.storage.database import missing_list_reasons_of, missing_lists_of
from src.sync.orchestrator import MissingLists, TimedLists
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    BASE,
    BODY,
    LISTEN,
    SOURCE,
    Quelle,
    Speicher,
    _kommune,
    _ohne_nebenlaeufe,
    _orchestrator,
)
from tests.test_zeitscheiben import BODY as HAMM_BODY
from tests.test_zeitscheiben import HammQuelle, ScheibenSpeicher
from tests.test_zeitscheiben import _lauf as _lauf_in_scheiben

PERSONEN = f"{BODY}/person"
MITGLIEDSCHAFTEN = f"{BODY}/membership"
PFAD_PERSONEN = "/oparl/body/1/person"
PFAD_MITGLIEDSCHAFTEN = "/oparl/body/1/membership"


@pytest.fixture(autouse=True)
def _vergleichsanfrage_an(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Vergleichsanfrage nach 403 wie im Betrieb; kein Schutzschalter, der die 403 mitzählt."""
    monkeypatch.setattr(settings, "oparl_ua_probe_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_enabled", False)
    yield


class BochumQuelle(Quelle):
    """Personen und Mitgliedschaften: HTTP 403 für jeden Client, mit und ohne Filter (Bochum, 19.10.2026)."""

    def __init__(self, geaendert: datetime) -> None:
        super().__init__(geaendert)
        self.fest[PFAD_PERSONEN] = 403
        self.fest[PFAD_MITGLIEDSCHAFTEN] = 403


class NurUnsGesperrt(Quelle):
    """Personen: HTTP 403 nur für unseren User-Agent, ein neutraler Client bekommt die Liste."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == PFAD_PERSONEN and request.headers.get("User-Agent") != NEUTRAL_USER_AGENT:
            self.anfragen.append(request.url)
            return httpx.Response(403, text="Forbidden")
        return super().__call__(request)


class NeutralWeitergeleitet(Quelle):
    """Personen: HTTP 403 für uns, der neutrale Client wird auf eine Anmeldung weitergeleitet (Sperre unklar)."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == PFAD_PERSONEN:
            self.anfragen.append(request.url)
            if request.headers.get("User-Agent") == NEUTRAL_USER_AGENT:
                return httpx.Response(302, headers={"Location": "https://ris.example.org/anmelden"})
            return httpx.Response(403, text="Forbidden")
        return super().__call__(request)


@asynccontextmanager
async def _client(quelle: Quelle, speicher: Speicher) -> AsyncIterator[OParlClient]:
    """Client mit unserem User-Agent wie im Betrieb (die Vergleichsanfrage sendet den neutralen)."""
    async with OParlClient(max_concurrent=2, wait_time=0, filter_notes=speicher.filtervermerke) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(quelle),
            headers={"Accept": "application/json", "User-Agent": client.user_agent},
        )
        yield client


async def _lauf(
    quelle: Quelle, speicher: Speicher, *, full: bool = False, kommune: dict[str, Any] | None = None
) -> tuple[dict[str, Any], OParlClient]:
    """Abgleich einer Kommune wie ``test_aenderungsfenster._lauf``, Vermerke mit Grund wie aus ``sync_config``."""
    quelle.anfragen.clear()
    async with _client(quelle, speicher) as client:
        stats = await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(membership=MITGLIEDSCHAFTEN),
            source_id=UUID(int=9),
            full=full,
            missing_lists=MissingLists(SOURCE, dict(speicher.fehlend), dict(speicher.gruende)),
            timed_lists=TimedLists(SOURCE, set(speicher.uhrzeit)),
        )
    return stats, client


# ---------------------------------------------------------------------------
# Bochum: Personen und Mitgliedschaften nicht freigegeben
# ---------------------------------------------------------------------------


async def test_bochum_personen_gelten_als_nicht_freigegeben():
    quelle = BochumQuelle(geaendert=datetime.now(UTC) - timedelta(minutes=30))
    speicher = Speicher()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)

    vorher = datetime.now(UTC)
    stats, client = await _lauf(quelle, speicher)

    # Keine Lücke, kein Fehler im Sync-Log: Das Fenster rückt vor
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert speicher.begonnen[BODY] >= vorher
    assert stats["organizations"] == 3 and stats["meetings"] == 3 and stats["papers"] == 3
    # Vermerkt wie fehlende Listen, mit Grund; die Pflichtliste Personen ebenso
    assert stats["missing_lists"] == [MITGLIEDSCHAFTEN, PERSONEN]
    assert set(speicher.fehlend) == {MITGLIEDSCHAFTEN, PERSONEN}
    assert speicher.gruende == {MITGLIEDSCHAFTEN: FORBIDDEN_LIST_REASON, PERSONEN: FORBIDDEN_LIST_REASON}
    # Kein Vermerk „filtert nicht“: Über den Filter sagt ein 403 für alle nichts
    assert speicher.filtervermerke.lists == {}
    assert speicher.filtervermerke.changed_lists.get(PERSONEN) is None
    # Mit Filter, Vergleichsanfrage (einmal je Host), ohne Filter; keine Sperre unseres User-Agents
    zaehler = quelle.anfragen_je_liste()
    assert zaehler["person"] == 3 and zaehler["membership"] == 2
    assert client.error_kind is None and client.host_findings() == []


async def test_nicht_freigegebene_liste_wird_einmal_am_tag_geprueft():
    quelle = BochumQuelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    await _lauf(quelle, speicher)
    assert set(speicher.fehlend) == {MITGLIEDSCHAFTEN, PERSONEN}

    # Innerhalb eines Tages: nicht angefragt, der Lauf bleibt vollständig
    vorher = datetime.now(UTC)
    stats, _ = await _lauf(quelle, speicher)
    zaehler = quelle.anfragen_je_liste()
    assert zaehler["person"] == 0 and zaehler["membership"] == 0
    assert stats["incomplete"] == [] and speicher.begonnen[BODY] >= vorher

    # Nach einem Tag: wieder angefragt, weiter vermerkt, neuer Zeitpunkt, Grund bleibt
    vorher = datetime.now(UTC)
    speicher.fehlend[PERSONEN] = vorher - timedelta(days=1, minutes=5)
    stats, _ = await _lauf(quelle, speicher)
    assert quelle.anfragen_je_liste()["person"] == 3
    assert stats["incomplete"] == [] and stats["missing_lists"] == [PERSONEN]
    geprueft = speicher.fehlend[PERSONEN]
    assert geprueft is not None and geprueft >= vorher.replace(microsecond=0)
    assert speicher.gruende[PERSONEN] == FORBIDDEN_LIST_REASON
    assert speicher.filtervermerke.lists == {}

    # Gibt Bochum die Personen frei, kommen sie (Fenster sieben Tage) und der Vermerk endet
    del quelle.fest[PFAD_PERSONEN]
    speicher.fehlend[PERSONEN] = datetime.now(UTC) - timedelta(days=2)
    stats, _ = await _lauf(quelle, speicher)
    assert stats["incomplete"] == [] and stats["persons"] == 3
    assert PERSONEN not in speicher.fehlend and PERSONEN not in speicher.gruende
    assert quelle.filter_der_anfragen("person")[0] is not None  # wieder mit Filter


async def test_vollabgleich_vermerkt_nicht_freigegebene_liste():
    quelle = BochumQuelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = Speicher()
    vorher = datetime.now(UTC)
    stats, _ = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert set(speicher.fehlend) == {MITGLIEDSCHAFTEN, PERSONEN}
    assert speicher.begonnen[BODY] >= vorher
    assert quelle.filter_der_anfragen("person") == [None, None]  # ohne Filter, Vergleichsanfrage

    # Der Vollabgleich fragt die vermerkte Liste jedes Mal an
    stats, _ = await _lauf(quelle, speicher, full=True)
    assert quelle.anfragen_je_liste()["person"] == 2
    assert stats["incomplete"] == [] and stats["missing_lists"] == [MITGLIEDSCHAFTEN, PERSONEN]


async def test_bestehender_vermerk_filtert_nicht_endet():
    """Produktion vor dem Fix: Bochum stand mit „HTTP 403“ in ``unfiltered_lists``. Der Vermerk endet."""
    quelle = BochumQuelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    speicher.filtervermerke.note_list(PERSONEN, "HTTP 403")
    stats, _ = await _lauf(quelle, speicher, kommune=_kommune())

    assert quelle.filter_der_anfragen("person") == [None, None]  # gleich ohne Filter, dann Vergleichsanfrage
    assert stats["incomplete"] == [] and stats["missing_lists"] == [PERSONEN]
    assert PERSONEN not in speicher.filtervermerke.lists
    assert speicher.filtervermerke.changed_lists == {PERSONEN: None}  # beim Speichern gelöscht


async def test_403_nur_mit_filter_bleibt_filtert_nicht():
    """Lehnt die Liste nur den Filter mit 403 ab und kommt ohne Filter, bleibt es beim Vermerk „filtert nicht“."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.mit_filter[PFAD_PERSONEN] = 403
    speicher = Speicher()
    stats, _ = await _lauf(quelle, speicher, kommune=_kommune())

    assert stats["incomplete"] == [] and stats["persons"] == 3
    assert speicher.fehlend == {} and "missing_lists" not in stats
    assert speicher.filtervermerke.lists[PERSONEN].reason == "HTTP 403"


async def test_alle_listen_403_ist_eine_luecke_und_kein_vermerk():
    """Verweigert die Quelle jede Liste, ist eher die Schnittstelle gestört: Lücke wie bei lauter 404."""
    quelle = Quelle(geaendert=datetime.now(UTC))
    for name in LISTEN:
        quelle.fest[f"/oparl/body/1/{name}"] = 403
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    stats, _ = await _lauf(quelle, speicher, kommune=_kommune())
    assert "lists" in stats["incomplete"]
    assert speicher.fehlend == {}
    assert speicher.begonnen[BODY] == beginn


async def test_vermerkte_nicht_freigegebene_pflichtliste_bekommt_keine_zeitscheiben():
    """Vollabgleich in Zeitscheiben: Eine Scheibe fragt nur mit Filter; die Prüfung bleibt beim täglichen Lauf."""
    quelle = HammQuelle()
    speicher = ScheibenSpeicher()
    speicher.fehlend[f"{HAMM_BODY}/person"] = datetime.now(UTC) - timedelta(days=3)
    stats = await _lauf_in_scheiben(quelle, speicher)
    assert stats["incomplete"] == []
    assert quelle.anfragen_an("person") == []
    assert quelle.anfragen_an("organization")


# ---------------------------------------------------------------------------
# Sperre unseres User-Agents und unklare Fälle bleiben Lücken
# ---------------------------------------------------------------------------


async def test_nur_unser_user_agent_gesperrt_bleibt_sperre_und_luecke():
    quelle = NurUnsGesperrt(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    stats, client = await _lauf(quelle, speicher, kommune=_kommune())

    assert stats["incomplete"] == ["person"]
    assert any("User-Agent gesperrt" in fehler for fehler in stats["errors"])
    assert speicher.fehlend == {} and "missing_lists" not in stats
    assert speicher.begonnen[BODY] == beginn
    # Sperre, kein Capability-Problem: kein Rückfall ohne Filter, kein Vermerk „filtert nicht“
    assert speicher.filtervermerke.lists == {}
    assert client.error_kind == ERROR_KIND_UA_BLOCKED


async def test_unklare_vergleichsanfrage_bleibt_eine_luecke():
    """Wird der neutrale Client weitergeleitet, ist offen, ob er durchkäme: kein Vermerk, wie bisher eine Lücke."""
    quelle = NeutralWeitergeleitet(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    stats, client = await _lauf(quelle, speicher, kommune=_kommune())
    assert stats["incomplete"] == ["person"]
    assert speicher.fehlend == {}
    assert BODY not in speicher.begonnen
    assert client.error_kind is None


async def test_client_ohne_report_missing_meldet_listenfehler_ohne_filtervermerk():
    quelle = BochumQuelle(geaendert=datetime.now(UTC))
    speicher = Speicher()
    seit = datetime.now(UTC) - timedelta(hours=3)
    async with _client(quelle, speicher) as client:
        with pytest.raises(ListFetchError) as fehler:
            async for _ in client.fetch_list(PERSONEN, modified_since=seit):
                pass
        assert fehler.value.result.status_code == 403
        assert PERSONEN not in client.filter_notes.lists
        with pytest.raises(ListMissingError) as fehlt:
            async for _ in client.fetch_list(PERSONEN, modified_since=seit, report_missing=True):
                pass
    assert fehlt.value.forbidden and not fehlt.value.not_found
    assert fehlt.value.reason == FORBIDDEN_LIST_REASON
    assert "gibt die Quelle nicht frei" in str(fehlt.value)


# ---------------------------------------------------------------------------
# Befund je Host und Vermerk in sync_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "fuer_alle"),
    [
        (None, False),  # keine Vergleichsanfrage (abgeschaltet)
        (0, False),  # ohne Antwort
        (302, False),  # weitergeleitet
        (401, True),
        (403, True),
        (404, True),
        (408, False),  # Zeitüberschreitung
        (429, False),  # gedrosselt
        (500, False),
        (503, False),
    ],
)
def test_verweigert_fuer_alle_nur_bei_klarer_absage(status, fuer_alle):
    assert HostHealth(host="ris.example.org", ua_probe_status=status).forbidden_for_all is fuer_alle


def test_sperre_unseres_user_agents_ist_nicht_verweigert_fuer_alle():
    befund = HostHealth(host="ris.example.org", ua_probe_status=200, ua_blocked_at=datetime.now(UTC))
    assert befund.forbidden_for_all is False


def test_vermerk_mit_grund_aus_sync_config():
    config = {
        "missing_lists": {
            f"{BASE}/a": "2026-10-15T10:00:00+00:00",
            f"{BASE}/b": {"checked_at": "2026-10-19T08:00:00+00:00", "reason": FORBIDDEN_LIST_REASON},
            f"{BASE}/c": {"checked_at": "kaputt", "reason": 7},
            "": {"checked_at": "2026-10-19T08:00:00+00:00", "reason": "x"},
        }
    }
    assert missing_lists_of(config) == {
        f"{BASE}/a": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
        f"{BASE}/b": datetime(2026, 10, 19, 8, 0, tzinfo=UTC),
        f"{BASE}/c": None,
    }
    assert missing_list_reasons_of(config) == {f"{BASE}/b": FORBIDDEN_LIST_REASON}
    assert missing_list_reasons_of({"missing_lists": "x"}) == {}
    assert missing_list_reasons_of(None) == {}
