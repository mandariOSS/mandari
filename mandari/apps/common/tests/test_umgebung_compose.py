# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``.env.example``, ``docker-compose.yml`` und Code passen zusammen (Issue #962).

Früher reichte Compose nur einen Teil der Variablen an die Container weiter; ``EMAIL_*``, ``PORTAL_HOSTS``,
``TWO_FACTOR_*`` und weitere standen in ``.env.example`` und blieben ohne Wirkung. Jetzt lesen die
Anwendungsdienste die ``.env`` über ``env_file``. Geprüft wird:

- Jede Variable aus ``.env.example`` liest der Code, Compose oder ein Skript (keine toten Einträge).
- Was nur Compose oder nur ein Skript liest, ist in ihrem Kommentar mit ``[Compose]`` bzw. ``[Skript]``
  gekennzeichnet.
- Jede Variable, die Compose einsetzt, steht in ``.env.example``.
- Ein ausdrücklicher Wert unter ``environment:`` hat Vorrang vor ``env_file``. Setzt ein Anwendungsdienst
  eine Variable aus ``.env.example`` dort fest, ohne sie selbst einzusetzen, bliebe der Wert aus der ``.env``
  wirkungslos (so war es mit ``CSRF_TRUSTED_ORIGINS``).
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

import yaml

WURZEL = Path(__file__).resolve().parents[4]
BEISPIEL = WURZEL / ".env.example"
COMPOSE = WURZEL / "docker-compose.yml"
COMPOSE_DATEIEN = (COMPOSE, WURZEL / "docker-compose.dev.yml", *sorted((WURZEL / "deploy" / "roles").glob("*.yml")))
SKRIPTE = (
    WURZEL / "install.sh",
    WURZEL / "update.sh",
    WURZEL / "backup.sh",
    *sorted((WURZEL / "deploy" / "scripts").glob("*.sh")),
)
CODE = (WURZEL / "mandari", WURZEL / "ingestor" / "src", WURZEL / "shared")
INGESTOR_EINSTELLUNGEN = WURZEL / "ingestor" / "src" / "config.py"
NICHT_DURCHSUCHEN = {".venv", "node_modules", "__pycache__", "tests", "tests_e2e", "static", "staticfiles"}

#: Dienste aus den Anwendungs-Images: Sie lesen die .env über env_file
ANWENDUNGSDIENSTE = ("mandari", "worker", "worker-heavy", "worker-live", "minutes-orchestrator", "ingestor")
#: Variablen, die Compose selbst auswertet
COMPOSE_EIGENE = {"COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PATH_SEPARATOR"}
#: Bewusst je Dienst anders gesetzt (eigene Variable in der .env), mit Grund
EIGENE_WERTE_JE_DIENST = {
    ("worker-heavy", "OCR_MEMORY_LIMIT_MB"): "eigene Grenze WORKER_HEAVY_OCR_MEMORY_LIMIT_MB",
    ("ingestor", "TEXT_LAYER_MEMORY_LIMIT_MB"): "eigene Grenze INGESTOR_TEXT_LAYER_MEMORY_LIMIT_MB",
}

VARIABLE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")
LITERAL = re.compile(r"[\"']([A-Z][A-Z0-9_]+)[\"']")
EINGESETZT = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")


def _beispiel() -> dict[str, list[str]]:
    """Variable → Kommentarblöcke über ihren Zeilen (eine Variable kann mehrfach vorkommen)."""
    zeilen = BEISPIEL.read_text(encoding="utf-8").splitlines()
    ergebnis: dict[str, list[str]] = {}
    for i, zeile in enumerate(zeilen):
        treffer = VARIABLE.match(zeile)
        if not treffer:
            continue
        j = i - 1
        # Benachbarte Variablen einer Gruppe teilen sich den Kommentar darüber
        while j >= 0 and VARIABLE.match(zeilen[j]):
            j -= 1
        block: list[str] = []
        while j >= 0 and zeilen[j].startswith("#") and not VARIABLE.match(zeilen[j]):
            block.append(zeilen[j])
            j -= 1
        ergebnis.setdefault(treffer.group(1), []).append("\n".join(reversed(block)))
    return ergebnis


def _python_dateien(basis: Path) -> list[Path]:
    dateien: list[Path] = []
    for ordner, unterordner, namen in os.walk(basis):
        unterordner[:] = [n for n in unterordner if n not in NICHT_DURCHSUCHEN and not n.startswith(".")]
        dateien.extend(Path(ordner) / n for n in namen if n.endswith(".py"))
    return dateien


def _im_code() -> set[str]:
    """Namen, die der Code als Zeichenkette nennt, dazu die Felder der Ingestor-Einstellungen (pydantic)."""
    namen: set[str] = set()
    for basis in CODE:
        for pfad in _python_dateien(basis):
            namen.update(LITERAL.findall(pfad.read_text(encoding="utf-8", errors="replace")))
    baum = ast.parse(INGESTOR_EINSTELLUNGEN.read_text(encoding="utf-8"))
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.ClassDef) and knoten.name == "Settings":
            for feld in knoten.body:
                if isinstance(feld, ast.AnnAssign) and isinstance(feld.target, ast.Name):
                    namen.add(feld.target.id.upper())
    return namen


