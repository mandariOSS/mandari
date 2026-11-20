# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dauerhaft nicht speicherbare Objekte halten das Änderungsfenster nicht mehr für immer fest.

Mönchengladbach (ITK Rheinland) nennt in elf Mitgliedschaften Personen, die die Quelle nicht veröffentlicht
(Einzelabruf HTTP 404). Jede Mitgliedschaft hielt das Fenster der Kommune bei ihrem ``modified``; es stand seit dem
13.11.2026 still, und jeder Lauf las die Änderungen einer Woche. Jetzt zählt der Ingestor je Objekt die Läufe, die es
nicht speichern konnten (``sync_config["unstored_holds"]``). Nach ``UNSTORED_HOLD_RUNS`` Läufen und
``UNSTORED_HOLD_MIN`` ist es zurückgestellt: Es hält das Fenster nicht mehr, Log und Sync-Log nennen es
(``unstored_deferred``). Verloren geht nichts: Der nächste Vollabgleich und jede Änderung liefern es wieder, und
gespeichert wird es, sobald sich seine Verweise auflösen lassen.

Antworten aus ``httpx.MockTransport`` wie in ``test_aenderungsfenster.py``, keine Abrufe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from src.config import settings
from src.storage.database import unstored_holds_of
from src.sync.orchestrator import (
    UNSTORED_DEFERRED_KEEP,
    UNSTORED_HOLD_MIN,
    UNSTORED_HOLD_RUNS,
    UNSTORED_HOLDS_MAX,
    UNSTORED_TRACKED_MAX,
    BodyItemCheck,
    SyncResult,
    settle_unstored_holds,
)
from tests.test_aenderungsfenster import BASE, BODY, SOURCE, Quelle, Speicher, _kommune, _lauf, _orchestrator

JETZT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
MITGLIEDSCHAFT = f"{BASE}/membership/17107"


