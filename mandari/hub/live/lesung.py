# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesung eines Einzelbilds nach Einblendungsprofil (Issue #915).

Gelesen werden nur die Felder des Profils: TOP-Nummer, TOP-Titel, Name und Fraktion bzw. Funktion der sprechenden
Person. Uhren und Redezeiten haben kein Feld und werden nie gelesen. Das Bild bleibt im Speicher; der Aufrufer
verwirft es nach der Lesung.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Final

from PIL import Image, ImageOps

from .ocr import Erkenner, tesseract
from .profil import Balken, Einblendungsprofil, Feld

#: Längen, auf die gelesene Texte gekürzt werden (Datenbankfelder)
MAX_NAME: Final = 200
MAX_TITEL: Final = 500
#: Kantenlänge der Probe für die Balkenerkennung (verkleinert, damit die Zählung billig bleibt)
_PROBE: Final = (40, 10)
#: Ab dieser Ähnlichkeit gilt der Anfang einer Zeile als Funktionsbezeichnung („Oberburgermeister“)
AEHNLICH_FUNKTION: Final = 0.85
#: Ein „TOP“ am Ende der Fraktionszeile (ohne Nummer), falls das TOP-Feld hineinragt
_TOP_REST = re.compile(r"\s+T\s?[O0]\s?P(?![a-zäöüß]).*$", re.IGNORECASE)
_ERLAUBT = re.compile(r"[^\wÄÖÜäöüß .,'\-/&()]")
_BUCHSTABEN = re.compile(r"[^A-Za-zÄÖÜäöüßÀ-ÿ]")


@dataclass(frozen=True)
class Lesung:
    """Ergebnis eines Bilds; ``balken`` False heißt: keine Einblendung, nichts gelesen."""

    balken: bool
    top: str | None = None
    titel: str | None = None
    name: str | None = None
    fraktion: str | None = None
    funktion: str | None = None

    def als_dict(self) -> dict[str, Any]:
        return asdict(self)


KEINE_EINBLENDUNG: Final = Lesung(balken=False)


def balken_sichtbar(bild: Image.Image, balken: Balken) -> bool:
    """Erfüllen genug Bildpunkte im Ausschnitt alle Farbregeln?"""
    probe = bild.crop(balken.box.pixel(*bild.size)).convert("RGB").resize(_PROBE)
    roh = probe.tobytes()
    index = {"r": 0, "g": 1, "b": 2}
    treffer = 0
    gesamt = len(roh) // 3
    for start in range(0, len(roh), 3):
        punkt = roh[start : start + 3]
        if all(punkt[index[r.kanal]] - punkt[index[r.bezug]] > r.abstand for r in balken.farbregeln):
            treffer += 1
    return gesamt > 0 and treffer / gesamt >= balken.mindestanteil


def feldbild(bild: Image.Image, feld: Feld) -> Image.Image:
    """Ausschnitt vergrößert, in Graustufen und zweifarbig (Schrift schwarz auf weiß)."""
    ausschnitt = bild.crop(feld.box.pixel(*bild.size))
    faktor = feld.vergroesserung
    groesser = ausschnitt.resize(
        (max(1, ausschnitt.width * faktor), max(1, ausschnitt.height * faktor)), Image.Resampling.LANCZOS
    )
    grau = ImageOps.grayscale(groesser)
    schwelle = feld.schwelle
    if feld.helle_schrift:
        return grau.point(lambda v: 0 if v > schwelle else 255)
    return grau.point(lambda v: 0 if v < schwelle else 255)


def nur_text(zeile: str) -> str | None:
    """Bereinigter Text einer Zeile oder ``None``, wenn kaum Buchstaben darin sind (Rauschen)."""
    sauber = _ERLAUBT.sub("", zeile.strip()).strip(" .,-/")
    sauber = re.sub(r"\s+", " ", sauber)
    return sauber if len(_BUCHSTABEN.sub("", sauber)) >= 2 else None


def buchstaben(text: str) -> int:
    return len(_BUCHSTABEN.sub("", text))


