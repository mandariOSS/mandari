# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stopp des Änderungsabgleichs nach Seiten ohne Änderung (Issue #1523).

Der Änderungsabgleich beendet eine Liste nach fünf Seiten ohne neue, geänderte oder gelöschte Einträge. Zwei Fälle
schnitten dabei echte Änderungen ab:

- Die Stadt Münster stellt gelöschte Beratungen seitenweise als Platzhalter ``…/consultations/0`` (``deleted: true``)
  vor die echten Änderungen. Solche Seiten tragen kein bekanntes Objekt und zählen nicht mehr.
- Filtert die Quelle eine Liste nachweislich mit ``modified_since``, ist jede Seite schon eine Änderung seit dem
  Fensteranfang; hält ein nicht gespeichertes Objekt das Fenster fest, stehen die neuen Einträge hinter vielen Seiten
  mit Bekanntem. Für solche Listen greift der Stopp nicht.

Ungefilterte Listen (more! rubin) stoppen wie bisher.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from src.client.filter_notes import FilterNotes
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor

BASE = "https://oparl.example.org/bodies/0001"
LIST_URL = f"{BASE}/consultations"
HOST = "oparl.example.org"
GESEHEN = "2026-11-10T10:00:00+01:00"


def _platzhalter(punkt: int) -> dict[str, Any]:
    """Gelöschte Beratung, wie die Stadt Münster sie seit dem 13.11.2026 liefert."""
    return {
        "id": f"{BASE}/consultations/0",
        "type": "https://schema.oparl.org/1.1/Consultation",
        "paper": f"{BASE}/papers/vo/0",
        "agendaItem": f"{BASE}/agendaitems/{punkt}",
        "meeting": f"{BASE}/meetings/0",
        "created": "2026-11-20T13:36:59+01:00",
        "modified": "2026-11-20T13:36:59+01:00",
        "deleted": True,
    }


def _beratung(nummer: int, modified: str = GESEHEN) -> dict[str, Any]:
    return {
        "id": f"{BASE}/consultations/{nummer}",
        "type": "https://schema.oparl.org/1.1/Consultation",
        "paper": f"{BASE}/papers/vo/{nummer}",
        "agendaItem": f"{BASE}/agendaitems/{nummer}",
        "meeting": f"{BASE}/meetings/14548",
        "created": "2026-10-27T09:36:10+01:00",
        "modified": modified,
    }


class _Client:
    """Liefert vorgegebene Seiten; ``proven``: Die Quelle filtert die Liste nachweislich."""

    def __init__(self, seiten: list[list[dict[str, Any]]], *, proven: bool) -> None:
        self.seiten = seiten
        self.proven = proven
        self.gelesen = 0

    def filter_proven(self, url: str) -> bool:
        return self.proven

    def filters_list(self, url: str) -> bool:
        return True

    async def fetch_list(self, list_url: str, **_: Any) -> AsyncIterator[list[dict[str, Any]]]:
        for seite in self.seiten:
            self.gelesen += 1
            yield seite


class _Speicher:
    """Bekannte Beratungen mit ihrem ``modified``."""

    def __init__(self, bekannt: dict[str, datetime]) -> None:
        self.bekannt = bekannt

    async def batch_check_entities_exist(self, entity_type: str, external_ids: list[str]) -> dict[str, datetime]:
        return {kennung: self.bekannt[kennung] for kennung in external_ids if kennung in self.bekannt}

    async def get_entity_content_hashes(self, entity_type: str, external_ids: list[str]) -> dict[str, str | None]:
        return {}


