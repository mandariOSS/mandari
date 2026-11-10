# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für toten Python-Code (Issue #1113): Die Liste der Funde von vulture darf nur kürzer werden.

vulture meldet Funktionen, Methoden, Klassen, Properties und Importe, deren Name nirgends im Code
verwendet wird, sowie unerreichbaren Code. In einem Django-Projekt ruft vieles das Framework auf, ohne
dass der Name im Python-Code steht. Was deshalb nicht zählt, steht mit Begründung in
``scripts/dead_code_ratchet.toml``:

- Dateien, die nur als Verwendung gelesen werden (Tests, Migrationen, Admin, ``apps.py``, ``scripts/``),
- Einstiegspunkte, die das Framework über einen Dekorator oder einen festen Namen findet (Signale,
  Template-Tags, Schnittstellen, Management-Befehle, Methoden, die eine Basisklasse aufruft),
- Einzelfälle mit Begründung.

Zusätzlich gelten als Verwendung: Namen in ``{{ … }}``, ``{% … %}`` und in dynamischen Attributen der
Vorlagen (Methoden und Properties, die nur eine Vorlage aufruft) sowie Punktpfade in Zeichenketten –
das letzte Glied (``"apps.common.middleware.Klasse"`` in den Einstellungen) und der Pfad mit
Unterstrichen (Nachrichtentyp ``"notizen.sync"`` für die Consumer-Methode ``notizen_sync``).

Variablen und Attribute zählt das Skript nicht: Auf Klassenebene sind sie fast immer Angaben für das
Framework (Modellfelder, ``Meta``, Optionen von Views und Formularen), als Parameter gibt sie ein
Protokoll vor.

Die Baseline ``scripts/dead_code_baseline.json`` nennt je Datei die Funde, die es heute gibt. Das Skript
schlägt fehl, sobald ein Fund dazukommt, der dort nicht steht, und nennt ihn. Ist ein Fund abgebaut,
streicht ``--update`` ihn aus der Baseline (im selben Pull Request committen); ``--update`` nimmt nie
einen Fund auf. Wandert toter Code in eine andere Datei, gilt er dort als neu. Ein Fund ist ein Hinweis,
kein Beweis: vor dem Löschen prüfen, ob der Name über ``getattr`` oder von außen (Betrieb, andere
Repositories) verwendet wird.

Aufruf aus dem Repo-Root (vulture in der Version aus ``mandari/pyproject.toml`` [dev]):

    python scripts/check_dead_code_ratchet.py [--update] [--verbose] [--json] [--report DATEI]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import keyword
import re
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scripts" / "dead_code_ratchet.toml"
BASELINE = ROOT / "scripts" / "dead_code_baseline.json"

#: Bezeichnungen der Arten, wie vulture sie nennt, für den Bericht.
KINDS = {
    "function": "Funktion",
    "method": "Methode",
    "class": "Klasse",
    "property": "Property",
    "import": "Import",
    "unreachable_code": "unerreichbarer Code",
    "variable": "Variable",
    "attribute": "Attribut",
}
#: Arten, bei denen die Namen der Einstiegspunkte gelten (ein Import oder unerreichbarer Code ist keiner).
_NAMED_KINDS = {"function", "method", "class", "property"}

_TEMPLATE_CODE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
#: Dynamische Attribute der Komponenten (django-cotton): ``:field="form.email"``.
_DYNAMIC_ATTRIBUTE = re.compile(r"\s:[\w.-]+=\"([^\"]*)\"")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Punktpfad in einer Zeichenkette: ``"paket.modul.Name"`` oder ``"paket.modul:Name"``.
_DOTTED_STRING = re.compile(r"[\"']([A-Za-z_][\w]*(?:[.:][A-Za-z_][\w]*)+)[\"']")


@dataclass(frozen=True)
class Finding:
    """Ein Fund: Datei relativ zum Repo-Root, erste Zeile, Art, Name, Umfang in Zeilen."""

    file: str
    line: int
    kind: str
    name: str
    size: int


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG).open("rb") as handle:
        return tomllib.load(handle)