def vereinfacht(text: str) -> str:
    """
    Vergleichsform: klein, Umlaute und Akzente auf den Grundbuchstaben (ü → u, ß → ss), ohne Satzzeichen, einfache
    Leerzeichen. Die Texterkennung verliert Umlaute oft („Sudliste“, „Su�dliste“): Ersatzzeichen (U+FFFD) und
    alleinstehende Umlautpunkte fallen weg, so sind die Lesarten gleich.
    """
    text = text.lower().replace("ß", "ss").replace("�", "").replace("¨", "")
    text = "".join(z for z in unicodedata.normalize("NFKD", text) if not unicodedata.combining(z))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def aehnlichkeit(a: str, b: str) -> float:
    """Ähnlichkeit zweier Vergleichsformen (0 bis 1)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def ist_funktion(text: str, funktionen: tuple[str, ...]) -> bool:
    """
    Beginnt die Zeile mit einer Funktionsbezeichnung? Unscharf und ohne Groß-/Kleinschreibung: Die ersten Wörter der
    Zeile (so viele, wie die Bezeichnung hat) müssen ihr zu mindestens ``AEHNLICH_FUNKTION`` gleichen.
    """
    einfach = vereinfacht(text)
    if not einfach:
        return False
    woerter = einfach.split()
    for funktion in funktionen:
        kurz = vereinfacht(funktion)
        if not kurz:
            continue
        anfang = " ".join(woerter[: len(kurz.split())])
        if anfang == kurz or aehnlichkeit(anfang, kurz) >= AEHNLICH_FUNKTION:
            return True
    return False


def trenne_funktion(text: str | None, funktionen: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(Fraktion, Funktion): Beginnt die Zeile mit einer Funktionsbezeichnung, ist sie keine Fraktion."""
    if not text:
        return None, None
    if ist_funktion(text, funktionen):
        return None, text
    return text, None


def ohne_top(text: str | None, profil: Einblendungsprofil) -> str | None:
    """Schneidet eine mitgelesene TOP-Angabe ab (falls das Fraktionsfeld an das TOP-Feld reicht)."""
    if not text:
        return text
    for muster in (profil.top_muster, _TOP_REST):
        treffer = muster.search(text)
        if treffer:
            text = text[: treffer.start()]
    return nur_text(text)


def top_nummer(text: str, profil: Einblendungsprofil) -> str | None:
    """TOP-Nummer aus dem gelesenen Text (``5``, ``1.1``) oder ``None``."""
    treffer = profil.top_muster.search(text)
    return re.sub(r"\s+", "", treffer.group(1)) if treffer else None


def lies_bild(bild: Image.Image, profil: Einblendungsprofil, erkenner: Erkenner = tesseract) -> Lesung:
    """Liest die Felder des Profils; ohne sichtbaren Balken ``KEINE_EINBLENDUNG``."""
    if not balken_sichtbar(bild, profil.balken):
        return KEINE_EINBLENDUNG
    felder = profil.felder

    top: str | None = None
    if "top" in felder:
        top = top_nummer(erkenner(feldbild(bild, felder["top"]), felder["top"].psm), profil)

    name: str | None = None
    if "name" in felder:
        name = nur_text(erkenner(feldbild(bild, felder["name"]), felder["name"].psm))
        if name and buchstaben(name) < profil.name_mindestbuchstaben:
            name = None

    fraktion: str | None = None
    funktion: str | None = None
    if name and "fraktion" in felder:
        gelesen = ohne_top(nur_text(erkenner(feldbild(bild, felder["fraktion"]), felder["fraktion"].psm)), profil)
        fraktion, funktion = trenne_funktion(gelesen, profil.funktionen)

    titel: str | None = None
    if "titel" in felder:
        feld = felder["titel"]
        zeilen = [z.strip() for z in erkenner(feldbild(bild, feld), feld.psm).splitlines() if z.strip()]
        titel = " ".join(zeilen[: feld.zeilen]) or None

    return Lesung(
        balken=True,
        top=top,
        titel=titel[:MAX_TITEL] if titel else None,
        name=name[:MAX_NAME] if name else None,
        fraktion=fraktion[:MAX_NAME] if fraktion else None,
        funktion=funktion[:MAX_NAME] if funktion else None,
    )
