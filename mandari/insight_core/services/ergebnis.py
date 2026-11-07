# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergebnis eines Tagesordnungspunkts in einem festen Vokabular (Issue #1260).

Die Ratsinformationssysteme schreiben Ergebnisse frei: „einstimmig beschlossen“, „Beschluss gemäß Vorlage“,
„Kenntnisnahme“, „Mit sechzehn Zustimmungen und einer Enthaltung wird folgender Beschluss gefasst:“. Das Bürgerportal
zeigt sie als kleines Etikett (``c-insight.ergebnis``) mit einem von sieben Begriffen und einer kurzen Erklärung:

    beschlossen, abgelehnt, zur Kenntnis genommen, vertagt, verwiesen, abgesetzt, angekündigt

Die Zuordnung steht nur hier. Was sich nicht sicher zuordnen lässt, bleibt neutral: etwa „zurückgezogen“, „erledigt“
oder ein Text, der zugleich „beschlossen“ und „abgelehnt“ enthält. Das Etikett zeigt dann den Rohtext ohne Farbe.
„angekündigt“ kommt nie aus dem Rohtext: Es steht für eine angesetzte Beratung, zu der es noch kein Ergebnis gibt.

Vorrang bei mehreren Treffern: abgesetzt, vertagt, verwiesen, dann die Entscheidung (beschlossen oder abgelehnt), zuletzt
die Kenntnisnahme. „Vertagung beschlossen“ ist also vertagt, „Absetzung beschlossen“ abgesetzt, „beschlossen; Kenntnis der
Stellungnahme“ ein Beschluss. Ein abgelehnter Antrag zum Verfahren („Verweisung abgelehnt“, „Antrag auf Absetzung
abgelehnt“) oder ein verneintes Verfahrensergebnis („nicht vertagt“) sagt nichts über die Sache selbst und bleibt neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

BESCHLOSSEN: Final = "beschlossen"
ABGELEHNT: Final = "abgelehnt"
KENNTNIS: Final = "kenntnis"
VERTAGT: Final = "vertagt"
VERWIESEN: Final = "verwiesen"
ABGESETZT: Final = "abgesetzt"
ANGEKUENDIGT: Final = "angekuendigt"
#: Kein Begriff des Vokabulars: das Etikett zeigt den Rohtext neutral
NEUTRAL: Final = ""

#: Begriff und Erklärung je Schlüssel, in der Reihenfolge des Vokabulars
VOKABULAR: Final[dict[str, tuple[str, str]]] = {
    BESCHLOSSEN: (
        "beschlossen",
        "Das Gremium hat zugestimmt, gegebenenfalls mit Änderungen. Den genauen Wortlaut enthält die Niederschrift.",
    ),
    ABGELEHNT: ("abgelehnt", "Das Gremium hat den Vorschlag abgelehnt."),
    KENNTNIS: (
        "zur Kenntnis genommen",
        "Das Gremium hat einen Bericht, eine Mitteilung oder eine Antwort erhalten, ohne darüber abzustimmen.",
    ),
    VERTAGT: ("vertagt", "Die Beratung wurde auf eine spätere Sitzung verschoben."),
    VERWIESEN: ("verwiesen", "Die Sache wurde zur Beratung an ein anderes Gremium gegeben."),
    ABGESETZT: ("abgesetzt", "Der Punkt wurde von der Tagesordnung genommen und nicht beraten."),
    ANGEKUENDIGT: ("angekündigt", "Der Termin steht fest, ein Ergebnis gibt es noch nicht."),
}

#: Längster Rohtext auf einem neutralen Etikett (Zeichen); der ganze Text steht in der Erklärung
_ETIKETT_MAX: Final = 40
#: Längster Rohtext in der Erklärung (Zeichen)
_ERKLAERUNG_MAX: Final = 200
#: „verschoben“ zählt nur in kurzen Ergebnissen („auf die nächste Sitzung verschoben“), nicht in Beschlussinhalten
_VERSCHOBEN_MAX_WOERTER: Final = 6