def _matches(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def is_counted(relative: str, config: dict[str, Any]) -> bool:
    """Zählen Funde in dieser Datei (Pfad relativ zum Repo-Root, mit ``/``)?"""
    return not _matches(relative, config["nur_verwendung"]["dateien"])


def template_names(text: str) -> set[str]:
    """Namen, die eine Vorlage verwendet: in ``{{ … }}``, ``{% … %}`` und dynamischen Attributen."""
    names: set[str] = set()
    for code in _TEMPLATE_CODE.findall(text):
        names.update(_IDENTIFIER.findall(code))
    for value in _DYNAMIC_ATTRIBUTE.findall(text):
        names.update(_IDENTIFIER.findall(value))
    return names


def dotted_string_names(source: str) -> set[str]:
    """
    Namen aus Punktpfaden in Zeichenketten: das letzte Glied (Klassen und Funktionen, die die Einstellungen
    nennen) und der Pfad mit Unterstrichen (channels ruft für den Nachrichtentyp ``notizen.sync`` die Methode
    ``notizen_sync`` auf).
    """
    names: set[str] = set()
    for path in _DOTTED_STRING.findall(source):
        names.add(re.split(r"[.:]", path)[-1])
        names.add(re.sub(r"[.:]", "_", path))
    return names


def _skipped(path: Path, skipped: set[str]) -> bool:
    return any(part in skipped or part.startswith(".") for part in path.parts[:-1])


def python_files(root: Path, config: dict[str, Any]) -> list[Path]:
    skipped = set(config["umfang"]["ausgelassen"])
    files: list[Path] = []
    for directory in config["umfang"]["pfade"]:
        base = root / directory
        files.extend(p for p in sorted(base.rglob("*.py")) if not _skipped(p.relative_to(root), skipped))
    return files


def template_files(root: Path, config: dict[str, Any]) -> list[Path]:
    skipped = set(config["umfang"]["ausgelassen"])
    files: list[Path] = []
    for directory in config["umfang"]["pfade"]:
        for base in sorted((root / directory).rglob("templates")):
            if base.is_dir() and not _skipped(base.relative_to(root) / "x", skipped):
                files.extend(p for p in sorted(base.rglob("*")) if p.is_file())
    return files


def collect(root: Path | None = None, config: dict[str, Any] | None = None) -> list[Finding]:
    """Funde der gezählten Arten in den gezählten Dateien, noch ohne die Einzelfälle; die größten zuerst."""
    try:
        from vulture import Vulture
    except ImportError:
        raise SystemExit('vulture fehlt: pip install "vulture==<Version aus mandari/pyproject.toml>"') from None

    root = root or ROOT
    config = config or load_config()
    entry = config["einstiegspunkte"]
    # Die Namen der Einstiegspunkte filtert das Skript unten selbst: vulture vergliche jedes Muster mit jeder
    # Definition, das kostet bei dieser Codegröße mehrere Sekunden.
    scanner = Vulture(ignore_decorators=entry["dekoratoren"])
    entry_names = re.compile("|".join(fnmatch.translate(pattern) for pattern in entry["namen"]) or r"(?!)")
    files = python_files(root, config)
    # scavenge statt scan je Datei: nimmt die Listen von vulture für die Standardbibliothek mit
    # (Methoden, die ast.NodeVisitor, logging, unittest und andere von sich aus aufrufen).
    scanner.scavenge([str(path) for path in files])

    used: set[str] = set()
    for path in files:
        used |= dotted_string_names(path.read_text(encoding="utf-8", errors="replace"))
    for path in template_files(root, config):
        used |= template_names(path.read_text(encoding="utf-8", errors="replace"))
    used = {name for name in used if not keyword.iskeyword(name)}
    scanner.scan("\n".join(sorted(used)), filename="vorlagen-und-zeichenketten")

    kinds = set(config["umfang"]["arten"])
    findings = []
    for item in scanner.get_unused_code(min_confidence=int(config["umfang"]["mindest_sicherheit"])):
        if item.typ not in kinds or (item.typ in _NAMED_KINDS and entry_names.match(str(item.name))):
            continue
        relative = Path(item.filename).resolve().relative_to(root.resolve()).as_posix()
        if is_counted(relative, config):
            findings.append(Finding(relative, item.first_lineno, item.typ, str(item.name), item.size))
    return sorted(findings, key=lambda f: (-f.size, f.file, f.line))


def apply_exceptions(findings: list[Finding], config: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    """
    (Funde ohne die Einzelfälle, Einträge, die nichts mehr ausnehmen). Ein Eintrag nennt eine Datei und
    Namen (Shell-Muster); ein Name, der auf keinen Fund mehr passt, ist überholt: Der Code ist gelöscht,
    umbenannt oder wird inzwischen sichtbar verwendet.
    """
    remaining = list(findings)
    stale: list[str] = []
    for exception in config.get("ausnahmen", []):
        for pattern in exception["namen"]:
            hit = [f for f in remaining if f.file == exception["datei"] and fnmatch.fnmatchcase(f.name, pattern)]
            if not hit:
                stale.append(f"{exception['datei']}: {pattern}")
            remaining = [f for f in remaining if f not in hit]
    return remaining, stale


def measure(root: Path | None = None, config: dict[str, Any] | None = None) -> tuple[list[Finding], list[str]]:
    """(Funde nach allen Regeln der Konfiguration, überholte Einzelfälle)."""
    config = config or load_config()
    return apply_exceptions(collect(root, config), config)


def by_file(findings: list[Finding]) -> dict[str, list[str]]:
    """Namen der Funde je Datei, sortiert; derselbe Name kann in einer Datei mehrfach stehen."""
    files: dict[str, list[str]] = {}
    for finding in findings:
        files.setdefault(finding.file, []).append(finding.name)
    return {name: sorted(names) for name, names in sorted(files.items())}


def compare(findings: list[Finding], baseline: dict[str, list[str]]) -> tuple[list[Finding], list[str]]:
    """(neue Funde, die nicht in der Baseline stehen; Einträge der Baseline, die es nicht mehr gibt)."""
    open_entries = {name: Counter(names) for name, names in baseline.items()}
    new: list[Finding] = []
    for finding in findings:
        entries = open_entries.get(finding.file, Counter())
        if entries[finding.name] > 0:
            entries[finding.name] -= 1
        else:
            new.append(finding)
    gone = [
        f"{name}: {entry}" for name, entries in sorted(open_entries.items()) for entry in sorted(entries.elements())
    ]
    return new, gone


def lowered_baseline(findings: list[Finding], baseline: dict[str, list[str]]) -> dict[str, list[str]]:
    """Neue Baseline: nur Einträge, die es noch gibt; nichts kommt hinzu."""
    current = {name: Counter(names) for name, names in by_file(findings).items()}
    result: dict[str, list[str]] = {}
    for name, names in baseline.items():
        kept = sorted((Counter(names) & current.get(name, Counter())).elements())
        if kept:
            result[name] = kept
    return result


def load_baseline(path: Path | None = None) -> dict[str, list[str]]:
    """Erlaubte Funde je Datei; ohne Datei ist nichts erlaubt."""
    path = path or BASELINE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): [str(entry) for entry in names] for name, names in data["dateien"].items()}


