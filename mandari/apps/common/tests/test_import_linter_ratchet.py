# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet der Schichtregeln (scripts/check_import_linter_ratchet.py, Issue #567): Die Ausnahmen von den
import-linter-Verträgen dürfen nur weniger werden, die Verträge selbst nur strenger, jedes Paket gehört
zu einer Schicht, und Abbau wie Verschärfung werden im selben Pull Request als neue Baseline
festgeschrieben.
"""

from __future__ import annotations

import copy
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
            "source_modules": ["apps.common", "apps.tenants"],
            "forbidden_modules": ["apps.session", "apps.work", "hub", "insight_core"],
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
    assert "OK: keine neuen Ausnahmen, Verträge unverändert" in capsys.readouterr().out


def test_alle_vorhandenen_fachmodule_sind_voneinander_unabhaengig(ratchet: ModuleType) -> None:
    """ADR Schichtenmodell, Regel 3: Jedes Fachmodul der obersten Schicht steht im Unabhängigkeitsvertrag."""
    linter = ratchet.load_config()
    vertraege = {v["id"]: v for v in linter["contracts"]}
    _, oberste = ratchet._parse_layers(vertraege["schichten"]["layers"])[0]
    vorhanden = {modul for modul, optional in oberste.items() if not optional}
    assert "apps.minutes" in vorhanden
    assert vorhanden <= set(vertraege["module-unabhaengig"]["modules"])


def test_konfiguration_im_repo_hat_die_drei_vertraege_und_nimmt_nur_tests_aus(ratchet: ModuleType) -> None:
    linter = ratchet.load_config()
    assert ratchet.contract_problems(linter) == []
    for vertrag in linter["contracts"]:
        platzhalter = [e for e in vertrag["ignore_imports"] if "*" in e]
        assert platzhalter == [ratchet.TEST_EXEMPTION]
    assert {"insight_ai", "insight_search", "insight_sync", "hub"} <= set(linter["root_packages"])


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


# --- Inhalt der Verträge: nur Verschärfungen ------------------------------------------------------


def _aenderung(ratchet: ModuleType, anpassen: Any) -> tuple[list[str], list[str]]:
    """Lockerungen und Verschärfungen, wenn ``anpassen`` die Konfiguration gegenüber der Baseline ändert."""
    vorher = _konfiguration()
    nachher = copy.deepcopy(vorher)
    anpassen(nachher, {v["id"]: v for v in nachher["contracts"]})
    gelockert, verschaerft = ratchet.content_changes(nachher, ratchet.snapshot(vorher))
    return list(gelockert), list(verschaerft)


def _schichten(*schichten: str) -> Any:
    return lambda k, v: v["schichten"].update(layers=list(schichten))


@pytest.mark.parametrize(
    ("anpassen", "erwartet"),
    [
        (lambda k, v: v["module-unabhaengig"]["modules"].remove("apps.work"), "modules: „apps.work“ entfernt"),
        (lambda k, v: v["plattform-fachfrei"]["forbidden_modules"].remove("hub"), "forbidden_modules: „hub“ entfernt"),
        (lambda k, v: v["plattform-fachfrei"]["source_modules"].pop(), "source_modules: „apps.tenants“ entfernt"),
        (lambda k, v: k["root_packages"].remove("hub"), "Einstellungen: root_packages: „hub“ entfernt"),
        (
            lambda k, v: v["plattform-fachfrei"].update(allow_indirect_imports=True),
            "allow_indirect_imports: eingeschaltet",
        ),
        (lambda k, v: v["schichten"].update(allow_indirect_imports="true"), "allow_indirect_imports: eingeschaltet"),
        (
            lambda k, v: v["schichten"].update(unmatched_ignore_imports_alerting="none"),
            "unmatched_ignore_imports_alerting: error → none",
        ),
        (lambda k, v: k.update(exclude_type_checking_imports=True), "exclude_type_checking_imports: eingeschaltet"),
        (lambda k, v: v["schichten"].update(containers=["apps"]), "containers: geändert"),
        (lambda k, v: k.update(include_external_packages=True), "include_external_packages: geändert"),
        (lambda k, v: v["schichten"]["layers"].reverse(), "Schichten umsortiert oder Pakete verschoben"),
        (
            _schichten("apps.session : (apps.portal)", "hub : insight_core : apps.work", "apps.common : apps.tenants"),
            "Schichten umsortiert oder Pakete verschoben",
        ),
        (
            _schichten("apps.session : apps.work : (apps.portal)", "hub : insight_core", "apps.common"),
            "layers: „apps.tenants“ keiner Schicht mehr zugeordnet",
        ),
        (
            _schichten(
                "apps.session : (apps.work) : (apps.portal)", "hub : insight_core", "apps.common : apps.tenants"
            ),
            "layers: „apps.work“ optional",
        ),
    ],
)
def test_gelockerter_oder_umgebauter_vertrag_wird_erkannt(ratchet: ModuleType, anpassen: Any, erwartet: str) -> None:
    gelockert, _ = _aenderung(ratchet, anpassen)
    assert any(erwartet in eintrag for eintrag in gelockert), gelockert


@pytest.mark.parametrize(
    ("anpassen", "erwartet"),
    [
        (lambda k, v: v["module-unabhaengig"]["modules"].append("apps.minutes"), "modules: „apps.minutes“ ergänzt"),
        (lambda k, v: v["plattform-fachfrei"]["forbidden_modules"].append("insight_ai"), "„insight_ai“ ergänzt"),
        (lambda k, v: k["root_packages"].append("insight_ai"), "root_packages: „insight_ai“ ergänzt"),
        (lambda k, v: v["plattform-fachfrei"].update(allow_indirect_imports=False), None),
        (lambda k, v: v["schichten"].update(unmatched_ignore_imports_alerting="error"), None),
        (lambda k, v: v["schichten"].update(name="Anderer Anzeigename"), None),
        (lambda k, v: v["module-unabhaengig"]["modules"].reverse(), None),
        (
            _schichten(
                "apps.session : apps.work : (apps.portal) : apps.minutes",
                "hub : insight_core",
                "apps.common : apps.tenants",
            ),
            "layers: „apps.minutes“ neu zugeordnet",
        ),
        (
            _schichten(
                "apps.session : apps.work : (apps.portal)",
                "hub : insight_core",
                "apps.data",
                "apps.common : apps.tenants",
            ),
            "layers: „apps.data“ neu zugeordnet",
        ),
        (
            _schichten("apps.session | apps.work | (apps.portal)", "hub : insight_core", "apps.common : apps.tenants"),
            "mit „|“ (unabhängig)",
        ),
        (
            _schichten("apps.session : apps.work : apps.portal", "hub : insight_core", "apps.common : apps.tenants"),
            "„apps.portal“ nicht mehr optional",
        ),
    ],
)
def test_verschaerfung_ist_keine_lockerung(ratchet: ModuleType, anpassen: Any, erwartet: str | None) -> None:
    gelockert, verschaerft = _aenderung(ratchet, anpassen)
    assert gelockert == []
    if erwartet is None:
        assert verschaerft == []
    else:
        assert any(erwartet in eintrag for eintrag in verschaerft), verschaerft


def test_unabhaengige_schicht_wieder_gegenseitig_offen_ist_lockerung(ratchet: ModuleType) -> None:
    vorher = _konfiguration()
    vorher["contracts"][0]["layers"][0] = "apps.session | apps.work | (apps.portal)"
    gelockert, verschaerft = ratchet.content_changes(_konfiguration(), ratchet.snapshot(vorher))
    assert gelockert == ["schichten: layers: Schicht apps.portal / apps.session / apps.work mit „:“ statt „|“"]
    assert verschaerft == []


@pytest.fixture
def isoliert(ratchet: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Skript gegen ein eigenes pyproject.toml und eine eigene Baseline laufen lassen."""
    projekt = _projekt(tmp_path / "mandari", ["apps", "apps/common", "hub", "insight_core"])
    monkeypatch.setattr(ratchet, "PROJECT", projekt)
    monkeypatch.setattr(ratchet, "PYPROJECT", projekt / "pyproject.toml")
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    return tmp_path


