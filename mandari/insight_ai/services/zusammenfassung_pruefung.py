# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mechanische Prüfung einer KI-Zusammenfassung vor dem Speichern (Issue #1271).

Ein Sprachmodell kann Zahlen verdrehen, Beträge zusammenrechnen, Fraktionen ergänzen, Sätze aus den Dokumenten
abschreiben oder Anschriften nennen. Ob es das getan hat, lässt sich ohne zweiten KI-Aufruf am Text prüfen. Verworfen
wird eine Zusammenfassung, wenn sie

- **leer** ist (``leer``),
- eine **Hausnummer** nennt (``hausnummer``, Datenschutz: Anschriften von Einreichenden gehören nicht in eine
  Kurzfassung),
- mehr als ``UEBERNAHME_WOERTER`` Wörter am Stück **wörtlich übernimmt** (``uebernahme``); Titel, Namen von Gremien und
  der Kommune zählen dabei nicht,
- ein **Datum** nennt, das nicht in der Quelle steht (``datum``; Tag und Monat, mit Jahr auch das Jahr),
- eine **Zahl oder einen Betrag** ab zwei Ziffern nennt, die nicht in der Quelle stehen (``zahl``). Große Beträge
  dürfen gerundet sein: mit „Tausend“, „Millionen“ oder „Milliarden“ höchstens ``RUNDUNG_GROSS`` Abweichung, mit
  „rund“, „etwa“, „knapp“ … höchstens ``RUNDUNG_UNGEFAEHR``. Ausgerechnete Summen fallen damit durch, gewollt,
- eine **Partei oder Fraktion** nennt, die die Quelle nicht nennt (``fraktion``, Liste ``PARTEIEN``).

Quelle ist genau das Material, das das Modell bekam: Metadaten, Beratungsverlauf und Dokumente aus dem Nutzerprompt
(``SummaryService.vorbereiten``). Die Fassung in Einfacher Sprache wird genauso geprüft; fällt nur sie durch, entfällt
nur sie. Verworfene Zusammenfassungen erscheinen nirgends; protokolliert werden sie in
``insight_ai.models.VerworfeneZusammenfassung`` (``protokollieren``).

