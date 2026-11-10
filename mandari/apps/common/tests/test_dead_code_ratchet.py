# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für toten Python-Code (``scripts/check_dead_code_ratchet.py``, Issue #1113): Die Liste der Funde
von vulture darf nur kürzer werden; Einstiegspunkte des Frameworks stehen in
``scripts/dead_code_ratchet.toml`` und zählen nicht.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "scripts" / "check_dead_code_ratchet.py"
PYPROJECT = REPO / "mandari" / "pyproject.toml"
PR_CHECK = REPO / ".github" / "workflows" / "pr-check.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dead_code_ratchet", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _fund(ratchet: ModuleType, datei: str, name: str, zeilen: int = 3, art: str = "function") -> Any:
    return ratchet.Finding(file=datei, line=1, kind=art, name=name, size=zeilen)


def test_namen_aus_vorlagen(ratchet: ModuleType) -> None:
    vorlage = (
        "<p>ueberschrift bleibt Text</p>\n"
        "{{ ticket.helpful_percentage|floatformat }}\n"
        "{% if sitzung.is_past and not gast %}{% url 'work:liste' org_slug=organisation.slug %}{% endif %}\n"
        '<c-form.field :field="form.email" label="beschriftung" />\n'
    )
    namen = ratchet.template_names(vorlage)
    assert {"helpful_percentage", "is_past", "gast", "slug", "form", "email"} <= namen
    # Fließtext und feste Attributwerte sind keine Verwendung
    assert not {"ueberschrift", "bleibt", "beschriftung"} & namen


def test_namen_aus_punktpfaden_in_zeichenketten(ratchet: ModuleType) -> None:
    quelltext = (
        'MIDDLEWARE = ["apps.common.middleware.SecurityHeadersMiddleware"]\n'
        "ziel = 'paket.modul:starte'\n"
        'layer.group_send(gruppe, {"type": "notizen.sync"})\n'
        'schluessel = "einzelwort"\n'
    )
    namen = ratchet.dotted_string_names(quelltext)
    assert {"SecurityHeadersMiddleware", "starte", "notizen_sync"} <= namen
    assert "einzelwort" not in namen


@pytest.mark.parametrize(
    ("pfad", "zaehlt"),
    [
        ("mandari/apps/work/faction/services.py", True),
        ("mandari/apps/work/tasks/views/panel.py", True),
        ("mandari/apps/common/management/commands/setup_roles.py", True),
        ("mandari/apps/common/admin_mixins.py", True),
        ("shared/mandari_oparl/kennungen.py", True),
        ("ingestor/src/metrics.py", True),
        ("mandari/apps/work/faction/tests/test_services.py", False),
        ("mandari/apps/common/tests/factories.py", False),
        ("mandari/tests_e2e/test_demo.py", False),
        ("mandari/conftest.py", False),
        ("ingestor/tests/test_sync.py", False),
        ("mandari/apps/work/migrations/0001_initial.py", False),
        ("mandari/apps/session/admin.py", False),
        ("mandari/insight_core/admin/quellen.py", False),
        ("mandari/apps/work/apps.py", False),
        ("scripts/smoke_pwa.py", False),
        ("mandari/apps/accounts/oidc/validator.py", False),
    ],
)
def test_umfang_der_zaehlung(ratchet: ModuleType, pfad: str, zaehlt: bool) -> None:
    assert ratchet.is_counted(pfad, ratchet.load_config()) is zaehlt


def test_konfiguration_zaehlt_keine_variablen_und_begruendet_jeden_einzelfall(ratchet: ModuleType) -> None:
    konfiguration = ratchet.load_config()
    arten = set(konfiguration["umfang"]["arten"])
    assert arten <= set(ratchet.KINDS)
    assert not arten & {"variable", "attribute"}, "Modellfelder und Meta-Angaben wären sonst Funde"
    for eintrag in konfiguration.get("ausnahmen", []):
        assert (REPO / eintrag["datei"]).is_file(), eintrag
        assert eintrag["namen"] and eintrag["grund"].strip(), eintrag


def test_einzelfaelle_nehmen_aus_und_ueberholte_fallen_auf(ratchet: ModuleType) -> None:
    funde = [
        _fund(ratchet, "a.py", "_handle_save", art="method"),
        _fund(ratchet, "a.py", "_handle_delete", art="method"),
        _fund(ratchet, "a.py", "wirklich_tot"),
        _fund(ratchet, "b.py", "_handle_save", art="method"),
    ]
    konfiguration = {
        "ausnahmen": [
            {"datei": "a.py", "namen": ["_handle_*"], "grund": "Verteilung über getattr"},
            {"datei": "c.py", "namen": ["gibt_es_nicht_mehr"], "grund": "früher über getattr"},
        ]
    }
    uebrig, ueberholt = ratchet.apply_exceptions(funde, konfiguration)
    assert [(f.file, f.name) for f in uebrig] == [("a.py", "wirklich_tot"), ("b.py", "_handle_save")]
    assert ueberholt == ["c.py: gibt_es_nicht_mehr"]


