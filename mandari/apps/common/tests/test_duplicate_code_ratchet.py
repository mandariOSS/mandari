# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für doppelten Code (``scripts/check_duplicate_code_ratchet.py``, Issue #1113): Klone und doppelte
Zeilen nach jscpd dürfen je Format nur sinken; Tests, Migrationen und Fremdcode zählen nicht.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "scripts" / "check_duplicate_code_ratchet.py"
PYPROJECT = REPO / "mandari" / "pyproject.toml"
PR_CHECK = REPO / ".github" / "workflows" / "pr-check.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"

Stand = dict[str, dict[str, int]]


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_duplicate_code_ratchet", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_aufruf_folgt_der_konfiguration(ratchet: ModuleType, tmp_path: Path) -> None:
    konfiguration = ratchet.load_config()
    befehl = ratchet.command("jscpd", konfiguration, tmp_path)
    umfang = konfiguration["umfang"]
    assert befehl[: 1 + len(umfang["pfade"])] == ["jscpd", *umfang["pfade"]]

    def wert(schalter: str) -> str:
        return str(befehl[befehl.index(schalter) + 1])

    assert wert("--format").split(",") == umfang["formate"]
    assert (wert("--min-tokens"), wert("--min-lines")) == (str(umfang["mindest_token"]), str(umfang["mindest_zeilen"]))
    assert wert("--reporters") == "json" and wert("--output") == str(tmp_path)
    # Ausnahmen je gelesenem Pfad verankert: Ein freies **/tests/** träfe auch Verzeichnisse oberhalb des Repositorys
    ausgenommen = set(wert("--ignore").split(","))
    for pfad in umfang["pfade"]:
        assert {f"{pfad}/**/migrations/**", f"{pfad}/**/tests/**", f"{pfad}/**/node_modules/**"} <= ausgenommen
    assert {"mandari/**/tests_e2e/**", "mandari/**/static/**", "mandari/**/conftest.py"} <= ausgenommen
    assert not [muster for muster in ausgenommen if muster.startswith("**")]
    # Jeden gelesenen Pfad gibt es; sonst zählte die Prüfung still weniger
    assert all((REPO / pfad).is_dir() for pfad in umfang["pfade"])


def _bericht(root: Path) -> dict[str, Any]:
    unter_windows = "//?/" + root.resolve().as_posix()
    return {
        "statistics": {
            "formats": {
                "python": {"clones": 2, "duplicatedLines": 31, "lines": 900},
                "markup": {"clones": 1, "duplicatedLines": 12, "lines": 300},
                "json": {"clones": 5, "duplicatedLines": 80, "lines": 400},
            }
        },
        "duplicates": [
            {
                "format": "markup",
                "lines": 12,
                "tokens": 140,
                "firstFile": {"name": f"{root.resolve()}/mandari/templates/a.html", "start": 3, "end": 14},
                "secondFile": {"name": f"{root.resolve()}/mandari/templates/b.html", "start": 8, "end": 19},
            },
            {
                "format": "python",
                "lines": 19,
                "tokens": 210,
                "firstFile": {"name": f"{unter_windows}/mandari/apps/a.py", "start": 10, "end": 28},
                "secondFile": {"name": f"{unter_windows}/ingestor/src/b.py", "start": 40, "end": 58},
            },
        ],
    }


def test_bericht_von_jscpd_wird_je_format_gelesen(ratchet: ModuleType, tmp_path: Path) -> None:
    konfiguration = {"umfang": {"formate": ["python", "markup", "typescript"]}}
    stand, klone = ratchet.parse_report(_bericht(tmp_path), konfiguration, tmp_path)
    # Nur die Formate der Konfiguration; eines ohne Dateien steht mit 0 da
    assert stand == {
        "markup": {"klone": 1, "zeilen": 12},
        "python": {"klone": 2, "zeilen": 31},
        "typescript": {"klone": 0, "zeilen": 0},
    }
    # Die größten zuerst, Pfade relativ zum Repo-Root (auch mit dem Windows-Präfix von jscpd)
    assert [(k.lines, k.first, k.second) for k in klone] == [
        (19, "mandari/apps/a.py:10-28", "ingestor/src/b.py:40-58"),
        (12, "mandari/templates/a.html:3-14", "mandari/templates/b.html:8-19"),
    ]


