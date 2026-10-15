# SPDX-License-Identifier: AGPL-3.0-or-later
"""pip-audit für die CI: jede bekannte Schwachstelle blockiert, außer mit befristeter, begründeter Ausnahme.

Ausnahmen stehen in ``mandari/pip-audit-ausnahmen.json`` (Kennung, Aliase, Paket, Grund, Ablaufdatum). Sie sind nur
für Meldungen gedacht, für die es noch keine korrigierte Version gibt und deren betroffener Teil bei uns nicht läuft.
Die Prüfung scheitert,

- wenn eine Meldung ohne gültige Ausnahme auftritt,
- wenn eine Ausnahme abgelaufen ist (damit sie nicht liegen bleibt),
- wenn es für eine ausgenommene Meldung inzwischen eine korrigierte Version gibt (dann aktualisieren statt ausnehmen).

Aufruf (aus dem Repository-Wurzelverzeichnis): ``python scripts/pip_audit_pruefen.py <requirements.txt> …``
"""

from __future__ import annotations

import datetime
import json
import pathlib
import subprocess
import sys

WURZEL = pathlib.Path(__file__).resolve().parent.parent
AUSNAHMEN = WURZEL / "mandari" / "pip-audit-ausnahmen.json"


def ausnahmen_laden(heute: datetime.date) -> tuple[dict[str, dict], list[str]]:
    """Gültige Ausnahmen je Kennung und Alias sowie Fehler für abgelaufene Einträge."""
    gueltig: dict[str, dict] = {}
    fehler: list[str] = []
    for eintrag in json.loads(AUSNAHMEN.read_text(encoding="utf-8")).get("ausnahmen", []):
        if datetime.date.fromisoformat(eintrag["bis"]) < heute:
            fehler.append(f"Ausnahme {eintrag['kennung']} ({eintrag['paket']}) ist am {eintrag['bis']} abgelaufen")
            continue
        for kennung in [eintrag["kennung"], *eintrag.get("aliase", [])]:
            gueltig[kennung] = eintrag
    return gueltig, fehler


def pruefen(requirements: str, gueltig: dict[str, dict]) -> tuple[list[str], set[str]]:
    """Fehler und genutzte Ausnahmen für eine Requirements-Datei."""
    lauf = subprocess.run(
        ["pip-audit", "-r", requirements, "--no-deps", "--progress-spinner", "off", "-f", "json"],
        capture_output=True,
        text=True,
    )
    try:
        pakete = json.loads(lauf.stdout)["dependencies"]
    except (json.JSONDecodeError, TypeError, KeyError):
        sys.stderr.write(lauf.stderr)
        return [f"pip-audit lieferte für {requirements} keinen auswertbaren Bericht (Exit {lauf.returncode})"], set()
    # pip-audit endet nur mit gefundenen Meldungen ungleich 0; jeder andere Fehler bleibt ein Fehler
    if lauf.returncode != 0 and not any(paket.get("vulns") for paket in pakete):
        sys.stderr.write(lauf.stderr)
        return [f"pip-audit endete für {requirements} mit Exit {lauf.returncode} ohne Meldung im Bericht"], set()

    fehler: list[str] = []
    genutzt: set[str] = set()
    for paket in pakete:
        for meldung in paket.get("vulns", []):
            kennungen = [meldung["id"], *meldung.get("aliases", [])]
            eintrag = next((gueltig[k] for k in kennungen if k in gueltig), None)
            beschreibung = f"{meldung['id']} in {paket['name']} {paket['version']}"
            if eintrag is None:
                fehler.append(
                    f"{beschreibung} (korrigiert in: {', '.join(meldung.get('fix_versions', [])) or 'noch keiner'})"
                )
            elif meldung.get("fix_versions"):
                fehler.append(
                    f"{beschreibung}: korrigiert in {', '.join(meldung['fix_versions'])} – aktualisieren und die "
                    f"Ausnahme {eintrag['kennung']} entfernen"
                )
            else:
                genutzt.add(eintrag["kennung"])
                print(f"Ausnahme bis {eintrag['bis']}: {beschreibung} – {eintrag['grund']}")
    return fehler, genutzt


def main(dateien: list[str]) -> int:
    if not dateien:
        print("Aufruf: python scripts/pip_audit_pruefen.py <requirements.txt> …", file=sys.stderr)
        return 2
    gueltig, fehler = ausnahmen_laden(datetime.date.today())
    genutzt: set[str] = set()
    for datei in dateien:
        f, g = pruefen(datei, gueltig)
        fehler.extend(f)
        genutzt |= g
    for kennung in sorted({e["kennung"] for e in gueltig.values()} - genutzt):
        print(f"Hinweis: Ausnahme {kennung} wird in diesem Lauf nicht gebraucht")
    for zeile in fehler:
        print(f"FEHLER: {zeile}", file=sys.stderr)
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
