# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einblendungsprofil: wo in der Einblendung der Kommune was steht (Issue #915).

Ein Profil ist Datenbankinhalt (JSON an der Übertragungsquelle), kein Code. Neue Kommunen brauchen damit nur ein neues
Profil. Aufbau (Version 1)::

    {
      "version": 1,
      "balken": {"box": [0.6, 0.83, 0.98, 0.95], "farbregeln": [["b", "r", 25], ["g", "r", 10]], "mindestanteil": 0.35},
      "felder": {
        "top": {"box": [0.36, 0.87, 0.545, 0.94], "psm": 7, "vergroesserung": 3, "schwelle": 175},
        "name": {...}, "fraktion": {...}, "titel": {..., "zeilen": 3}
      },
      "top_muster": "<regulärer Ausdruck mit genau einer Gruppe für die TOP-Nummer>",
      "titel_zur_nummer": 0.6,
      "titel_allein": 0.75,
      "name_mindestbuchstaben": 4,
      "funktionen": ["Oberbürgermeister", ...],
      "fraktionen": ["Fraktion A", ...],
      "bestaetigungen": 2
    }

- **Boxen** sind relativ zur Bildgröße: links, oben, rechts, unten, jeweils 0 bis 1. Damit gilt ein Profil für
  jede Auflösung desselben Layouts.
- **Balken:** Die Einblendung gilt als sichtbar, wenn in ``box`` mindestens ``mindestanteil`` der Bildpunkte jede
  Farbregel erfüllt. Eine Regel ``[a, b, d]`` heißt: Kanal ``a`` liegt um mehr als ``d`` über Kanal ``b`` (Kanäle
  ``r``, ``g``, ``b``). Ohne Balken wird nichts gelesen.
- **Felder:** nur ``top``, ``name``, ``fraktion`` und ``titel``. Uhren und Redezeiten gibt es als Feld bewusst
  nicht. Je Feld: ``psm`` (Seitenaufteilung für Tesseract), ``vergroesserung`` (ganzzahlig), ``schwelle``
  (Graustufe, ab der ein Bildpunkt als Schrift gilt), ``helle_schrift`` (Standard: helle Schrift auf dunklem
  Balken) und ``zeichen`` (erlaubte Zeichen für Tesseract, ``tessedit_char_whitelist``; leer = alle).
- **TOP-Feld nur mit Ziffern, Punkt und „TOP“:** Ohne Angabe ``zeichen`` liest das Feld ``top`` nur
  ``TOP_ZEICHEN``, solange das Profil das Standardmuster ``top_muster`` nutzt. Sonst verliert die Texterkennung
  Punkte („TOP 1.1“ → „11“) oder liest Buchstaben als Ziffern. Ein eigenes Muster (andere Beschriftung) liest
  ohne Einschränkung; ``"zeichen": ""`` schaltet sie ab. Die Vorgabe steht bewusst nicht in den Vorlagen: Ein
  älteres Image kennt die Angabe nicht und lehnte ein Profil mit ihr ab (Rückfall ohne Profiländerung).
- **Funktionsbezeichnungen** (Oberbürgermeisterin, Beigeordneter, Stadtkämmerin, Verwaltung …) stehen in der
  Einblendung oft dort, wo sonst die Fraktion steht. Sie gelten als Funktion, nicht als Fraktion. Verglichen wird
  unscharf und ohne Groß-/Kleinschreibung (``lesung.ist_funktion``), „Oberburgermeister“ ist also auch eine Funktion.
- **Fraktionen** (optional): bekannte Bezeichnungen der Kommune. Gelesene Fraktionen werden auf sie abgebildet
  (Umlautfehler, abgeschnittene Bezeichnungen, ``hub.live.bezeichnungen``); zusätzlich zählen die bisher gelesenen.
- **Bestätigungen:** Ein Wechsel von TOP oder Person gilt erst, wenn er so oft hintereinander gleich gelesen wurde.
- **Titelprüfung** (``titel_zur_nummer``, ``titel_allein``, je 0,3 bis 1): Schwellen der Zuordnung gelesener TOPs
  zu Tagesordnungspunkten (``hub.live.zuordnung.top_zuordnen``). Ab ``titel_zur_nummer`` bestätigt der Titel eine
  Lesart der Nummer („11“ als 1.1), ab ``titel_allein`` entscheidet der Titel auch ohne passende Nummer.

