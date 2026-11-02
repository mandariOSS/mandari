# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, dass die Images (mandari, Ingestor) kein Python-Paket ohne Hash-Prüfung installieren.

Die Abhängigkeiten kommen aus ``uv.lock`` und werden mit ``--require-hashes`` installiert. Daneben bauen die
Images die lokalen Pakete (``shared/`` und ``ingestor/``). Dafür braucht der Installer ihr Build-Backend
(``[build-system] requires``). In einer isolierten Build-Umgebung lädt er es frisch aus dem Paketindex, ohne
feste Version und ohne Hash; ``--no-deps`` ändert daran nichts. Dasselbe gilt für jedes gesperrte Paket, das
nur als Quelltext vorliegt und deshalb im Image gebaut werden müsste.

Regeln für jeden Installationsbefehl in einer RUN-Anweisung (``pip install``/``wheel``/``download``,
``uv pip install``, ``uv sync`` …):

- entweder ``--require-hashes`` und nur fertige Wheels (``--only-binary :all:`` bei pip, ``--no-build`` bei uv),
- oder ohne Netz: ``RUN --network=none`` und ``--no-index`` (bzw. ``--offline``). Ein Build-Backend kommt dann
  nur aus einer Umgebung, die vorher mit Hashes befüllt wurde (``--no-build-isolation``).

Außerdem steht das Build-Backend jedes lokalen Pakets in der Abhängigkeitsgruppe ``build-backend`` des
Projekts, dessen Lock-Datei das Image liest, ist dort samt seinen Abhängigkeiten mit Hashes gesperrt, und das
Dockerfile exportiert genau diese Gruppe.

    python scripts/check_image_hashes.py

Nur Standardbibliothek: Der Schritt läuft im Job „Qualität“ und im Release-Workflow vor dem Bau der Images.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

# Abhängigkeitsgruppe in pyproject.toml, aus der die Images das Build-Backend installieren
GRUPPE = "build-backend"


@dataclass(frozen=True)
class Image:
    name: str
    dockerfile: Path
    # Ordner mit pyproject.toml und uv.lock, aus denen das Dockerfile exportiert
    projekt: Path
    # Lokale Pakete, die das Image baut
    lokale_pakete: tuple[Path, ...]


IMAGES = (
    Image("mandari", WURZEL / "mandari" / "Dockerfile", WURZEL / "mandari", (WURZEL / "shared",)),
    Image(
        "ingestor",
        WURZEL / "ingestor" / "Dockerfile",
        WURZEL / "ingestor",
        (WURZEL / "shared", WURZEL / "ingestor"),
    ),
)

# Befehle, die Python-Pakete holen oder bauen. `uv venv --seed` installiert pip aus dem Paketindex.
INSTALLATION = re.compile(
    r"\bpip3?\s+(?:install|wheel|download)\b|\buv\s+(?:sync|build|tool|add|run)\b|\buv\s+venv\b.*--seed\b"
)
NUR_WHEELS = re.compile(r"--only-binary[=\s]+[\"']?:all:|--no-build(?![-\w])")
OHNE_INDEX = re.compile(r"--no-index(?![-\w])|--offline(?![-\w])")


def _name(anforderung: str) -> str:
    """Normalisierter Paketname einer Anforderung (PEP 503), ohne Version, Extras und Marker."""
    treffer = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", anforderung)
    if not treffer:
        raise ValueError(f"keine Anforderung: {anforderung!r}")
    return re.sub(r"[-_.]+", "-", treffer.group(1)).lower()


def anweisungen(text: str) -> Iterator[tuple[int, str]]:
    """Anweisungen eines Dockerfiles mit ihrer ersten Zeile; Fortsetzungszeilen zusammengefügt, Kommentare entfernt."""
    teile: list[str] = []
    beginn = 0
    for nummer, zeile in enumerate(text.splitlines(), start=1):
        # Kommentar- und Leerzeilen überspringt Docker auch mitten in einer Fortsetzung
        if not zeile.strip() or zeile.lstrip().startswith("#"):
            continue
        if not teile:
            beginn = nummer
        inhalt = zeile.rstrip()
        teile.append(inhalt.removesuffix("\\"))
        if not inhalt.endswith("\\"):
            yield beginn, " ".join(teil.strip() for teil in teile)
            teile = []
    if teile:
        yield beginn, " ".join(teil.strip() for teil in teile)


def run_befehle(text: str) -> Iterator[tuple[int, list[str], str]]:
    """Einzelbefehle aller RUN-Anweisungen: (Zeile, Optionen der RUN-Anweisung, Befehl)."""
    for nummer, anweisung in anweisungen(text):
        treffer = re.match(r"RUN\s+(.*)$", anweisung, flags=re.IGNORECASE)
        if not treffer:
            continue
        rest = treffer.group(1).strip()
        optionen: list[str] = []
        while rest.startswith("--"):
            option, _, rest = rest.partition(" ")
            optionen.append(option)
            rest = rest.lstrip()
        if rest.startswith("["):  # Exec-Form: RUN ["pip", "install", …]
            try:
                rest = shlex.join(json.loads(rest))
            except (ValueError, TypeError):
                pass
        for befehl in re.split(r"&&|\|\||;|\|", rest):
            if befehl.strip():
                yield nummer, optionen, befehl.strip()


