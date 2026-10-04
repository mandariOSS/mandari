# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Gate der Ereignisverträge (Issue #519, ``scripts/check_event_contracts.py``).

Jeder Ereignistyp im Code hat ein Schema, bestehende Versionen ändern sich nur additiv, und der
Ereigniskatalog unter ``docs/`` ist aus dem Register erzeugt. Die Tests laufen auch im Job „Test“: Wer ein
Schema ändert und den Katalog nicht neu erzeugt, sieht es dort ebenfalls.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from hub.contracts import get_registry

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_event_contracts.py"


@pytest.fixture(scope="module")
def pruefung() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_event_contracts", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


QUELLE = """
from typing import Final

VERSION: Final = 2
GEAENDERT: Final = "ris.paper.changed"
BEFEHL = "submission.submit"

def melden(r):
    publish("ris.meeting.scheduled", version=1, payload={})
    publish("ris.paper.unbekannt_changed", version=VERSION)
    r.publish("mandari:sync:trigger", "{}")
    r.publish("ris.notes", "{}")

@command_handler("submission.withdraw", 3)
def zurueckziehen(command): ...
"""


def test_namen_und_versionen_aus_dem_quelltext(pruefung: ModuleType) -> None:
    gefunden = {(r.name, r.kind, r.version) for r in pruefung.references(QUELLE, "beispiel.py")}

    assert gefunden == {
        ("ris.paper.changed", "event", 2),  # Modulkonstante VERSION
        ("ris.meeting.scheduled", "event", 1),  # feste Version im Aufruf
        ("ris.paper.unbekannt_changed", "event", 2),
        ("ris.notes", "event", None),  # kein Ereignisname, aber ein Aufruf von publish() mit Bereich ris
        ("submission.withdraw", "command", 3),
    }


def test_befunde_ohne_schema_falsche_art_oder_version(pruefung: ModuleType) -> None:
    probleme = pruefung.reference_problems(pruefung.references(QUELLE, "beispiel.py"), get_registry())

    assert probleme == [
        "beispiel.py:5: ris.paper.changed v2 hat kein Schema (vorhanden: (1,))",
        "beispiel.py:10: Ereignis ris.paper.unbekannt_changed hat kein Schema im Register",
        "beispiel.py:12: Ereignis ris.notes hat kein Schema im Register",
        "beispiel.py:14: submission.withdraw v3 hat kein Schema (vorhanden: (1,))",
    ]
    befehl_als_ereignis = pruefung.references('publish("submission.submit", version=1)', "x.py")
    assert pruefung.reference_problems(befehl_als_ereignis, get_registry()) == [
        "x.py:1: submission.submit ist im Register kein Ereignis"
    ]


def test_tests_und_migrationen_zaehlen_nicht(pruefung: ModuleType, tmp_path: Path) -> None:
    for relativ in ("app/tests/test_x.py", "app/migrations/0001.py", "app/test_y.py", "app/conftest.py"):
        datei = tmp_path / relativ
        datei.parent.mkdir(parents=True, exist_ok=True)
        datei.write_text('X = "ris.paper.vanished"\n', encoding="utf-8")
    (tmp_path / "app" / "dienst.py").write_text('X = "ris.paper.vanished"\n', encoding="utf-8")

    assert [p.name for p in pruefung.python_files([tmp_path])] == ["dienst.py"]


def test_jeder_ereignistyp_im_code_hat_ein_schema(pruefung: ModuleType) -> None:
    nennungen = pruefung.code_references()

    assert pruefung.reference_problems(nennungen, get_registry()) == []
    # Erzeuger in hub/ris und im Ingestor sowie Abonnenten werden gefunden
    pfade = {n.path for n in nennungen}
    assert {"mandari/hub/ris/session_events.py", "ingestor/src/storage/ris_events.py"} <= pfade
    assert any(n.version == 1 for n in nennungen if n.path == "ingestor/src/storage/ris_events.py")


def test_ereigniskatalog_ist_aktuell(pruefung: ModuleType) -> None:
    assert pruefung.catalog_current(), (
        "docs/EREIGNISKATALOG.md neu erzeugen: python scripts/check_event_contracts.py --write-catalog"
    )


def test_veralteter_katalog_faellt_auf(pruefung: ModuleType, tmp_path: Path) -> None:
    datei = tmp_path / "EREIGNISKATALOG.md"
    datei.write_text(pruefung.rendered_catalog().replace("Vorlage geändert", "Vorlage verändert"), encoding="utf-8")

    assert not pruefung.catalog_current(datei)
    assert not pruefung.catalog_current(tmp_path / "fehlt.md")
    datei.write_text(pruefung.rendered_catalog().replace("\n", "\r\n"), encoding="utf-8", newline="")
    assert pruefung.catalog_current(datei)


def test_ablage_auf_der_platte_und_im_git_stand(pruefung: ModuleType) -> None:
    ablage = pruefung.contracts_on_disk()

    assert len(ablage) == len(get_registry()) + 1
    assert "envelope/v1" in ablage and "ris.paper.changed/v1" in ablage
    if shutil.which("git") is None or not (pruefung.ROOT / ".git").exists():
        pytest.skip("kein Git-Arbeitsverzeichnis")
    try:
        stand = pruefung.contracts_at("HEAD")
    except subprocess.CalledProcessError:
        pytest.skip("HEAD nicht lesbar")
    assert set(stand) <= set(ablage) | {"envelope/v1"}
    with pytest.raises(subprocess.CalledProcessError):
        pruefung.contracts_at("kein-solcher-stand-519")