def _toml(ausnahmen: list[str], module: str = '"apps.common", "hub"') -> str:
    eintraege = ",\n".join(f'    "{e}"' for e in ausnahmen)
    inhalt = {
        "schichten": 'layers = ["hub : insight_core", "apps.common"]',
        "module-unabhaengig": f"modules = [{module}]",
        "plattform-fachfrei": 'source_modules = ["apps.common"]\nforbidden_modules = ["hub", "insight_core"]',
    }
    vertraege = "".join(
        f'\n[[tool.importlinter.contracts]]\nid = "{cid}"\ntype = "{art}"\n{inhalt[cid]}\n'
        f"ignore_imports = [\n{eintraege}\n]\n"
        for cid, art in (
            ("schichten", "layers"),
            ("module-unabhaengig", "independence"),
            ("plattform-fachfrei", "forbidden"),
        )
    )
    return '[tool.importlinter]\nroot_packages = ["apps", "hub", "insight_core"]\n' + vertraege


def _schreiben(ratchet: ModuleType, wurzel: Path, ausnahmen: list[str], baseline: list[str]) -> None:
    """pyproject.toml mit ``ausnahmen``; Baseline mit gleichem Vertragsinhalt und den Ausnahmen ``baseline``."""
    pyproject = wurzel / "mandari" / "pyproject.toml"
    pyproject.write_text(_toml(baseline), encoding="utf-8")
    stand = ratchet.snapshot(ratchet.load_config(pyproject))
    (wurzel / "baseline.json").write_text(json.dumps(stand), encoding="utf-8")
    pyproject.write_text(_toml(ausnahmen), encoding="utf-8")