def test_jeder_gestiegene_wert_ist_ein_verstoss(ratchet: ModuleType) -> None:
    baseline: Stand = {"python": {"klone": 2, "zeilen": 31}, "markup": {"klone": 1, "zeilen": 12}}
    stand: Stand = {
        "python": {"klone": 2, "zeilen": 33},
        "markup": {"klone": 1, "zeilen": 12},
        "typescript": {"klone": 1, "zeilen": 14},
    }
    verstoesse, gesunken = ratchet.compare(stand, baseline)
    assert verstoesse == [
        "python: 33 zeilen, erlaubt 31",
        # Ein Format ohne Eintrag in der Baseline hat die Grenze 0
        "typescript: 1 klone, erlaubt 0",
        "typescript: 14 zeilen, erlaubt 0",
    ]
    assert gesunken == []


def test_abbau_wird_gemeldet_und_update_senkt_nur(ratchet: ModuleType) -> None:
    baseline: Stand = {"python": {"klone": 2, "zeilen": 31}, "markup": {"klone": 1, "zeilen": 12}}
    stand: Stand = {"python": {"klone": 1, "zeilen": 40}, "markup": {"klone": 0, "zeilen": 0}}
    verstoesse, gesunken = ratchet.compare(stand, baseline)
    assert verstoesse == ["python: 40 zeilen, erlaubt 31"]
    assert gesunken == ["markup: 0 klone statt 1", "markup: 0 zeilen statt 12", "python: 1 klone statt 2"]
    assert ratchet.lowered_baseline(stand, baseline) == {
        "python": {"klone": 1, "zeilen": 31},
        "markup": {"klone": 0, "zeilen": 0},
    }


def _lauf(
    ratchet: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stand: Stand,
    baseline: Stand | None,
    *argumente: str,
) -> tuple[int, Path]:
    datei = tmp_path / "scripts" / "duplicate_code_baseline.json"
    datei.parent.mkdir(exist_ok=True)
    if baseline is not None:
        ratchet.write_baseline(baseline, datei)
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "BASELINE", datei)
    monkeypatch.setattr(ratchet, "measure", lambda: (stand, []))
    return ratchet.main(list(argumente)), datei


def test_lauf_scheitert_wenn_ein_wert_steigt(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline: Stand = {"python": {"klone": 2, "zeilen": 31}}
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, {"python": {"klone": 3, "zeilen": 45}}, baseline, "--update")
    assert code == 1
    assert "python: 3 klone, erlaubt 2" in capsys.readouterr().out
    assert ratchet.load_baseline(datei) == baseline, "--update erhöht nie"


def test_lauf_ohne_baseline_erlaubt_nichts(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, {"python": {"klone": 1, "zeilen": 12}}, None)
    assert code == 1
    assert not datei.exists(), "eine fehlende Baseline darf nicht still aus dem heutigen Stand entstehen"


def test_lauf_besteht_am_stand_der_baseline_und_update_zieht_nach(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline: Stand = {"python": {"klone": 2, "zeilen": 31}}
    stand: Stand = {"python": {"klone": 1, "zeilen": 14}}
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, stand, baseline)
    assert code == 0
    assert "check_duplicate_code_ratchet.py --update" in capsys.readouterr().out
    assert ratchet.load_baseline(datei) == baseline, "ohne --update bleibt die Baseline stehen"

    code, datei = _lauf(ratchet, monkeypatch, tmp_path, stand, baseline, "--update")
    assert code == 0
    assert ratchet.load_baseline(datei) == stand


def test_bericht_nennt_zahlen_grenzen_und_die_groessten_klone(ratchet: ModuleType, tmp_path: Path) -> None:
    konfiguration = {"umfang": {"formate": ["python", "markup"]}}
    stand, klone = ratchet.parse_report(_bericht(tmp_path), konfiguration, tmp_path)
    bericht = ratchet.report(stand, klone, {"python": {"klone": 2, "zeilen": 31}})
    assert "**3 Klone, 43 doppelte Zeilen.**" in bericht
    assert "| Python | 2 | 2 | 31 | 31 |" in bericht
    assert "### Die 2 größten Klone" in bericht
    assert "| 19 | Python | `mandari/apps/a.py:10-28` | `ingestor/src/b.py:40-58` |" in bericht


