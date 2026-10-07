# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufteilung der Testsuite für die CI (Issue #935): ``apps/common/tests/testlauf.py`` und ``scripts/ci_testteile.py``.

Die Aufteilung muss vollständig, überschneidungsfrei und in jedem Prozess gleich sein, sonst gingen Tests zwischen
den Teilen verloren. Migrationstests werden am Aufruf von ``….migrate(...)`` erkannt; ein Test ohne Kennzeichen,
der trotzdem migriert, scheitert am Wächter.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.common.tests import testlauf

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "ci_testteile.py"


# --- Migrationstests erkennen -----------------------------------------------------------------------------


def _beispiel_executor() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([("events", None)])


def _beispiel_call_command() -> None:
    call_command("migrate", "events", "zero")


def _beispiel_nur_historischer_stand() -> None:
    # Nur den alten Stand lesen ist kein Migrationstest
    MigrationExecutor(connection).loader.project_state([("events", "0001_initial")])
    call_command("check")


def test_migrationstests_werden_am_aufruf_erkannt() -> None:
    assert testlauf.spielt_migrationen_ab(_beispiel_executor)
    assert testlauf.spielt_migrationen_ab(_beispiel_call_command)
    assert not testlauf.spielt_migrationen_ab(_beispiel_nur_historischer_stand)
    assert not testlauf.spielt_migrationen_ab(len)  # ohne Quelltext


def test_bekannter_migrationstest_wird_erkannt() -> None:
    from apps.events.tests import test_journal

    assert testlauf.spielt_migrationen_ab(test_journal.test_migration_laesst_sich_zurueck_und_wieder_einspielen)
    assert not testlauf.spielt_migrationen_ab(test_journal.test_lease_ist_eindeutig_je_rolle)


class _Element:
    def __init__(self, gekennzeichnet: bool) -> None:
        self.nodeid = "apps/x/tests/test_y.py::test_z"
        self.gekennzeichnet = gekennzeichnet

    def get_closest_marker(self, name: str) -> object | None:
        return object() if self.gekennzeichnet and name == testlauf.KENNZEICHEN else None


def _waechter(element: _Element) -> Generator[None, object, object]:
    return testlauf.pytest_runtest_call(cast(pytest.Item, element))


# Ersatz für MigrationExecutor.migrate im Aufrufteil des Tests (nicht über die Fixture monkeypatch: deren Abbau
# käme erst nach dem Wächter dieses Tests selbst und hinterließe dessen Hülle)


def test_waechter_laesst_ungekennzeichnete_migration_scheitern() -> None:
    aufrufe: list[object] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MigrationExecutor, "migrate", lambda self, *args, **kwargs: aufrufe.append(args))
        lauf = _waechter(_Element(gekennzeichnet=False))
        next(lauf)
        MigrationExecutor.migrate(cast(MigrationExecutor, None), [("events", None)])
        with pytest.raises(pytest.fail.Exception, match="nicht als Migrationstest gekennzeichnet"):
            lauf.send(None)
    assert aufrufe == [([("events", None)],)], "der Aufruf selbst geht durch"


def test_waechter_laesst_gekennzeichnete_und_migrationsfreie_tests_durch() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MigrationExecutor, "migrate", lambda self, *args, **kwargs: None)
        lauf = _waechter(_Element(gekennzeichnet=True))
        next(lauf)
        MigrationExecutor.migrate(cast(MigrationExecutor, None), [])
        with pytest.raises(StopIteration):
            lauf.send(None)

        lauf = _waechter(_Element(gekennzeichnet=False))
        next(lauf)
        with pytest.raises(StopIteration):
            lauf.send(None)


# --- Aufteilung -------------------------------------------------------------------------------------------


def test_teil_wird_streng_gelesen() -> None:
    assert testlauf.Teil.lesen("2/3") == testlauf.Teil(2, 3)
    for falsch in ("0/3", "4/3", "3", "a/b", "1/0", ""):
        with pytest.raises(pytest.UsageError):
            testlauf.Teil.lesen(falsch)


def test_aufteilung_ist_vollstaendig_ueberschneidungsfrei_und_ausgeglichen() -> None:
    dateien = {f"apps/a/tests/test_{i:03}.py": 1 + i % 7 for i in range(200)}
    dauern = {datei: float(1 + (i * 37) % 90) for i, datei in enumerate(dateien) if i % 5}
    teile = testlauf.aufteilen(dateien, dauern, 0.5, 3)

    alle = [datei for teil in teile for datei in teil]
    assert sorted(alle) == sorted(dateien) and len(alle) == len(set(alle))
    lasten = [sum(dauern.get(d, dateien[d] * 0.5) for d in teil) for teil in teile]
    assert max(lasten) - min(lasten) <= max(dauern.values())
    # Gleiche Eingabe, gleiche Aufteilung – unabhängig von der Reihenfolge der Sammlung
    umgekehrt = dict(reversed(list(dateien.items())))
    assert testlauf.aufteilen(umgekehrt, dauern, 0.5, 3) == teile


