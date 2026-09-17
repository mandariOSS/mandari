#!/usr/bin/env python3
"""
Prüft die Compose-Rollenprofile unter deploy/roles/ gegen docker-compose.yml (Issue #55).

Regeln:
- Die Basisdatei bleibt profilfrei: Ein-Server-Installation startet unverändert alles.
- Jede Rollen-Datei kennt nur Dienste der Basisdatei (plus eigene Zusätze wie pgbouncer),
  und jeder Basisdienst ist je Rolle entweder aktiv oder über das Profil „aus“ abgeschaltet.
- Jeder Basisdienst ist in genau einer der Rollen data/web/worker aktiv.
- Ein aktiver Dienst in web/worker, der in der Basis von Datendiensten abhängt, setzt
  ``depends_on`` zurück (die Datendienste laufen auf einem anderen Server).

Aufruf: python scripts/check_compose_roles.py  (Exit 1 bei Verstößen)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASIS = ROOT / "docker-compose.yml"
ROLLEN = {name: ROOT / "deploy" / "roles" / f"{name}.yml" for name in ("data", "web", "worker")}
DATENDIENSTE = {"postgres", "redis", "elasticsearch"}
AUS = "aus"


class _Reset:
    """Platzhalter für Composes ``!reset``-Tag."""


def _lade(pfad: Path) -> dict[str, Any]:
    loader = yaml.SafeLoader
    loader.add_constructor("!reset", lambda ld, node: _Reset())
    loader.add_constructor("!override", lambda ld, node: ld.construct_sequence(node) if node.value else [])
    data = yaml.load(pfad.read_text(encoding="utf-8"), Loader=loader)
    return data if isinstance(data, dict) else {}


def pruefe() -> list[str]:
    fehler: list[str] = []
    basis = _lade(BASIS).get("services", {})
    for name, definition in basis.items():
        if "profiles" in definition:
            fehler.append(f"Basisdatei: Dienst {name} trägt profiles – Ein-Server-Betrieb würde ihn nicht starten")

    aktiv_in: dict[str, list[str]] = {name: [] for name in basis}
    for rolle, pfad in ROLLEN.items():
        if not pfad.exists():
            fehler.append(f"Rollen-Datei fehlt: {pfad.relative_to(ROOT)}")
            continue
        dienste = _lade(pfad).get("services", {})
        for name, definition in dienste.items():
            if name not in basis and "image" not in (definition or {}):
                fehler.append(f"{rolle}: unbekannter Dienst {name} ohne image")
        for name in basis:
            definition = dienste.get(name) or {}
            abgeschaltet = AUS in (definition.get("profiles") or [])
            if abgeschaltet:
                continue
            aktiv_in[name].append(rolle)
            haengt_an_daten = DATENDIENSTE & set(basis[name].get("depends_on") or {})
            if rolle != "data" and haengt_an_daten and not isinstance(definition.get("depends_on"), _Reset):
                fehler.append(f"{rolle}: {name} hängt in der Basis von {sorted(haengt_an_daten)} ab, depends_on nicht zurückgesetzt")
            if rolle != "data" and name in DATENDIENSTE:
                fehler.append(f"{rolle}: Datendienst {name} nicht abgeschaltet")
    for name, rollen in aktiv_in.items():
        if len(rollen) != 1:
            fehler.append(f"Dienst {name} ist in {rollen or 'keiner Rolle'} aktiv – erwartet genau eine")
    return fehler


def main() -> int:
    fehler = pruefe()
    for zeile in fehler:
        print(f"FEHLER  {zeile}")
    if not fehler:
        print(f"OK  Rollenprofile {', '.join(ROLLEN)} decken alle Dienste der Basisdatei genau einmal ab.")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
