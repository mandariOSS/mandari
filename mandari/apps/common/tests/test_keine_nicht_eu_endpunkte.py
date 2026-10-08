# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wächter (Issue #950): kein fest eingebauter KI-Endpunkt außerhalb der freigegebenen Konfiguration.

Durchsucht den Code der Anwendung (ohne Migrationen und Tests), die gemeinsame Bibliothek, den Ingestor, die
Templates, Skripte, Betriebsdateien unter ``deploy/``, die Dokumentation unter ``docs/``, ``docker-compose.yml`` und
``.env.example`` nach Adressen und Variablen früherer Anbieter. Erwartet werden null Treffer: KI-Anbieter kommen
nur aus Admin und ``KI_ERLAUBTE_HOSTS``.

Strukturtest: Die Adresse einer Chat-Schnittstelle (``/chat/completions``) bilden nur die zentrale
KI-Konfiguration, der OpenAI-kompatible Anbieter des Bürgerportals und die Texterkennung. Ein neuer KI-Aufruf an
einer anderen Stelle fiele hier auf, statt an der Positivliste vorbei zu laufen.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[4]

#: Verbotene Zeichenketten (Groß-/Kleinschreibung wie angegeben)
VERBOTEN = ("tokenfactory.nebius", "NEBIUS_API_KEY", "api.anthropic.com", "api.openai.com", "api.mistral.ai")

#: Durchsuchte Bereiche (Verzeichnisse rekursiv, Dateien einzeln)
BEREICHE = ("mandari", "shared", "ingestor/src", "scripts", "deploy", "docs", "docker-compose.yml", ".env.example")

#: Einzige Stellen, die die Adresse einer Chat-Schnittstelle bilden dürfen
CHAT_SCHNITTSTELLE = "/chat/completions"
CHAT_ERLAUBT = {
    "mandari/apps/common/ki_anbieter.py",
    "mandari/insight_ai/providers/openai_kompatibel.py",
    "shared/mandari_dokumente/mistral.py",
}

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
ENDUNGEN = {
    ".py",
    ".html",
    ".txt",
    ".js",
    ".ts",
    ".css",
    ".yml",
    ".yaml",
    ".toml",
    ".md",
    ".json",
    ".cfg",
    ".ini",
    ".sh",
    ".conf",
    ".example",
    ".tpl",
    ".service",
    ".timer",
}
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
    assert "scripts/smoke_collab_ai.py" in namen
    assert "deploy/scripts/deploy.sh" in namen
    assert "docs/KRYPTOKONZEPT.md" in namen
    assert CHAT_ERLAUBT.issubset(namen)


def test_keine_nicht_eu_endpunkte() -> None:
    treffer = []
    for datei in _dateien():
        inhalt = datei.read_text(encoding="utf-8", errors="replace")
        for zeichenkette in VERBOTEN:
            if zeichenkette in inhalt:
                treffer.append(f"{datei.relative_to(REPO).as_posix()}: {zeichenkette}")
    assert treffer == [], "Fest eingebaute KI-Endpunkte außerhalb der Konfiguration:\n" + "\n".join(treffer)


def test_chat_schnittstelle_nur_an_den_freigegebenen_stellen() -> None:
    treffer = sorted(
        datei.relative_to(REPO).as_posix()
        for datei in _dateien()
        if CHAT_SCHNITTSTELLE in datei.read_text(encoding="utf-8", errors="replace")
    )
    fremd = [name for name in treffer if name not in CHAT_ERLAUBT]
    assert fremd == [], "Chat-Schnittstelle außerhalb der zentralen KI-Konfiguration:\n" + "\n".join(fremd)
    # Die zentrale Konfiguration bildet die Adresse selbst
    assert "mandari/apps/common/ki_anbieter.py" in treffer


#: Variablen der KI-Anbindung, die Anwendung und Ingestor gleich bekommen (DEPLOYMENT.md, „KI-Anbieter“)
KI_VARIABLEN = ("KI_ERLAUBTE_HOSTS", "MISTRAL_API_KEY", "MISTRAL_BASE_URL")


def test_compose_reicht_ki_variablen_einheitlich_weiter() -> None:
    """Schlüssel und Basis-URL der Texterkennung gibt es nur zusammen, in Anwendung und Ingestor gleich, Standard aus."""
    compose: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    umgebungen = {
        "Anwendung": compose["x-app-environment"],
        "Ingestor": compose["services"]["ingestor"]["environment"],
    }
    for name, umgebung in umgebungen.items():
        for variable in KI_VARIABLEN:
            assert umgebung.get(variable) == f"${{{variable}:-}}", f"{name}: {variable} fehlt oder ist nicht leer"
    assert "NEBIUS_API_KEY" not in str(compose)


def test_deployment_nennt_den_rueckfall_auf_nebius() -> None:
    """Ein älteres Image nutzt Nebius wieder, sobald NEBIUS_API_KEY in der Umgebung steht."""
    text = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
    abschnitt = text.split("### KI-Anbieter", 1)[1].split("\n### ", 1)[0]
    rueckfall = re.sub(r"\s+", " ", abschnitt.split("**Rückfall auf ein älteres Image:**", 1)[1])
    assert "NEBIUS_API_KEY" in rueckfall and "entfernen" in rueckfall and "widerrufen" in rueckfall
