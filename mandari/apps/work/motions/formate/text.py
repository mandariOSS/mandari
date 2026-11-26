# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Markdown und reiner Text aus dem Dokumentmodell (Issue #1584).

- Markdown (CommonMark mit Tabellen und Aufgabenlisten wie bei GitHub): für Wikis, Websites und KI-Werkzeuge.
  Bilder stehen als Hinweis mit ihrem Alternativtext da, nicht als Datei.
- Text: für Formulare von Ratsinformationssystemen und E-Mails; Überschriften unterstrichen, Listen mit Zeichen,
  Tabellen mit Tabulatoren.
"""

from __future__ import annotations

import re

from .modell import Absatz, Bild, Block, Lauf, Seitenumbruch, Tabelle, Trennlinie

_MD_ZEICHEN = re.compile(r"([\\`*_\[\]#|<>])")


def _md_text(text: str) -> str:
    return _MD_ZEICHEN.sub(r"\\\1", text)


def _md_laeufe(laeufe: list[Lauf]) -> str:
    teile = []
    for lauf in laeufe:
        if lauf.umbruch:
            teile.append("  \n")
            continue
        text = lauf.text
        if not text:
            continue
        f = lauf.format
        kern = text.strip()
        if not kern:
            teile.append(text)
            continue
        vorne = text[: len(text) - len(text.lstrip())]
        hinten = text[len(text.rstrip()) :]
        inhalt = f"`{kern}`" if f.code else _md_text(kern)
        if f.durchgestrichen:
            inhalt = f"~~{inhalt}~~"
        if f.kursiv:
            inhalt = f"*{inhalt}*"
        if f.fett:
            inhalt = f"**{inhalt}**"
        if f.link and f.link.startswith(("http://", "https://", "mailto:", "tel:")):
            inhalt = f"[{inhalt}]({f.link})"
        teile.append(vorne + inhalt + hinten)
    return "".join(teile)


def _md_absatz(a: Absatz) -> str:
    inhalt = _md_laeufe(a.laeufe)
    if a.art in ("h1", "h2", "h3"):
        return f"{'#' * int(a.art[1])} {inhalt}"
    if a.art == "code":
        return f"```\n{a.text}\n```"
    if a.liste:
        art, ebene = a.liste
        einzug = "  " * ebene if art == "punkte" else "   " * ebene
        if a.fortsetzung:
            return f"{einzug}  {inhalt}"
        if art == "zahlen":
            return f"{einzug}{a.nummer or 1}. {inhalt}"
        if art == "aufgaben":
            return f"{einzug}- [{'x' if a.erledigt else ' '}] {inhalt}"
        return f"{einzug}- {inhalt}"
    if a.art == "zitat":
        return "\n".join(f"> {zeile}" for zeile in inhalt.split("\n"))
    return inhalt


def _md_tabelle(t: Tabelle) -> str:
    spalten = t.spaltenzahl
    if not spalten:
        return ""
    zeilen = []
    for zeile in t.zeilen:
        zellen: list[str] = []
        for zelle in zeile:
            text = " ".join(_md_laeufe(a.laeufe).replace("  \n", " ") for a in zelle.absaetze).replace("\n", " ")
            zellen.append(text.replace("|", "\\|"))
            zellen.extend("" for _ in range(zelle.spalten - 1))
        zellen.extend("" for _ in range(spalten - len(zellen)))
        zeilen.append("| " + " | ".join(zellen[:spalten]) + " |")
    trenner = "| " + " | ".join("---" for _ in range(spalten)) + " |"
    return "\n".join([zeilen[0], trenner, *zeilen[1:]])


def markdown_schreiben(bloecke: list[Block], *, titel: str = "", signatur: str = "") -> str:
    teile: list[str] = []
    vorher_liste = False
    for block in bloecke:
        if isinstance(block, Absatz):
            text = _md_absatz(block)
            # Listenpunkte ohne Leerzeile untereinander, sonst Leerzeile zwischen Blöcken
            if teile and not (vorher_liste and block.liste):
                teile.append("")
            teile.append(text)
            vorher_liste = block.liste is not None
            continue
        vorher_liste = False
        if teile:
            teile.append("")
        if isinstance(block, Tabelle):
            teile.append(_md_tabelle(block))
        elif isinstance(block, Bild):
            teile.append(f"*[Bild{': ' + _md_text(block.alt) if block.alt else ''}]*")
        elif isinstance(block, Trennlinie):
            teile.append("---")
        elif isinstance(block, Seitenumbruch):
            teile.append("<!-- Seitenumbruch -->")
    if signatur:
        teile += ["", "  \n".join(_md_text(z) for z in signatur.splitlines())]
    text = "\n".join(teile).strip() + "\n"
    if titel and not text.startswith("# "):
        text = f"# {_md_text(titel)}\n\n{text}"
    return text


def _txt_absatz(a: Absatz) -> str:
    inhalt = a.text
    if a.art in ("h1", "h2"):
        return f"{inhalt}\n{('=' if a.art == 'h1' else '-') * max(3, len(inhalt))}"
    if a.liste:
        art, ebene = a.liste
        einzug = "  " * ebene
        if a.fortsetzung:
            return einzug + "  " + inhalt.replace("\n", "\n" + einzug + "  ")
        zeichen = {"zahlen": f"{a.nummer or 1}.", "aufgaben": "[x]" if a.erledigt else "[ ]"}.get(art, "•")
        return f"{einzug}{zeichen} " + inhalt.replace("\n", "\n" + einzug + " " * (len(zeichen) + 1))
    if a.art == "zitat":
        return "\n".join(f"> {zeile}" for zeile in inhalt.split("\n"))
    return inhalt


def text_schreiben(bloecke: list[Block], *, signatur: str = "") -> str:
    teile: list[str] = []
    vorher_liste = False
    for block in bloecke:
        if isinstance(block, Absatz):
            if teile and not (vorher_liste and block.liste):
                teile.append("")
            teile.append(_txt_absatz(block))
            vorher_liste = block.liste is not None
            continue
        vorher_liste = False
        if teile:
            teile.append("")
        if isinstance(block, Tabelle):
            for zeile in block.zeilen:
                teile.append("\t".join(" ".join(a.text.replace("\n", " ") for a in z.absaetze) for z in zeile))
        elif isinstance(block, Bild):
            teile.append(f"[Bild{': ' + block.alt if block.alt else ''}]")
        elif isinstance(block, Trennlinie):
            teile.append("-" * 40)
        elif isinstance(block, Seitenumbruch):
            teile.append("")
    if signatur:
        teile += ["", signatur]
    return "\n".join(teile).strip() + "\n"
