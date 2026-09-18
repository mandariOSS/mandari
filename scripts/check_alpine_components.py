#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Alpine-Komponenten ohne Definition finden.

Ein ``x-data="importForm()"`` braucht eine Funktion ``importForm`` – entweder als Inline-Skript
derselben Vorlage (bzw. einer eingebundenen oder erweiterten Vorlage) oder global aus dem
Vite-Bundle (``window.importForm = …`` oder ``Alpine.data("importForm", …)``). Fehlt sie, bleibt
die Seite still kaputt: Buttons ohne Funktion, Formulare, die nie absenden.

Inline-Skripte zählen nur, wenn sie in einem Block stehen, den die Vererbungskette auch
rendert – ein Skript in einem verwaisten Block (siehe ``check_template_blocks.py``) kommt nie
im Browser an. Genau so fiel der Dokument-Import aus (``{% block extra_js %}`` statt
``extra_scripts``).

Aufruf (aus dem Repo-Wurzelverzeichnis): ``python scripts/check_alpine_components.py`` – Exitcode 1 bei Befunden.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_template_blocks import verwaiste_bloecke  # noqa: E402

TEMPLATE_DIRS = [ROOT / "templates", *sorted(ROOT.glob("apps/*/templates")), *sorted(ROOT.glob("*/templates"))]
FRONTEND_DIRS = [ROOT / "frontend", ROOT / "static" / "js"]

# x-data="name" (Alpine.data) oder x-data="name(…)" (globale Funktion)
XDATA_RE = re.compile(r"""x-data\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*(?=[("'])""")
EXTENDS_RE = re.compile(r"""{%\s*extends\s+["']([^"']+)["']""")
INCLUDE_RE = re.compile(r"""{%\s*include\s+["']([^"']+)["']""")
COTTON_RE = re.compile(r"<c-([\w.-]+)")
COMMENT_RE = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}|<!--.*?--!?>", re.S | re.I)
BLOCK_RE = re.compile(r"{%-?\s*block\s+(\w+)\s*-?%}(.*?){%-?\s*endblock\b[^%]*%}", re.S)
SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script\b[^>]*>", re.S | re.I)


def _definitionen(text: str) -> set[str]:
    namen: set[str] = set()
    namen.update(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", text))
    namen.update(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", text))
    namen.update(re.findall(r"""Alpine\.data\(\s*["']([\w$]+)["']""", text))
    namen.update(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|function|async)", text))
    for block in re.findall(r"Object\.assign\(\s*window\s*,\s*{([^}]*)}", text):
        namen.update(re.findall(r"([A-Za-z_$][\w$]*)", block))
    return namen


def _global_definiert() -> set[str]:
    namen: set[str] = set()
    for base in FRONTEND_DIRS:
        if not base.is_dir():
            continue
        for pfad in base.rglob("*"):
            if pfad.suffix in {".ts", ".js"} and not {"node_modules", "dist"} & set(pfad.parts):
                namen |= _definitionen(pfad.read_text(encoding="utf-8", errors="ignore"))
    return namen


def _find(name: str) -> Path | None:
    for base in TEMPLATE_DIRS:
        kandidat = base / name
        if kandidat.is_file():
            return kandidat
    return None


def _rel(pfad: Path) -> str:
    return pfad.relative_to(ROOT).as_posix()


def _lokal_definiert(pfad: Path, verwaist: dict[str, set[str]], gesehen: set[Path]) -> set[str]:
    """Definitionen aus der Vorlage, ihren Includes, Cotton-Komponenten und Vorfahren."""
    if pfad in gesehen:
        return set()
    gesehen.add(pfad)
    text = COMMENT_RE.sub("", pfad.read_text(encoding="utf-8"))
    # Skripte in verwaisten Blöcken rendert Django nie – vorher entfernen
    tote = verwaist.get(_rel(pfad), set())
    if tote:
        text = BLOCK_RE.sub(lambda m: "" if m.group(1) in tote else m.group(0), text)
    namen: set[str] = set()
    for skript in SCRIPT_RE.findall(text):
        namen |= _definitionen(skript)
    ziele = [_find(inc) for inc in INCLUDE_RE.findall(text)]
    ziele += [_find("cotton/" + c.replace(".", "/") + ".html") for c in COTTON_RE.findall(text)]
    m = EXTENDS_RE.search(text)
    if m:
        ziele.append(_find(m.group(1)))
    for ziel in ziele:
        if ziel:
            namen |= _lokal_definiert(ziel, verwaist, gesehen)
    return namen


def _einbindende(ziel: Path, alle: list[Path]) -> list[Path]:
    """Vorlagen, die ``ziel`` per include/extends/Cotton einbinden (Partials ohne eigenes Skript)."""
    namen = {ziel.relative_to(b).as_posix() for b in TEMPLATE_DIRS if ziel.is_relative_to(b)}
    treffer = []
    for p in alle:
        text = p.read_text(encoding="utf-8")
        for n in namen:
            if f'"{n}"' in text or f"'{n}'" in text:
                treffer.append(p)
                break
            if n.startswith("cotton/") and f"<c-{n[7:-5].replace('/', '.')}" in text:
                treffer.append(p)
                break
    return treffer


def pruefen() -> list[str]:
    global_namen = _global_definiert()
    verwaist: dict[str, set[str]] = {}
    for datei, block in verwaiste_bloecke():
        verwaist.setdefault(datei, set()).add(block)
    alle = [p for b in TEMPLATE_DIRS if b.is_dir() for p in sorted(b.rglob("*.html")) if "node_modules" not in p.parts]
    befunde: list[str] = []
    for pfad in alle:
        text = COMMENT_RE.sub("", pfad.read_text(encoding="utf-8"))
        aufrufe = set(XDATA_RE.findall(text))
        offen = aufrufe - global_namen - _lokal_definiert(pfad, verwaist, set())
        if not offen:
            continue
        eltern = _einbindende(pfad, alle)
        if eltern:
            offen = {n for n in offen if not all(n in _lokal_definiert(e, verwaist, set()) for e in eltern)}
        for name in sorted(offen):
            befunde.append(f"{_rel(pfad)}: x-data verweist auf '{name}', Definition fehlt oder wird nie gerendert")
    return befunde


def main() -> int:
    befunde = pruefen()
    for zeile in befunde:
        print(zeile)
    if befunde:
        print(f"\n{len(befunde)} Alpine-Komponenten ohne wirksame Definition.")
        return 1
    print("Alpine-Komponenten: alle definiert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
