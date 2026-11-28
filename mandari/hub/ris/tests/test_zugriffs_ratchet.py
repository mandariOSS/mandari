# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für Direktzugriffe auf den RIS-Bestand (``scripts/check_ris_access_ratchet.py``, Issues #522, #1593):
Zugriffe auf ``OParl*.objects`` außerhalb von ``hub`` und ``insight_core`` (K5) und im Code des Bürgerportals in
``insight_core`` (K5b) dürfen je Datei nur sinken.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_ris_access_ratchet.py"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ris_access_ratchet", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


@pytest.mark.parametrize(
    ("quelltext", "anzahl"),
    [
        ("OParlPaper.objects.filter(pk=1)", 1),
        ("OParlPaper.objects.all()\nOParlMeeting.objects.none()", 2),
        ("OParlPaper._default_manager.all()\nOParlPaper._base_manager.all()", 2),
        ("from insight_core import models\nmodels.OParlFile.objects.all()", 1),
        ("from insight_core.models import OParlPaper as Vorlage\nVorlage.objects.all()", 1),
        ("def f():\n    from insight_core.models import OParlMeeting\n    return OParlMeeting.objects.first()", 1),
        # keine RIS-Modelle bzw. keine Manager
        ("SessionOParlTombstone.objects.all()", 0),
        ("OParlPaper.DoesNotExist", 0),
        ("paper.objects", 0),
        ("from apps.work.models import OParlPaper as Fremd\nOParlish = 1", 0),
        ("# OParlPaper.objects.all()\ntext = 'OParlPaper.objects'", 0),
    ],
)
def test_zaehlt_zugriffe_auf_ris_modelle(ratchet: ModuleType, quelltext: str, anzahl: int) -> None:
    assert ratchet.count_accesses(quelltext) == anzahl


@pytest.mark.parametrize(
    ("pfad", "zaehlt"),
    [
        ("apps/work/ris/selectors.py", True),
        ("insight_sync/session_mirror.py", True),
        ("apps/common/management/commands/setup_demo_environment.py", True),
        ("hub/ris/selectors.py", False),
        ("insight_core/views/meetings.py", False),
        ("apps/work/ris/tests/test_services.py", False),
        ("apps/work/tests/helpers.py", False),
        ("apps/work/test_something.py", False),
        ("apps/work/migrations/0001_initial.py", False),
        ("tests_e2e/test_demo.py", False),
        ("conftest.py", False),
    ],
)
def test_umfang_der_zaehlung(ratchet: ModuleType, pfad: str, zaehlt: bool) -> None:
    assert ratchet.is_counted(Path(pfad)) is zaehlt


@pytest.mark.parametrize(
    ("pfad", "portal"),
    [
        ("insight_core/views/meetings.py", True),
        ("insight_core/templatetags/insight_listen.py", True),
        ("insight_core/portal.py", True),
        ("insight_core/services/portal_stats.py", True),
        # Dateivorschau der Dokumentablage, RIS-Bestand und Geodaten gehören zur Drehscheibe
        ("insight_core/views/files.py", False),
        ("insight_core/models.py", False),
        ("insight_core/services/file_store.py", False),
        ("insight_core/services/georeferencing.py", False),
        ("insight_core/portal_extra.py", False),
        ("insight_core/views/tests/test_home.py", False),
        ("insight_core/tests/test_portal.py", False),
        ("apps/work/ris/selectors.py", False),
    ],
)
def test_umfang_des_buergerportals(ratchet: ModuleType, pfad: str, portal: bool) -> None:
    assert ratchet.is_portal(Path(pfad)) is portal
    # Keine Datei zählt doppelt
    assert not (ratchet.is_portal(Path(pfad)) and ratchet.is_counted(Path(pfad)))


def test_jeder_eintrag_des_buergerportals_gibt_es(ratchet: ModuleType) -> None:
    """Ein umbenannter oder umgezogener Eintrag fiele sonst still aus der Zählung."""
    for eintrag in (*ratchet.PORTAL, *ratchet.PORTAL_OHNE):
        pfad = ratchet.PROJECT / eintrag
        assert pfad.is_dir() if eintrag.endswith("/") else pfad.is_file(), eintrag


def test_neue_und_zusaetzliche_zugriffe_sind_verstoesse(ratchet: ModuleType) -> None:
    verstoesse, gesunken = ratchet.compare({"a.py": 3, "b.py": 1, "neu.py": 1}, {"a.py": 2, "b.py": 1})
    assert verstoesse == [
        "a.py: 3 Zugriffe, erlaubt 2",
        "neu.py: 1 neue Zugriffe (Datei steht nicht in der Baseline)",
    ]
    assert gesunken == []