_ABGESETZT: Final = re.compile(r"\babgesetzt\b|\babsetzung\b|\bvon der tagesordnung (genommen|abgesetzt|gestrichen)\b")
_VERTAGT: Final = re.compile(r"\bvertagt\b|\bzurückgestellt\b|\bnicht behandelt\b|\bvertagung beschlossen\b")
_VERWIESEN: Final = re.compile(r"\b(verwiesen|überwiesen|zurückverwiesen|verweisung|überweisung)\b")
#: Antrag zum Verfahren (auch „Verweisungsantrag“); wird er abgelehnt, ist über die Sache nichts gesagt
_VERFAHREN: Final = re.compile(r"\b(absetzung|vertagung|verweisung|überweisung|zurückverweisung|rückverweisung)")
#: Verneintes Verfahrensergebnis („nicht vertagt“, „nicht verwiesen“): ebenfalls nichts über die Sache
_VERFAHREN_VERNEINT: Final = re.compile(
    r"\b(nicht|keine?)\s+(abgesetzt|vertagt|verwiesen|überwiesen|zurückverwiesen|zurückgestellt)\b"
)
#: Verneinte Zustimmung; sie zählt als Ablehnung und nicht als Beschluss
_VERNEINT: Final = re.compile(
    r"\b(nicht|keine?)\s+(beschlossen|zugestimmt|angenommen|empfohlen|genehmigt|bestätigt|zustimmung|mehrheit)\b"
)
_ABGELEHNT: Final = re.compile(r"\babgelehnt\b|\bablehnung\b|\bverworfen\b")
_BESCHLOSSEN: Final = re.compile(
    r"\bbeschlossen\b|\bbeschluss (gefasst|gemäß|gemäss|wie|nach|laut)\b|\bangenommen\b|\bzugestimmt\b|\bzustimmung\b"
    r"|\bgenehmigt\b|\bempfohlen\b|\bgewählt\b|\bbestätigt\b|\bwie vorgeschlagen\b|\bgemä(ß|ss) (beschluss)?vorschlag\b"
    r"|\bgemä(ß|ss) vorlage\b"
)
_KENNTNIS: Final = re.compile(r"kenntnis|\bbeantwortet\b")
_VERSCHOBEN: Final = re.compile(r"\bverschoben\b")
#: „einstimmig“ oder „mehrheitlich“ ohne Partizip (manche Systeme lassen es weg, etwa „einstimmig bei einer
#: Enthaltung“) gilt als Beschluss, wenn nichts anderes zutrifft und nichts auf ein offenes Ende deutet
_NUR_MEHRHEIT: Final = re.compile(r"^(einstimmig|mehrheitlich|einvernehmlich)\b")
_OFFEN: Final = re.compile(r"\b(zurückgezogen|erledigt|ohne beschluss|nicht abgestimmt)\b")


@dataclass(frozen=True)
class Ergebnis:
    """Ergebnis für das Etikett: Schlüssel des Vokabulars (leer bei neutral), Begriff, Erklärung und Rohtext."""

    schluessel: str
    text: str
    erklaerung: str
    roh: str = ""

    @property
    def bekannt(self) -> bool:
        """Gehört das Ergebnis zum Vokabular (sonst neutral)?"""
        return bool(self.schluessel)

    @property
    def roh_abweichend(self) -> str:
        """Rohtext, wenn er mehr sagt als das Etikett („einstimmig beschlossen“ neben „beschlossen“), sonst leer."""
        if not self.roh or self.roh.casefold() == self.text.casefold():
            return ""
        return self.roh

    @property
    def titel(self) -> str:
        """Kurze Erklärung für ``title``; weicht der Rohtext ab, steht er dahinter."""
        if not self.bekannt or not self.roh_abweichend:
            return self.erklaerung
        return f"{self.erklaerung} Im Ratsinformationssystem: „{_kuerzen(self.roh, _ERKLAERUNG_MAX)}“"


def _bereinigen(roh: object) -> str:
    return " ".join(str(roh or "").split()).strip(" .:;,")