def write_baseline(files: dict[str, list[str]], path: Path | None = None) -> None:
    data = {
        "beschreibung": (
            "Funde von vulture (toter Python-Code) je Datei nach scripts/dead_code_ratchet.toml; "
            "die Liste darf nur kürzer werden (scripts/check_dead_code_ratchet.py, Issue #1113)."
        ),
        "funde": sum(len(names) for names in files.values()),
        "dateien": {name: sorted(names) for name, names in sorted(files.items())},
    }
    (path or BASELINE).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _line(finding: Finding) -> str:
    kind = KINDS.get(finding.kind, finding.kind)
    return f"{finding.file}:{finding.line}: {kind} {finding.name} ({finding.size} Zeilen)"


def report(findings: list[Finding], limit: int, top: int = 10) -> str:
    """Bericht als Markdown: Zahl, Aufteilung nach Art, die größten Posten, alle Funde."""
    out = [
        "## Toter Python-Code (vulture)",
        "",
        f"**{len(findings)} Funde (Grenze {limit}), zusammen {sum(f.size for f in findings)} Zeilen.**",
        "",
        "| Art | Funde | Zeilen |",
        "|---|---:|---:|",
    ]
    kinds = Counter(f.kind for f in findings)
    for kind, count in sorted(kinds.items(), key=lambda item: (-item[1], item[0])):
        out.append(f"| {KINDS.get(kind, kind)} | {count} | {sum(f.size for f in findings if f.kind == kind)} |")
    out += [
        "",
        f"### Die {min(top, len(findings))} größten Posten",
        "",
        "| Zeilen | Art | Name | Stelle |",
        "|---:|---|---|---|",
    ]
    out += [f"| {f.size} | {KINDS.get(f.kind, f.kind)} | `{f.name}` | `{f.file}:{f.line}` |" for f in findings[:top]]
    out += ["", "<details><summary>Alle Funde</summary>", "", "```"]
    out += [_line(f) for f in findings]
    out += ["```", "", "</details>", ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ratchet für toten Python-Code (vulture)")
    parser.add_argument("--update", action="store_true", help="abgebaute Funde aus der Baseline streichen")
    parser.add_argument("--verbose", action="store_true", help="alle Funde ausgeben")
    parser.add_argument("--json", action="store_true", help="aktuellen Stand als JSON ausgeben")
    parser.add_argument("--report", metavar="DATEI", help="Bericht als Markdown in diese Datei schreiben")
    args = parser.parse_args(argv)

    findings, stale = measure()
    if args.json:
        stand = {"funde": len(findings), "posten": [asdict(f) for f in findings]}
        print(json.dumps(stand, indent=2, ensure_ascii=False))
        return 0

    baseline = load_baseline()
    new, gone = compare(findings, baseline)
    if args.update and gone:
        write_baseline(lowered_baseline(findings, baseline))
        print(f"Baseline nachgezogen: {BASELINE.relative_to(ROOT).as_posix()}")
        baseline = load_baseline()
        gone = []
    limit = sum(len(names) for names in baseline.values())
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report(findings, limit), encoding="utf-8")
    if args.verbose:
        print("\n".join(_line(f) for f in findings))

    print(f"Toter Python-Code (vulture): {len(findings)} Funde (Baseline {limit})")
    if stale:
        print("\nEinzelfälle in scripts/dead_code_ratchet.toml, die nichts mehr ausnehmen – bitte streichen:")
        print("\n".join(f"- {line}" for line in stale))
    if new:
        print(
            "\nNeuer toter Code – bitte entfernen oder verwenden. Ruft ihn das Framework auf, gehört der "
            "Einstiegspunkt mit Begründung nach scripts/dead_code_ratchet.toml:"
        )
        print("\n".join(f"- {_line(f)}" for f in new))
    if stale or new:
        return 1
    if gone:
        print("\nHinweis: toter Code abgebaut, Baseline nachziehen: python scripts/check_dead_code_ratchet.py --update")
        print("\n".join(f"- {line}" for line in gone))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
