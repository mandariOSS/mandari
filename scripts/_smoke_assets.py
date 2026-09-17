# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gebaute Frontend-Dateien und Quelltexte für die Smoke-Skripte finden.

Der Editor lag früher als ``static/js/editor.bundle.js`` vor. Seit Issue #170
ist er ein TypeScript-Modul, das Vite unter einem inhaltsabhängigen Namen nach
``static/dist/assets/`` baut — der Dateiname ändert sich also bei jeder
Änderung. Zwei Smoke-Skripte lasen weiterhin den alten Pfad und brachen seitdem
mit ``FileNotFoundError`` ab, ohne dass es auffiel (Issue #249).
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = Path("static") / "dist" / "manifest.json"


class BuildFehltError(RuntimeError):
    """Das Frontend wurde nicht gebaut — mit Hinweis statt nacktem Dateifehler."""


def _manifest(project_dir: Path) -> dict:
    pfad = project_dir / MANIFEST
    if not pfad.is_file():
        raise BuildFehltError(
            f"{MANIFEST.as_posix()} fehlt — das Frontend wurde nicht gebaut.\n"
            f"Abhilfe: cd {project_dir.name} && npm ci && npx vite build"
        )
    return json.loads(pfad.read_text(encoding="utf-8"))


def gebautes_modul(project_dir: Path, eintrag: str) -> str:
    """Inhalt des gebauten Moduls zu einem Vite-Eintrag, z. B. ``frontend/editor/index.ts``."""
    manifest = _manifest(project_dir)
    if eintrag not in manifest:
        bekannt = ", ".join(sorted(manifest)[:8])
        raise BuildFehltError(f"Eintrag „{eintrag}“ steht nicht im Manifest. Vorhanden u. a.: {bekannt}")
    datei = project_dir / "static" / "dist" / manifest[eintrag]["file"]
    if not datei.is_file():
        raise BuildFehltError(f"{datei} steht im Manifest, fehlt aber auf der Platte.")
    return datei.read_text(encoding="utf-8", errors="ignore")


def editor_quelltext(project_dir: Path) -> str:
    """TypeScript-Quelltext des Frontends, aneinandergehängt.

    Für Prüfungen auf Verdrahtung („wird diese Funktion benutzt?") ist der
    Quelltext die richtige Grundlage. Das gebaute Modul ist minifiziert — dort
    sind Bezeichner umbenannt, und eine Suche nach Namen schlägt fehl, obwohl
    alles vorhanden ist. Genau daran sind zwei Smoke-Skripte nach der Umstellung
    auf TypeScript (#170) gescheitert.
    """
    teile = [
        pfad.read_text(encoding="utf-8", errors="ignore")
        for pfad in sorted((project_dir / "frontend").rglob("*.ts"))
        if not pfad.name.endswith(".d.ts")
    ]
    if not teile:
        raise BuildFehltError(f"Keine TypeScript-Quellen unter {project_dir / 'frontend'} gefunden.")
    return "\n".join(teile)