@pytest.fixture(autouse=True)
def _ohne_nebenlaeufe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keine Texterkennung, kein Suchindex, ein Versuch je Anfrage."""
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "oparl_max_retries", 1)
    yield


class SpeicherMitVermerk(Speicher):
    """Speicher aus ``test_aenderungsfenster.py`` mit dem Vermerk ``unstored_holds`` wie in ``sync_config``."""

    def __init__(self) -> None:
        super().__init__()
        self.vermerk: dict[str, dict[str, dict[str, Any]]] = {}

    async def get_unstored_holds(self, source_id: UUID, body_external_id: str) -> dict[str, dict[str, Any]]:
        return unstored_holds_of({"unstored_holds": self.vermerk}, body_external_id)

    async def set_unstored_holds(
        self, source_id: UUID, body_external_id: str, entries: dict[str, dict[str, Any]]
    ) -> None:
        if entries:
            self.vermerk[body_external_id] = dict(entries)
        else:
            self.vermerk.pop(body_external_id, None)


def _mitgliedschaft(geaendert: datetime) -> dict[str, Any]:
    return {
        "id": MITGLIEDSCHAFT,
        "type": "https://schema.oparl.org/1.1/Membership",
        "person": f"{BASE}/people/2877",
        "organization": f"{BASE}/organization/0",
        "role": "stellv. sachk. Bürger*in",
        "modified": geaendert.isoformat(),
    }


# --- Fortschreiben des Vermerks ------------------------------------------------------------------------------


def test_haltendes_objekt_zaehlt_laeufe_und_wird_zurueckgestellt() -> None:
    halten = {"a": ("membership", JETZT - timedelta(days=7))}
    vermerk: dict[str, dict[str, Any]] = {}
    for lauf in range(1, UNSTORED_HOLD_RUNS + 1):
        vermerk, zurueck, neu = settle_unstored_holds(vermerk, halten, JETZT + timedelta(minutes=15 * lauf))
        assert vermerk["a"]["runs"] == lauf
        # Genug Läufe, aber noch keine zwölf Stunden: hält weiter
        assert zurueck == set() and neu == set()
    assert vermerk["a"]["since"] == (JETZT + timedelta(minutes=15)).isoformat()

    spaeter = JETZT + timedelta(minutes=15) + UNSTORED_HOLD_MIN
    vermerk, zurueck, neu = settle_unstored_holds(vermerk, halten, spaeter)
    assert zurueck == neu == {"a"}
    assert vermerk["a"]["deferred"] == spaeter.isoformat()

    # Auch im nächsten Lauf (etwa im Vollabgleich) bleibt es zurückgestellt, ohne neue Meldung
    vermerk, zurueck, neu = settle_unstored_holds(vermerk, halten, spaeter + timedelta(hours=1))
    assert zurueck == {"a"} and neu == set()


def test_viele_laeufe_in_kurzer_zeit_stellen_nicht_zurueck() -> None:
    """Ein vorübergehender Fehler (Gremium kommt erst im nächsten Lauf) hält das Fenster, bis er behoben ist."""
    vermerk = {"a": {"type": "agendaitem", "since": JETZT.isoformat(), "seen": JETZT.isoformat(), "runs": 50}}
    _, zurueck, _ = settle_unstored_holds(vermerk, {"a": ("agendaitem", None)}, JETZT + UNSTORED_HOLD_MIN / 2)
    assert zurueck == set()


def test_nicht_mehr_gesehene_objekte() -> None:
    gesehen = (JETZT - timedelta(days=1)).isoformat()
    vermerk: dict[str, dict[str, Any]] = {
        # Gespeichert oder nicht mehr geliefert: fällt weg
        "gespeichert": {"type": "paper", "since": gesehen, "seen": gesehen, "runs": 2},
        # Zurückgestellt: bleibt, bis es lange kein Lauf mehr gesehen hat
        "zurueck": {"type": "membership", "since": gesehen, "seen": gesehen, "runs": 9, "deferred": gesehen},
        "alt": {
            "type": "membership",
            "since": "2026-01-01T00:00:00+00:00",
            "seen": (JETZT - UNSTORED_DEFERRED_KEEP - timedelta(days=1)).isoformat(),
            "runs": 9,
            "deferred": "2026-01-02T00:00:00+00:00",
        },
        "kaputt": {"type": "membership", "runs": "x", "deferred": "kaputt"},
    }
    neu, zurueck, _ = settle_unstored_holds(vermerk, {}, JETZT)
    assert neu == {"zurueck": vermerk["zurueck"]}
    assert zurueck == set()


def test_vermerk_ist_begrenzt() -> None:
    gesehen = JETZT - timedelta(days=1)
    vermerk = {
        f"z{nummer}": {
            "type": "membership",
            "since": gesehen.isoformat(),
            "seen": (gesehen - timedelta(minutes=nummer)).isoformat(),
            "runs": 5,
            "deferred": gesehen.isoformat(),
        }
        for nummer in range(UNSTORED_HOLDS_MAX + 5)
    }
    neu, _, _ = settle_unstored_holds(vermerk, {"frisch": ("membership", gesehen)}, JETZT)
    assert len(neu) == UNSTORED_HOLDS_MAX
    # Die zuletzt gesehenen bleiben
    assert "frisch" in neu and "z0" in neu and f"z{UNSTORED_HOLDS_MAX + 4}" not in neu


def test_fensteranfang_ohne_zurueckgestellte() -> None:
    befund = BodyItemCheck()
    befund.note_unstored("membership", {"id": "a", "modified": "2026-11-13T05:43:17+00:00"})
    befund.note_unstored("membership", {"id": "b"})  # ohne modified
    befund.note_unstored("agendaitem", {"id": "c", "modified": "2026-11-19T10:00:00+00:00"})
    assert befund.window_start(JETZT) is None
    assert befund.window_start(JETZT, {"b"}) == datetime(2026, 11, 13, 5, 43, 17, tzinfo=UTC)
    assert befund.window_start(JETZT, {"a", "b"}) == datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    assert befund.window_start(JETZT, {"a", "b", "c"}) == JETZT
    # Ohne Kennung lässt sich nichts zurückstellen: Das Objekt hält das Fenster wie bisher
    befund.note_unstored("membership", {"modified": "2026-11-15T00:00:00+00:00"})
    assert befund.window_start(JETZT, {"a", "b", "c"}) == datetime(2026, 11, 15, tzinfo=UTC)


def test_haltende_objekte_ueber_der_grenze_halten_wie_bisher() -> None:
    befund = BodyItemCheck()
    for nummer in range(UNSTORED_TRACKED_MAX):
        befund.note_unstored("membership", {"id": f"m{nummer}", "modified": "2026-11-19T00:00:00+00:00"})
    befund.note_unstored("membership", {"id": "mehr", "modified": "2026-11-14T00:00:00+00:00"})
    assert len(befund.holding) == UNSTORED_TRACKED_MAX and "mehr" not in befund.holding
    assert befund.window_start(JETZT, set(befund.holding)) == datetime(2026, 11, 14, tzinfo=UTC)
    # Dieselbe Kennung zweimal: Es gilt der ältere Stand
    befund.note_unstored("membership", {"id": "m0", "modified": "2026-11-10T00:00:00+00:00"})
    befund.note_unstored("membership", {"id": "m0", "modified": "2026-11-18T00:00:00+00:00"})
    assert befund.holding["m0"] == ("membership", datetime(2026, 11, 10, tzinfo=UTC))


# --- Im Abgleich -----------------------------------------------------------------------------------------------


async def test_dauerhaft_nicht_speicherbare_mitgliedschaft_haelt_das_fenster_nicht_mehr() -> None:
    geaendert = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=40))
    quelle.listen["/oparl/body/1/membership"] = [_mitgliedschaft(geaendert)]
    kommune = _kommune(membership=f"{BODY}/membership")
    speicher = SpeicherMitVermerk()
    speicher.begonnen[BODY] = datetime.now(UTC) - timedelta(hours=1)
    speicher.unaufloesbar.add(MITGLIEDSCHAFT)

    # Erste Läufe: Die Mitgliedschaft hält das Fenster, der Vermerk zählt mit
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["unstored"] == {"membership": 1} and "unstored_deferred" not in stats
    assert speicher.begonnen[BODY] == geaendert
    eintrag = speicher.vermerk[BODY][MITGLIEDSCHAFT]
    assert (eintrag["type"], eintrag["runs"]) == ("membership", 1) and "deferred" not in eintrag

    # Seit einem Tag und vier Läufen nicht speicherbar: zurückgestellt, das Fenster rückt vor
    seit = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    eintrag.update(since=seit, runs=UNSTORED_HOLD_RUNS - 1)
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["unstored"] == {"membership": 1}
    assert stats["unstored_deferred"] == {"membership": 1}
    assert speicher.begonnen[BODY] >= vorher - timedelta(seconds=1)
    assert "deferred" in speicher.vermerk[BODY][MITGLIEDSCHAFT]

    # Ein Vollabgleich liefert sie wieder: Sie hält das Fenster weiter nicht
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, kommune=kommune, full=True)
    assert stats["unstored_deferred"] == {"membership": 1}
    assert speicher.begonnen[BODY] >= vorher - timedelta(seconds=1)

    # Ist die Person veröffentlicht, speichert der nächste Lauf, der sie liefert, die Mitgliedschaft
    speicher.unaufloesbar.clear()
    stats = await _lauf(quelle, speicher, kommune=kommune, full=True)
    assert "unstored" not in stats and MITGLIEDSCHAFT in speicher.modified


async def test_zurueckgestellte_im_sync_log() -> None:
    speicher = SpeicherMitVermerk()
    orchestrator = _orchestrator(speicher)
    ergebnis = SyncResult(source_url=SOURCE, source_name="Neuss", success=True)
    ergebnis.unstored.update({"membership": 11})
    ergebnis.unstored_deferred.update({"membership": 11})
    ruhig = SyncResult(source_url=f"{SOURCE}/2", source_name="Ruhig", success=True)
    await orchestrator.write_results_to_synclog([ergebnis, ruhig], datetime.now(UTC))
    details = speicher.protokoll[0]["details"]
    assert details["Neuss"]["unstored_deferred"] == {"membership": 11}
    assert "unstored_deferred" not in details["Ruhig"]


async def test_zurueckgestellte_im_sync_log_des_daemons() -> None:
    """Der Daemon schreibt das Sync-Log selbst (``SyncScheduler._write_log``); auch dort steht der Zähler."""
    from src.scheduler import SyncScheduler
    from src.scheduler.takt import SyncState

    speicher = SpeicherMitVermerk()
    scheduler = SyncScheduler.__new__(SyncScheduler)
    scheduler._orchestrator = _orchestrator(speicher)
    ergebnis = SyncResult(source_url=SOURCE, source_name="Neuss", success=True)
    ergebnis.unstored_deferred.update({"membership": 11})

    class Quelle:
        id = UUID(int=9)

    jetzt = datetime.now(UTC)
    await scheduler._write_log(Quelle(), ergebnis, jetzt, jetzt, "scheduler", SyncState(), 0)

    assert speicher.protokoll[0]["details"]["unstored_deferred"] == {"membership": 11}


async def test_lauf_mit_luecke_zaehlt_nicht() -> None:
    """Ein Lauf mit Lücke rückt das Fenster nicht vor; er schreibt auch den Vermerk nicht fort."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(minutes=40))
    quelle.listen["/oparl/body/1/membership"] = [_mitgliedschaft(datetime.now(UTC) - timedelta(minutes=20))]
    quelle.fest["/oparl/body/1/paper"] = 500
    speicher = SpeicherMitVermerk()
    speicher.unaufloesbar.add(MITGLIEDSCHAFT)
    stats = await _lauf(quelle, speicher, kommune=_kommune(membership=f"{BODY}/membership"))
    assert stats["incomplete"] == ["paper"]
    assert speicher.vermerk == {}
