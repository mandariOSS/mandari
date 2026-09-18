# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Integrität: verwaiste Blöcke und Alpine-Komponenten ohne Definition.

Anlass: Der Dokument-Import und das Anlegen von Aufgaben lieferten ihr Skript in
``{% block extra_js %}`` aus, ``work/base_work.html`` kennt aber nur ``extra_scripts``. Django
verwirft solche Blöcke stillschweigend – im Browser fehlten ``importForm`` und
``assignmentHandler``, beide Formulare waren tot. Die Prüfskripte laufen hier im Testlauf mit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SKRIPTE = Path(__file__).resolve().parents[4] / "scripts"


def _lade(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SKRIPTE / f"{name}.py")
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules[name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_keine_verwaisten_bloecke() -> None:
    assert _lade("check_template_blocks").pruefen() == []


def test_alle_alpine_komponenten_definiert() -> None:
    _lade("check_template_blocks")
    assert _lade("check_alpine_components").pruefen() == []


@pytest.fixture
def vorlagen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Eigenes Vorlagenverzeichnis mit Basis (Block ``extra_scripts``) und leerem Frontend."""
    root = tmp_path / "mandari"
    tpl = root / "templates"
    tpl.mkdir(parents=True)
    (root / "frontend").mkdir()
    (tpl / "base.html").write_text(
        "<html>{% block content %}{% endblock %}{% block extra_scripts %}{% endblock %}</html>", encoding="utf-8"
    )
    bloecke = _lade("check_template_blocks")
    alpine = _lade("check_alpine_components")
    for modul in (bloecke, alpine):
        monkeypatch.setattr(modul, "ROOT", root)
        monkeypatch.setattr(modul, "TEMPLATE_DIRS", [tpl])
    monkeypatch.setattr(alpine, "FRONTEND_DIRS", [root / "frontend"])
    return tpl


SEITE = """{{% extends "base.html" %}}
{{% block content %}}<form x-data="importForm()"></form>{{% endblock %}}
{{% block {block} %}}<script>function importForm() {{ return {{}} }}</script>{{% endblock %}}
"""


def test_import_fehler_wird_erkannt(vorlagen: Path) -> None:
    (vorlagen / "import.html").write_text(SEITE.format(block="extra_js"), encoding="utf-8")
    assert _lade("check_template_blocks").pruefen() == [
        "templates/import.html: Block 'extra_js' fehlt in der Vererbungskette"
    ]
    befunde = _lade("check_alpine_components").pruefen()
    assert len(befunde) == 1 and "importForm" in befunde[0]


def test_richtiger_block_ist_sauber(vorlagen: Path) -> None:
    (vorlagen / "import.html").write_text(SEITE.format(block="extra_scripts"), encoding="utf-8")
    assert _lade("check_template_blocks").pruefen() == []
    assert _lade("check_alpine_components").pruefen() == []


def test_komponente_aus_dem_bundle_zaehlt(vorlagen: Path) -> None:
    (vorlagen / "board.html").write_text(
        '{% extends "base.html" %}{% block content %}<div x-data="kanbanBoard"></div>{% endblock %}', encoding="utf-8"
    )
    alpine = _lade("check_alpine_components")
    assert len(alpine.pruefen()) == 1
    (vorlagen.parent / "frontend" / "work.ts").write_text("Alpine.data('kanbanBoard', board)\n", encoding="utf-8")
    assert alpine.pruefen() == []
