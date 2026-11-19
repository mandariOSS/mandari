# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremiennamen aus dem öffentlichen SessionNet-Kalender für Sitzungen, deren Gremium die OParl-Schnittstelle nicht
herausgibt (Münster: Interfraktionelle Arbeitsgruppe Internationales, HTTP 401 auf das Gremium).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from src.sync.gremien_kalender import (
    MONATE_JE_LAUF,
    KalenderConfig,
    OhneGremium,
    ergaenzen,
    namen_im_kalender,
    sitzungsnummer,
)
from tests.test_sessionnet_luecken import announced, calendar_row, page, termin

BASIS = "https://www.stadt.example/sessionnet/sessionnetbi/"
KALENDER = page(
    '<table class="table smc-table smctablesitzungen"><tbody>'
    + calendar_row(
        19,
        announced("Interfraktionelle Arbeitsgruppe Internationales"),
        termin(14591, 19, "Interfraktionelle Arbeitsgruppe Internationales"),
    )
    + calendar_row(
        24, announced("AirportPark FMO GmbH, Aufsichtsrat"), termin(14557, 24, "AirportPark FMO GmbH, Aufsichtsrat")
    )
    + "</tbody></table>",
    title="Sitzungskalender",
)


@dataclass
class Seite:
    status_code: int = 200
    error: str | None = None
    text: str | None = None


def _sitzung(nummer: int, tag: int, monat: int = 11, jahr: int = 2026) -> OhneGremium:
    return OhneGremium(
        id=f"id-{nummer}",
        external_id=f"https://oparl.stadt.example/bodies/0001/meetings/{nummer}",
        start=datetime(jahr, monat, tag, 16, 0, tzinfo=UTC),
    )


def test_konfiguration_und_kennung() -> None:
    assert KalenderConfig.parse({}) is None
    assert KalenderConfig.parse({"sessionnet_kalender": "ftp://x"}) is None
    config = KalenderConfig.parse({"sessionnet_kalender": BASIS.rstrip("/")})
    assert config is not None and config.monat(2026, 11) == f"{BASIS}si0040.php?__cjahr=2026&__cmonat=11"
    assert sitzungsnummer("https://oparl.stadt.example/bodies/0001/meetings/14591") == 14591
    assert sitzungsnummer("https://oparl.stadt.example/meetings/abc") is None


def test_namen_aus_angekuendigten_eintraegen() -> None:
    assert namen_im_kalender(KALENDER) == {
        14591: "Interfraktionelle Arbeitsgruppe Internationales",
        14557: "AirportPark FMO GmbH, Aufsichtsrat",
    }
    assert namen_im_kalender("<html>Wartung</html>") == {}


@pytest.mark.asyncio
async def test_ergaenzt_nur_bekannte_und_liest_je_monat_eine_seite() -> None:
    config = KalenderConfig.parse({"sessionnet_kalender": BASIS})
    assert config is not None
    gespeichert: dict[Any, dict[str, str]] = {}
    geladen: list[str] = []

    async def lade_ohne_gremium(von: datetime, bis: datetime) -> list[OhneGremium]:
        return [_sitzung(14591, 19), _sitzung(14557, 24), _sitzung(99999, 26)]

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        return Seite(text=KALENDER)

    async def speichere(meeting_id: Any, wert: dict[str, str]) -> None:
        gespeichert[meeting_id] = wert

    stats = await ergaenzen(
        config, lade_ohne_gremium, lade_seite, speichere, jetzt=datetime(2026, 11, 19, 12, tzinfo=UTC)
    )
    assert geladen == [f"{BASIS}si0040.php?__cjahr=2026&__cmonat=11"]
    assert gespeichert["id-14591"]["name"] == "Interfraktionelle Arbeitsgruppe Internationales"
    assert gespeichert["id-14591"]["seite"] == geladen[0]
    assert set(gespeichert) == {"id-14591", "id-14557"}
    assert stats.as_dict() == {"seiten": 1, "benannt": 2, "ohne_eintrag": 1, "fehlgeschlagen": 0, "verschoben": 0}


@pytest.mark.asyncio
async def test_grenze_je_lauf_und_fehler_halten_nicht_auf() -> None:
    config = KalenderConfig.parse({"sessionnet_kalender": BASIS})
    assert config is not None
    monate = [(2026 + (11 + i - 1) // 12, (11 + i - 1) % 12 + 1) for i in range(MONATE_JE_LAUF + 2)]

    async def lade_ohne_gremium(von: datetime, bis: datetime) -> list[OhneGremium]:
        return [_sitzung(1000 + i, 10, monat, jahr) for i, (jahr, monat) in enumerate(monate)]

    geladen: list[str] = []

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        if "__cmonat=12" in url:
            return Seite(status_code=503, error="Wartung")
        return Seite(text=KALENDER)

    async def speichere(meeting_id: Any, wert: dict[str, str]) -> None:
        raise AssertionError("keine der Sitzungen steht im Kalender")

    stats = await ergaenzen(
        config, lade_ohne_gremium, lade_seite, speichere, jetzt=datetime(2026, 11, 19, 12, tzinfo=UTC)
    )
    # Nächste Monate zuerst, höchstens MONATE_JE_LAUF Seiten; ein Fehler zählt, der Lauf geht weiter
    assert len(geladen) == MONATE_JE_LAUF and "__cmonat=11" in geladen[0] and "__cmonat=12" in geladen[1]
    assert stats.fehlgeschlagen == 1 and stats.verschoben == 2 and stats.ohne_eintrag == MONATE_JE_LAUF - 1
