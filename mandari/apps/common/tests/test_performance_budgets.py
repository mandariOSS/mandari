# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bewertungslogik des Performance-Budget-Gates (scripts/check_performance_budgets.py, Issue #228):
zu viele Abfragen sind ein Fehler, überschrittene Zeit nur eine Warnung, Fehlerseiten fallen durch,
und das Nachziehen senkt Abfrage-Budgets, hebt sie aber nie an.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_performance_budgets.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """Das Skript als Modul laden — es richtet Django erst in main() ein, der Import ist frei von Nebenwirkungen."""
    spec = importlib.util.spec_from_file_location("check_performance_budgets", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    # dataclasses löst die Annotationen über sys.modules auf — ohne Eintrag scheitert der Import
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


BUDGETS = {
    "startseite": {"beschreibung": "Startseite", "abfragen": 10, "ms": 500},
    "liste": {"beschreibung": "Liste", "abfragen": 20, "ms": 800},
}


def test_zu_viele_abfragen_sind_eine_verletzung(gate: ModuleType) -> None:
    messungen = {"startseite": gate.Messwert(abfragen=11, ms=40, status=200)}
    ergebnis = gate.bewerten(messungen, BUDGETS)
    assert ergebnis.verletzungen == ["startseite: 11 Abfragen, Budget 10"]
    assert not ergebnis.warnungen


def test_zeitueberschreitung_ist_nur_eine_warnung(gate: ModuleType) -> None:
    messungen = {"startseite": gate.Messwert(abfragen=10, ms=900, status=200)}
    ergebnis = gate.bewerten(messungen, BUDGETS)
    assert not ergebnis.verletzungen
    assert ergebnis.warnungen == ["startseite: 900 ms, Budget 500 ms"]


def test_fehlerseite_und_fehlendes_budget_fallen_durch(gate: ModuleType) -> None:
    messungen = {
        "startseite": gate.Messwert(abfragen=1, ms=5, status=500),
        "unbekannt": gate.Messwert(abfragen=1, ms=5, status=200),
    }
    ergebnis = gate.bewerten(messungen, BUDGETS)
    assert "startseite: HTTP 500 statt 200" in ergebnis.verletzungen
    assert any(zeile.startswith("unbekannt: kein Budget") for zeile in ergebnis.verletzungen)


def test_weniger_abfragen_sind_ein_hinweis(gate: ModuleType) -> None:
    messungen = {"liste": gate.Messwert(abfragen=15, ms=100, status=200)}
    ergebnis = gate.bewerten(messungen, BUDGETS)
    assert not ergebnis.verletzungen
    assert ergebnis.verbesserungen == ["liste: 15 Abfragen, Budget 20 — nachziehen"]


def test_nachziehen_senkt_abfragen_und_hebt_nie_an(gate: ModuleType) -> None:
    messungen = {
        "startseite": gate.Messwert(abfragen=12, ms=40, status=200),  # schlechter: Budget bleibt
        "liste": gate.Messwert(abfragen=15, ms=2000, status=200),  # besser: Budget sinkt, Zeit bleibt
        "neu": gate.Messwert(abfragen=7, ms=33, status=200),  # neu: Ist-Wert, Zeit großzügig
    }
    neu = gate.budgets_nachziehen(messungen, BUDGETS, {"neu": "Neue Seite"})
    assert neu["startseite"]["abfragen"] == 10
    assert neu["liste"]["abfragen"] == 15
    assert neu["liste"]["ms"] == 800
    assert neu["neu"] == {"beschreibung": "Neue Seite", "abfragen": 7, "ms": 500}
