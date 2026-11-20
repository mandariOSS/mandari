# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen anstehender Sitzungen einzeln nachladen (Issue #1523): Reihenfolge, Wartezeit, Kontingent, Abbruch bei
Sperre oder Störung, Vorlage vor der Beratung. Ohne Datenbank; das Zusammenspiel mit dem Speicher prüft
``test_beratungen_nachladen_db.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.sync.beratungen_nachladen import (
    ERNEUT_NACH,
    STANDARD_KONTINGENT,
    faellig,
    kontingent,
    nachladen,
)

JETZT = datetime(2026, 11, 20, 12, tzinfo=UTC)
HOST = "darmstadt.gremien.info"
GI = f"https://{HOST}/oparl"


@dataclass
class Antwort:
    data: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None


def beratung(nummer: int, vorlage: int | None = 1, **felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": f"{GI}/consultation/{nummer}",
        "type": "https://schema.oparl.org/1.0/Consultation",
        "agendaItem": f"{GI}/agendaItem/2026-UmweltA-194%7C{nummer}%7C1",
        "meeting": f"{GI}/meeting/2026-UmweltA-194",
    }
    if vorlage is not None:
        daten["paper"] = f"{GI}/paper/{vorlage}"
    daten.update(felder)
    return daten


def vorlage(nummer: int) -> dict[str, Any]:
    return {"id": f"{GI}/paper/{nummer}", "type": "https://schema.oparl.org/1.0/Paper", "name": "Umweltbericht"}


class Lauf:
    """Antworten je Adresse, gespeicherte Objekte in Reihenfolge."""

    def __init__(self, antworten: dict[str, Antwort], bekannte_vorlagen: set[str] | None = None) -> None:
        self.antworten = antworten
        self.bekannte_vorlagen = bekannte_vorlagen or set()
        self.abgerufen: list[str] = []
        self.gespeichert: list[tuple[str, str]] = []
        self.offen = True

    async def abrufen(self, url: str) -> Antwort:
        self.abgerufen.append(url)
        return self.antworten.get(url, Antwort(error="HTTP 404"))

    async def vorlage_bekannt(self, url: str) -> bool:
        return url in self.bekannte_vorlagen

    async def speichern(self, art: str, daten: dict[str, Any]) -> bool:
        self.gespeichert.append((art, daten["id"]))
        return True

    async def los(self, reihenfolge: list[str], budget: int = 10) -> tuple[Any, dict[str, str]]:
        return await nachladen(
            reihenfolge,
            kontingent=budget,
            jetzt=JETZT,
            abrufen=self.abrufen,
            vorlage_bekannt=self.vorlage_bekannt,
            speichern=self.speichern,
            weiter=lambda: self.offen,
        )


@pytest.mark.parametrize(
    ("sync_config", "erwartet"),
    [({}, STANDARD_KONTINGENT), ({"beratungen_nachladen": 0}, 0), ({"beratungen_nachladen": 5}, 5)]
    + [({"beratungen_nachladen": wert}, STANDARD_KONTINGENT) for wert in (-1, True, "10", None)],
)
def test_kontingent(sync_config: dict[str, Any], erwartet: int) -> None:
    assert kontingent(sync_config) == erwartet


def test_reihenfolge_anstehende_zuerst_ohne_fremde_und_frisch_versuchte() -> None:
    kandidaten = [
        (f"{GI}/consultation/1", JETZT - timedelta(days=3)),
        (f"{GI}/consultation/2", JETZT + timedelta(days=6)),
        (f"{GI}/consultation/3", JETZT + timedelta(days=1)),
        (f"{GI}/consultation/4", JETZT - timedelta(days=1)),
        ("https://anderer.example/oparl/consultation/5", JETZT + timedelta(days=1)),
        (f"{GI}/vo0050.asp?__kvonr=1#consultation/6", JETZT + timedelta(days=1)),
        (f"{GI}/consultation/7", JETZT + timedelta(days=2)),
        (f"{GI}/consultation/8", JETZT + timedelta(days=2)),
    ]
    versuche = {
        f"{GI}/consultation/7": (JETZT - timedelta(hours=3)).isoformat(),  # frisch: wartet
        f"{GI}/consultation/8": (JETZT - ERNEUT_NACH - timedelta(minutes=1)).isoformat(),  # wieder fällig
    }
    reihenfolge = faellig(kandidaten, versuche, HOST, JETZT)
    assert [kennung.rsplit("/", 1)[1] for kennung in reihenfolge] == ["3", "8", "2", "4", "1"]


async def test_unbekannte_vorlage_zuerst_dann_die_beratung() -> None:
    lauf = Lauf(
        {f"{GI}/consultation/61510": Antwort(beratung(61510)), f"{GI}/paper/1": Antwort(vorlage(1))},
    )
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/61510"])

    assert lauf.gespeichert == [("paper", f"{GI}/paper/1"), ("consultation", f"{GI}/consultation/61510")]
    assert (ergebnis.beratungen, ergebnis.vorlagen, ergebnis.abrufe, ergebnis.fehlgeschlagen) == (1, 1, 2, 0)
    assert versuche == {}