def test_neuer_fund_ist_ein_verstoss_und_wird_genannt(ratchet: ModuleType) -> None:
    baseline = {"a.py": ["alt"], "b.py": ["is_past", "is_past"]}
    funde = [
        _fund(ratchet, "a.py", "alt"),
        _fund(ratchet, "a.py", "neu"),
        _fund(ratchet, "b.py", "is_past", art="property"),
        _fund(ratchet, "b.py", "is_past", art="property"),
        _fund(ratchet, "b.py", "is_past", art="property"),
        _fund(ratchet, "c.py", "alt"),
    ]
    neu, abgebaut = ratchet.compare(funde, baseline)
    # Derselbe Name in einer anderen Datei oder ein drittes Mal in derselben Datei ist neu
    assert [(f.file, f.name) for f in neu] == [("a.py", "neu"), ("b.py", "is_past"), ("c.py", "alt")]
    assert abgebaut == []


def test_abbau_wird_gemeldet_und_update_streicht_nur(ratchet: ModuleType) -> None:
    baseline = {"a.py": ["alt", "weg"], "b.py": ["weg"]}
    funde = [_fund(ratchet, "a.py", "alt"), _fund(ratchet, "a.py", "neu")]
    neu, abgebaut = ratchet.compare(funde, baseline)
    assert [(f.file, f.name) for f in neu] == [("a.py", "neu")]
    assert abgebaut == ["a.py: weg", "b.py: weg"]
    # --update nimmt den neuen Fund nicht auf
    assert ratchet.lowered_baseline(funde, baseline) == {"a.py": ["alt"]}