Die Regeln sind bewusst einfach und erklärbar. Sie fangen keine falschen Aussagen ab, die ohne Zahlen und Namen
auskommen; dafür gibt es die Bewertung einer festen Stichprobe je Prompt-Fassung (``manage.py
zusammenfassung_stichprobe``).
"""

from __future__ import annotations

import bisect
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cached_property
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Höchstens so viele Wörter am Stück dürfen wörtlich aus der Quelle stammen
UEBERNAHME_WOERTER: Final = 10
#: Längere Angaben, die wörtlich stehen dürfen (Titel, Gremien), zählen ab so vielen Wörtern
ERLAUBT_AB_WOERTERN: Final = 3
#: Erlaubte Abweichung eines großen Betrags („1,7 Millionen Euro“ für 1.661.000 Euro)
RUNDUNG_GROSS: Final = Decimal("0.05")
#: Erlaubte Abweichung einer ausdrücklich ungefähren Angabe („rund 20 Jahre“, „etwa 2 Millionen Euro“)
RUNDUNG_UNGEFAEHR: Final = Decimal("0.10")

#: Regeln in der Reihenfolge der Prüfung; Hausnummern zuerst, damit kein Entwurf mit Anschrift aufbewahrt wird
REGELN: Final = ("leer", "hausnummer", "uebernahme", "datum", "zahl", "fraktion")
#: Regel, wenn die Prüfung selbst scheitert: Dann gilt der Text als nicht bestanden (lieber keine als eine ungeprüfte)
FEHLER: Final = "fehler"
#: Bei diesen Regeln wird der Entwurf nicht aufbewahrt (Datenschutz)
OHNE_ENTWURF: Final = frozenset({"hausnummer"})

#: Parteien und Fraktionen, die eine Zusammenfassung nur nennen darf, wenn die Quelle sie nennt: Name → Muster
PARTEIEN: Final[dict[str, str]] = {
    "CDU": r"\bCDU\b",
    "CSU": r"\bCSU\b",
    "SPD": r"\bSPD\b",
    "Grüne": r"\bGrüne|\bGRÜNE|\bGruene|Bündnis\s*90|\bB\s?90\b",
    "FDP": r"\bFDP\b|\bF\.D\.P\.",
    "Linke": r"\bLinke\b|\bLinken\b|\bLINKE\b|\bLinksfraktion",
    "AfD": r"\bAfD\b",
    "Volt": r"\bVolt\b|\bVOLT\b",
    "BSW": r"\bBSW\b|Wagenknecht",
    "Freie Wähler": r"Freien? Wähler|FREIE WÄHLER",
    "ÖDP": r"\bÖDP\b|\bödp\b",
    "Piraten": r"\bPiraten(?:partei)?\b",
    "Die PARTEI": r"\bDie PARTEI\b|\bDIE PARTEI\b",
    "Tierschutzpartei": r"Tierschutzpartei",
    "UWG": r"\bUWG\b",
}
_PARTEIEN: Final = {name: re.compile(muster) for name, muster in PARTEIEN.items()}

_MONATE: Final = {
    "januar": 1,
    "jänner": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_MONATSNAME: Final = "|".join(sorted(_MONATE, key=len, reverse=True))
#: „07.10.2026“, „7.10.26“, „07.10.“
_DATUM_ZIFFERN: Final = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2}(?!\d))?")
#: „7. Oktober 2026“, „7. Oktober“
_DATUM_NAME: Final = re.compile(rf"(?<![\d.,])(\d{{1,2}})\.\s*({_MONATSNAME})\b(?:\s+(\d{{4}}))?", re.IGNORECASE)
#: Doppeljahr „2026/27“ (Haushalts-, Schul- und Wirtschaftsjahre): Jahrhundert und Kurzjahr
_DOPPELJAHR: Final = re.compile(r"(?<!\d)(\d{2})\d{2}/(\d{2})(?!\d)")
#: „2026-10-07“ (Datum im Nutzerprompt)
_DATUM_ISO: Final = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

#: Zahl mit Tausenderpunkten, Dezimalkomma oder Uhrzeit; nicht als Teil eines Wortes („U3“, „CO2“)
_ZAHL: Final = re.compile(r"(?<![\w.,])\d+(?:[.,:]\d+)*")
#: Tausendergruppen mit Leerzeichen („1 661 000“, auch schmale geschützte Leerzeichen)
_ZAHL_LEERZEICHEN: Final = re.compile(r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?![\d])")
#: Größenwort hinter einer Zahl und sein Faktor
_GROESSE: Final = re.compile(
    r"\s*(Mrd\.?|Milliarden?|Mio\.?|Millionen|Million|Tsd\.?|Tausend|TEUR|T€|T\s?Euro)(?![\w])", re.IGNORECASE
)
_FAKTOR: Final = {
    "mrd": 10**9,
    "milliarde": 10**9,
    "milliarden": 10**9,
    "mio": 10**6,
    "million": 10**6,
    "millionen": 10**6,
    "tsd": 10**3,
    "tausend": 10**3,
    "teur": 10**3,
    "t€": 10**3,
    "t euro": 10**3,
    "teuro": 10**3,
}
#: Wörter vor einer ungefähren Angabe
_UNGEFAEHR: Final = re.compile(
    r"(?:\brund|\betwa|\bungefähr|\bcirca|\bca\.|\bknapp|\bfast|\bgut|\büber|\bmehr als|\bweniger als|\bunter"
    r"|\bbis zu|\bnahezu|\bannähernd|\bfrüher als|\bspätestens)\s*$",
    re.IGNORECASE,
)

#: Straßenwörter, hinter denen eine Zahl eine Hausnummer ist („Hauptstraße 12“, „Endenicher Allee 11a“)
_STRASSE: Final = re.compile(
    r"(?:straße|strasse|str\.?|weg|platz|allee|gasse|ring|ufer|damm|pfad|graben|wall|chaussee|promenade|steig|stieg"
    r"|kamp|markt|hof)$",
    re.IGNORECASE,
)
#: Straßenwörter, die allein stehen und erst mit einem Namen davor eine Anschrift sind („Bonner Platz 3“)
_STRASSE_ALLEIN: Final = frozenset(
    {"straße", "strasse", "str.", "weg", "platz", "allee", "gasse", "ring", "ufer", "damm", "pfad", "graben", "wall"}
    | {"chaussee", "promenade", "steig", "stieg", "kamp"}
)
#: Wörter vor einem allein stehenden Straßenwort, die keinen Straßennamen bilden („Auf Platz 3“)
_FUNKTIONSWOERTER: Final = frozenset(
    {"auf", "am", "im", "an", "in", "der", "die", "das", "den", "dem", "des", "zum", "zur", "vom", "beim", "bis"}
    | {"von", "für", "mit", "nach", "über", "unter", "vor", "hinter", "ein", "eine", "einen", "jeder", "jede"}
)
#: Straßen mit Nummer statt Hausnummer („Bundesstraße 51“, „Kreisstraße 12“)
_NUMMERNSTRASSE: Final = re.compile(r"^(?:bundes|landes|kreis|staats|gemeinde|kommunal)", re.IGNORECASE)
#: Wort vor einer Zahl, die Zahl und ggf. ein Buchstabe dahinter; keine Maße, Beträge oder Dezimalzahlen
_WORT_NUMMER: Final = re.compile(
    r"(?<!\S)(?P<wort>[^\s\d]\S*)\s+(?:Nr\.\s*)?(?P<nummer>\d{1,3})(?:\s?[a-zA-Z])?(?![\w])"
    r"(?!\s?(?:%|Prozent|Euro|EUR|€|Cent|Jahre|Jahren|Monate|Wochen|Tage|Meter|m\b|km|Kilometer|Uhr|[.,]\d))",
)
#: „Am Burggraben 3“, „am Markt 2“: Ortsangabe mit Name und Nummer (nicht „zur Vorlage 123/2026“)
_ORTSANGABE: Final = re.compile(
    r"\b(?i:am|im|an der|an den|in der|auf der|auf dem|zum|zur|unter den|hinter der|vor dem)\s+"
    r"(?P<name>[A-ZÄÖÜ][\wäöüß-]+)\s+(?P<nummer>\d{1,3})(?:\s?[a-zA-Z])?(?![\w/])"
    r"(?!\s?(?:%|Prozent|Euro|EUR|€|Jahre|Meter|km|Uhr|[.,]\d))",
)
#: Keine Anschrift: Zeitangaben („am Montag 12 …“) und Verweise („zur Vorlage 123“, „im Ausschuss 3“)
_KEIN_ORT: Final = frozenset(
    {"montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag", "jahr", "tag", "ende", "anfang"}
    | {"vorlage", "drucksache", "anlage", "nummer", "seite", "punkt", "top", "ziffer", "paragraf", "absatz"}
    | {"kapitel", "abschnitt", "teil", "antrag", "anfrage", "beschluss", "sitzung", "ausschuss", "rat", "haushalt"}
    | {"frage", "fragen", "variante", "option", "stufe", "phase", "schritt", "runde", "los", "zone", "ebene", "etage"}
    | {"klasse", "gruppe", "linie", "wahlkreis", "bezirk", "stadtbezirk", "artikel", "tagesordnungspunkt", "quartal"}
    | {"halbjahr", "kalenderwoche", "woche", "jahrgang", "stockwerk", "geschoss", "bauabschnitt", "bebauungsplan"}
    | {"plan", "fall", "modul", "projekt", "maßnahme", "position", "titel", "produkt", "konto", "haushaltsjahr"}
    | set(_MONATE)
)
#: Wortenden von Verweisen und Zählungen („Bebauungsplan 245“, „Entgeltgruppe 9“, „Bauabschnitt 2“)
_KEIN_ORT_ENDE: Final = (
    "plan",
    "plans",
    "gruppe",
    "abschnitt",
    "klasse",
    "stufe",
    "phase",
    "linie",
    "kreis",
    "bezirk",
    "nummer",
    "punkt",
)


@dataclass(frozen=True)
class Befund:
    """Grund, aus dem eine Zusammenfassung verworfen wird."""

    #: Regel aus ``REGELN``
    regel: str
    #: Für Menschen: was genau nicht passt (ohne Hausnummer selbst)
    grund: str

    @property
    def entwurf_aufbewahren(self) -> bool:
        """Darf der verworfene Text im Protokoll stehen? Nicht mit Hausnummer."""
        return self.regel not in OHNE_ENTWURF


def _woerter(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").casefold())


def _wert(zahl: str) -> Decimal | None:
    """Wert einer Zahl in deutscher Schreibweise: „1.661.000“, „4,1“, „18:00“ (als 18.00); sonst ``None``."""
    text = zahl.replace(":", ".")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+(?:,\d+)?", text):
        text = text.replace(",", ".")
    elif not re.fullmatch(r"\d+\.\d+", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _faktor(text: str, ende: int) -> int:
    """Faktor eines Größenworts direkt hinter der Zahl („Millionen“), sonst 1."""
    treffer = _GROESSE.match(text, ende)
    if not treffer:
        return 1
    return _FAKTOR.get(treffer.group(1).casefold().rstrip("."), 1)


def _jahr(text: str | None) -> int | None:
    if not text:
        return None
    jahr = int(text)
    return jahr + 2000 if jahr < 100 else jahr


def _daten(text: str) -> list[tuple[tuple[int, int, int | None], tuple[int, int]]]:
    """Daten im Text als ((Tag, Monat, Jahr oder None), Stelle), nur gültige Tage und Monate."""
    gefunden: list[tuple[tuple[int, int, int | None], tuple[int, int]]] = []
    for treffer in _DATUM_ZIFFERN.finditer(text):
        tag, monat = int(treffer.group(1)), int(treffer.group(2))
        if 1 <= tag <= 31 and 1 <= monat <= 12:
            gefunden.append(((tag, monat, _jahr(treffer.group(3))), treffer.span()))
    for treffer in _DATUM_NAME.finditer(text):
        # Mit IGNORECASE passen auch „Julı“ oder „JULİ“, deren casefold kein Monat der Tabelle ist
        monat_oder_none = _MONATE.get(treffer.group(2).casefold())
        tag = int(treffer.group(1))
        if monat_oder_none is None:
            continue
        monat = monat_oder_none
        if 1 <= tag <= 31:
            gefunden.append(((tag, monat, _jahr(treffer.group(3))), treffer.span()))
    return gefunden


@dataclass
class Quelle:
    """
    Material, gegen das geprüft wird: Text aus Metadaten, Beratungsverlauf und Dokumenten, dazu Angaben, die wörtlich
    stehen dürfen (Titel, Gremien, Kommune). Zählungen entstehen erst bei Bedarf und einmal je Quelle.
    """

    text: str
    erlaubt: Sequence[str] = field(default_factory=tuple)

    @cached_property
    def wortfolgen(self) -> set[tuple[str, ...]]:
        woerter = _woerter(self.text)
        laenge = UEBERNAHME_WOERTER + 1
        return {tuple(woerter[i : i + laenge]) for i in range(len(woerter) - laenge + 1)}

    @cached_property
    def werte(self) -> list[Decimal]:
        """Alle Zahlen der Quelle als Werte, sortiert: wie geschrieben, mit Größenwort und jeder Teil einzeln."""
        werte: set[Decimal] = set()
        for muster in (_ZAHL, _ZAHL_LEERZEICHEN):
            for treffer in muster.finditer(self.text):
                roh = re.sub(r"[   ]", ".", treffer.group(0))
                wert = _wert(roh)
                if wert is not None:
                    werte.add(wert)
                    werte.add(wert * _faktor(self.text, treffer.end()))
                # Teile von Datumsangaben, Aktenzeichen und Uhrzeiten („31.12.2024“ → 31, 12, 2024)
                werte.update(Decimal(teil) for teil in re.split(r"[.,:]", roh) if teil)
        for tag, monat, jahr in self.daten:
            werte.update(Decimal(teil) for teil in (tag, monat, jahr) if teil is not None)
        # Doppeljahre: „2026/27“ nennt auch das Jahr 2027
        for treffer in _DOPPELJAHR.finditer(self.text):
            werte.add(Decimal(int(treffer.group(1)) * 100 + int(treffer.group(2))))
        return sorted(werte)

    @cached_property
    def daten(self) -> set[tuple[int, int, int | None]]:
        """Daten der Quelle als (Tag, Monat, Jahr), aus Ziffern, Monatsnamen und ISO-Schreibweise."""
        daten = {datum for datum, _stelle in _daten(self.text)}
        for treffer in _DATUM_ISO.finditer(self.text):
            jahr, monat, tag = (int(teil) for teil in treffer.groups())
            daten.add((tag, monat, jahr))
        return daten

    @cached_property
    def tage(self) -> set[tuple[int, int]]:
        return {(tag, monat) for tag, monat, _jahr in self.daten}

    def hat_wert(self, wert: Decimal, toleranz: Decimal = Decimal(0)) -> bool:
        werte = self.werte
        if not toleranz:
            stelle = bisect.bisect_left(werte, wert)
            return stelle < len(werte) and werte[stelle] == wert
        unten, oben = wert * (1 - toleranz), wert * (1 + toleranz)
        stelle = bisect.bisect_left(werte, unten)
        return stelle < len(werte) and werte[stelle] <= oben


def hausnummer(text: str) -> bool:
    """Nennt der Text eine Hausnummer („Hauptstraße 12“, „Bonner Platz 3a“, „Am Burggraben 3“)?"""
    text = text or ""
    for treffer in _WORT_NUMMER.finditer(text):
        wort = treffer.group("wort").strip("„“\"'()")
        if wort[-1:] in ",;:":
            # „an der Hauptstraße, 12 Bäume …“: Aufzählung, keine Anschrift
            continue
        klein = wort.casefold()
        if not _STRASSE.search(klein) or _NUMMERNSTRASSE.match(klein):
            continue
        if klein not in _STRASSE_ALLEIN:
            return True
        # „auf Platz 3“ ist keine Anschrift, „Bonner Platz 3“ schon: davor ein Name, kein Artikel oder Verhältniswort
        davor = text[: treffer.start()].split()
        if davor and davor[-1][:1].isupper() and davor[-1].casefold() not in _FUNKTIONSWOERTER:
            return True
    if re.search(r"\bHaus(?:nummer|\s?-?Nr\.?)\s*\d", text, re.IGNORECASE):
        return True
    return any(_ortsname(treffer.group("name")) for treffer in _ORTSANGABE.finditer(text))


def _ortsname(name: str) -> bool:
    """Kann ``name`` in „am/zur/in der … 12“ eine Straße sein? Nicht bei Verweisen, Zählungen und Zeitangaben."""
    klein = name.casefold()
    return klein not in _KEIN_ORT and not klein.endswith(_KEIN_ORT_ENDE)


def uebernahme(text: str, quelle: Quelle) -> str | None:
    """
    Erste Wortfolge von mehr als ``UEBERNAHME_WOERTER`` Wörtern, die wörtlich aus der Quelle stammt; sonst ``None``.

    Gezählt wird die ganze zusammenhängende Übernahme. Wörter, mit denen der Text eine erlaubte Angabe der Quelle
    wiederholt (Titel, Namen von Gremien; ab ``ERLAUBT_AB_WOERTERN`` Wörtern), zählen nicht mit.
    """
    woerter = _woerter(text)
    laenge = UEBERNAHME_WOERTER + 1
    if len(woerter) < laenge:
        return None
    folgen = quelle.wortfolgen
    gedeckt = [False] * len(woerter)
    for start in range(len(woerter) - laenge + 1):
        if tuple(woerter[start : start + laenge]) in folgen:
            gedeckt[start : start + laenge] = [True] * laenge
    if not any(gedeckt):
        return None
    frei: set[int] = set()
    for angabe in quelle.erlaubt:
        teile = _woerter(angabe)
        if len(teile) < ERLAUBT_AB_WOERTERN:
            continue
        for start in range(len(woerter) - len(teile) + 1):
            if woerter[start : start + len(teile)] == teile:
                frei.update(range(start, start + len(teile)))
    start = 0
    while start < len(woerter):
        if not gedeckt[start]:
            start += 1
            continue
        ende = start
        while ende < len(woerter) and gedeckt[ende]:
            ende += 1
        if sum(1 for stelle in range(start, ende) if stelle not in frei) > UEBERNAHME_WOERTER:
            return " ".join(woerter[start:ende])
        start = ende
    return None


def fremdes_datum(text: str, quelle: Quelle) -> str | None:
    """Erstes Datum des Texts, das so nicht in der Quelle steht; sonst ``None``."""
    for (tag, monat, jahr), (anfang, ende) in _daten(text or ""):
        if jahr is None:
            if (tag, monat) in quelle.tage:
                continue
        elif (tag, monat, jahr) in quelle.daten or ((tag, monat) in quelle.tage and quelle.hat_wert(Decimal(jahr))):
            continue
        return text[anfang:ende].strip()
    return None


def _ohne_daten(text: str) -> str:
    """Text mit Leerzeichen statt Datumsangaben (die prüft ``fremdes_datum``)."""
    zeichen = list(text)
    for _datum, (anfang, ende) in _daten(text):
        zeichen[anfang:ende] = " " * (ende - anfang)
    return "".join(zeichen)


def fremde_zahl(text: str, quelle: Quelle) -> str | None:
    """
    Erste Zahl ab zwei Ziffern (auch als Betrag mit Größenwort), die so nicht in der Quelle steht; sonst ``None``.

    Gerundete große Beträge und ausdrücklich ungefähre Angaben gelten in den Grenzen ``RUNDUNG_GROSS`` bzw.
    ``RUNDUNG_UNGEFAEHR``. Datumsangaben prüft ``fremdes_datum``.
    """
    ohne = _ohne_daten(text or "")
    for treffer in _ZAHL.finditer(ohne):
        roh = treffer.group(0)
        groesse = _GROESSE.match(ohne, treffer.end())
        # Einstellige Zahlen schreibt ein Text oft als Wort („drei“), außer bei Beträgen („2 Millionen“)
        if sum(zeichen.isdigit() for zeichen in roh) < 2 and groesse is None:
            continue
        wert = _wert(roh)
        if wert is None:
            # Mehrteilige Angaben wie Aktenzeichen: jeder Teil ab zwei Ziffern muss in der Quelle stehen
            if all(len(teil) < 2 or quelle.hat_wert(Decimal(teil)) for teil in re.split(r"[.,:]", roh) if teil):
                continue
            return roh
        faktor = _faktor(ohne, treffer.end())
        ungefaehr = bool(_UNGEFAEHR.search(ohne[max(0, treffer.start() - 20) : treffer.start()]))
        toleranz = RUNDUNG_UNGEFAEHR if ungefaehr else RUNDUNG_GROSS if faktor > 1 else Decimal(0)
        if not quelle.hat_wert(wert * faktor, toleranz):
            return roh + groesse.group(0) if groesse is not None else roh
    return None


def fremde_fraktion(text: str, quelle: Quelle) -> str | None:
    """Erste Partei oder Fraktion aus ``PARTEIEN``, die der Text nennt, die Quelle aber nicht; sonst ``None``."""
    for name, muster in _PARTEIEN.items():
        if muster.search(text or "") and not muster.search(quelle.text):
            return name
    return None


def pruefen(text: str, quelle: Quelle) -> Befund | None:
    """
    Grund, aus dem ``text`` verworfen wird (Regeln in der Reihenfolge von ``REGELN``), oder ``None``. Scheitert die
    Prüfung selbst, gilt der Text als nicht bestanden (Regel ``FEHLER``), statt Klick oder Tagesbatch abzubrechen.
    """
    try:
        return _pruefen(text, quelle)
    except Exception:
        logger.exception("Prüfung einer KI-Zusammenfassung gescheitert")
        return Befund(FEHLER, "Prüfung fehlgeschlagen")


def _pruefen(text: str, quelle: Quelle) -> Befund | None:
    if not (text or "").strip():
        return Befund("leer", "Kein Text")
    if hausnummer(text):
        return Befund("hausnummer", "Hausnummer genannt")
    folge = uebernahme(text, quelle)
    if folge:
        return Befund("uebernahme", f"Mehr als {UEBERNAHME_WOERTER} Wörter wörtlich übernommen: „{folge[:200]}“")
    datum = fremdes_datum(text, quelle)
    if datum:
        return Befund("datum", f"Datum nicht in der Quelle: {datum}")
    zahl = fremde_zahl(text, quelle)
    if zahl:
        return Befund("zahl", f"Zahl nicht in der Quelle: {zahl}")
    fraktion = fremde_fraktion(text, quelle)
    if fraktion:
        return Befund("fraktion", f"Nicht in der Quelle: {fraktion}")
    return None


@dataclass(frozen=True)
class Pruefung:
    """Ergebnis für Zusammenfassung und Fassung in Einfacher Sprache."""

    befund: Befund | None
    befund_einfach: Befund | None = None

    @property
    def bestanden(self) -> bool:
        return self.befund is None


def pruefen_beide(zusammenfassung: str, einfach: str, quelle: Quelle) -> Pruefung:
    """Prüft die Zusammenfassung und, wenn vorhanden, die Fassung in Einfacher Sprache gegen dieselbe Quelle."""
    befund = pruefen(zusammenfassung, quelle)
    befund_einfach = pruefen(einfach, quelle) if (einfach or "").strip() else None
    return Pruefung(befund=befund, befund_einfach=befund_einfach)


def protokollieren(
    vorgang_id: Any,
    kommune_id: Any,
    text: str,
    befund: Befund,
    *,
    weg: str,
    teil: str = "zusammenfassung",
    hinweis: str = "",
    batch_id: str = "",
    prompt_fassung: int | None = None,
) -> None:
    """
    Verworfene Fassung festhalten (``VerworfeneZusammenfassung``) und ins Protokoll schreiben; ohne Hausnummer mit dem
    Text, damit sie bei geänderten Regeln nachvollziehbar bleibt. Ein Fehler beim Speichern hält nichts auf (eigener
    Sicherungspunkt, auch innerhalb der Übernahme eines Batches). ``prompt_fassung``: Fassung, mit der der Text
    entstand (Standard: die aktuelle).
    """
    from django.db import transaction

    from insight_ai.models import VerworfeneZusammenfassung

    from .prompts import PROMPT_FASSUNG

    logger.info("KI-Zusammenfassung von Vorgang %s verworfen (%s, %s): %s", vorgang_id, weg, teil, befund.grund)
    try:
        with transaction.atomic():
            VerworfeneZusammenfassung.objects.create(
                vorgang_id=vorgang_id,
                kommune_id=kommune_id,
                prompt_fassung=PROMPT_FASSUNG if prompt_fassung is None else prompt_fassung,
                weg=weg,
                teil=teil,
                regel=befund.regel,
                grund=befund.grund[:500],
                entwurf=text if befund.entwurf_aufbewahren else "",
                modell=_modell(hinweis),
                batch_id=batch_id[:100],
            )
    except Exception:
        logger.exception("Verworfene KI-Zusammenfassung von Vorgang %s nicht protokolliert", vorgang_id)


def _modell(hinweis: str) -> str:
    treffer = re.search(r"\(([^()]{1,100})\)", hinweis or "")
    return treffer.group(1).strip() if treffer else ""


def verworfene_vorgaenge() -> Any:
    """
    Vorgänge, deren Zusammenfassung mit der aktuellen Prompt-Fassung schon verworfen wurde (Unterabfrage der
    Kennungen): Der Tagesbatch reicht sie nicht erneut ein, das würde nur wieder kosten. Eine neue Prompt-Fassung oder
    ein Klick auf „Zusammenfassen“ versucht es erneut.
    """
    from insight_ai.models import VerworfeneZusammenfassung

    from .prompts import PROMPT_FASSUNG

    return VerworfeneZusammenfassung.objects.filter(
        prompt_fassung=PROMPT_FASSUNG, teil=VerworfeneZusammenfassung.Teil.ZUSAMMENFASSUNG
    ).values("vorgang_id")


def quelle_aus(nutzerprompt: str, erlaubt: Iterable[str] = ()) -> Quelle:
    """Quelle aus dem Nutzerprompt einer Zusammenfassung und den Angaben, die wörtlich stehen dürfen."""
    return Quelle(text=nutzerprompt or "", erlaubt=tuple(angabe for angabe in erlaubt if angabe))