def test_abbau_muss_festgeschrieben_werden(ratchet: ModuleType) -> None:
    verstoesse, gesunken = ratchet.compare({"a.py": 1}, {"a.py": 2, "weg.py": 4})
    assert verstoesse == []
    assert gesunken == ["a.py: 1 statt 2", "weg.py: 0 statt 4"]


def test_update_senkt_nur(ratchet: ModuleType) -> None:
    aktuell = {"a.py": 1, "b.py": 5, "neu.py": 2}
    basis = {"a.py": 2, "b.py": 3, "weg.py": 4}
    assert ratchet.lowered_baseline(aktuell, basis) == {"a.py": 1, "b.py": 3}


def test_main_meldet_verstoesse_und_abbau(
    ratchet: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    projekt = tmp_path / "mandari"
    (projekt / "apps" / "work").mkdir(parents=True)
    datei = projekt / "apps" / "work" / "lesen.py"
    datei.write_text("OParlPaper.objects.all()\nOParlMeeting.objects.all()\n", encoding="utf-8")
    basis = tmp_path / "baseline.json"
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "PROJECT", projekt)
    monkeypatch.setattr(ratchet, "BASELINE", basis)

    ratchet.write_baseline({"apps/work/lesen.py": 1})
    assert ratchet.main([]) == 1
    assert "apps/work/lesen.py: 2 Zugriffe, erlaubt 1" in capsys.readouterr().out
    # --update erhöht nie
    assert ratchet.main(["--update"]) == 1
    assert ratchet.load_baseline() == {"apps/work/lesen.py": 1}

    ratchet.write_baseline({"apps/work/lesen.py": 3})
    assert ratchet.main([]) == 1
    assert "--update" in capsys.readouterr().out
    assert ratchet.main(["--update"]) == 0
    assert json.loads(basis.read_text(encoding="utf-8"))["files"] == {"apps/work/lesen.py": 2}
    assert ratchet.main([]) == 0


def test_abschnitt_buergerportal_getrennt(
    ratchet: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    projekt = tmp_path / "mandari"
    (projekt / "insight_core" / "views").mkdir(parents=True)
    (projekt / "insight_core" / "services").mkdir()
    (projekt / "insight_core" / "views" / "home.py").write_text("OParlBody.objects.first()\n", encoding="utf-8")
    # Bestand: zählt in keinem Abschnitt
    (projekt / "insight_core" / "services" / "file_store.py").write_text("OParlFile.objects.all()\n", encoding="utf-8")
    basis = tmp_path / "baseline.json"
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "PROJECT", projekt)
    monkeypatch.setattr(ratchet, "BASELINE", basis)

    assert ratchet.measure(section="files") == {}
    assert ratchet.measure(section="buergerportal") == {"insight_core/views/home.py": 1}
    ratchet.write_baseline({}, section="files")
    assert ratchet.main([]) == 1
    assert "insight_core/views/home.py: 1 neue Zugriffe" in capsys.readouterr().out

    # Ein Abschnitt lässt den anderen unberührt
    ratchet.write_baseline({"insight_core/views/home.py": 2}, section="buergerportal")
    ratchet.write_baseline({}, section="files")
    assert ratchet.load_baseline(section="buergerportal") == {"insight_core/views/home.py": 2}
    assert ratchet.main(["--update"]) == 0
    daten = json.loads(basis.read_text(encoding="utf-8"))
    assert list(daten) == ["beschreibung", "files", "buergerportal"]
    assert daten["buergerportal"] == {"insight_core/views/home.py": 1}
    assert ratchet.main([]) == 0
    assert "(K5b): 1 (Baseline 1)" in capsys.readouterr().out


@pytest.mark.parametrize("abschnitt", ["files", "buergerportal"])
def test_ausgelieferter_stand_haelt_die_baseline_ein(ratchet: ModuleType, abschnitt: str) -> None:
    verstoesse, gesunken = ratchet.compare(ratchet.measure(section=abschnitt), ratchet.load_baseline(section=abschnitt))
    assert verstoesse == [], "Neue Direktzugriffe: bitte hub/ris/selectors.py nutzen"
    assert gesunken == [], "Baseline mit scripts/check_ris_access_ratchet.py --update nachziehen"


def test_seiten_und_rahmen_des_portals_lesen_nur_ueber_die_fassade(ratchet: ModuleType) -> None:
    """Seiten, Einstieg je Körperschaft und Kontext des Bürgerportals sind frei von Direktzugriffen (Issue #1593)."""
    stand = ratchet.measure(section="buergerportal")
    rahmen = ("insight_core/views/", "insight_core/portal.py", "insight_core/context_processors.py")
    assert [pfad for pfad in stand if pfad.startswith(rahmen)] == []
