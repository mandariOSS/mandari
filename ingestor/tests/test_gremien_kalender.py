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
    ERNEUT_NACH,
    MONATE_JE_LAUF,
    KalenderConfig,
    KalenderLauf,
    OhneGremium,
    ergaenzen,
    namen_im_kalender,
    sitzungsnummer,
)
from tests.test_sessionnet_luecken import announced, calendar_row, page, termin

BASIS = "https://www.stadt.example/sessionnet/sessionnetbi/"
JETZT = datetime(2026, 11, 19, 12, tzinfo=UTC)
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


def _lauf() -> KalenderLauf:
    lauf = KalenderLauf.fuer({"sessionnet_kalender": BASIS})
    assert lauf is not None
    return lauf


def test_konfiguration_und_kennung() -> None:
    assert KalenderConfig.parse({}) is None
    assert KalenderConfig.parse({"sessionnet_kalender": "ftp://x"}) is None
    assert KalenderLauf.fuer({}) is None
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
async def test_ergaenzt_bekannte_merkt_sich_unbekannte_und_liest_je_monat_eine_seite() -> None:
    gespeichert: dict[Any, dict[str, str]] = {}
    geladen: list[str] = []
    gefragt: list[datetime] = []

    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        gefragt.append(erneut_ab)
        return [_sitzung(14591, 19), _sitzung(14557, 24), _sitzung(99999, 26)]

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        gespeichert[sitzung.id] = wert
        return True

    stats = await ergaenzen(_lauf(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    assert gefragt == [JETZT - ERNEUT_NACH]
    assert geladen == [f"{BASIS}si0040.php?__cjahr=2026&__cmonat=11"]
    assert gespeichert["id-14591"]["name"] == "Interfraktionelle Arbeitsgruppe Internationales"
    assert gespeichert["id-14591"]["seite"] == geladen[0]
    assert gespeichert["id-14557"]["name"] == "AirportPark FMO GmbH, Aufsichtsrat"
    # Nicht im Kalender: nur die Prüfung merken, damit der Monat die übrigen nicht blockiert
    assert gespeichert["id-99999"] == {"geprueft": JETZT.isoformat(), "seite": geladen[0]}
    assert stats.as_dict() == {"seiten": 1, "benannt": 2, "ohne_eintrag": 1, "fehlgeschlagen": 0, "verschoben": 0}


@pytest.mark.asyncio
async def test_grenze_je_lauf_und_fehler_halten_nicht_auf() -> None:
    monate = [(2026 + (11 + i - 1) // 12, (11 + i - 1) % 12 + 1) for i in range(MONATE_JE_LAUF + 2)]

    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return [_sitzung(1000 + i, 10, monat, jahr) for i, (jahr, monat) in enumerate(monate)]

    geladen: list[str] = []
    gespeichert: dict[Any, dict[str, str]] = {}

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        if "__cmonat=12" in url:
            return Seite(status_code=503, error="Wartung")
        if url.endswith("__cmonat=1"):
            return Seite(text="<html><body>Wartungsarbeiten</body></html>")
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        gespeichert[sitzung.id] = wert
        return True

    stats = await ergaenzen(_lauf(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    # Nächste Monate zuerst, höchstens MONATE_JE_LAUF Seiten; Fehler zählen, der Lauf geht weiter
    assert len(geladen) == MONATE_JE_LAUF and "__cmonat=11" in geladen[0] and "__cmonat=12" in geladen[1]
    assert stats.fehlgeschlagen == 2 and stats.verschoben == 2 and stats.ohne_eintrag == MONATE_JE_LAUF - 2
    assert stats.seiten == MONATE_JE_LAUF - 2 and stats.benannt == 0
    # Geprüfte Monate (auch die mit Fehler) kommen erst nach ERNEUT_NACH wieder dran, die verschobenen nicht gemerkt
    assert len(gespeichert) == MONATE_JE_LAUF and all("name" not in wert for wert in gespeichert.values())
    assert {f"id-{1000 + i}" for i in range(MONATE_JE_LAUF, MONATE_JE_LAUF + 2)}.isdisjoint(gespeichert)


@pytest.mark.asyncio
async def test_kommunen_einer_quelle_teilen_seiten_und_kontingent() -> None:
    lauf = _lauf()
    geladen: list[str] = []

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        return True

    async def erste(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return [_sitzung(14591, 19)] + [_sitzung(2000 + m, 10, m + 1, 2027) for m in range(MONATE_JE_LAUF - 2)]

    async def zweite(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return [_sitzung(14557, 24), _sitzung(3001, 10, 10, 2027), _sitzung(3002, 10, 11, 2027)]

    eins = await ergaenzen(lauf, erste, lade_seite, speichere, jetzt=JETZT)
    zwei = await ergaenzen(lauf, zweite, lade_seite, speichere, jetzt=JETZT)
    # November lädt nur die erste Kommune; das Kontingent reicht danach noch für einen Monat
    assert len(geladen) == MONATE_JE_LAUF and len(set(geladen)) == MONATE_JE_LAUF
    assert eins.seiten == MONATE_JE_LAUF - 1 and zwei.seiten == 1
    assert zwei.benannt == 1 and zwei.verschoben == 1 and lauf.rest == 0


@pytest.mark.asyncio
async def test_geaenderte_sitzung_wird_nicht_benannt() -> None:
    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return [_sitzung(14591, 19)]

    async def lade_seite(url: str) -> Seite:
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        return False  # ein paralleler Abgleich hat die Sitzung inzwischen geändert

    stats = await ergaenzen(_lauf(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    assert stats.benannt == 0 and stats.seiten == 1
