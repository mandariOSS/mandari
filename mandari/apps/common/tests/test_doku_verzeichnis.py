# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verzeichnis der Implementierungsnotizen in ``docs/README.md`` (Issue #962).

Jede Notiz ``docs/*.md`` auf oberster Ebene steht in der Tabelle von ``docs/README.md``, als ``NAME.md`` in
Backticks. Früher fehlten dort 17 Notizen (etwa Kryptokonzept, SLA und Release-Politik), weil niemand das
Verzeichnis beim Anlegen einer Notiz mitzog. Umgekehrt nennt die Tabelle keine Notiz, die es nicht gibt.
"""

from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[4]
DOCS = WURZEL / "docs"
VERZEICHNIS = DOCS / "README.md"


def _notizen() -> list[str]:
    return sorted(pfad.name for pfad in DOCS.glob("*.md") if pfad.name != "README.md")


def test_jede_notiz_steht_im_verzeichnis() -> None:
    text = VERZEICHNIS.read_text(encoding="utf-8")
    notizen = _notizen()
    assert notizen, "keine Notizen unter docs/ gefunden"
    fehlend = [name for name in notizen if f"`{name}`" not in text]
    assert not fehlend, f"in docs/README.md nachtragen: {', '.join(fehlend)}"


def test_verzeichnis_nennt_nur_vorhandene_notizen() -> None:
    erste_spalten = [
        zeile.split("|")[1] for zeile in VERZEICHNIS.read_text(encoding="utf-8").splitlines() if zeile.startswith("| `")
    ]
    genannt = [name for spalte in erste_spalten for name in re.findall(r"`([^`]+\.md)`", spalte)]
    assert genannt, "keine Tabellenzeilen in docs/README.md gefunden"
    unbekannt = [name for name in genannt if not (DOCS / name).is_file()]
    assert not unbekannt, f"in docs/README.md genannt, aber nicht vorhanden: {', '.join(unbekannt)}"
