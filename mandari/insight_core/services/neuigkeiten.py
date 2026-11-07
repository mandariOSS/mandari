# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Was ist neu?“ im Bürgerportal und die Versionsnummer im Fuß (Issue #1261).

Die Neuigkeiten sind kuratiert: kurze Einträge in Bürgersprache aus ``insight_core/neuigkeiten.toml`` (ein Satz Nutzen,
ein Absatz Erklärung), nicht die technische CHANGELOG.md. Die Datei wird beim ersten Aufruf gelesen und bleibt bis
zum Neustart im Speicher; ein Fehler in der Datei bricht keine Seite, sondern lässt die Liste leer (und wird
protokolliert). Pflichtfelder, Längen und Anrede prüft ``insight_core/tests/test_neuigkeiten.py``.

Die Version kommt aus ``pyproject.toml`` (die eine Stelle nach ``docs/RELEASE_POLITIK.md``), im Image unter
``/app/pyproject.toml``.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib import metadata
from itertools import groupby
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

DATEI = Path(__file__).resolve().parent.parent / "neuigkeiten.toml"
#: Grenzen für Titel und Absatz: Ein Eintrag soll sich in wenigen Sekunden lesen lassen
TITEL_MAX = 80
TEXT_MAX = 500


@dataclass(frozen=True)
class Neuigkeit:
    datum: date
    version: str
    titel: str
    text: str


@dataclass(frozen=True)
class Gruppe:
    """Neuigkeiten eines Tags (und einer Version) für eine Überschrift auf der Seite."""

    datum: date
    version: str
    eintraege: tuple[Neuigkeit, ...]


def lies(pfad: Path | None = None) -> list[Neuigkeit]:
    """Einträge der Datei (Standard ``DATEI``), neueste zuerst; ``ValueError`` bei fehlenden oder falschen Feldern."""
    with (pfad or DATEI).open("rb") as datei:
        daten = tomllib.load(datei)
    eintraege = []
    for nummer, roh in enumerate(daten.get("eintrag", []), start=1):
        datum, titel, text = roh.get("datum"), roh.get("titel"), roh.get("text")
        version = roh.get("version", "")
        if not isinstance(datum, date) or not isinstance(version, str):
            raise ValueError(f"Eintrag {nummer}: datum (JJJJ-MM-TT) und version (Text) prüfen")
        if not isinstance(titel, str) or not titel.strip() or not isinstance(text, str) or not text.strip():
            raise ValueError(f"Eintrag {nummer}: titel und text sind Pflicht")
        eintraege.append(Neuigkeit(datum=datum, version=version.strip(), titel=titel.strip(), text=text.strip()))
    # Stabil sortiert: Innerhalb eines Tags bleibt die Reihenfolge der Datei
    return sorted(eintraege, key=lambda eintrag: eintrag.datum, reverse=True)


@cache
def neuigkeiten() -> tuple[Neuigkeit, ...]:
    """Alle Einträge, neueste zuerst; leer, wenn die Datei fehlt oder fehlerhaft ist."""
    try:
        return tuple(lies())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        logger.exception("Neuigkeiten des Bürgerportals nicht lesbar (%s)", DATEI)
        return ()


def gruppen() -> list[Gruppe]:
    """Einträge je Tag und Version, neueste zuerst."""
    return [
        Gruppe(datum=datum, version=version, eintraege=tuple(eintraege))
        for (datum, version), eintraege in groupby(neuigkeiten(), key=lambda eintrag: (eintrag.datum, eintrag.version))
    ]


@cache
def mandari_version() -> str:
    """Version der Installation aus ``pyproject.toml``, sonst aus den Paketdaten; leer, wenn keine bekannt ist."""
    try:
        with (Path(settings.BASE_DIR) / "pyproject.toml").open("rb") as datei:
            version = tomllib.load(datei).get("project", {}).get("version")
        if isinstance(version, str) and version:
            return version
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("mandari")
    except metadata.PackageNotFoundError:
        return ""
