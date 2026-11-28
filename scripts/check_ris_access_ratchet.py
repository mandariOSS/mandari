# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für Direktzugriffe auf den RIS-Bestand (Issues #522, #1593).

Fachmodule lesen RIS-Daten über die Lese-Fassade ``hub/ris/selectors.py``
(``docs/adr/20260929-kanonisches-modell.md``). Dieses Skript zählt je Datei die Zugriffe auf
``OParl*.objects`` (auch ``_default_manager``/``_base_manager`` und umbenannte Importe), getrennt in zwei
Abschnitten der Baseline:

- ``files`` (Kennzahl K5): außerhalb von ``hub`` und ``insight_core``,
- ``buergerportal`` (Kennzahl K5b): im Code des Bürgerportals, der noch in ``insight_core`` liegt (``PORTAL``,
  Inventar in Issue #1593). Er besitzt den Bestand nicht und liest wie ein Fachmodul über die Fassade.

Tests, Migrationen und die E2E-Tests zählen nicht mit: Sie legen Testdaten an und koppeln nichts im Betrieb.

Die Baseline ``scripts/ris_access_baseline.json`` hält je Abschnitt und Datei den erlaubten Höchststand fest.
Das Skript schlägt fehl, wenn

- eine Datei mehr Zugriffe hat als in der Baseline oder eine neue Datei Zugriffe bekommt,
- Zugriffe abgebaut sind, die Baseline aber noch den alten Stand hat: mit ``--update`` nachziehen
  und im selben Pull Request committen, damit der niedrigere Stand festgeschrieben ist.

``--update`` senkt nur: Es übernimmt kleinere Werte und streicht Dateien ohne Zugriffe, nimmt aber
nie eine Datei auf und erhöht nie einen Wert. Wandert Code samt Zugriff in eine andere Datei, geht das
nur über eine Änderung der Baseline von Hand, die im Review sichtbar ist; die Summe darf dabei nicht
steigen. Das gilt auch, wenn Portal-Code nach ``apps/portal`` umzieht: Seine Einträge wechseln dann von
``buergerportal`` nach ``files``. ``--json`` gibt den aktuellen Stand aus.

Aufruf aus dem Repo-Root:  python scripts/check_ris_access_ratchet.py [--update] [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
BASELINE = ROOT / "scripts" / "ris_access_baseline.json"

#: Pakete, die den RIS-Bestand besitzen bzw. die Fassade bereitstellen (dürfen direkt zugreifen).
OWNERS = ("hub", "insight_core")
#: Code des Bürgerportals in ``insight_core`` (Inventar in Issue #1593): Verzeichnisse mit „/“ am Ende, sonst Dateien.
#: Er zieht nach ``apps/portal`` um und liest bis dahin schon über die Fassade.
PORTAL = (
    "insight_core/views/",
    "insight_core/templatetags/",
    "insight_core/api.py",
    "insight_core/api_urls.py",
    "insight_core/context_processors.py",
    "insight_core/farbskala.py",
    "insight_core/forms.py",
    "insight_core/listenadresse.py",
    "insight_core/logo_vorschau.py",
    "insight_core/navigation.py",
    "insight_core/portal.py",
    "insight_core/seo.py",
    "insight_core/sitzung.py",
    "insight_core/throttle.py",
    "insight_core/urls.py",
    "insight_core/management/commands/build_logo_thumbnails.py",
    "insight_core/management/commands/generate_alerts.py",
    "insight_core/management/commands/kommunenverzeichnis_importieren.py",
    "insight_core/management/commands/send_digest.py",
    "insight_core/management/commands/send_question_reminders.py",
    "insight_core/services/beschlussliste.py",
    "insight_core/services/chat_guard.py",
    "insight_core/services/decision_tracking.py",
    "insight_core/services/feed_eintraege.py",
    "insight_core/services/feeds.py",
    "insight_core/services/fragen_antworten.py",
    "insight_core/services/indexierung.py",
    "insight_core/services/indexnow.py",
    "insight_core/services/ki_bewertung.py",
    "insight_core/services/kommunenverzeichnis.py",
    "insight_core/services/kommunenverzeichnis_import.py",
    "insight_core/services/kurzfassung.py",
    "insight_core/services/orte_anzeige.py",
    "insight_core/services/page_feedback.py",
    "insight_core/services/portal_stats.py",
    "insight_core/services/question_service.py",
    "insight_core/services/sitemaps.py",
    "insight_core/services/sitzung_kalender.py",
    "insight_core/services/sitzungsabbruch.py",
    "insight_core/services/sitzungsort.py",
    "insight_core/services/sitzverteilung.py",
    "insight_core/services/summary_guard.py",
    "insight_core/services/vorgang_weg.py",
)
#: Ausnahmen in den Verzeichnissen von ``PORTAL``: die Dateivorschau gehört zur Dokumentablage der Drehscheibe.
PORTAL_OHNE = ("insight_core/views/files.py",)
#: Verzeichnisse, die nie mitzählen.
SKIPPED_DIRS = {"migrations", "tests", "tests_e2e", "node_modules", "static", "staticfiles", "media", "__pycache__"}
MODEL = re.compile(r"^OParl[A-Z][A-Za-z]*$")
MANAGERS = {"objects", "_default_manager", "_base_manager"}


def _betrieb(relative: Path) -> bool:
    """Betriebscode: keine Tests, Migrationen, E2E-Tests oder Hilfsdateien der Tests."""
    parts = relative.parts
    if not parts:
        return False
    if any(part in SKIPPED_DIRS or part.startswith(".") for part in parts[:-1]):
        return False
    name = parts[-1]
    return not (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def is_counted(relative: Path) -> bool:
    """Zählt diese Datei (Pfad relativ zu ``mandari/``) für K5, also außerhalb von ``hub`` und ``insight_core``?"""
    return _betrieb(relative) and relative.parts[0] not in OWNERS


def is_portal(relative: Path) -> bool:
    """Gehört diese Datei (Pfad relativ zu ``mandari/``) zum Bürgerportal in ``insight_core`` (K5b)?"""
    pfad = relative.as_posix()
    if not _betrieb(relative) or pfad in PORTAL_OHNE:
        return False
    return any(pfad.startswith(eintrag) if eintrag.endswith("/") else pfad == eintrag for eintrag in PORTAL)


#: Abschnitte der Baseline mit ihrer Auswahl der Dateien
SECTIONS: dict[str, Callable[[Path], bool]] = {"files": is_counted, "buergerportal": is_portal}
#: Beschriftung je Abschnitt in der Ausgabe
LABELS = {
    "files": "außerhalb von hub/insight_core (K5)",
    "buergerportal": "im Bürgerportal in insight_core (K5b)",
}


def count_accesses(source: str) -> int:
    """Zugriffe auf Manager von RIS-Modellen in einem Modul."""
    tree = ast.parse(source)
    aliases = {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("insight_core")
        for alias in node.names
        if alias.asname and MODEL.match(alias.name)
    }
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in MANAGERS:
            continue
        target = node.value
        name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
        if MODEL.match(name) or name in aliases:
            count += 1
    return count


def measure(project: Path | None = None, section: str = "files") -> dict[str, int]:
    """Zugriffe je Datei eines Abschnitts (nur Dateien mit mindestens einem Zugriff), sortiert nach Pfad."""
    project = project or PROJECT
    auswahl = SECTIONS[section]
    result: dict[str, int] = {}
    for path in sorted(project.rglob("*.py")):
        relative = path.relative_to(project)
        if not auswahl(relative):
            continue
        count = count_accesses(path.read_text(encoding="utf-8"))
        if count:
            result[relative.as_posix()] = count
    return result


def compare(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(Verstöße, abgebaute Zugriffe ohne nachgezogene Baseline)."""
    violations: list[str] = []
    lowered: list[str] = []
    for name, count in current.items():
        allowed = baseline.get(name)
        if allowed is None:
            violations.append(f"{name}: {count} neue Zugriffe (Datei steht nicht in der Baseline)")
        elif count > allowed:
            violations.append(f"{name}: {count} Zugriffe, erlaubt {allowed}")
    for name, allowed in baseline.items():
        count = current.get(name, 0)
        if count < allowed:
            lowered.append(f"{name}: {count} statt {allowed}")
    return violations, lowered


def lowered_baseline(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Neue Baseline: je Datei der kleinere Wert, Dateien ohne Zugriffe entfallen, nichts kommt hinzu."""
    return {name: min(allowed, current.get(name, 0)) for name, allowed in baseline.items() if current.get(name, 0)}


def _read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}


def load_baseline(path: Path | None = None, section: str = "files") -> dict[str, int]:
    """Erlaubter Stand je Datei eines Abschnitts; ein fehlender Abschnitt ist leer."""
    werte = _read(path or BASELINE).get(section) or {}
    return {str(name): int(count) for name, count in werte.items()}


def write_baseline(files: dict[str, int], path: Path | None = None, section: str = "files") -> None:
    """Schreibt einen Abschnitt der Baseline; die anderen Abschnitte bleiben, wie sie sind."""
    path = path or BASELINE
    data = _read(path)
    data["beschreibung"] = (
        "Direktzugriffe auf OParl*.objects je Datei: files außerhalb von hub und insight_core (K5), buergerportal "
        "im Code des Bürgerportals in insight_core (K5b); dürfen nur sinken (scripts/check_ris_access_ratchet.py, "
        "Issues #522, #1593)."
    )
    data[section] = dict(sorted(files.items()))
    geordnet = {key: data[key] for key in ("beschreibung", *SECTIONS) if key in data}
    path.write_text(json.dumps(geordnet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    current = {section: measure(section=section) for section in SECTIONS}
    if "--json" in argv:
        stand = {"summe": sum(current["files"].values()), "files": current["files"]}
        stand["buergerportal"] = {"summe": sum(current["buergerportal"].values()), "files": current["buergerportal"]}
        print(json.dumps(stand, indent=2, ensure_ascii=False))
        return 0
    ergebnis = 0
    for section in SECTIONS:
        baseline = load_baseline(section=section)
        violations, lowered = compare(current[section], baseline)
        if "--update" in argv:
            write_baseline(lowered_baseline(current[section], baseline), section=section)
            print(f"Baseline nachgezogen ({section}): {BASELINE.relative_to(ROOT).as_posix()}")
            baseline = load_baseline(section=section)
            lowered = []
        total, allowed_total = sum(current[section].values()), sum(baseline.values())
        print(f"Direktzugriffe auf OParl*.objects {LABELS[section]}: {total} (Baseline {allowed_total})")
        if violations:
            print("\nNeue Direktzugriffe – bitte die Lese-Fassade hub/ris/selectors.py nutzen (bei Bedarf ergänzen):")
            print("\n".join(f"- {line}" for line in violations))
            ergebnis = 1
        elif lowered:
            print("\nZugriffe abgebaut, Baseline noch auf altem Stand. Bitte festschreiben:")
            print("  python scripts/check_ris_access_ratchet.py --update  (und die Baseline mitcommitten)")
            print("\n".join(f"- {line}" for line in lowered))
            ergebnis = 1
    if ergebnis == 0:
        print("OK: keine neuen Direktzugriffe.")
    return ergebnis


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