def _baseline(wurzel: Path) -> dict[str, Any]:
    return dict(json.loads((wurzel / "baseline.json").read_text(encoding="utf-8")))


def test_abbau_ohne_nachgezogene_baseline_schlaegt_fehl(
    ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _schreiben(ratchet, isoliert, [], [AUSNAHME])
    assert ratchet.main([]) == 1
    ausgabe = capsys.readouterr().out
    assert "Baseline noch nicht nachgezogen" in ausgabe
    assert f"  - schichten: {AUSNAHME}" in ausgabe


def test_update_streicht_abgebaute_ausnahmen(ratchet: ModuleType, isoliert: Path) -> None:
    _schreiben(ratchet, isoliert, [], [AUSNAHME])
    assert ratchet.main(["--update"]) == 0
    assert _baseline(isoliert)["ausnahmen"]["schichten"] == []
    assert ratchet.main([]) == 0


def test_update_nimmt_keine_neuen_ausnahmen_auf(
    ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _schreiben(ratchet, isoliert, [AUSNAHME], [])
    assert ratchet.main(["--update"]) == 1
    assert f"  + schichten: {AUSNAHME}" in capsys.readouterr().out
    assert _baseline(isoliert)["ausnahmen"]["schichten"] == []


def test_update_lockert_keinen_vertrag(ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _schreiben(ratchet, isoliert, [], [])
    vorher = _baseline(isoliert)
    (isoliert / "mandari" / "pyproject.toml").write_text(_toml([], module='"apps.common"'), encoding="utf-8")
    assert ratchet.main(["--update"]) == 1
    assert "  ! module-unabhaengig: modules: „hub“ entfernt" in capsys.readouterr().out
    assert _baseline(isoliert) == vorher


def test_verschaerfung_wird_mit_update_festgeschrieben(
    ratchet: ModuleType, isoliert: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _schreiben(ratchet, isoliert, [], [])
    pyproject = isoliert / "mandari" / "pyproject.toml"
    pyproject.write_text(_toml([], module='"apps.common", "hub", "insight_core"'), encoding="utf-8")
    assert ratchet.main([]) == 1
    assert "  ^ module-unabhaengig: modules: „insight_core“ ergänzt" in capsys.readouterr().out
    assert ratchet.main(["--update"]) == 0
    assert _baseline(isoliert)["vertraege"]["module-unabhaengig"]["modules"] == ["apps.common", "hub", "insight_core"]
    assert ratchet.main([]) == 0
    # Die festgeschriebene Verschärfung lässt sich danach nicht still zurücknehmen.
    pyproject.write_text(_toml([]), encoding="utf-8")
    assert ratchet.main([]) == 1