BLOCK = "\n".join(
    [
        "def auswertung_{name}(werte, grenze):",
        "    ergebnis = []",
        "    for index, wert in enumerate(werte):",
        "        if wert is None:",
        "            continue",
        "        if wert > grenze and index % 2 == 0:",
        "            ergebnis.append((index, wert * 2, 'hoch'))",
        "        elif wert < -grenze and index % 3 == 0:",
        "            ergebnis.append((index, wert * 3, 'tief'))",
        "        else:",
        "            ergebnis.append((index, wert, 'mittel'))",
        "    summe = sum(eintrag[1] for eintrag in ergebnis)",
        "    anzahl = len([eintrag for eintrag in ergebnis if eintrag[2] != 'mittel'])",
        "    mittel = summe / len(ergebnis) if ergebnis else 0",
        "    return {{'summe': summe, 'anzahl': anzahl, 'mittel': mittel, 'werte': ergebnis}}",
        "",
    ]
)


def _jscpd_vorhanden() -> bool:
    neben_python = any((Path(sys.executable).parent / name).is_file() for name in ("jscpd", "jscpd.exe"))
    return neben_python or shutil.which("jscpd") is not None


@pytest.mark.skipif(not _jscpd_vorhanden(), reason="jscpd nicht installiert (läuft im Job Qualität)")
def test_messung_findet_kopierten_block_ausserhalb_von_tests_und_migrationen(
    ratchet: ModuleType, tmp_path: Path
) -> None:
    """Mit jscpd selbst: derselbe Block in zwei Modulen ist ein Klon; in Tests und Migrationen zählt er nicht."""
    rumpf = BLOCK.split("\n", 1)[1]
    dateien = {
        "app/a.py": BLOCK.format(name="a"),
        "app/b.py": "import os\n\n\n" + BLOCK.format(name="b"),
        "app/tests/test_a.py": BLOCK.format(name="test"),
        "app/migrations/0001_initial.py": BLOCK.format(name="migration"),
    }
    for name, inhalt in dateien.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(inhalt, encoding="utf-8")
    konfiguration = ratchet.load_config()
    konfiguration["umfang"] = {**konfiguration["umfang"], "pfade": ["app"]}

    stand, klone = ratchet.measure(tmp_path, konfiguration)

    assert stand["python"]["klone"] == 1
    assert stand["python"]["zeilen"] >= rumpf.count("\n") - 1
    assert stand["markup"] == {"klone": 0, "zeilen": 0}
    (klon,) = klone
    assert {klon.first.split(":")[0], klon.second.split(":")[0]} == {"app/a.py", "app/b.py"}


def test_pruefung_laeuft_im_job_qualitaet_mit_der_version_aus_pyproject() -> None:
    """Eine andere Version von jscpd zählt anders: Schritt und Entwicklungsumgebung nennen dieselbe."""
    dev = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    (anforderung,) = [eintrag for eintrag in dev if eintrag.startswith("jscpd")]
    schritte = yaml.safe_load(PR_CHECK.read_text(encoding="utf-8"))["jobs"]["qualitaet"]["steps"]
    befehle = [str(schritt.get("run", "")) for schritt in schritte]
    assert any("scripts/check_duplicate_code_ratchet.py" in befehl for befehl in befehle), "Schritt fehlt"
    installiert = {treffer for befehl in befehle for treffer in re.findall(r"jscpd==[\w.]+", befehl)}
    assert installiert == {anforderung}
    # Die Anleitung für die lokale Prüfung nennt dieselbe Version
    assert set(re.findall(r"jscpd==[\w.]+", CONTRIBUTING.read_text(encoding="utf-8"))) == {anforderung}
    # Die Berichte beider Prüfungen liegen als Artefakt am Lauf
    (ablage,) = [s for s in schritte if s.get("with", {}).get("name") == "code-bericht"]
    assert str(ablage["uses"]).startswith("actions/upload-artifact@")
