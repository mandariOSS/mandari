# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wächter (Issue #950): kein fest eingebauter KI-Endpunkt außerhalb der freigegebenen Konfiguration.

Durchsucht den Code der Anwendung (ohne Migrationen und Tests), die gemeinsame Bibliothek, den Ingestor, die
Templates, ``docker-compose.yml`` und ``.env.example`` nach Adressen und Variablen früherer Anbieter. Erwartet
werden null Treffer: KI-Anbieter kommen nur aus Admin und ``KI_ERLAUBTE_HOSTS``.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

#: Verbotene Zeichenketten (Groß-/Kleinschreibung wie angegeben)
VERBOTEN = ("tokenfactory.nebius", "NEBIUS_API_KEY", "api.anthropic.com", "api.openai.com", "api.mistral.ai")

#: Durchsuchte Bereiche (Verzeichnisse rekursiv, Dateien einzeln)
BEREICHE = ("mandari", "shared", "ingestor/src", "docker-compose.yml", ".env.example")

#: Verzeichnisse ohne eigenen Code (Migrationen, Tests, Abhängigkeiten, Build-Ergebnisse)
AUSGENOMMEN = {
    "migrations",
    "tests",
    "tests_e2e",
    ".venv",
    "node_modules",
    "__pycache__",
    "dist",
    "staticfiles",
    "htmlcov",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

#: Dateiarten mit Quelltext, Vorlagen oder Konfiguration
ENDUNGEN = {".py", ".html", ".txt", ".js", ".ts", ".css", ".yml", ".yaml", ".toml", ".md", ".json", ".cfg", ".ini"}
#: Erzeugte Sperrdateien gehören nicht dazu
SPERRDATEIEN = {"package-lock.json", "uv.lock"}


def _dateien() -> list[Path]:
    gefunden: list[Path] = []
    for bereich in BEREICHE:
        pfad = REPO / bereich
        if pfad.is_file():
            gefunden.append(pfad)
            continue
        for ordner, unterordner, namen in os.walk(pfad):
            # Ausgenommene Verzeichnisse gar nicht erst betreten
            unterordner[:] = [name for name in unterordner if name not in AUSGENOMMEN]
            for name in namen:
                datei = Path(ordner) / name
                if datei.suffix in ENDUNGEN and name not in SPERRDATEIEN:
                    gefunden.append(datei)
    return gefunden


def test_bereiche_vorhanden() -> None:
    for bereich in BEREICHE:
        assert (REPO / bereich).exists(), bereich
    namen = {datei.relative_to(REPO).as_posix() for datei in _dateien()}
    assert "mandari/apps/common/ki_anbieter.py" in namen
    assert "shared/mandari_dokumente/ki_hosts.py" in namen
    assert "mandari/templates/partials/chat_einwilligung.html" in namen


def test_keine_nicht_eu_endpunkte() -> None:
    treffer = []
    for datei in _dateien():
        inhalt = datei.read_text(encoding="utf-8", errors="replace")
        for zeichenkette in VERBOTEN:
            if zeichenkette in inhalt:
                treffer.append(f"{datei.relative_to(REPO).as_posix()}: {zeichenkette}")
    assert treffer == [], "Fest eingebaute KI-Endpunkte außerhalb der Konfiguration:\n" + "\n".join(treffer)
