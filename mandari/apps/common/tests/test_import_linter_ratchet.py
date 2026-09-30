# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet der Schichtregeln (scripts/check_import_linter_ratchet.py, Issue #567): Die Ausnahmen von den
import-linter-Verträgen dürfen nur weniger werden, jedes Paket gehört zu einer Schicht, und ein
Abbau wird im selben Pull Request als neue Baseline festgeschrieben.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_import_linter_ratchet.py"

AUSNAHME = "apps.tenants.models -> insight_core.models"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_import_linter_ratchet", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _vertraege(**ausnahmen: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "id": "schichten",
            "type": "layers",
            "layers": ["apps.session : apps.work : (apps.portal)", "hub : insight_core", "apps.common : apps.tenants"],
            "ignore_imports": ausnahmen.get("schichten", []),
        },
        {
            "id": "module-unabhaengig",
            "type": "independence",
            "modules": ["apps.session", "apps.work"],
            "ignore_imports": ausnahmen.get("module_unabhaengig", []),
        },
        {
            "id": "plattform-fachfrei",
            "type": "forbidden",
            "ignore_imports": ausnahmen.get("plattform_fachfrei", []),
        },
    ]


def _konfiguration(**ausnahmen: list[str]) -> dict[str, Any]:
    return {"root_packages": ["apps", "hub", "insight_core"], "contracts": _vertraege(**ausnahmen)}


def _projekt(wurzel: Path, pakete: list[str]) -> Path:
    for paket in pakete:
        (wurzel / paket).mkdir(parents=True, exist_ok=True)
        (wurzel / paket / "__init__.py").write_text("", encoding="utf-8")
    return wurzel