``VORLAGEN`` liefert eine allgemeine Vorlage („Balken unten, dreizeilig“) mit den am Prototyp geprüften Werten.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: Aktuelle Version des Profilformats
VERSION: Final = 1
#: Felder, die ein Profil lesen darf; Uhren und Redezeiten gehören bewusst nicht dazu
FELDER: Final = ("top", "name", "fraktion", "titel")
#: Pflichtfelder: ohne TOP-Nummer und Name gibt es nichts zu entprellen
PFLICHTFELDER: Final = ("top", "name")
KANAELE: Final = ("r", "g", "b")

#: Funktionsbezeichnungen, die nicht als Fraktion gelten (Vorlage; je Profil anpassbar)
STANDARD_FUNKTIONEN: Final[tuple[str, ...]] = (
    "Oberbürgermeister",
    "Oberbürgermeisterin",
    "Bürgermeister",
    "Bürgermeisterin",
    "Stellv. Bürgermeister",
    "Stellv. Bürgermeisterin",
    "Stadtdirektor",
    "Stadtdirektorin",
    "Erster Beigeordneter",
    "Erste Beigeordnete",
    "Beigeordneter",
    "Beigeordnete",
    "Stadtrat",
    "Stadträtin",
    "Stadtkämmerer",
    "Stadtkämmerin",
    "Kämmerer",
    "Kämmerin",
    "Dezernent",
    "Dezernentin",
    "Stadtbaurat",
    "Stadtbaurätin",
    "Landrat",
    "Landrätin",
    "Kreisdirektor",
    "Kreisdirektorin",
    "Bezirksbürgermeister",
    "Bezirksbürgermeisterin",
    "Verwaltung",
)

_TOP_MUSTER: Final = r"\bT\s*[O0]\s*P\s*[:.]?\s*(\d{1,3}(?:\s*\.\s*\d{1,2})?)"
#: Erlaubte Zeichen des TOP-Felds beim Standardmuster: Ziffern, Punkt und „TOP“ (Punkte bleiben erhalten)
TOP_ZEICHEN: Final = "0123456789.TOP"
#: Erlaubte Zeichen einer Angabe ``zeichen`` (geht als ein Argument an Tesseract)
_ZEICHEN: Final = re.compile(r"[0-9A-Za-zÄÖÜäöüß.,:;()/\-]{1,100}")
#: Ab dieser Titelähnlichkeit bestätigt der gelesene Titel eine Lesart der Nummer (z. B. „11“ als 1.1)
TITEL_ZUR_NUMMER: Final = 0.6
#: Ab dieser Titelähnlichkeit entscheidet der Titel allein, auch wenn keine Lesart der Nummer passt
TITEL_ALLEIN: Final = 0.75

#: Allgemeine Vorlagen; die Werte von „balken_unten_dreizeilig“ sind an echten Einblendungen geprüft (Issue #915).
#: Titel ab 0.545 (vorher 0.555: der erste Buchstabe fehlte); das TOP-Feld endet dort und ragt nicht hinein.
VORLAGEN: Final[Mapping[str, Mapping[str, Any]]] = {
    "balken_unten_dreizeilig": {
        "version": VERSION,
        "beschreibung": "Balken im unteren Bilddrittel: links Name und Fraktion, Mitte TOP-Nummer, rechts TOP-Titel",
        "balken": {
            "box": [0.6, 0.83, 0.98, 0.95],
            "farbregeln": [["b", "r", 25], ["g", "r", 10]],
            "mindestanteil": 0.35,
        },
        "felder": {
            "top": {"box": [0.36, 0.87, 0.545, 0.94], "psm": 7, "vergroesserung": 3, "schwelle": 175},
            "name": {"box": [0.05, 0.825, 0.42, 0.878], "psm": 7, "vergroesserung": 3, "schwelle": 175},
            # bis 0.36: lange Bezeichnungen wurden bei 0.33 abgeschnitten; „TOP“ beginnt rechts davon (geprüft)
            "fraktion": {"box": [0.05, 0.87, 0.36, 0.925], "psm": 7, "vergroesserung": 3, "schwelle": 175},
            "titel": {"box": [0.545, 0.82, 1.0, 0.955], "psm": 6, "vergroesserung": 2, "schwelle": 175, "zeilen": 3},
        },
        "top_muster": _TOP_MUSTER,
        "titel_zur_nummer": TITEL_ZUR_NUMMER,
        "titel_allein": TITEL_ALLEIN,
        "name_mindestbuchstaben": 4,
        "funktionen": list(STANDARD_FUNKTIONEN),
        "fraktionen": [],
        "bestaetigungen": 2,
    },
}