async def _abgleich(client: _Client, bekannt: dict[str, datetime] | None = None) -> list[str]:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.processor = OParlProcessor()
    orchestrator.storage = _Speicher(bekannt or {})  # type: ignore[assignment]
    gespeichert: list[str] = []

    async def unbekannt(item: dict[str, Any], entity_type: str, es_deletions: Any, *, body_id: UUID) -> bool:
        return False  # Platzhalter: keine Zeile im Bestand

    async def speichern(entity: Any, body_id: UUID, entity_type: str, body_name: str | None = None) -> bool:
        gespeichert.append(entity.external_id)
        return True

    orchestrator._mark_deleted = unbekannt  # type: ignore[method-assign]
    orchestrator._store_entity = speichern  # type: ignore[method-assign]
    await orchestrator._sync_entity_type(
        client=client,  # type: ignore[arg-type]
        list_url=LIST_URL,
        entity_type="consultation",
        body_id=UUID(int=1),
        body_external_id=BASE,
        body_name="Musterstadt",
        modified_since=datetime(2026, 11, 13, tzinfo=UTC),
        full=False,
    )
    return gespeichert


def _bekannt(nummern: range) -> dict[str, datetime]:
    return {f"{BASE}/consultations/{n}": datetime.fromisoformat(GESEHEN) for n in nummern}


async def test_seiten_nur_mit_platzhaltern_zaehlen_nicht() -> None:
    """Sieben Seiten Platzhalter vor der echten Änderung: Der Abgleich liest bis zu ihr (ohne Nachweis des Filters)."""
    seiten = [[_platzhalter(seite * 25 + n) for n in range(25)] for seite in range(7)]
    seiten.append([_beratung(90777, "2026-11-17T14:57:16+01:00")])
    client = _Client(seiten, proven=False)

    assert await _abgleich(client) == [f"{BASE}/consultations/90777"]
    assert client.gelesen == 8


async def test_gefilterte_liste_liest_hinter_bekanntem_weiter() -> None:
    """Nachweislich gefiltert: Auch nach sechs Seiten Bekanntem kommt die neue Beratung an."""
    seiten = [[_beratung(seite * 10 + n) for n in range(10)] for seite in range(6)]
    seiten.append([_beratung(39524, "2026-11-19T06:37:28+01:00")])
    client = _Client(seiten, proven=True)

    assert await _abgleich(client, _bekannt(range(60))) == [f"{BASE}/consultations/39524"]
    assert client.gelesen == 7


async def test_ungefilterte_liste_stoppt_wie_bisher() -> None:
    """Ohne Nachweis des Filters endet die Liste nach fünf Seiten Bekanntem (more! rubin, ungefilterte Listen)."""
    seiten = [[_beratung(seite * 10 + n) for n in range(10)] for seite in range(6)]
    seiten.append([_beratung(39524, "2026-11-19T06:37:28+01:00")])
    client = _Client(seiten, proven=False)

    assert await _abgleich(client, _bekannt(range(60))) == []
    assert client.gelesen == 5


async def test_gemischte_seiten_mit_bekanntem_zaehlen() -> None:
    """Platzhalter neben Bekanntem: Die Seite trägt Bekanntes und zählt wie bisher."""
    seiten = [[_platzhalter(seite), _beratung(seite)] for seite in range(6)]
    seiten.append([_beratung(39524, "2026-11-19T06:37:28+01:00")])
    client = _Client(seiten, proven=False)

    assert await _abgleich(client, _bekannt(range(6))) == []
    assert client.gelesen == 5


@pytest.mark.parametrize(
    ("check", "unfiltered_lists", "erwartet"),
    [
        ({"host": HOST, "result": "supported"}, {}, True),
        ({"host": HOST, "result": "ignored"}, {}, False),
        ({"host": "anderer.example.org", "result": "supported"}, {}, False),
        ({"host": HOST, "result": "supported"}, {LIST_URL: {"reason": "HTTP 401"}}, False),
        (None, {}, False),
    ],
)
def test_nachweis_des_filters(check: Any, unfiltered_lists: dict[str, Any], erwartet: bool) -> None:
    sync_config: dict[str, Any] = {"unfiltered_lists": unfiltered_lists}
    if check is not None:
        sync_config["modified_since_check"] = {**check, "checked_at": "2026-11-18T06:08:03+00:00"}
    notes = FilterNotes.from_sync_config(sync_config, f"https://{HOST}/system")
    assert notes.proven(LIST_URL, HOST) is erwartet
