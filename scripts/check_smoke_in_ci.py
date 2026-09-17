# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meldet Smoke-Skripte, die in keinem CI-Schritt aufgerufen werden.

Von 64 Skripten liefen 42 in der CI. Sieben der übrigen waren unbemerkt
verfallen (Issue #249) — eines davon verdeckte einen echten Fehler: Die
Sichtbarkeit von Dokumenten berücksichtigte weder Federführung noch Mitarbeit,
sodass eine Zuweisung zwar eine Benachrichtigung auslöste, das Dokument danach
aber nicht zu öffnen war.

Ein Skript, das nirgends läuft, prüft nichts — es sieht nur so aus.

    python scripts/check_smoke_in_ci.py
"""

from __future__ import annotations

import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPTE = WURZEL / "scripts"
WORKFLOWS = WURZEL / ".github" / "workflows"

#: Skripte, die bewusst nicht in der CI laufen. Jeder Eintrag braucht einen Grund.
AUSNAHMEN: dict[str, str] = {
    "smoke_georef.py": "braucht Zugriff auf OpenStreetMap-Dienste",
}


def main() -> int:
    if not WORKFLOWS.is_dir():
        sys.exit(f"{WORKFLOWS} nicht gefunden")

    workflow_text = "\n".join(p.read_text(encoding="utf-8") for p in WORKFLOWS.glob("*.yml"))
    alle = sorted(p.name for p in SKRIPTE.glob("smoke_*.py"))
    fehlend = [name for name in alle if name not in workflow_text and name not in AUSNAHMEN]

    unnoetige = [name for name in AUSNAHMEN if name in workflow_text]

    if fehlend:
        print(f"{len(fehlend)} von {len(alle)} Smoke-Skripten laufen in keinem CI-Schritt:\n")
        for name in fehlend:
            print(f"  - {name}")
        print(
            "\nEin Skript, das nirgends laeuft, verfaellt unbemerkt.\n"
            "Entweder in .github/workflows/pr-check.yml aufnehmen, loeschen —\n"
            "oder mit Begruendung in AUSNAHMEN dieser Datei eintragen."
        )
        return 1

    if unnoetige:
        print("Diese Eintraege in AUSNAHMEN sind ueberholt, die Skripte laufen inzwischen:\n")
        for name in unnoetige:
            print(f"  - {name}")
        return 1

    print(f"OK: {len(alle)} Smoke-Skripte, alle in der CI oder begruendet ausgenommen ({len(AUSNAHMEN)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
