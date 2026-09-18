#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaiste Template-Blöcke finden.

Django verwirft einen ``{% block name %}`` in einer abgeleiteten Vorlage stillschweigend, wenn
keine Vorlage der Vererbungskette diesen Block definiert. So lieferte die Import-Seite ihr
Skript in ``{% block extra_js %}`` aus, obwohl ``work/base_work.html`` nur ``extra_scripts``
kennt – die Alpine-Komponente ``importForm`` existierte im Browser nie.

Geprüft wird jede Vorlage mit ``{% extends "…" %}``: Jeder Block auf oberster Ebene (nicht in
einem anderen Block geschachtelt) muss in einer Vorfahrin definiert sein. Blöcke, die in einem
anderen Block stehen, definieren selbst neue Blöcke und sind immer gültig.

Aufruf (aus dem Repo-Wurzelverzeichnis): ``python scripts/check_template_blocks.py`` – Exitcode 1 bei Befunden.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "mandari"
TEMPLATE_DIRS = [ROOT / "templates", *sorted(ROOT.glob("apps/*/templates")), *sorted(ROOT.glob("*/templates"))]

TAG_RE = re.compile(r"{%-?\s*(block|endblock|extends)\b\s*([^%]*?)\s*-?%}")
COMMENT_RE = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.S)


def _find(name: str) -> Path | None:
    for base in TEMPLATE_DIRS:
        kandidat = base / name
        if kandidat.is_file():
            return kandidat
    return None


def _parse(path: Path) -> tuple[str | None, list[str], set[str]]:
    """(Elternvorlage, Blöcke oberster Ebene, alle definierten Blöcke)."""
    text = COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
    parent: str | None = None
    top: list[str] = []
    alle: set[str] = set()
    tiefe = 0
    for tag, arg in TAG_RE.findall(text):
        if tag == "extends":
            m = re.match(r"""["']([^"']+)["']""", arg)
            if m:
                parent = m.group(1)
        elif tag == "block":
            name = arg.split()[0] if arg else ""
            alle.add(name)
            if tiefe == 0:
                top.append(name)
            tiefe += 1
        else:
            tiefe = max(0, tiefe - 1)
    return parent, top, alle


def _ahnen_bloecke(name: str, gesehen: set[str]) -> set[str] | None:
    """Alle Blöcke der Vererbungskette ab ``name``; None, wenn die Kette nicht auflösbar ist."""
    if name in gesehen:
        return set()
    gesehen.add(name)
    pfad = _find(name)
    if pfad is None:
        return None
    parent, _top, alle = _parse(pfad)
    if parent:
        weiter = _ahnen_bloecke(parent, gesehen)
        if weiter is None:
            return None
        alle |= weiter
    return alle


def verwaiste_bloecke() -> list[tuple[str, str]]:
    """(Vorlage relativ zu ``mandari/``, Blockname) für jeden Block, den Django nie rendert."""
    treffer: list[tuple[str, str]] = []
    for base in TEMPLATE_DIRS:
        if not base.is_dir():
            continue
        for pfad in sorted(base.rglob("*.html")):
            if "node_modules" in pfad.parts:
                continue
            parent, top, _alle = _parse(pfad)
            if not parent or "{{" in parent:
                continue
            erlaubt = _ahnen_bloecke(parent, set())
            if erlaubt is None:
                continue  # Elternvorlage aus Drittpaket (admin, unfold) – nicht prüfbar
            treffer.extend((pfad.relative_to(ROOT).as_posix(), block) for block in top if block not in erlaubt)
    return treffer


def pruefen() -> list[str]:
    return [f"{datei}: Block '{block}' fehlt in der Vererbungskette" for datei, block in verwaiste_bloecke()]


def main() -> int:
    befunde = pruefen()
    for zeile in befunde:
        print(zeile)
    if befunde:
        print(f"\n{len(befunde)} verwaiste Template-Blöcke – Inhalt wird von Django nie gerendert.")
        return 1
    print("Template-Blöcke: keine verwaisten Blöcke.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