def pruefe_dockerfile(text: str, name: str = "Dockerfile") -> list[str]:
    """Installationsbefehle, die ein Paket ohne Hash-Prüfung holen oder bauen könnten."""
    probleme: list[str] = []
    gruppe_exportiert = False
    for nummer, optionen, befehl in run_befehle(text):
        if re.search(rf"\buv\s+export\b.*--only-group[=\s]+{re.escape(GRUPPE)}(?![-\w])", befehl):
            gruppe_exportiert = True
        if not INSTALLATION.search(befehl):
            continue
        ort = f"{name}:{nummer}: {befehl}"
        if "--require-hashes" in befehl:
            if not NUR_WHEELS.search(befehl):
                probleme.append(
                    f"{ort}\n    Hash-geprüft, aber Quelltextpakete würden mit einem frisch geladenen Build-Backend "
                    "gebaut: `--only-binary :all:` (pip) bzw. `--no-build` (uv) ergänzen."
                )
        elif "--network=none" not in optionen or not OHNE_INDEX.search(befehl):
            probleme.append(
                f"{ort}\n    Installation ohne `--require-hashes`. Ohne Hash-Prüfung nur ohne Netz: "
                "`RUN --network=none` und `--no-index`, das Build-Backend vorher mit Hashes installieren "
                "und mit `--no-build-isolation` nutzen."
            )
    if not gruppe_exportiert:
        probleme.append(
            f"{name}: exportiert die Gruppe `{GRUPPE}` nicht (`uv export --only-group {GRUPPE}`); "
            "das Build-Backend der lokalen Pakete käme sonst nicht aus der Lock-Datei."
        )
    return probleme


def _build_backend(pyproject: dict) -> set[str]:
    return {_name(anforderung) for anforderung in pyproject.get("build-system", {}).get("requires", [])}


def pruefe_build_backend(pyproject: dict, lock: dict, lokale_pakete: dict[str, dict], name: str) -> list[str]:
    """
    Das Build-Backend aller lokalen Pakete steht in der Gruppe ``build-backend`` und ist in der Lock-Datei
    samt Abhängigkeiten nur mit Wheels und Hashes gesperrt.

    ``lokale_pakete``: Name des Pakets → Inhalt seiner pyproject.toml.
    """
    probleme: list[str] = []
    noetig: dict[str, str] = {}
    for paket, inhalt in lokale_pakete.items():
        for backend in sorted(_build_backend(inhalt)):
            noetig.setdefault(backend, paket)

    gruppe = {_name(eintrag) for eintrag in pyproject.get("dependency-groups", {}).get(GRUPPE, [])}
    for backend, paket in sorted(noetig.items()):
        if backend not in gruppe:
            probleme.append(
                f"{name}/pyproject.toml: Build-Backend `{backend}` von {paket} fehlt in [dependency-groups] "
                f"{GRUPPE} (danach `uv lock` im Ordner {name})"
            )

    pakete = {_name(paket["name"]): paket for paket in lock.get("package", [])}
    projekt_name = _name(pyproject["project"]["name"])
    projekt = pakete.get(projekt_name, {})
    gesperrt = {_name(eintrag["name"]) for eintrag in projekt.get("dev-dependencies", {}).get(GRUPPE, [])}
    if gruppe and gesperrt != gruppe:
        probleme.append(
            f"{name}/uv.lock: Gruppe {GRUPPE} passt nicht zu pyproject.toml "
            f"(Lock: {sorted(gesperrt)}, pyproject.toml: {sorted(gruppe)}) – `uv lock` im Ordner {name}"
        )

    # Alles, was die Gruppe nach sich zieht, braucht Wheels mit Hash
    offen, gesehen = sorted(gesperrt), set()
    while offen:
        paket_name = offen.pop()
        if paket_name in gesehen:
            continue
        gesehen.add(paket_name)
        paket = pakete.get(paket_name)
        if paket is None:
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) ist nicht gesperrt")
            continue
        if "registry" not in paket.get("source", {}):
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) kommt nicht aus dem Paketindex")
        wheels = paket.get("wheels", [])
        if not wheels or not all(wheel.get("hash") for wheel in wheels):
            probleme.append(f"{name}/uv.lock: `{paket_name}` (Gruppe {GRUPPE}) ohne Wheels mit Hash")
        offen.extend(_name(abhaengigkeit["name"]) for abhaengigkeit in paket.get("dependencies", []))
    return probleme


def _lies_toml(pfad: Path) -> dict:
    return tomllib.loads(pfad.read_text(encoding="utf-8"))


def pruefe(images: tuple[Image, ...] = IMAGES) -> list[str]:
    probleme: list[str] = []
    for image in images:
        name = image.projekt.name
        probleme += pruefe_dockerfile(
            image.dockerfile.read_text(encoding="utf-8"), image.dockerfile.relative_to(WURZEL).as_posix()
        )
        probleme += pruefe_build_backend(
            _lies_toml(image.projekt / "pyproject.toml"),
            _lies_toml(image.projekt / "uv.lock"),
            {paket.name: _lies_toml(paket / "pyproject.toml") for paket in image.lokale_pakete},
            name,
        )
    return probleme


def main() -> int:
    probleme = pruefe()
    if probleme:
        print("Die Images würden Pakete ohne Hash-Prüfung installieren:\n")
        for problem in probleme:
            print(f"  - {problem}")
        print(
            "\nAbhängigkeiten kommen aus uv.lock und werden mit --require-hashes installiert; das Build-Backend "
            "der lokalen Pakete steht in der Gruppe build-backend (siehe CONTRIBUTING.md)."
        )
        return 1
    print(f"OK: {', '.join(image.name for image in IMAGES)} installieren nur hash-geprüfte Pakete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
