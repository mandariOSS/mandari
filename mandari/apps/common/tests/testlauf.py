# SPDX-License-Identifier: AGPL-3.0-or-later
"""
pytest-Erweiterungen für die CI (Issue #935): Migrationstests kennzeichnen, die Suite auf Teile verteilen und
belegen, dass dabei kein Test verloren geht. Eingebunden über ``pytest_plugins`` in ``mandari/conftest.py``.

Migrationstests
    Tests, die Migrationen zurück- und wieder vorspielen (``MigrationExecutor(...).migrate(...)`` oder
    ``call_command("migrate", ...)`` im Test selbst), dauern einzeln bis zu zwei Minuten. Sie bekommen das
    Kennzeichen ``migrationen`` automatisch. Die CI führt sie im Job „Migrationstests“ aus
    (``-m migrationen``), die Teile des Jobs „Test“ lassen sie aus (``-m "not migrationen"``). Spielt ein
    Test ohne Kennzeichen trotzdem Migrationen ab (etwa über eine Hilfsfunktion), scheitert er mit einem
    Hinweis; dann ``@pytest.mark.migrationen`` setzen.

Aufteilung (``--teil N/M``)
    Führt nur Teil N von M aus. Verteilt werden ganze Testdateien nach den gemessenen Laufzeiten in
    ``mandari/testdauern.json``: die längste zuerst, jeweils auf den Teil mit der bisher kleinsten Summe.
    Dateien ohne Messwert zählen mit der mittleren Dauer je Test. Die Aufteilung hängt nur von den
    gesammelten Tests und dieser Datei ab und ist deshalb in jedem Teil und jedem xdist-Worker dieselbe.

Protokoll (``--teil-protokoll VERZEICHNIS``)
    ``auswahl.json``: alle gesammelten Tests, die Migrationstests darunter, die Kandidaten nach ``-m`` und
    die Tests dieses Teils (geschrieben vom ersten xdist-Worker bzw. ohne xdist vom Lauf selbst).
    ``ergebnis.json``: je Test das Ergebnis und je Datei die Laufzeit (geschrieben vom steuernden Prozess).
    ``scripts/ci_testteile.py`` prüft damit, dass über alle Teile jeder Test genau einmal lief, und
    erneuert die Laufzeiten.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import textwrap
from collections import Counter, defaultdict
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

KENNZEICHEN = "migrationen"
DAUERN_DATEI = Path(__file__).resolve().parents[3] / "testdauern.json"
AUSWAHL_DATEI = "auswahl.json"
ERGEBNIS_DATEI = "ergebnis.json"

_ALLE = pytest.StashKey[list[str]]()
_MIGRATIONSTESTS = pytest.StashKey[list[str]]()

# Rangfolge, wenn ein Test mehrere Berichte hat (Aufbau, Aufruf, Abbau): das Schlechteste zählt
_RANG = {"bestanden": 0, "uebersprungen": 1, "fehlgeschlagen": 2, "fehler": 3}


# --- Migrationstests erkennen ---------------------------------------------------------------------------


@cache
def _erwaehnt_migrate(datei: str) -> bool:
    try:
        return "migrate" in Path(datei).read_text(encoding="utf-8")
    except OSError:
        return False


def _ist_migrate_aufruf(knoten: ast.AST) -> bool:
    if not isinstance(knoten, ast.Call):
        return False
    ziel = knoten.func
    if isinstance(ziel, ast.Attribute) and ziel.attr == "migrate":
        return True  # MigrationExecutor(connection).migrate([...])
    name = ziel.id if isinstance(ziel, ast.Name) else ziel.attr if isinstance(ziel, ast.Attribute) else ""
    erstes = knoten.args[0] if knoten.args else None
    return name == "call_command" and isinstance(erstes, ast.Constant) and erstes.value == "migrate"


@cache
def spielt_migrationen_ab(funktion: Callable[..., Any]) -> bool:
    """Ruft die Testfunktion selbst ``….migrate(...)`` oder ``call_command("migrate", ...)`` auf?"""
    try:
        datei = inspect.getsourcefile(funktion)
    except TypeError:
        return False
    # Vorprüfung je Datei: Quelltext nur dort zerlegen, wo "migrate" überhaupt vorkommt
    if not datei or not _erwaehnt_migrate(datei):
        return False
    try:
        baum = ast.parse(textwrap.dedent(inspect.getsource(funktion)))
    except (OSError, TypeError, SyntaxError):
        return False
    return any(_ist_migrate_aufruf(knoten) for knoten in ast.walk(baum))


# --- Aufteilung -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Teil:
    nummer: int
    anzahl: int

    @classmethod
    def lesen(cls, wert: str) -> Teil:
        nummer, trenner, anzahl = wert.partition("/")
        try:
            teil = cls(int(nummer), int(anzahl))
        except ValueError:
            teil = cls(0, 0)
        if not trenner or not 1 <= teil.nummer <= teil.anzahl:
            raise pytest.UsageError(f"--teil erwartet N/M mit 1 <= N <= M, nicht {wert!r}")
        return teil


def datei_der_kennung(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def dauern_lesen(pfad: Path) -> tuple[dict[str, float], float]:
    """Gemessene Sekunden je Testdatei und die mittlere Dauer je Test (für Dateien ohne Messwert)."""
    if not pfad.is_file():
        return {}, 1.0
    daten = json.loads(pfad.read_text(encoding="utf-8"))
    dauern = {str(datei): float(sekunden) for datei, sekunden in daten.get("dateien", {}).items()}
    return dauern, float(daten.get("je_test", 1.0))


def aufteilen(tests_je_datei: dict[str, int], dauern: dict[str, float], je_test: float, anzahl: int) -> list[list[str]]:
    """Ganze Dateien auf ``anzahl`` Teile verteilen, längste zuerst auf den Teil mit der kleinsten Summe."""
    gewicht = {datei: dauern.get(datei, tests * je_test) for datei, tests in tests_je_datei.items()}
    lasten = [0.0] * anzahl
    teile: list[list[str]] = [[] for _ in range(anzahl)]
    for datei in sorted(gewicht, key=lambda d: (-gewicht[d], d)):
        ziel = min(range(anzahl), key=lambda i: (lasten[i], i))
        teile[ziel].append(datei)
        lasten[ziel] += gewicht[datei]
    return teile


def _json_schreiben(pfad: Path, daten: dict[str, Any]) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    zwischen = pfad.with_name(f".{pfad.name}.{os.getpid()}")
    zwischen.write_text(json.dumps(daten, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    zwischen.replace(pfad)


class _Aufteilung:
    """Läuft nach der Auswahl mit ``-m`` (trylast) und behält nur die Dateien des eigenen Teils."""

    def __init__(self, teil: Teil | None, dauern: Path, protokoll: Path | None) -> None:
        self.teil = teil
        self.dauern = dauern
        self.protokoll = protokoll

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        if self.teil is None and self.protokoll is None:
            return
        kandidaten = sorted(item.nodeid for item in items)
        if self.teil is not None:
            dauern, je_test = dauern_lesen(self.dauern)
            tests_je_datei = Counter(datei_der_kennung(item.nodeid) for item in items)
            eigene = set(aufteilen(dict(tests_je_datei), dauern, je_test, self.teil.anzahl)[self.teil.nummer - 1])
            weg = [item for item in items if datei_der_kennung(item.nodeid) not in eigene]
            if weg:
                config.hook.pytest_deselected(items=weg)
                items[:] = [item for item in items if datei_der_kennung(item.nodeid) in eigene]
        worker = getattr(config, "workerinput", {}).get("workerid")
        if self.protokoll is not None and worker in (None, "gw0"):
            _json_schreiben(
                self.protokoll / AUSWAHL_DATEI,
                {
                    "auswahl": config.option.markexpr or "",
                    "teil": self.teil.nummer if self.teil else 1,
                    "teile": self.teil.anzahl if self.teil else 1,
                    "alle": config.stash.get(_ALLE, kandidaten),
                    "migrationstests": config.stash.get(_MIGRATIONSTESTS, []),
                    "kandidaten": kandidaten,
                    "ausgewaehlt": sorted(item.nodeid for item in items),
                },
            )


class _Ergebnisprotokoll:
    """Im steuernden Prozess: Ergebnis je Test und Laufzeit je Datei (unter xdist aus den Berichten der Worker)."""

    def __init__(self, protokoll: Path) -> None:
        self.protokoll = protokoll
        self.ergebnisse: dict[str, str] = {}
        self.dauern: defaultdict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.dauern[datei_der_kennung(report.nodeid)] += report.duration
        if report.failed:
            status = "fehlgeschlagen" if report.when == "call" else "fehler"
        elif report.skipped:
            status = "uebersprungen"
        elif report.when == "call":
            status = "bestanden"
        else:
            return
        bisher = self.ergebnisse.get(report.nodeid)
        if bisher is None or _RANG[status] > _RANG[bisher]:
            self.ergebnisse[report.nodeid] = status

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self) -> None:
        _json_schreiben(
            self.protokoll / ERGEBNIS_DATEI,
            {"ergebnisse": self.ergebnisse, "dauern": {d: round(s, 3) for d, s in self.dauern.items()}},
        )


# --- Hooks ------------------------------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    gruppe = parser.getgroup("mandari", "Aufteilung der Testsuite (CI, Issue #935)")
    gruppe.addoption("--teil", default=None, metavar="N/M", help="nur Teil N von M ausführen (ganze Dateien)")
    gruppe.addoption(
        "--testdauern",
        default=str(DAUERN_DATEI),
        metavar="DATEI",
        help="gemessene Sekunden je Testdatei für --teil (Standard: mandari/testdauern.json)",
    )
    gruppe.addoption(
        "--teil-protokoll",
        default=None,
        metavar="VERZEICHNIS",
        help="Auswahl und Ergebnis je Test als JSON ablegen (Prüfung mit scripts/ci_testteile.py)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{KENNZEICHEN}: spielt Migrationen zurück und wieder vor; wird automatisch gesetzt, eigener CI-Job",
    )
    wert = config.getoption("teil")
    teil = Teil.lesen(wert) if wert else None
    protokoll_wert = config.getoption("teil_protokoll")
    protokoll = Path(protokoll_wert).resolve() if protokoll_wert else None
    config.pluginmanager.register(_Aufteilung(teil, Path(config.getoption("testdauern")), protokoll), "mandari-teil")
    if protokoll is not None and not hasattr(config, "workerinput"):
        config.pluginmanager.register(_Ergebnisprotokoll(protokoll), "mandari-teil-protokoll")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Kennzeichen setzen, bevor ``-m`` auswählt; dazu die vollständige Liste für das Protokoll merken."""
    for item in items:
        if (
            isinstance(item, pytest.Function)
            and item.get_closest_marker(KENNZEICHEN) is None
            and spielt_migrationen_ab(item.function)
        ):
            item.add_marker(KENNZEICHEN)
    config.stash[_ALLE] = sorted(item.nodeid for item in items)
    config.stash[_MIGRATIONSTESTS] = sorted(item.nodeid for item in items if item.get_closest_marker(KENNZEICHEN))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Wächter: Ein Test ohne Kennzeichen darf keine Migrationen abspielen (sonst liefe er im falschen Job)."""
    if item.get_closest_marker(KENNZEICHEN) is not None:
        return (yield)
    from django.db.migrations.executor import MigrationExecutor

    original = MigrationExecutor.migrate
    aufrufe: list[object] = []

    def beobachtet(self: MigrationExecutor, *args: Any, **kwargs: Any) -> Any:
        aufrufe.append(args)
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MigrationExecutor, "migrate", beobachtet)
        ergebnis = yield
    if aufrufe:
        pytest.fail(
            f"{item.nodeid} spielt Migrationen ab, ist aber nicht als Migrationstest gekennzeichnet. "
            "Bitte @pytest.mark.migrationen setzen (eigener CI-Job, siehe apps/common/tests/testlauf.py).",
            pytrace=False,
        )
    return ergebnis
