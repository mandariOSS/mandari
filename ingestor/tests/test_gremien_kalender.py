# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremiennamen aus dem öffentlichen SessionNet-Kalender für Sitzungen, deren Gremium die OParl-Schnittstelle nicht
herausgibt (Münster: Interfraktionelle Arbeitsgruppe Internationales, HTTP 401 auf das Gremium).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
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


def _config() -> KalenderConfig:
    lauf = KalenderLauf.fuer({"sessionnet_kalender": BASIS})
    assert lauf is not None and lauf.kommunen == []
    return lauf.config


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

    stats = await ergaenzen(_config(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
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

    stats = await ergaenzen(_config(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    # Nächste Monate zuerst, höchstens MONATE_JE_LAUF Seiten; Fehler zählen, der Lauf geht weiter
    assert len(geladen) == MONATE_JE_LAUF and "__cmonat=11" in geladen[0] and "__cmonat=12" in geladen[1]
    assert stats.fehlgeschlagen == 2 and stats.verschoben == 2 and stats.ohne_eintrag == MONATE_JE_LAUF - 2
    assert stats.seiten == MONATE_JE_LAUF - 2 and stats.benannt == 0
    # Geprüfte Monate (auch die mit Fehler) kommen erst nach ERNEUT_NACH wieder dran, die verschobenen nicht gemerkt
    assert len(gespeichert) == MONATE_JE_LAUF and all("name" not in wert for wert in gespeichert.values())
    assert {f"id-{1000 + i}" for i in range(MONATE_JE_LAUF, MONATE_JE_LAUF + 2)}.isdisjoint(gespeichert)


@pytest.mark.asyncio
async def test_zwei_kommunen_einer_quelle_ueber_zwei_tage() -> None:
    """
    Ein Lauf je Quelle über die Sitzungen aller Kommunen: Eine Seite dient beiden, das Kontingent gilt für die Quelle,
    und am nächsten Tag kommen die Monate der zweiten Kommune vor den schon geprüften der ersten dran.
    """
    erste = [_sitzung(14591, 19)] + [_sitzung(2000 + m, 10, m, 2027) for m in range(1, MONATE_JE_LAUF)]
    zweite = [_sitzung(14557, 24)] + [_sitzung(3000 + m, 10, m, 2027) for m in range(MONATE_JE_LAUF, 9)]
    bestand: dict[Any, dict[str, str]] = {}

    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        offen = []
        for sitzung in erste + zweite:
            wert = bestand.get(sitzung.id, {})
            geprueft = datetime.fromisoformat(wert["geprueft"]) if "geprueft" in wert else None
            if "name" not in wert and (geprueft is None or geprueft <= erneut_ab):
                offen.append(replace(sitzung, geprueft=geprueft))
        return offen

    geladen: list[str] = []

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        bestand[sitzung.id] = wert
        return True

    config = _config()
    heute = await ergaenzen(config, lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    # November einmal für beide Kommunen; sechs Seiten, die Monate der zweiten Kommune ab Juni warten
    assert heute.seiten == MONATE_JE_LAUF and len(set(geladen)) == MONATE_JE_LAUF
    assert bestand["id-14591"]["name"] and bestand["id-14557"]["name"] and heute.verschoben == 3
    geladen.clear()

    morgen = await ergaenzen(
        config, lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT + ERNEUT_NACH + timedelta(hours=1)
    )
    # Die noch nie geprüften Monate der zweiten Kommune zuerst, danach die am längsten geprüften der ersten
    assert morgen.seiten == MONATE_JE_LAUF
    assert geladen[:3] == [f"{BASIS}si0040.php?__cjahr=2027&__cmonat={monat}" for monat in (6, 7, 8)]
    assert {f"id-{3000 + m}" for m in range(MONATE_JE_LAUF, 9)} <= set(bestand)


@pytest.mark.asyncio
async def test_geaenderte_sitzung_wird_nicht_benannt() -> None:
    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return [_sitzung(14591, 19)]

    async def lade_seite(url: str) -> Seite:
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        return False  # ein paralleler Abgleich hat die Sitzung inzwischen geändert

    stats = await ergaenzen(_config(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    assert stats.benannt == 0 and stats.seiten == 1


@pytest.mark.asyncio
async def test_geprueft_monate_kommen_nach_den_ungeprueften() -> None:
    """Auch bei Läufen im Tagesabstand verdrängen die nahen, schon geprüften Monate die übrigen nicht."""
    monate = [(2026 + (11 + i - 1) // 12, (11 + i - 1) % 12 + 1) for i in range(MONATE_JE_LAUF + 2)]
    sitzungen = []
    for i, (jahr, monat) in enumerate(monate):
        sitzung = _sitzung(1000 + i, 10, monat, jahr)
        if i < MONATE_JE_LAUF:
            # Die nahen Monate wurden zuletzt ohne Ergebnis geprüft, der nächste am frühesten
            sitzung = replace(sitzung, geprueft=JETZT - timedelta(days=3) + timedelta(hours=i))
        sitzungen.append(sitzung)

    async def lade_ohne_gremium(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
        return sitzungen

    geladen: list[str] = []

    async def lade_seite(url: str) -> Seite:
        geladen.append(url)
        return Seite(text=KALENDER)

    async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
        return True

    await ergaenzen(_config(), lade_ohne_gremium, lade_seite, speichere, jetzt=JETZT)
    erwartet = [monate[MONATE_JE_LAUF], monate[MONATE_JE_LAUF + 1], *monate[: MONATE_JE_LAUF - 2]]
    assert geladen == [f"{BASIS}si0040.php?__cjahr={jahr}&__cmonat={monat}" for jahr, monat in erwartet]
