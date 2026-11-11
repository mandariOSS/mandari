# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rückportierung in Pflegelinien (scripts/rueckportieren.py, docs/RELEASE_POLITIK.md Abschnitt 5, Issue #1341).

Die Liste der Pflegelinien steht nur in der Tabelle „Unterstützte Versionen“ in SECURITY.md; das Skript liest
sie dort. Bricht das Format der Tabelle, darf das nicht still „keine Linie“ ergeben.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "scripts" / "rueckportieren.py"


def _skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rueckportieren", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


TABELLE = """
| Version | Unterstützt | bis |
|---------|-------------|-----|
| jüngstes Release (Image-Tag `latest` oder `v<Version>`) | alle Korrekturen | Erscheinen der nächsten Version |
| 0.16 (Pflegeversion 1. Quartal 2027) | Sicherheitskorrekturen ab „mittel“, kritische Fehler | 12.07.2027 |
| 0.12 (Pflegeversion 4. Quartal 2026) | Sicherheitskorrekturen ab „mittel“, kritische Fehler | 17.05.2027 |
| 0.11 (Übergang, bisherige Regel) | Sicherheitskorrekturen ab „mittel“, kritische Fehler | 03.05.2027 |
| übrige und ältere Releases | nein | – |
"""


def test_liest_linien_aelteste_zuerst() -> None:
    linien = _skript().pflegelinien(TABELLE, dt.date(2027, 1, 1))
    assert [linie for linie, _ in linien] == ["0.11", "0.12", "0.16"]
    assert linien[0][1] == dt.date(2027, 5, 3)


def test_abgelaufene_linien_fallen_weg() -> None:
    skript = _skript()
    assert [linie for linie, _ in skript.pflegelinien(TABELLE, dt.date(2027, 5, 4))] == ["0.12", "0.16"]
    # Der letzte Tag zählt noch
    assert [linie for linie, _ in skript.pflegelinien(TABELLE, dt.date(2027, 5, 17))] == ["0.12", "0.16"]
    assert skript.pflegelinien(TABELLE, dt.date(2027, 7, 13)) == []


def test_security_md_nennt_die_pflegelinien() -> None:
    """Die echte Tabelle ist lesbar: Am 11.11.2026 sind 0.11 und 0.12 in Pflege."""
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    linien = [linie for linie, _ in _skript().pflegelinien(text, dt.date(2026, 11, 11))]
    assert {"0.11", "0.12"} <= set(linien)


def test_migrationen_werden_erkannt() -> None:
    skript = _skript()
    assert skript.enthaelt_migration(["mandari/apps/work/meetings/migrations/0050_x.py", "README.md"])
    assert not skript.enthaelt_migration(["mandari/apps/work/meetings/services.py", "docs/migrations.md"])


def test_ci_ausloeser_der_linie_wird_erkannt() -> None:
    """Linien vor 0.13 lösen für PRs gegen sich keine CI aus; das Skript bricht dann ab, statt PRs ohne Prüfung zu öffnen."""
    skript = _skript()
    assert skript.ci_ausloeser_vorhanden((REPO / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8"))
    assert not skript.ci_ausloeser_vorhanden("on:\n  pull_request:\n    branches:\n      - dev\n      - main\n")


def test_ci_ausloeser_zaehlt_nur_unter_pull_request() -> None:
    """Ein Kommentar oder ein Eintrag unter push löst für Pull Requests gegen die Linie keine CI aus."""
    skript = _skript()
    nur_kommentar = "on:\n  pull_request:\n    branches:\n      - dev\n      # - 'release/v*'\n"
    nur_push = "on:\n  push:\n    branches:\n      - 'release/v*'\n  pull_request:\n    branches:\n      - dev\n"
    anderer_schluessel = "on:\n  pull_request:\n    paths:\n      - 'release/v*'\n"
    unter_jobs = "on:\n  pull_request:\n    branches:\n      - dev\njobs:\n  a:\n    branches:\n      - 'release/v*'\n"
    for workflow in (nur_kommentar, nur_push, anderer_schluessel, unter_jobs):
        assert not skript.ci_ausloeser_vorhanden(workflow), workflow
    mit_typen = 'on:\n  pull_request:\n    types: [opened]\n    branches:\n      - dev\n      - "release/v*"\n'
    assert skript.ci_ausloeser_vorhanden(mit_typen)
