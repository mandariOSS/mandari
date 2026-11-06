# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schemas der kommParl-Spezifikation übernehmen und prüfen (Issue #1216).

    python scripts/kommparl_schemas.py --commit <sha>   # Stand eines Commits übernehmen
    python scripts/kommparl_schemas.py --pruefen        # übernommene Dateien gegen ihre Herkunft prüfen

**Übernehmen:** Holt den angegebenen Commit aus ``mandariOSS/kommParl`` (``--repo``), liest die Fassung aus
``src/0-00-metadata.md`` (``kommparl-version``) und legt die Schemas unverändert unter
``mandari/hub/api/kommparl/<fassung>/`` ab:

- ``schema/*.json``: die Objekttypen im Format der OParl-Spezifikation,
- ``profiles/<Profil>/*.json``: die Schemas der Profile (JSON Schema 2020-12), ohne Beispiele.

Dazu schreibt es ``HERKUNFT.json`` mit Repository, Commit, Datum des Commits, Tag der Übernahme, Lizenz und der
SHA-256-Prüfsumme jeder Datei. Vorhandene ``schema/`` und ``profiles/`` dieser Fassung werden ersetzt. Ändert sich
die Fassung, muss ``FASSUNG`` in ``mandari/hub/api/kommparl/__init__.py`` nachgezogen werden; das Skript weist
darauf hin.

**Prüfen:** Jede Datei in ``HERKUNFT.json`` ist vorhanden und unverändert, und es liegen keine weiteren Schemas
daneben. Zeilenenden zählen nicht (Windows-Arbeitskopien).

Nur Standardbibliothek und ``git``; ohne Django.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "mandari" / "hub" / "api" / "kommparl"
REPOSITORY = "https://github.com/mandariOSS/kommParl"
LICENSE = "CC-BY-SA-4.0"
METADATA = "src/0-00-metadata.md"
PROVENANCE = "HERKUNFT.json"

_VERSION = re.compile(r'^kommparl-version:\s*["\']?([0-9]+\.[0-9]+)["\']?\s*$', re.MULTILINE)
_COMMIT = re.compile(r"[0-9a-f]{7,40}")


def wanted(path: str) -> bool:
    """Gehört die Datei zu den übernommenen Schemas? (``schema/*.json``, ``profiles/<Profil>/*.json``)"""
    parts = path.split("/")
    if not path.endswith(".json"):
        return False
    return (len(parts) == 2 and parts[0] == "schema") or (len(parts) == 3 and parts[0] == "profiles")


def checksum(content: bytes) -> str:
    """SHA-256 über den Inhalt mit Zeilenenden LF."""
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def _git(*args: str, cwd: Path) -> bytes:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)  # noqa: S603, S607
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def fetch(repository: str, commit: str) -> int:
    """Stand ``commit`` übernehmen; Rückgabewert 0 bei Erfolg."""
    if not _COMMIT.fullmatch(commit):
        raise SystemExit(f"Kein Commit: {commit!r} (erwartet: SHA, 7 bis 40 Hexadezimalzeichen)")
    with tempfile.TemporaryDirectory(prefix="kommparl-") as tmp:
        work = Path(tmp)
        _git("init", "-q", cwd=work)
        _git("fetch", "-q", "--depth", "1", f"{repository}.git", commit, cwd=work)
        sha = _git("rev-parse", "FETCH_HEAD", cwd=work).decode().strip()
        committed = _git("log", "-1", "--format=%cI", "FETCH_HEAD", cwd=work).decode().strip()
        metadata = _git("show", f"FETCH_HEAD:{METADATA}", cwd=work).decode("utf-8")
        match = _VERSION.search(metadata)
        if match is None:
            raise SystemExit(f"{METADATA} nennt keine Fassung (kommparl-version)")
        version = match.group(1)
        names = sorted(
            name
            for name in _git("ls-tree", "-r", "--name-only", "FETCH_HEAD", "--", "schema", "profiles", cwd=work)
            .decode("utf-8")
            .splitlines()
            if wanted(name)
        )
        if not names:
            raise SystemExit("Der Stand enthält keine Schemas unter schema/ und profiles/")
        files = {name: _git("show", f"FETCH_HEAD:{name}", cwd=work) for name in names}

    target = TARGET / version
    for folder in ("schema", "profiles"):
        shutil.rmtree(target / folder, ignore_errors=True)
    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    provenance = {
        "repository": repository,
        "commit": sha,
        "commitDate": committed,
        "retrieved": datetime.now(UTC).date().isoformat(),
        "version": version,
        "license": LICENSE,
        "files": {name: checksum(content) for name, content in files.items()},
    }
    (target / PROVENANCE).write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"kommParl {version} (Commit {sha[:12]} vom {committed[:10]}): {len(files)} Schemas nach {target}")
    package = (TARGET / "__init__.py").read_text(encoding="utf-8")
    if f'FASSUNG: Final = "{version}"' not in package:
        print(f"Hinweis: FASSUNG in {TARGET / '__init__.py'} auf {version!r} setzen und Kennungen prüfen.")
    return 0


def check() -> int:
    """Alle übernommenen Fassungen gegen ihre ``HERKUNFT.json`` prüfen; Rückgabewert 1 bei Abweichungen."""
    problems: list[str] = []
    provenances = sorted(TARGET.glob(f"*/{PROVENANCE}"))
    if not provenances:
        problems.append(f"Keine übernommenen Schemas unter {TARGET}")
    for provenance in provenances:
        folder = provenance.parent
        data = json.loads(provenance.read_text(encoding="utf-8"))
        listed: dict[str, str] = data.get("files", {})
        present = {
            path.relative_to(folder).as_posix()
            for path in folder.rglob("*.json")
            if path.name != PROVENANCE and wanted(path.relative_to(folder).as_posix())
        }
        for name in sorted(present - set(listed)):
            problems.append(f"{folder.name}/{name}: nicht in {PROVENANCE}")
        for name, expected in sorted(listed.items()):
            path = folder / name
            if not path.is_file():
                problems.append(f"{folder.name}/{name}: fehlt")
            elif checksum(path.read_bytes()) != expected:
                problems.append(f"{folder.name}/{name}: verändert (Prüfsumme weicht ab)")
    for problem in problems:
        print(f"FEHLER: {problem}")
    if problems:
        return 1
    print(f"kommParl-Schemas unverändert: {', '.join(p.parent.name for p in provenances)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--commit", help="Commit von kommParl, dessen Schemas übernommen werden")
    action.add_argument("--pruefen", action="store_true", help="übernommene Dateien gegen ihre Herkunft prüfen")
    parser.add_argument("--repo", default=REPOSITORY, help=f"Repository (Vorgabe {REPOSITORY})")
    args = parser.parse_args(argv)
    if args.pruefen:
        return check()
    return fetch(args.repo.removesuffix(".git"), args.commit)


if __name__ == "__main__":
    sys.exit(main())