def _kuerzen(text: str, grenze: int) -> str:
    if len(text) <= grenze:
        return text
    schnitt = text[: grenze - 1]
    if " " in schnitt:
        schnitt = schnitt[: schnitt.rindex(" ")]
    return schnitt.rstrip(" ,;:-–") + "…"


def einordnen(roh: object) -> str:
    """
    Schlüssel des Vokabulars für den Ergebnistext des Ratsinformationssystems, ``NEUTRAL`` (leer), wenn er sich nicht
    sicher zuordnen lässt. ``ANGEKUENDIGT`` kommt hier nie heraus.
    """
    # lower statt casefold: casefold macht aus „ß“ ein „ss“
    text = _bereinigen(roh).lower()
    if not text:
        return NEUTRAL
    abgelehnt = bool(_ABGELEHNT.search(text) or _VERNEINT.search(text))
    if (abgelehnt and _VERFAHREN.search(text)) or _VERFAHREN_VERNEINT.search(text):
        # „Verweisung abgelehnt“ ist das Gegenteil von verwiesen, und was mit der Sache geschah, steht nicht da
        return NEUTRAL
    if _ABGESETZT.search(text):
        return ABGESETZT
    worte = len(text.split())
    if _VERTAGT.search(text) or (worte <= _VERSCHOBEN_MAX_WOERTER and _VERSCHOBEN.search(text)):
        return VERTAGT
    if _VERWIESEN.search(text):
        return VERWIESEN
    beschlossen = bool(_BESCHLOSSEN.search(_VERNEINT.sub(" ", text)))
    if abgelehnt and beschlossen:
        # Widersprüchlich („Änderungsantrag abgelehnt, Vorlage beschlossen“): lieber neutral als falsch
        return NEUTRAL
    if abgelehnt:
        return ABGELEHNT
    if beschlossen:
        return BESCHLOSSEN
    if _KENNTNIS.search(text):
        return KENNTNIS
    if _NUR_MEHRHEIT.match(text) and not _OFFEN.search(text):
        return BESCHLOSSEN
    return NEUTRAL


def ergebnis(roh: object) -> Ergebnis | None:
    """Etikett für einen Ergebnistext; ``None`` ohne Text."""
    bereinigt = _bereinigen(roh)
    if not bereinigt:
        return None
    schluessel = einordnen(bereinigt)
    if schluessel:
        text, erklaerung = VOKABULAR[schluessel]
        return Ergebnis(schluessel, text, erklaerung, bereinigt)
    return Ergebnis(
        NEUTRAL,
        _kuerzen(bereinigt, _ETIKETT_MAX),
        f"Ergebnis laut Ratsinformationssystem: {_kuerzen(bereinigt, _ERKLAERUNG_MAX)}",
        bereinigt,
    )


def angekuendigt(erklaerung: str = "") -> Ergebnis:
    """Etikett „angekündigt“ für eine angesetzte Beratung bzw. Sitzung ohne Ergebnis (eigene Erklärung möglich)."""
    text, standard = VOKABULAR[ANGEKUENDIGT]
    return Ergebnis(ANGEKUENDIGT, text, erklaerung or standard)


#: Stand einer Anfrage (Issue #1273): eigene Etiketten neben dem Vokabular der Beratungsergebnisse. Sie kommen nie
#: aus dem Rohtext eines Tagesordnungspunkts, sondern aus ``vorgang_weg.anfrage_stand``.
BEANTWORTET: Final = "beantwortet"
ANTWORT_OFFEN: Final = "antwort_offen"
ANFRAGE_STAND: Final[dict[str, tuple[str, str]]] = {
    BEANTWORTET: ("beantwortet", "Die Verwaltung hat auf die Anfrage geantwortet."),
    ANTWORT_OFFEN: ("Antwort steht aus", "Eine Antwort der Verwaltung ist noch nicht veröffentlicht."),
}


def anfrage_etikett(schluessel: str, erklaerung: str = "") -> Ergebnis:
    """Etikett „beantwortet“ bzw. „Antwort steht aus“ für eine Anfrage (eigene Erklärung möglich)."""
    text, standard = ANFRAGE_STAND[schluessel]
    return Ergebnis(schluessel, text, erklaerung or standard)
