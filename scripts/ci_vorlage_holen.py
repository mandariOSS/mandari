# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fertig migrierte Datenbankvorlagen aus dem Vorrat der CI holen (Issue #1315).

    python scripts/ci_vorlage_holen.py NAME ZIELVERZEICHNIS

``.github/workflows/testvorlage.yml`` migriert bei jeder Änderung der Migrationen auf dev einmal und legt das
Ergebnis als Artefakt ``NAME`` ab (``testvorlage-<Prüfsumme der Migrationen, Abhängigkeiten und Einstellungen>``).
Die Jobs der CI holen das Artefakt hier ab, statt selbst rund 100 Sekunden zu migrieren. Die Prüfsumme im Namen
bestimmt, ob es zum Arbeitsstand passt; die Skripte, die die Vorlagen einspielen, prüfen zusätzlich den
Fingerabdruck der Migrationsdateien (``scripts/testdb_vorlage.py``, ``scripts/build_smoke_db_template.py``).

Angenommen wird nur ein Artefakt, das ein Lauf im eigenen Repository auf dem Zweig dev (oder main) abgelegt hat:
Ein Pull Request aus einem Fork kann ein Artefakt gleichen Namens ablegen, sein Lauf gehört aber zum Fork.

Das Skript scheitert nie: Ohne Artefakt, ohne Token oder bei einem Netzfehler endet es mit 0 und einer Meldung,
und der Job migriert wie bisher. Braucht ``GH_TOKEN`` (Recht ``actions: read``) und ``GITHUB_REPOSITORY``.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

API = "https://api.github.com"
ZWEIGE = ("dev", "main")
ZEITGRENZE = 60
#: Größtes Artefakt, das geladen wird (Bytes); die Vorlagen sind wenige Megabyte groß
HOECHSTENS = 200 * 1024 * 1024


def vertrauenswuerdig(artefakt: dict[str, Any]) -> bool:
    """Nicht abgelaufen und von einem Lauf des eigenen Repositorys auf dev oder main abgelegt."""
    lauf = artefakt.get("workflow_run") or {}
    return (
        not artefakt.get("expired", True)
        and lauf.get("head_branch") in ZWEIGE
        and lauf.get("repository_id") is not None
        and lauf.get("head_repository_id") == lauf.get("repository_id")
    )


def auswaehlen(artefakte: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Das jüngste vertrauenswürdige Artefakt."""
    passend = [a for a in artefakte if vertrauenswuerdig(a)]
    return max(passend, key=lambda a: a.get("created_at") or "") if passend else None


def _abruf(adresse: str, token: str) -> bytes:
    kopf = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mandari-ci"}
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    anfrage = urllib.request.Request(adresse, headers=kopf)  # noqa: S310 – feste https-Adresse der GitHub-API
    with urllib.request.urlopen(anfrage, timeout=ZEITGRENZE) as antwort:  # noqa: S310
        daten: bytes = antwort.read(HOECHSTENS + 1)
    if len(daten) > HOECHSTENS:
        raise ValueError("Artefakt größer als erwartet")
    return daten


def entpacken(archiv: bytes, ziel: Path) -> list[str]:
    """Nur einfache Dateinamen aus dem Archiv, nie Pfade (kein Schreiben außerhalb des Ziels)."""
    ziel.mkdir(parents=True, exist_ok=True)
    namen = []
    with zipfile.ZipFile(io.BytesIO(archiv)) as zip_datei:
        for eintrag in zip_datei.infolist():
            name = Path(eintrag.filename).name
            if eintrag.is_dir() or not name or name != eintrag.filename:
                continue
            (ziel / name).write_bytes(zip_datei.read(eintrag))
            namen.append(name)
    return sorted(namen)


def holen(name: str, ziel: Path, repository: str, token: str) -> str:
    liste = json.loads(_abruf(f"{API}/repos/{repository}/actions/artifacts?name={name}&per_page=30", token))
    artefakt = auswaehlen(liste.get("artifacts") or [])
    if artefakt is None:
        return f"Kein Artefakt {name} im Vorrat"
    archiv = _abruf(f"{API}/repos/{repository}/actions/artifacts/{artefakt['id']}/zip", token)
    namen = entpacken(archiv, ziel)
    return f"Artefakt {name} vom {artefakt.get('created_at')} geholt: {', '.join(namen)}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    name, ziel = argv[0], Path(argv[1])
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not repository or not token:
        print("Vorrat übersprungen: GITHUB_REPOSITORY oder GH_TOKEN fehlt")
        return 0
    try:
        print(holen(name, ziel, repository, token))
    except (OSError, ValueError, KeyError, http.client.HTTPException, zipfile.BadZipFile) as fehler:
        # urllib.error.URLError ist ein OSError; json.JSONDecodeError ein ValueError
        print(f"Vorrat nicht erreichbar ({type(fehler).__name__}: {fehler}), der Job migriert selbst")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