def test_stand_im_repo_besteht_den_ratchet(ratchet: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert ratchet.main([]) == 0
    assert "OK: keine neuen Ausnahmen" in capsys.readouterr().out


def test_konfiguration_im_repo_hat_die_drei_vertraege_und_nimmt_nur_tests_aus(ratchet: ModuleType) -> None:
    linter = ratchet.load_config()
    assert ratchet.contract_problems(linter) == []
    for vertrag in linter["contracts"]:
        platzhalter = [e for e in vertrag["ignore_imports"] if "*" in e]
        assert platzhalter == [ratchet.TEST_EXEMPTION]
    assert {"insight_ai", "insight_search", "insight_sync", "oparl_api", "hub"} <= set(linter["root_packages"])


def test_neue_ausnahme_wird_erkannt(ratchet: ModuleType) -> None:
    neu = "apps.common.mail -> apps.work.models"
    hinzu, weg = ratchet.compare({"schichten": [AUSNAHME, neu]}, {"schichten": [AUSNAHME]})
    assert hinzu == [f"schichten: {neu}"]
    assert weg == []


def test_tausch_zaehlt_als_neue_ausnahme(ratchet: ModuleType) -> None:
    neu = "apps.common.mail -> apps.work.models"
    hinzu, weg = ratchet.compare({"schichten": [neu]}, {"schichten": [AUSNAHME]})
    assert hinzu == [f"schichten: {neu}"]
    assert weg == [f"schichten: {AUSNAHME}"]


def test_platzhalter_und_doppelte_eintraege_sind_unzulaessig(ratchet: ModuleType) -> None:
    konfiguration = _konfiguration(
        schichten=[ratchet.TEST_EXEMPTION, "apps.common.** -> apps.work.**", AUSNAHME, AUSNAHME]
    )
    aktuell, probleme = ratchet.exceptions(konfiguration)
    assert aktuell["schichten"] == ["apps.common.** -> apps.work.**", AUSNAHME]
    assert any("Platzhalter sind nicht erlaubt" in p for p in probleme)
    assert any("Ausnahme doppelt" in p for p in probleme)


def test_testfreistellung_zaehlt_nicht(ratchet: ModuleType) -> None:
    aktuell, probleme = ratchet.exceptions(_konfiguration(schichten=[ratchet.TEST_EXEMPTION]))
    assert aktuell["schichten"] == []
    assert probleme == []


def test_fehlender_oder_veraenderter_vertrag(ratchet: ModuleType) -> None:
    konfiguration = _konfiguration()
    konfiguration["contracts"] = konfiguration["contracts"][:2]
    konfiguration["contracts"][1]["type"] = "layers"
    assert ratchet.contract_problems(konfiguration) == [
        "Vertrag „module-unabhaengig“ muss vom Typ independence sein, ist layers",
        "Vertrag „plattform-fachfrei“ fehlt",
    ]


def test_neue_app_und_neues_paket_brauchen_eine_schicht(ratchet: ModuleType, tmp_path: Path) -> None:
    projekt = _projekt(
        tmp_path,
        [
            "apps",
            "apps/session",
            "apps/work",
            "apps/common",
            "apps/tenants",
            "apps/data",
            "hub",
            "insight_core",
            "mandari",
            "neu",
        ],
    )
    assert ratchet.coverage_problems(_konfiguration(), projekt) == [
        "Paket „neu“ fehlt in root_packages",
        "Paket „neu“ ist keiner Schicht zugeordnet",
        "App „apps.data“ ist keiner Schicht zugeordnet",
    ]


def test_optionale_schicht_zaehlt_als_zugeordnet(ratchet: ModuleType, tmp_path: Path) -> None:
    projekt = _projekt(tmp_path, ["apps", "apps/portal", "hub", "insight_core"])
    assert ratchet.coverage_problems(_konfiguration(), projekt) == []


@pytest.fixture
def isoliert(ratchet: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Skript gegen ein eigenes pyproject.toml und eine eigene Baseline laufen lassen."""
    projekt = _projekt(tmp_path / "mandari", ["apps", "apps/common", "hub", "insight_core"])
    monkeypatch.setattr(ratchet, "PROJECT", projekt)
    monkeypatch.setattr(ratchet, "PYPROJECT", projekt / "pyproject.toml")
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    return tmp_path


def _schreiben(wurzel: Path, ausnahmen: list[str], baseline: list[str]) -> None:
    eintraege = ",\n".join(f'    "{e}"' for e in ausnahmen)
    vertraege = "".join(
        f'\n[[tool.importlinter.contracts]]\nid = "{cid}"\ntype = "{art}"\n'
        f'layers = ["apps.common", "hub : insight_core"]\nignore_imports = [\n{eintraege}\n]\n'
        for cid, art in (
            ("schichten", "layers"),
            ("module-unabhaengig", "independence"),
            ("plattform-fachfrei", "forbidden"),
        )
    )
    (wurzel / "mandari" / "pyproject.toml").write_text(
        '[tool.importlinter]\nroot_packages = ["apps", "hub", "insight_core"]\n' + vertraege, encoding="utf-8"
    )
    stand = dict.fromkeys(("schichten", "module-unabhaengig", "plattform-fachfrei"), baseline)
    (wurzel / "baseline.json").write_text(json.dumps(stand), encoding="utf-8")


def test_abbau_ohne_nachgezogene_baseline_schlaegt_fehl(
    ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _schreiben(isoliert, [], [AUSNAHME])
    assert ratchet.main([]) == 1
    ausgabe = capsys.readouterr().out
    assert "Baseline noch nicht nachgezogen" in ausgabe
    assert f"  - schichten: {AUSNAHME}" in ausgabe


def test_update_streicht_abgebaute_ausnahmen(ratchet: ModuleType, isoliert: Path) -> None:
    _schreiben(isoliert, [], [AUSNAHME])
    assert ratchet.main(["--update"]) == 0
    assert json.loads((isoliert / "baseline.json").read_text(encoding="utf-8"))["schichten"] == []
    assert ratchet.main([]) == 0


def test_update_nimmt_keine_neuen_ausnahmen_auf(
    ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _schreiben(isoliert, [AUSNAHME], [])
    assert ratchet.main(["--update"]) == 1
    assert f"  + schichten: {AUSNAHME}" in capsys.readouterr().out
    assert json.loads((isoliert / "baseline.json").read_text(encoding="utf-8"))["schichten"] == []