def _in_compose() -> set[str]:
    return {name for datei in COMPOSE_DATEIEN for name in EINGESETZT.findall(datei.read_text(encoding="utf-8"))}


def _in_skripten() -> set[str]:
    text = "\n".join(datei.read_text(encoding="utf-8") for datei in SKRIPTE)
    return set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", text))


def _dienste() -> dict[str, Any]:
    daten = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(daten["services"])


def test_anwendungsdienste_lesen_die_env() -> None:
    dienste = _dienste()
    for name in ANWENDUNGSDIENSTE:
        env_file = dienste[name].get("env_file")
        dateien = [env_file] if isinstance(env_file, str) else list(env_file or [])
        assert ".env" in dateien, f"{name}: env_file mit .env fehlt, Variablen aus der .env kämen nicht an"
    # Die Website (anderes Repository, eigene Einstellungen) bekommt nur ihre ausdrücklichen Werte
    assert "env_file" not in dienste["website"]


def test_jede_variable_aus_dem_beispiel_wirkt() -> None:
    wirkt = _im_code() | _in_compose() | COMPOSE_EIGENE | _in_skripten()
    tot = sorted(set(_beispiel()) - wirkt)
    assert not tot, f"In .env.example, aber weder im Code noch in Compose oder einem Skript: {tot}"


def test_nur_compose_oder_skript_ist_gekennzeichnet() -> None:
    im_code = _im_code()
    ohne = sorted(
        name
        for name, bloecke in _beispiel().items()
        if name not in im_code and not any("[Compose]" in b or "[Skript]" in b for b in bloecke)
    )
    assert not ohne, f"Erreicht keinen Container, aber ohne [Compose]/[Skript] im Kommentar darüber: {ohne}"


def test_jede_variable_aus_compose_steht_im_beispiel() -> None:
    fehlt = sorted(_in_compose() - set(_beispiel()))
    assert not fehlt, f"Compose setzt sie ein, .env.example nennt sie nicht: {fehlt}"


def test_compose_ueberschreibt_keine_variable_aus_dem_beispiel() -> None:
    beispiel = _beispiel()
    # Nur für die Entwicklung ohne Container (DATABASE_URL usw.): im Container setzt Compose sie selbst
    entwicklung = {name for name, bloecke in beispiel.items() if all("[Entwicklung]" in b for b in bloecke)}
    dienste = _dienste()
    fest = sorted(
        f"{dienst}: {name}"
        for dienst in ANWENDUNGSDIENSTE
        for name, wert in (dienste[dienst].get("environment") or {}).items()
        if name in beispiel
        and name not in entwicklung
        and (dienst, name) not in EIGENE_WERTE_JE_DIENST
        and "${" + name not in str(wert)
    )
    assert not fest, f"environment: setzt den Wert fest, die Zeile in der .env bliebe wirkungslos: {fest}"


def test_entwicklungsvariablen_sind_im_container_festgelegt() -> None:
    """Gegenprobe: Was nur für die Entwicklung gilt, darf über env_file nicht in einen Container gelangen."""
    beispiel = _beispiel()
    entwicklung = {name for name, bloecke in beispiel.items() if all("[Entwicklung]" in b for b in bloecke)}
    assert {"DEBUG", "DATABASE_URL", "REDIS_URL", "ELASTICSEARCH_URL"} <= entwicklung, entwicklung
    dienste = _dienste()
    for dienst in ("mandari", "worker", "worker-heavy", "worker-live", "minutes-orchestrator"):
        umgebung = dienste[dienst]["environment"]
        for name in ("DEBUG", "DATABASE_URL", "REDIS_URL"):
            assert name in umgebung, f"{dienst}: {name} fehlt, der Wert aus der .env (Entwicklung) gälte"
        assert umgebung["DEBUG"] == "false", dienst


def _gigabyte(wert: str) -> float:
    """``1g``, ``512m`` bzw. ``${VAR:-3g}`` (Vorgabe) in GB."""
    vorgabe = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]+)\}", wert)
    treffer = re.fullmatch(r"(\d+(?:\.\d+)?)([mg])", (vorgabe.group(1) if vorgabe else wert).lower())
    assert treffer, wert
    return float(treffer.group(1)) / (1024 if treffer.group(2) == "m" else 1)


def test_speicherangabe_in_der_doku_passt_zu_compose() -> None:
    """README und DEPLOYMENT.md nannten 4 GB, die Speichergrenzen in Compose ergeben weit mehr."""
    summe = sum(_gigabyte(str(d["mem_limit"])) for d in _dienste().values() if "profiles" not in d)
    angabe = f"rund {summe:.1f} GB".replace(".", ",").replace(",0 ", " ")
    for datei in (WURZEL / "README.md", WURZEL / "DEPLOYMENT.md"):
        text = " ".join(datei.read_text(encoding="utf-8").split())
        assert angabe in text, f"{datei.name}: „{angabe}“ fehlt (Summe der Speichergrenzen in docker-compose.yml)"
        assert "4 GB Arbeitsspeicher" not in text, datei.name