async def test_bekannte_vorlage_wird_nicht_abgerufen() -> None:
    lauf = Lauf({f"{GI}/consultation/1": Antwort(beratung(1))}, bekannte_vorlagen={f"{GI}/paper/1"})
    ergebnis, _ = await lauf.los([f"{GI}/consultation/1"])
    assert lauf.abgerufen == [f"{GI}/consultation/1"]
    assert ergebnis.beratungen == 1 and ergebnis.vorlagen == 0


async def test_nicht_ladbares_wird_gemerkt_und_der_lauf_geht_weiter() -> None:
    lauf = Lauf(
        {
            f"{GI}/consultation/2": Antwort({"data": []}),  # alte Kennung bei more! rubin
            f"{GI}/consultation/3": Antwort(beratung(3, deleted=True)),
            f"{GI}/consultation/4": Antwort(beratung(5)),  # anderes Objekt als angefragt
            f"{GI}/consultation/6": Antwort(beratung(6, vorlage=None)),
        }
    )
    reihenfolge = [f"{GI}/consultation/{n}" for n in (1, 2, 3, 4, 6)]
    ergebnis, versuche = await lauf.los(reihenfolge)

    assert lauf.gespeichert == [("consultation", f"{GI}/consultation/6")]
    assert ergebnis.fehlgeschlagen == 4
    assert set(versuche) == {f"{GI}/consultation/{n}" for n in (1, 2, 3, 4)}
    assert set(versuche.values()) == {JETZT.isoformat(timespec="seconds")}


async def test_sperre_oder_stoerung_beendet_das_nachladen() -> None:
    lauf = Lauf({f"{GI}/consultation/1": Antwort(error="robots.txt", error_kind="robots_blocked")})
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/{n}" for n in (1, 2, 3)])
    assert lauf.abgerufen == [f"{GI}/consultation/1"]
    assert ergebnis.abbruch == "robots.txt" and ergebnis.zurueckgestellt == 2
    assert list(versuche) == [f"{GI}/consultation/1"]


async def test_kontingent_haelt_zwei_abrufe_fuer_beratung_und_vorlage_frei() -> None:
    antworten = {f"{GI}/consultation/{n}": Antwort(beratung(n, vorlage=n)) for n in range(1, 5)}
    antworten.update({f"{GI}/paper/{n}": Antwort(vorlage(n)) for n in range(1, 5)})
    lauf = Lauf(antworten)
    ergebnis, _ = await lauf.los([f"{GI}/consultation/{n}" for n in range(1, 5)], budget=5)

    assert ergebnis.abrufe == 4 and ergebnis.beratungen == 2 and ergebnis.zurueckgestellt == 2


async def test_fensterende_beendet_das_nachladen() -> None:
    lauf = Lauf({})
    lauf.offen = False
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/1"])
    assert lauf.abgerufen == [] and ergebnis.abbruch == "Abruffenster beendet" and versuche == {}


async def test_ohne_vorlage_bleibt_die_beratung_offen() -> None:
    """Lässt sich die unbekannte Vorlage nicht laden, wird die Beratung nicht gespeichert und später erneut versucht."""
    lauf = Lauf({f"{GI}/consultation/1": Antwort(beratung(1, vorlage=7)), f"{GI}/consultation/2": Antwort(beratung(2))})
    lauf.bekannte_vorlagen = {f"{GI}/paper/1"}
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/1", f"{GI}/consultation/2"])

    assert lauf.abgerufen == [f"{GI}/consultation/1", f"{GI}/paper/7", f"{GI}/consultation/2"]
    assert lauf.gespeichert == [("consultation", f"{GI}/consultation/2")]
    assert list(versuche) == [f"{GI}/consultation/1"] and ergebnis.fehlgeschlagen == 1


async def test_sperre_beim_vorlagenabruf_beendet_das_nachladen() -> None:
    lauf = Lauf(
        {
            f"{GI}/consultation/1": Antwort(beratung(1)),
            f"{GI}/paper/1": Antwort(error="HTTP 503", error_kind="server_error_series"),
        }
    )
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/1", f"{GI}/consultation/2"])
    assert lauf.gespeichert == [] and lauf.abgerufen == [f"{GI}/consultation/1", f"{GI}/paper/1"]
    assert ergebnis.abbruch == "HTTP 503" and ergebnis.zurueckgestellt == 1
    assert list(versuche) == [f"{GI}/consultation/1"]


async def test_fensterende_vor_dem_vorlagenabruf() -> None:
    lauf = Lauf({f"{GI}/consultation/1": Antwort(beratung(1))})
    original = lauf.abrufen

    async def abrufen_und_schliessen(url: str) -> Antwort:
        antwort = await original(url)
        lauf.offen = False  # das Fenster endet während des Beratungsabrufs
        return antwort

    lauf.abrufen = abrufen_und_schliessen  # type: ignore[method-assign]
    ergebnis, versuche = await lauf.los([f"{GI}/consultation/1"])
    assert lauf.abgerufen == [f"{GI}/consultation/1"] and lauf.gespeichert == []
    assert ergebnis.abbruch == "Abruffenster beendet" and ergebnis.zurueckgestellt == 1 and versuche == {}
