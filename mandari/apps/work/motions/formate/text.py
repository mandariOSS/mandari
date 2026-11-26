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
from collections.abc import Callable

from .modell import Absatz, Bild, Block, Lauf, Seitenumbruch, Tabelle, Trennlinie, Zelle

_MD_ZEICHEN = re.compile(r"([\\`*_\[\]#|<>])")
#: Am Zeilenanfang würde Markdown aus „1. Januar“ oder „- Punkt“ eine Liste machen, aus „---“ eine Linie
_MD_LISTENMARKE = re.compile(r"^(\s*)(\d{1,9})([.)])(?=\s|$)|^(\s*)([-+])(?=\s|$)|^(\s*)([-=])(?=[-=\s]*$)", re.M)
#: Senkrechter Strich ohne vorangestellten Rückstrich (in Codestellen einer Tabellenzelle)
_MD_STRICH = re.compile(r"(?<!\\)\|")


def _md_text(text: str) -> str:
    return _MD_ZEICHEN.sub(r"\\\1", text)


def _md_marken(text: str) -> str:
    """Listenmarken und Linien am Zeilenanfang schützen, damit gewöhnlicher Text Text bleibt."""

    def schuetzen(t: re.Match[str]) -> str:
        if t.group(2):
            return f"{t.group(1)}{t.group(2)}\\{t.group(3)}"
        if t.group(5):
            return f"{t.group(4)}\\{t.group(5)}"
        return f"{t.group(6)}\\{t.group(7)}"

    return _MD_LISTENMARKE.sub(schuetzen, text)


def _md_ziel(url: str) -> str:
    """Linkziel; mit Klammern oder Leerzeichen in spitzen Klammern, sonst schlösse „)“ das Ziel vorzeitig."""
    url = url.replace("<", "%3C").replace(">", "%3E")
    return f"<{url}>" if any(zeichen in url for zeichen in " ()") else url


def _md_bild(bild: Bild) -> str:
    return f"*[Bild{': ' + _md_text(bild.alt) if bild.alt else ''}]*"


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
            inhalt = f"[{inhalt}]({_md_ziel(f.link)})"
        teile.append(vorne + inhalt + hinten)
    return "".join(teile)


def _md_absatz(a: Absatz) -> str:
    inhalt = _md_laeufe(a.laeufe)
    if a.art in ("h1", "h2", "h3"):
        return f"{'#' * int(a.art[1])} {inhalt}"
    if a.art == "code":
        return f"```\n{a.text}\n```"
    inhalt = _md_marken(inhalt)
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


def _md_zelle(zelle: Zelle) -> str:
    teile = [
        _md_bild(block) if isinstance(block, Bild) else _md_laeufe(block.laeufe).replace("  \n", " ")
        for block in zelle.bloecke
    ]
    # Striche aus Text sind schon geschützt, die in Codestellen noch nicht
    return _MD_STRICH.sub(r"\\|", " ".join(teile).replace("\n", " "))


def _felder(t: Tabelle, zelle_text: Callable[[Zelle], str]) -> list[list[str]]:
    """Je Zeile die Felder des Rasters: Text am Platz der Zelle, überspannte Felder bleiben leer."""
    spalten, plaetze = t.raster()
    zeilen = []
    for reihe in plaetze:
        felder = [""] * spalten
        for platz in reihe:
            felder[platz.spalte] = zelle_text(platz.zelle)
        zeilen.append(felder)
    return zeilen


def _md_tabelle(t: Tabelle) -> str:
    spalten = t.spaltenzahl
    if not spalten:
        return ""
    zeilen = ["| " + " | ".join(felder) + " |" for felder in _felder(t, _md_zelle)]
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
            teile.append(_md_bild(block))
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


def _txt_zelle(zelle: Zelle) -> str:
    return " ".join(
        (f"[Bild{': ' + block.alt if block.alt else ''}]" if isinstance(block, Bild) else block.text.replace("\n", " "))
        for block in zelle.bloecke
    )


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
            teile.extend("\t".join(felder) for felder in _felder(block, _txt_zelle))
        elif isinstance(block, Bild):
            teile.append(f"[Bild{': ' + block.alt if block.alt else ''}]")
        elif isinstance(block, Trennlinie):
            teile.append("-" * 40)
        elif isinstance(block, Seitenumbruch):
            teile.append("")
    if signatur:
        teile += ["", signatur]
    return "\n".join(teile).strip() + "\n"