class ProfilError(ValueError):
    """Ein Profil ist ungültig; ``probleme`` nennt Stelle und Regel."""

    def __init__(self, probleme: Sequence[str]) -> None:
        super().__init__("; ".join(probleme))
        self.probleme = list(probleme)


@dataclass(frozen=True)
class Box:
    """Relativer Bildausschnitt (0 bis 1)."""

    links: float
    oben: float
    rechts: float
    unten: float

    def pixel(self, breite: int, hoehe: int) -> tuple[int, int, int, int]:
        """Ausschnitt in Bildpunkten für ein Bild der Größe ``breite`` × ``hoehe``."""
        return (
            int(breite * self.links),
            int(hoehe * self.oben),
            int(breite * self.rechts),
            int(hoehe * self.unten),
        )


@dataclass(frozen=True)
class Farbregel:
    """Kanal ``kanal`` liegt um mehr als ``abstand`` über Kanal ``bezug``."""

    kanal: str
    bezug: str
    abstand: int


@dataclass(frozen=True)
class Balken:
    box: Box
    farbregeln: tuple[Farbregel, ...]
    mindestanteil: float


@dataclass(frozen=True)
class Feld:
    box: Box
    psm: int
    vergroesserung: int
    schwelle: int
    helle_schrift: bool = True
    zeilen: int = 1
    #: Erlaubte Zeichen für Tesseract (leer = alle)
    zeichen: str = ""


@dataclass(frozen=True)
class Einblendungsprofil:
    """Geprüftes Profil (``lade_profil``)."""

    balken: Balken
    felder: Mapping[str, Feld]
    top_muster: re.Pattern[str]
    name_mindestbuchstaben: int
    funktionen: tuple[str, ...]
    bestaetigungen: int
    fraktionen: tuple[str, ...] = ()
    titel_zur_nummer: float = TITEL_ZUR_NUMMER
    titel_allein: float = TITEL_ALLEIN


def vorlage(name: str) -> dict[str, Any]:
    """Kopie einer Vorlage (``KeyError`` bei unbekanntem Namen)."""
    return copy.deepcopy(dict(VORLAGEN[name]))


def _zahl(wert: object) -> float | None:
    if isinstance(wert, bool) or not isinstance(wert, int | float):
        return None
    return float(wert)


def _ganzzahl(wert: object, unten: int, oben: int) -> int | None:
    if isinstance(wert, bool) or not isinstance(wert, int) or not unten <= wert <= oben:
        return None
    return wert


def _box(wert: object, stelle: str, probleme: list[str]) -> Box | None:
    if not isinstance(wert, list | tuple) or len(wert) != 4:
        probleme.append(f"{stelle}: vier Zahlen erwartet (links, oben, rechts, unten)")
        return None
    zahlen = [_zahl(teil) for teil in wert]
    if any(z is None or not 0.0 <= z <= 1.0 for z in zahlen):
        probleme.append(f"{stelle}: Werte zwischen 0 und 1 erwartet")
        return None
    links, oben, rechts, unten = (float(z or 0.0) for z in zahlen)
    if links >= rechts or oben >= unten:
        probleme.append(f"{stelle}: links < rechts und oben < unten erwartet")
        return None
    return Box(links, oben, rechts, unten)


