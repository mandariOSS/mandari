# SPDX-License-Identifier: AGPL-3.0-or-later
"""npm audit für die CI: ausgelieferte Pakete streng, Entwicklungswerkzeuge mit befristeten Ausnahmen.

1. Abhängigkeiten, die im Browser landen (``dependencies``), dürfen keine Meldung ab „high“ haben –
   ohne Ausnahme (``npm audit --omit=dev``).
2. Entwicklungswerkzeuge (``devDependencies``, z. B. Tailwind beim Bauen) dürfen eine Meldung ab „high“
   nur haben, wenn sie in ``mandari/npm-audit-ausnahmen.json`` mit Begründung und Ablaufdatum steht.
   Abgelaufene Ausnahmen lassen die Prüfung scheitern, damit sie nicht liegen bleiben.

Aufruf (aus dem Repository-Wurzelverzeichnis): ``python scripts/npm_audit_pruefen.py``
"""

from __future__ import annotations

import datetime
import json
import pathlib
import shutil
import subprocess
import sys

WURZEL = pathlib.Path(__file__).resolve().parent.parent
PAKET = WURZEL / "mandari"
AUSNAHMEN = PAKET / "npm-audit-ausnahmen.json"
STUFEN = {"high", "critical"}
NPM = shutil.which("npm") or "npm"  # unter Windows npm.cmd


def ghsa(advisory: dict) -> str:
    return str(advisory.get("url", "")).rstrip("/").rsplit("/", 1)[-1]


def main() -> int:
    heute = datetime.date.today()
    fehler: list[str] = []
    gueltig: dict[str, dict] = {}
    for eintrag in json.loads(AUSNAHMEN.read_text(encoding="utf-8")).get("ausnahmen", []):
        if datetime.date.fromisoformat(eintrag["bis"]) < heute:
            fehler.append(f"Ausnahme {eintrag['ghsa']} ({eintrag['paket']}) ist am {eintrag['bis']} abgelaufen")
        else:
            gueltig[eintrag["ghsa"]] = eintrag

    # 1. Ausgelieferte Abhängigkeiten: streng
    if subprocess.run([NPM, "audit", "--omit=dev", "--audit-level=high"], cwd=PAKET).returncode:
        fehler.append("Ausgelieferte Abhängigkeiten haben Meldungen ab „high“ (npm audit --omit=dev)")

    # 2. Alle Abhängigkeiten einschließlich Entwicklungswerkzeugen: nur mit gültiger Ausnahme
    ausgabe = subprocess.run([NPM, "audit", "--json"], cwd=PAKET, capture_output=True, text=True).stdout
    meldungen = json.loads(ausgabe or "{}").get("vulnerabilities", {})
    gemeldet: set[str] = set()
    for name, eintrag in meldungen.items():
        for quelle in eintrag.get("via", []):
            if isinstance(quelle, dict) and quelle.get("severity") in STUFEN:
                kennung = ghsa(quelle)
                gemeldet.add(kennung)
                if kennung in gueltig:
                    print(f"Ausnahme bis {gueltig[kennung]['bis']}: {kennung} in {name} – {gueltig[kennung]['grund']}")
                else:
                    fehler.append(f"{kennung} ({quelle.get('severity')}) in {name}: {quelle.get('title', '')}")
    for kennung in sorted(set(gueltig) - gemeldet):
        print(f"Hinweis: Ausnahme {kennung} wird nicht mehr gebraucht und kann entfernt werden")

    for zeile in fehler:
        print(f"FEHLER: {zeile}", file=sys.stderr)
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
