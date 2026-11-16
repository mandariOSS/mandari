# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adresssuche der Nachbarschaftsseite ohne Straßenverzeichnis: Der Rückfall auf Photon zählt je Aufrufer und
insgesamt, gleiche Eingaben kommen aus dem Cache. Der Takt bei Photon gilt für die ganze Plattform; ohne diese
Grenzen belegte ihn ein einzelner Aufrufer. Es geht kein Aufruf ins Netz.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import Client

from insight_core import throttle
from insight_core.models import OParlBody
from insight_core.services import neighborhood

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _frisch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Leerer Cache und eine stehende Uhr für die Zeitfenster der Zähler (kein Wechsel der Minute im Test)."""
    cache.clear()
    monkeypatch.setattr(throttle, "time", types.SimpleNamespace(time=lambda: 1_800_000_000.0))
    yield
    cache.clear()


@pytest.fixture
def photon(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Ersatz für Photon: liefert je Eingabe einen Treffer und merkt sich die Eingaben."""
    eingaben: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        eingabe = str(kwargs["params"]["q"])
        eingaben.append(eingabe)
        if eingabe.startswith("Fehler"):
            return httpx.Response(503, request=httpx.Request("GET", url))
        inhalt = {"features": [{"geometry": {"coordinates": [7.62, 51.96]}, "properties": {"name": eingabe}}]}
        return httpx.Response(200, json=inhalt, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.services.neighborhood.httpx.get", get)
    return eingaben


def test_ein_aufrufer_belegt_nur_seinen_anteil(photon: list[str]) -> None:
    grenze = throttle.setting("INSIGHT_PHOTON_PER_IP_MINUTE")

    antworten = [
        neighborhood.photon_autocomplete(None, f"Domplatz {n}", client_key="198.51.100.7") for n in range(grenze + 10)
    ]

    # Über der Grenze: keine Vorschläge und kein Abruf bei Photon
    assert [bool(antwort) for antwort in antworten] == [True] * grenze + [False] * 10
    assert len(photon) == grenze
    # Ein anderer Besucher bekommt weiter Vorschläge
    assert neighborhood.photon_autocomplete(None, "Prinzipalmarkt", client_key="203.0.113.4")
    assert len(photon) == grenze + 1


def test_gleiche_eingabe_kommt_aus_dem_cache_und_zaehlt_nicht(photon: list[str], geo_body: OParlBody) -> None:
    grenze = throttle.setting("INSIGHT_PHOTON_PER_IP_MINUTE")

    antworten = [
        neighborhood.photon_autocomplete(geo_body, eingabe, client_key="198.51.100.8")
        for eingabe in ["Prinzipalmarkt", "prinzipalmarkt ", "PRINZIPALMARKT"] * grenze
    ]

    assert len(photon) == 1 and all(antwort == antworten[0] for antwort in antworten)
    # Für eine andere Kommune gilt der Treffer nicht: anderer Mittelpunkt, anderer Ausschnitt
    assert neighborhood.photon_autocomplete(None, "Prinzipalmarkt", client_key="198.51.100.8")
    assert len(photon) == 2


def test_gesamtgrenze_gilt_ueber_alle_aufrufer(photon: list[str], settings: Any) -> None:
    settings.INSIGHT_PHOTON_PER_MINUTE = 3

    antworten = [
        neighborhood.photon_autocomplete(None, f"Hafenweg {n}", client_key=f"198.51.100.{n}") for n in range(6)
    ]

    assert [bool(antwort) for antwort in antworten] == [True] * 3 + [False] * 3 and len(photon) == 3


def test_fehlschlag_wird_nicht_gemerkt(photon: list[str]) -> None:
    assert neighborhood.photon_autocomplete(None, "Fehler am Dom", client_key="198.51.100.9") == []
    assert neighborhood.photon_autocomplete(None, "Fehler am Dom", client_key="198.51.100.9") == []

    assert photon == ["Fehler am Dom", "Fehler am Dom"]


def test_ansicht_zaehlt_je_adresse_und_kuerzt_die_eingabe(photon: list[str], geo_body: OParlBody) -> None:
    grenze = throttle.setting("INSIGHT_PHOTON_PER_IP_MINUTE")
    besucher = Client(REMOTE_ADDR="198.51.100.10")
    sitzung = besucher.session
    sitzung["active_body_id"] = str(geo_body.id)
    sitzung.save()

    lang = besucher.get("/insight/nachbarschaft/autocomplete/", {"q": "A" * 500})
    weitere = [
        besucher.get("/insight/nachbarschaft/autocomplete/", {"q": f"Domplatz {n}"}).json() for n in range(grenze + 4)
    ]

    assert lang.status_code == 200 and photon[0] == "A" * neighborhood.MAX_QUERY_LEN
    assert [bool(antwort) for antwort in weitere] == [True] * (grenze - 1) + [False] * 5
    assert len(photon) == grenze