def _balken(wert: object, probleme: list[str]) -> Balken | None:
    if not isinstance(wert, Mapping):
        probleme.append("balken: Objekt erwartet")
        return None
    box = _box(wert.get("box"), "balken.box", probleme)
    regeln: list[Farbregel] = []
    roh = wert.get("farbregeln")
    if not isinstance(roh, list) or not roh or len(roh) > 6:
        probleme.append("balken.farbregeln: Liste mit 1 bis 6 Regeln [kanal, bezug, abstand] erwartet")
    else:
        for nummer, regel in enumerate(roh):
            if (
                isinstance(regel, list | tuple)
                and len(regel) == 3
                and regel[0] in KANAELE
                and regel[1] in KANAELE
                and regel[0] != regel[1]
                and _ganzzahl(regel[2], -255, 255) is not None
            ):
                regeln.append(Farbregel(str(regel[0]), str(regel[1]), int(regel[2])))
            else:
                probleme.append(f"balken.farbregeln[{nummer}]: [kanal, bezug, abstand] mit Kanälen r, g, b erwartet")
    anteil = _zahl(wert.get("mindestanteil"))
    if anteil is None or not 0.0 < anteil <= 1.0:
        probleme.append("balken.mindestanteil: Zahl über 0 bis 1 erwartet")
    if box is None or anteil is None or len(regeln) != len(roh or []):
        return None
    return Balken(box=box, farbregeln=tuple(regeln), mindestanteil=anteil)


def _feld(name: str, wert: object, probleme: list[str], standard_zeichen: str = "") -> Feld | None:
    stelle = f"felder.{name}"
    if not isinstance(wert, Mapping):
        probleme.append(f"{stelle}: Objekt erwartet")
        return None
    unbekannt = set(wert) - {"box", "psm", "vergroesserung", "schwelle", "helle_schrift", "zeilen", "zeichen"}
    if unbekannt:
        probleme.append(f"{stelle}: unbekannte Angaben {sorted(unbekannt)}")
    box = _box(wert.get("box"), f"{stelle}.box", probleme)
    psm = _ganzzahl(wert.get("psm", 7), 3, 13)
    if psm is None:
        probleme.append(f"{stelle}.psm: ganze Zahl von 3 bis 13 erwartet")
    faktor = _ganzzahl(wert.get("vergroesserung", 3), 1, 6)
    if faktor is None:
        probleme.append(f"{stelle}.vergroesserung: ganze Zahl von 1 bis 6 erwartet")
    schwelle = _ganzzahl(wert.get("schwelle", 175), 1, 254)
    if schwelle is None:
        probleme.append(f"{stelle}.schwelle: ganze Zahl von 1 bis 254 erwartet")
    zeilen = _ganzzahl(wert.get("zeilen", 1), 1, 6)
    if zeilen is None:
        probleme.append(f"{stelle}.zeilen: ganze Zahl von 1 bis 6 erwartet")
    hell = wert.get("helle_schrift", True)
    if not isinstance(hell, bool):
        probleme.append(f"{stelle}.helle_schrift: true oder false erwartet")
    zeichen = wert.get("zeichen", standard_zeichen)
    if not isinstance(zeichen, str) or (zeichen and not _ZEICHEN.fullmatch(zeichen)):
        probleme.append(f"{stelle}.zeichen: bis zu 100 Buchstaben, Ziffern oder Satzzeichen (.,:;()/-) erwartet")
        zeichen = None
    if (
        box is None
        or psm is None
        or faktor is None
        or schwelle is None
        or zeilen is None
        or not isinstance(hell, bool)
        or zeichen is None
    ):
        return None
    return Feld(
        box=box,
        psm=psm,
        vergroesserung=faktor,
        schwelle=schwelle,
        helle_schrift=hell,
        zeilen=zeilen,
        zeichen=zeichen,
    )


def _schwelle(daten: Mapping[str, Any], name: str, standard: float, probleme: list[str]) -> float:
    wert = _zahl(daten.get(name, standard))
    if wert is None or not 0.3 <= wert <= 1.0:
        probleme.append(f"{name}: Zahl von 0,3 bis 1 erwartet")
        return standard
    return wert


def pruefe_profil(daten: object) -> list[str]:
    """Probleme eines Profils als feste Texte (leer = gültig)."""
    try:
        lade_profil(daten)
    except ProfilError as fehler:
        return fehler.probleme
    return []