def _lauf(
    ratchet: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    funde: list[Any],
    baseline: dict[str, list[str]] | None,
    *argumente: str,
    ueberholt: tuple[str, ...] = (),
) -> tuple[int, Path]:
    datei = tmp_path / "scripts" / "dead_code_baseline.json"
    datei.parent.mkdir(exist_ok=True)
    if baseline is not None:
        ratchet.write_baseline(baseline, datei)
    monkeypatch.setattr(ratchet, "ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "BASELINE", datei)
    monkeypatch.setattr(ratchet, "measure", lambda: (funde, list(ueberholt)))
    return ratchet.main(list(argumente)), datei


def test_lauf_scheitert_an_neuem_fund_und_nennt_ihn(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    funde = [_fund(ratchet, "a.py", "alt"), _fund(ratchet, "a.py", "neu", zeilen=12)]
    code, _ = _lauf(ratchet, monkeypatch, tmp_path, funde, {"a.py": ["alt"]})
    ausgabe = capsys.readouterr().out
    assert code == 1
    assert "2 Funde (Baseline 1)" in ausgabe
    assert "a.py:1: Funktion neu (12 Zeilen)" in ausgabe


def test_lauf_ohne_baseline_erlaubt_nichts(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, [_fund(ratchet, "a.py", "alt")], None)
    assert code == 1
    assert not datei.exists(), "eine fehlende Baseline darf nicht still aus dem heutigen Stand entstehen"


def test_lauf_besteht_am_stand_der_baseline_und_weist_auf_abbau_hin(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = {"a.py": ["alt", "weg"]}
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, [_fund(ratchet, "a.py", "alt")], baseline)
    assert code == 0
    assert "check_dead_code_ratchet.py --update" in capsys.readouterr().out
    assert ratchet.load_baseline(datei) == baseline, "ohne --update bleibt die Baseline stehen"

    code, datei = _lauf(ratchet, monkeypatch, tmp_path, [_fund(ratchet, "a.py", "alt")], baseline, "--update")
    assert code == 0
    assert ratchet.load_baseline(datei) == {"a.py": ["alt"]}
    assert json.loads(datei.read_text(encoding="utf-8"))["funde"] == 1


def test_update_erhoeht_die_baseline_nie(ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    funde = [_fund(ratchet, "a.py", "alt"), _fund(ratchet, "a.py", "neu")]
    code, datei = _lauf(ratchet, monkeypatch, tmp_path, funde, {"a.py": ["alt"]}, "--update")
    assert code == 1
    assert ratchet.load_baseline(datei) == {"a.py": ["alt"]}


def test_lauf_scheitert_an_ueberholtem_einzelfall(
    ratchet: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _lauf(ratchet, monkeypatch, tmp_path, [], {}, ueberholt=("a.py: _handle_*",))
    assert code == 1
    assert "a.py: _handle_*" in capsys.readouterr().out


def test_bericht_nennt_zahl_grenze_und_die_groessten_posten(ratchet: ModuleType) -> None:
    funde = [_fund(ratchet, f"modul_{i}.py", f"funktion_{i}", zeilen=40 - i) for i in range(12)]
    bericht = ratchet.report(funde, limit=12)
    assert "**12 Funde (Grenze 12), zusammen 414 Zeilen.**" in bericht
    assert "### Die 10 größten Posten" in bericht
    tabelle = bericht.split("### Die 10 größten Posten")[1].split("<details>")[0]
    assert "`funktion_9`" in tabelle and "`funktion_10`" not in tabelle
    assert "modul_11.py:1: Funktion funktion_11 (29 Zeilen)" in bericht


BEISPIEL = {
    "app/dienste.py": (
        "from django.dispatch import receiver\n\n\n"
        "def wirklich_tot():\n    return 1\n\n\n"
        "def nur_im_test_verwendet():\n    return 2\n\n\n"
        "def aus_den_einstellungen():\n    return 3\n\n\n"
        "@receiver(None)\ndef bei_signal(sender, **kwargs):\n    return None\n\n\n"
        "class Sitzung:\n"
        "    ungenutztes_feld = 1\n\n"
        "    @property\n    def nur_in_der_vorlage(self):\n        return True\n\n"
        "    @property\n    def tote_property(self):\n        return False\n\n"
        "    def clean_titel(self):\n        return ''\n\n"
        "    def notizen_sync(self, event):\n        return event\n\n\n"
        "def sende():\n"
        '    return Sitzung(), {"type": "notizen.sync"}, "app.dienste.aus_den_einstellungen"\n\n\n'
        "sende()\n"
    ),
    "app/tests/test_dienste.py": (
        "from app.dienste import nur_im_test_verwendet\n\n\n"
        "def hilfsfunktion_im_test():\n    return nur_im_test_verwendet()\n"
    ),
    "app/admin.py": "class SitzungAdmin:\n    def anzeige_spalte(self, obj):\n        return obj\n",
    "app/migrations/0001_initial.py": "def vorgabe():\n    return 0\n",
    "app/templates/app/sitzung.html": "{% if sitzung.nur_in_der_vorlage %}ja{% endif %}\n",
    "app/node_modules/paket/tot.py": "def fremdcode():\n    return 0\n",
}


def test_messung_zaehlt_nur_toten_anwendungscode(ratchet: ModuleType, tmp_path: Path) -> None:
    """Mit vulture selbst: Einstiegspunkte, Vorlagen, Tests und Zeichenketten gelten als Verwendung."""
    pytest.importorskip("vulture")
    for name, inhalt in BEISPIEL.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(inhalt, encoding="utf-8")
    konfiguration = ratchet.load_config()
    konfiguration["umfang"] = {**konfiguration["umfang"], "pfade": ["app"]}
    konfiguration["ausnahmen"] = []

    funde, ueberholt = ratchet.measure(tmp_path, konfiguration)

    assert sorted((f.file, f.kind, f.name) for f in funde) == [
        ("app/dienste.py", "function", "wirklich_tot"),
        ("app/dienste.py", "property", "tote_property"),
    ]
    assert ueberholt == []


def test_pruefung_laeuft_im_job_qualitaet_mit_der_version_aus_pyproject() -> None:
    """Eine andere Version von vulture zählt anders: Schritt und Entwicklungsumgebung nennen dieselbe."""
    dev = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    (anforderung,) = [eintrag for eintrag in dev if eintrag.startswith("vulture")]
    schritte = yaml.safe_load(PR_CHECK.read_text(encoding="utf-8"))["jobs"]["qualitaet"]["steps"]
    befehle = [str(schritt.get("run", "")) for schritt in schritte]
    assert any("scripts/check_dead_code_ratchet.py" in befehl for befehl in befehle), "Schritt im Job qualitaet fehlt"
    installiert = {treffer for befehl in befehle for treffer in re.findall(r"vulture==[\w.]+", befehl)}
    assert installiert == {anforderung}
    # Die Anleitung für die lokale Prüfung nennt dieselbe Version
    assert set(re.findall(r"vulture==[\w.]+", CONTRIBUTING.read_text(encoding="utf-8"))) == {anforderung}
