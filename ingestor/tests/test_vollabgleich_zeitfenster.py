# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitfenster für Vollabgleiche je Quelle (``sync_config["full_sync_window"]``, Issue #348).

Zusage an die Stadt Köln: vollständige Abgleiche nur nachts. Außerhalb des Fensters läuft ein angeforderter
Vollabgleich inkrementell, und ein laufender Vollabgleich holt nach dem Fensterende keine Seite mehr.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from src.client.oparl_client import FetchWindowClosedError, ListFetchError, OParlClient
from src.client.source_options import FullSyncWindow, SourceFetchOptions
from src.sync.orchestrator import SyncOrchestrator

NACHTS = {"full_sync_window": {"start": "00:00", "end": "06:00"}}
UEBER_MITTERNACHT = {"full_sync_window": {"start": "22:00", "end": "06:00"}}


def berlin(stunde: int) -> datetime:
    """Ortszeit Berlin am 15.10.2026 (MESZ, UTC+2, ab 02:00) als UTC-Zeitpunkt."""
    return datetime(2026, 10, 15, stunde - 2, tzinfo=UTC)


@pytest.mark.parametrize(
    "wert",
    [
        None,
        "00:00-06:00",
        {},
        {"start": "00:00"},
        {"start": "6:00", "end": "6:00"},
        {"start": "25:00", "end": "06:00"},
        {"start": "00:00", "end": "06:00", "tz": "Mars/Olympus"},
    ],
)
def test_ungueltige_fenster_gelten_als_nicht_gesetzt(wert):
    options = SourceFetchOptions.from_sync_config({"full_sync_window": wert})
    assert options.full_sync_window is None
    assert options.full_sync_plan(True) == (True, None)


def test_fenster_wird_gelesen():
    fenster = SourceFetchOptions.from_sync_config(NACHTS).full_sync_window
    assert fenster is not None
    assert (fenster.start, fenster.end, str(fenster.tz)) == (time(0, 0), time(6, 0), "Europe/Berlin")


def test_vollabgleich_im_fenster_mit_fensterende():
    options = SourceFetchOptions.from_sync_config(NACHTS)
    full, deadline = options.full_sync_plan(True, now=berlin(4))
    assert full is True
    assert deadline == datetime(2026, 10, 15, 4, 0, tzinfo=UTC)  # 06:00 MESZ


@pytest.mark.parametrize("stunde", [6, 9, 14, 23])
def test_vollabgleich_ausserhalb_wird_inkrementell(stunde):
    options = SourceFetchOptions.from_sync_config(NACHTS)
    assert options.full_sync_plan(True, now=berlin(stunde)) == (False, None)


def test_inkrementeller_abgleich_bleibt_unberuehrt():
    options = SourceFetchOptions.from_sync_config(NACHTS)
    assert options.full_sync_plan(False, now=berlin(14)) == (False, None)
    assert options.full_sync_plan(False, now=berlin(4)) == (False, None)


def test_fenster_ueber_mitternacht():
    fenster = FullSyncWindow.parse(UEBER_MITTERNACHT["full_sync_window"])
    assert fenster is not None
    # 23:00 am 14.10. -> Ende 06:00 am 15.10. (04:00 UTC)
    assert fenster.closes_at(datetime(2026, 10, 14, 21, 0, tzinfo=UTC)) == datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    # 03:00 am 15.10. -> Ende 06:00 am selben Tag
    assert fenster.closes_at(datetime(2026, 10, 15, 1, 0, tzinfo=UTC)) == datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    # 12:00 -> außerhalb
    assert fenster.closes_at(datetime(2026, 10, 15, 10, 0, tzinfo=UTC)) is None


def test_orchestrator_stuft_herunter():
    options = SourceFetchOptions.from_sync_config(NACHTS)
    full, deadline = SyncOrchestrator._full_sync_plan("https://rat.example/oparl/system", options, True)
    # Ergebnis hängt von der Uhrzeit ab, aber nie Vollabgleich ohne Fensterende
    assert (full, deadline is not None) in {(True, True), (False, False)}


@pytest.mark.asyncio
async def test_liste_bricht_nach_fensterende_ohne_anfrage_ab(monkeypatch):
    client = OParlClient(fetch_deadline=datetime(2000, 1, 1, tzinfo=UTC))

    async def keine_anfrage(*args, **kwargs):  # pragma: no cover - darf nicht laufen
        raise AssertionError("Nach dem Fensterende darf keine Anfrage mehr rausgehen")

    monkeypatch.setattr(client, "fetch", keine_anfrage)
    with pytest.raises(FetchWindowClosedError) as fehler:
        async for _ in client.fetch_list("https://rat.example/oparl/papers"):
            pass
    # Als Listenfehler zählt die Liste als unvollständig (kein Löschabgleich, kein „aktuell“)
    assert isinstance(fehler.value, ListFetchError)