def lade_profil(daten: object) -> Einblendungsprofil:
    """Prüft ein Profil und gibt es als ``Einblendungsprofil`` zurück; wirft ``ProfilError``."""
    probleme: list[str] = []
    if not isinstance(daten, Mapping):
        raise ProfilError(["Profil: Objekt erwartet"])
    if daten.get("version") != VERSION:
        probleme.append(f"version: {VERSION} erwartet")
    balken = _balken(daten.get("balken"), probleme)

    roh_muster = daten.get("top_muster", _TOP_MUSTER)
    felder: dict[str, Feld] = {}
    roh_felder = daten.get("felder")
    if not isinstance(roh_felder, Mapping):
        probleme.append("felder: Objekt erwartet")
    else:
        unbekannt = set(roh_felder) - set(FELDER)
        if unbekannt:
            probleme.append(f"felder: nur {', '.join(FELDER)} erlaubt, nicht {sorted(unbekannt)}")
        for name in FELDER:
            if name not in roh_felder:
                if name in PFLICHTFELDER:
                    probleme.append(f"felder.{name}: fehlt")
                continue
            # Standardmuster: TOP-Feld nur mit Ziffern, Punkt und „TOP“ (siehe Moduldokumentation)
            standard_zeichen = TOP_ZEICHEN if name == "top" and roh_muster == _TOP_MUSTER else ""
            feld = _feld(name, roh_felder[name], probleme, standard_zeichen)
            if feld is not None:
                felder[name] = feld

    muster: re.Pattern[str] | None = None
    if not isinstance(roh_muster, str) or not 0 < len(roh_muster) <= 300:
        probleme.append("top_muster: regulärer Ausdruck mit höchstens 300 Zeichen erwartet")
    else:
        try:
            muster = re.compile(roh_muster, re.IGNORECASE)
        except re.error:
            probleme.append("top_muster: kein gültiger regulärer Ausdruck")
        else:
            if muster.groups != 1:
                muster = None
                probleme.append("top_muster: genau eine Gruppe (die TOP-Nummer) erwartet")

    mindestbuchstaben = _ganzzahl(daten.get("name_mindestbuchstaben", 4), 1, 20)
    if mindestbuchstaben is None:
        probleme.append("name_mindestbuchstaben: ganze Zahl von 1 bis 20 erwartet")
    funktionen = daten.get("funktionen", list(STANDARD_FUNKTIONEN))
    if (
        not isinstance(funktionen, list)
        or len(funktionen) > 100
        or not all(isinstance(f, str) and 0 < len(f.strip()) <= 80 for f in funktionen)
    ):
        probleme.append("funktionen: Liste mit höchstens 100 Bezeichnungen (je 1 bis 80 Zeichen) erwartet")
        funktionen = []
    fraktionen = daten.get("fraktionen", [])
    if (
        not isinstance(fraktionen, list)
        or len(fraktionen) > 100
        or not all(isinstance(f, str) and 0 < len(f.strip()) <= 200 for f in fraktionen)
    ):
        probleme.append("fraktionen: Liste mit höchstens 100 Bezeichnungen (je 1 bis 200 Zeichen) erwartet")
        fraktionen = []
    bestaetigungen = _ganzzahl(daten.get("bestaetigungen", 2), 1, 5)
    if bestaetigungen is None:
        probleme.append("bestaetigungen: ganze Zahl von 1 bis 5 erwartet")
    zur_nummer = _schwelle(daten, "titel_zur_nummer", TITEL_ZUR_NUMMER, probleme)
    allein = _schwelle(daten, "titel_allein", TITEL_ALLEIN, probleme)
    if allein < zur_nummer:
        probleme.append("titel_allein: mindestens so groß wie titel_zur_nummer erwartet")

    if probleme or balken is None or muster is None or mindestbuchstaben is None or bestaetigungen is None:
        raise ProfilError(probleme or ["Profil unvollständig"])
    return Einblendungsprofil(
        balken=balken,
        felder=felder,
        top_muster=muster,
        name_mindestbuchstaben=mindestbuchstaben,
        funktionen=tuple(f.strip() for f in funktionen),
        bestaetigungen=bestaetigungen,
        fraktionen=tuple(f.strip() for f in fraktionen),
        titel_zur_nummer=zur_nummer,
        titel_allein=allein,
    )
