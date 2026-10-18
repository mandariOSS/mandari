# SPDX-License-Identifier: AGPL-3.0-or-later
"""Doppelte Umgebungsvariablen je Container in gerenderten Manifesten finden (Issue #919).

    helm template mandari deploy/kubernetes/helm/mandari | python deploy/kubernetes/umgebung_pruefen.py
    python deploy/kubernetes/umgebung_pruefen.py deploy/kubernetes/manifests/*.yaml

Steht ein Name zweimal in der Umgebung eines Containers, gilt in Kubernetes der letzte Eintrag, und
Server-Side Apply lehnt das Manifest ab. Ein Schalter aus zwei Werten wirkt dann je nach Reihenfolge nicht
(etwa ``TEXT_EXTRACTION_RUNNER`` aus zwei Schlüsseln des Charts). Exit 1 bei einem Fund.

Nur Standardbibliothek, damit die CI ohne zusätzliche Pakete prüft. Gelesen wird die Blockschreibweise, die
``helm template`` erzeugt: ``env:`` mit Einträgen ``- name: …``; verschachtelte Namen (``valueFrom``) zählen
nicht. Den Container erkennt die Prüfung an ``image:`` unter seinem Eintrag ``- name: …``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SCHLUESSEL = re.compile(r"^(?P<einzug> *)(?P<schluessel>[A-Za-z][\w.-]*):")
_EINTRAG = re.compile(r"^(?P<einzug> *)- name:\s*(?P<name>\S+)\s*$")


def _einzug(zeile: str) -> int:
    return len(zeile) - len(zeile.lstrip(" "))


def _art_und_name(zeilen: list[str]) -> str:
    art = next((z.split(":", 1)[1].strip() for z in zeilen if z.startswith("kind:")), "?")
    name = next((z.split(":", 1)[1].strip() for z in zeilen if z.startswith("  name:")), "?")
    return f"{art}/{name}"


def _env_block(zeilen: list[str], start: int, grenze: int) -> tuple[list[str], int]:
    """Namen der Einträge des Blocks ``env:`` ab Zeile ``start`` (Einzug des Schlüssels ``grenze``) und Ende."""
    namen: list[str] = []
    listen_einzug: int | None = None
    i = start
    while i < len(zeilen):
        zeile = zeilen[i]
        inhalt = zeile.strip()
        if inhalt and not inhalt.startswith("#"):
            einzug = _einzug(zeile)
            # Ende: Schlüssel auf gleicher oder kleinerer Ebene (Einträge dürfen auf Höhe des Schlüssels stehen)
            if einzug < grenze or (einzug == grenze and not inhalt.startswith("- ")):
                break
            eintrag = _EINTRAG.match(zeile)
            if inhalt.startswith("- "):
                if listen_einzug is None:
                    listen_einzug = einzug
                if einzug == listen_einzug:
                    namen.append(eintrag["name"] if eintrag else "?")
        i += 1
    return namen, i


def doppelte(text: str, quelle: str = "-") -> list[str]:
    """Je doppelter Umgebungsvariable eine Meldung ``quelle: Art/Name, Container c: NAME doppelt``."""
    meldungen: list[str] = []
    for dokument in re.split(r"(?m)^---\s*$", text.replace("\r\n", "\n")):
        zeilen = dokument.split("\n")
        objekt = _art_und_name(zeilen)
        letzter_eintrag: dict[int, str] = {}
        container = "?"
        i = 0
        while i < len(zeilen):
            zeile = zeilen[i]
            if (eintrag := _EINTRAG.match(zeile)) is not None:
                letzter_eintrag[len(eintrag["einzug"])] = eintrag["name"]
            schluessel = _SCHLUESSEL.match(zeile)
            if schluessel is None:
                i += 1
                continue
            einzug = len(schluessel["einzug"])
            if schluessel["schluessel"] == "image":
                # Container: der Listeneintrag "- name: …", unter dem image steht
                container = letzter_eintrag.get(einzug - 2, "?")
            if schluessel["schluessel"] == "env" and zeile.strip() == "env:":
                namen, i = _env_block(zeilen, i + 1, einzug)
                gesehen: set[str] = set()
                for name in namen:
                    if name in gesehen:
                        meldungen.append(f"{quelle}: {objekt}, Container {container}: {name} doppelt")
                    gesehen.add(name)
                continue
            i += 1
    return meldungen


def main(argumente: list[str]) -> int:
    if argumente:
        meldungen = [m for pfad in argumente for m in doppelte(Path(pfad).read_text(encoding="utf-8"), pfad)]
    else:
        meldungen = doppelte(sys.stdin.read())
    for meldung in meldungen:
        print(meldung)
    if meldungen:
        print("Doppelte Umgebungsvariablen: je Container darf jeder Name nur einmal vorkommen.", file=sys.stderr)
        return 1
    print("Keine doppelten Umgebungsvariablen.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