def test_aufteilung_mit_einem_teil_und_mehr_teilen_als_dateien() -> None:
    assert testlauf.aufteilen({"b.py": 1, "a.py": 2}, {}, 1.0, 1) == [["a.py", "b.py"]]
    assert testlauf.aufteilen({"a.py": 1}, {}, 1.0, 3) == [["a.py"], [], []]


def test_gespeicherte_laufzeiten_decken_die_testdateien_ab() -> None:
    dauern, je_test = testlauf.dauern_lesen(testlauf.DAUERN_DATEI)
    assert testlauf.DAUERN_DATEI.is_file(), "mandari/testdauern.json fehlt (Grundlage von --teil)"
    assert je_test > 0 and dauern
    projekt = testlauf.DAUERN_DATEI.parent
    vorhanden = {
        p.relative_to(projekt).as_posix() for p in projekt.glob("**/tests/test_*.py") if ".venv" not in p.parts
    }
    # Fehlende Dateien zählen mit der mittleren Dauer; verschwundene Einträge dürfen nicht überwiegen
    assert len(set(dauern) - vorhanden) <= len(dauern) // 4, "testdauern.json veraltet: aus dem CI-Artefakt erneuern"


# --- Prüfung der Teile (scripts/ci_testteile.py) -----------------------------------------------------------


def _skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_testteile", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


ALLE = [f"apps/a/tests/test_{i}.py::test_{j}" for i in range(4) for j in range(3)]
MIGRATION = "apps/a/tests/test_0.py::test_0"


def _ablegen(ziel: Path, name: str, auswahl: str, teil: int, teile: int, ausgewaehlt: list[str]) -> None:
    kandidaten = [k for k in ALLE if k != MIGRATION] if auswahl == "not migrationen" else [MIGRATION]
    (ziel / name).mkdir(parents=True)
    daten: dict[str, Any] = {
        "auswahl": auswahl,
        "teil": teil,
        "teile": teile,
        "alle": ALLE,
        "migrationstests": [MIGRATION],
        "kandidaten": kandidaten,
        "ausgewaehlt": ausgewaehlt,
    }
    (ziel / name / "auswahl.json").write_text(json.dumps(daten), encoding="utf-8")
    ergebnis = {"ergebnisse": dict.fromkeys(ausgewaehlt, "bestanden"), "dauern": {"apps/a/tests/test_1.py": 2.0}}
    (ziel / name / "ergebnis.json").write_text(json.dumps(ergebnis), encoding="utf-8")


def _teile(ziel: Path, erster: list[str], zweiter: list[str]) -> None:
    _ablegen(ziel, "testteil-1", "not migrationen", 1, 2, erster)
    _ablegen(ziel, "testteil-2", "not migrationen", 2, 2, zweiter)


def test_pruefung_bestaetigt_vollstaendige_teile(tmp_path: Path) -> None:
    rest = [k for k in ALLE if k != MIGRATION]
    _teile(tmp_path, rest[:5], rest[5:])
    _ablegen(tmp_path, "testteil-migrationstests", "migrationen", 1, 1, [MIGRATION])

    fehler, zeilen, dauern, _ = _skript().pruefen(tmp_path, 2, True)

    assert fehler == []
    assert any(f"**{len(ALLE)}** von {len(ALLE)}" in zeile for zeile in zeilen)
    assert dauern == {"apps/a/tests/test_1.py": 2.0}


def test_pruefung_findet_fehlende_doppelte_und_ausgelassene_tests(tmp_path: Path) -> None:
    rest = [k for k in ALLE if k != MIGRATION]
    _teile(tmp_path / "fehlt", rest[:5], rest[6:])
    _teile(tmp_path / "doppelt", rest[:6], rest[5:])
    _teile(tmp_path / "ohne_migration", rest[:5], rest[5:])

    assert any("in keinem Teil" in f for f in _skript().pruefen(tmp_path / "fehlt", 2, False)[0])
    assert any("in mehreren Teilen" in f for f in _skript().pruefen(tmp_path / "doppelt", 2, False)[0])
    assert _skript().pruefen(tmp_path / "ohne_migration", 2, False)[0] == []
    assert any("war nötig" in f for f in _skript().pruefen(tmp_path / "ohne_migration", 2, True)[0])
    assert any("Teile [1, 2] statt 1 bis 3" in f for f in _skript().pruefen(tmp_path / "ohne_migration", 3, False)[0])
