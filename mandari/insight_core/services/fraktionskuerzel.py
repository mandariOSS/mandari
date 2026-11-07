# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionskürzel und Titel ohne Absender (Issue #1272), ohne Datenbank und ohne KI.

Viele Ratsinformationssysteme schreiben den Absender in den Titel: „Grüne-Antrag: Nahversorgung …“, „Antrag der
Fraktion Bündnis 90/Die Grünen: Radweg …“, „Bürgerantrag: Gefahrenstelle …“. In Listen wiederholt sich das in jeder
Zeile und schiebt das Thema nach hinten. Das Bürgerportal zeigt deshalb den Titel ohne diesen Absender und den
Absender als kleines Kürzel mit schmalem Farbstrich daneben (``c-insight.absender``). **Gespeichert bleibt der Titel
unverändert**; die Vorgangsseite nennt ihn unter „Titel im RIS“.

**Woher das Kürzel kommt:** aus den antragstellenden Fraktionen (``VorgangUrheber``, Issue #1138), sobald die
Erkennung sie ermittelt hat, sonst aus dem abgetrennten Absender im Titel (dann im Tooltip als abgeleitet
gekennzeichnet). So wirkt die Anzeige schon vor dem Bestandslauf der Erkennung, und der Lauf verbessert sie später.

**Regeln für den Titel, bewusst eng (lieber ein Präfix zu viel stehen lassen als ein Thema abschneiden):**

- nur diese Formen, am Anfang des Titels oder als Klammer am Ende:
  „Grüne-Antrag: …“, „CDU-SPD-FDP-Kooperation-Antrag: …“, „BBB-Große Anfrage: …“ (Absender vor der Art),
  „Antrag der Fraktion Bündnis 90/Die Grünen: …“, „Anfrage der SPD-Fraktion vom 12.03.2026 – …“,
  „Antrag der Fraktionen … betreffend „…““ (Art vor dem Absender), „CDU-Fraktion: …“ (nur der Absender),
  „Bürgerantrag gem. § 24 GO NRW: …“, „Einwohnerfrage: …“, „Interfraktioneller Antrag: …“,
  „Radweg Hauptstraße (Antrag der SPD-Fraktion)“;
- der Absender muss **ganz** aus Parteinamen (``urheber_erkennung.PARTEIEN``), Kürzeln örtlicher Gruppen (zwei bis
  sechs Buchstaben, davon mindestens zwei groß: „BBB“, „UWG“, „AfB“), Wörtern wie Fraktion, Gruppe, Kooperation und
  Füllwörtern bestehen. Bleibt etwas übrig („Stellungnahme der Verwaltung zum Grüne-Antrag:“, „Anfrage des
  Ratsmitglieds …“, „Antrag zur Änderung der Satzung:“), bleibt der Titel ganz. Ausnahme: Die Erkennung (#1138) kennt
  die Fraktion schon und der Absender nennt eine Fraktion („Antrag der Fraktion Unabhängige Wählergemeinschaft: …“);
- ein Gruppenkürzel ohne Partei zählt nur mit dem Wort Fraktion, Gruppe oder Kooperation im Absender („BBB-Fraktion:“)
  oder wenn die Erkennung (#1138) eine Fraktion mit genau diesem Kürzel kennt. Allein ist es oft ein Thema oder ein
  Verein („WLAN-Antrag:“, „LED-Antrag:“, „DRK-Antrag:“), und der Titel bleibt ganz;
- Antworten und Stellungnahmen der Verwaltung (Vorlagenart) behalten ihren Titel;
- was nach dem Absender bleibt, muss ein Titel sein (mindestens vier Zeichen mit Buchstaben);
- nennt der Absender eine Art, die die Vorlagenart nicht nennt („Dringlichkeitsantrag“ bei der Art „Antrag“), bleibt
  sie vorn: „Dringlichkeitsantrag: …“. Bürgeranträge und Einwohnerfragen stehen statt eines Kürzels als Wort daneben.

**Farben, neutral:** eine gepflegte Tabelle je Partei (``PARTEIEN``), angelehnt an das Erscheinungsbild der Parteien.
Nur der schmale Strich ist farbig; Schrift, Größe und Gewicht sind für alle gleich, und die Farbe trägt nie allein,
das Kürzel steht immer als Text daneben. Örtliche Gruppen und alles, wofür wir keine gesicherte Farbe kennen, sind
grau. Gemeinsame Anträge zeigen je Fraktion ein Kürzel; eine Fraktion aus mehreren Parteien („SPD/Volt“) ein Kürzel
in der Farbe der zuerst genannten.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from . import urheber_erkennung as erkennung

# =============================================================================
# Gepflegte Tabelle: Kürzel und Farbe je Partei
# =============================================================================

#: Strich für örtliche Gruppen und Unbekanntes
GRAU: Final = "#9ca3af"


@dataclass(frozen=True)
class Partei:
    """Kürzel für Leser und Farbe des Strichs einer Partei; ``dunkel`` nur, wo die Farbe im Dunkelmodus verschwände."""

    kurz: str
    farbe: str = GRAU
    dunkel: str = ""


#: Schlüssel wie ``urheber_erkennung.PARTEIEN``. Farben angelehnt an das Erscheinungsbild der Parteien, nur für den
#: schmalen Strich; ohne gesicherte Farbe grau. Neue Parteien hier und in ``urheber_erkennung.PARTEIEN`` ergänzen.
PARTEIEN: Final[dict[str, Partei]] = {
    "cdu": Partei("CDU", "#1f1f1f", dunkel="#e5e5e5"),
    "csu": Partei("CSU", "#0080c8"),
    "spd": Partei("SPD", "#e3000f"),
    "gruene": Partei("Grüne", "#1aa037"),
    "fdp": Partei("FDP", "#e6c300"),
    "linke": Partei("Linke", "#be3075"),
    "afd": Partei("AfD", "#009ee0"),
    "volt": Partei("Volt", "#502379"),
    "bsw": Partei("BSW", "#7d254f"),
    "oedp": Partei("ÖDP", "#ff6400"),
    "partei": Partei("PARTEI", "#b5152b"),
    "fw": Partei("Freie Wähler", "#f39200"),
    "piraten": Partei("Piraten", "#ff8800"),
    "tierschutz": Partei("Tierschutzpartei"),
}

#: Farbe im Dunkelmodus, wo sie von der hellen abweicht (Schwarz verschwände auf dunklem Grund)
_DUNKEL: Final[dict[str, str]] = {p.farbe: p.dunkel for p in PARTEIEN.values() if p.dunkel}

#: So lang darf ein Kürzel werden, das nicht aus der Tabelle kommt („Bürgerliste“, „Die PARTEI & MOVE“)
KUERZEL_MAX: Final = 18
#: So viele Kürzel zeigen Listen je Vorgang, der Rest als „und n weitere“
SICHTBAR: Final = 3
#: Mindestlänge des Titels, der nach dem Absender bleibt („Bäume“)
REST_MIN: Final = 4

HINWEIS_TITEL: Final = "aus dem Titel abgeleitet"


# =============================================================================
# Ergebnis
# =============================================================================


@dataclass(frozen=True)
class Kuerzel:
    """Ein Kürzel mit Farbe des Strichs und Tooltip (voller Name bzw. Herkunft)."""

    text: str
    farbe: str = GRAU
    titel: str = ""

    @property
    def farbe_dunkel(self) -> str:
        """Farbe des Strichs im Dunkelmodus (meist dieselbe)."""
        return _DUNKEL.get(self.farbe, self.farbe)


@dataclass(frozen=True)
class Absender:
    """Was Listen und Vorgangsseite zum Absender eines Vorgangs zeigen."""

    #: Titel zur Anzeige: ohne Absender, sonst der Titel des RIS
    titel: str
    kuerzel: tuple[Kuerzel, ...] = ()
    #: Absender ohne Fraktion, als Wort ohne Strich: „Bürgerantrag“, „Einwohnerfrage“, „interfraktionell“
    von: str = ""
    #: Abgetrennter Teil des Titels („Grüne-Antrag“), leer, wenn der Titel unverändert ist
    abgetrennt: str = ""
    #: Vorlagenart laut RIS, damit Listen mit Spalte „Typ“ „Bürgerantrag“ nicht doppelt zeigen (``von_zusatz``)
    vorlagenart: str = ""
    #: Der abgetrennte Absender mit Stelle im Titel (für die Hervorhebung der Suche), ``None`` ohne Kürzung
    fund: Fund | None = field(default=None, compare=False, repr=False)

    @property
    def gekuerzt(self) -> bool:
        return bool(self.abgetrennt)

    @property
    def sichtbar(self) -> tuple[Kuerzel, ...]:
        return self.kuerzel[:SICHTBAR]

    @property
    def weitere(self) -> int:
        return max(0, len(self.kuerzel) - SICHTBAR)

    @property
    def von_zusatz(self) -> str:
        """``von``, wenn die Vorlagenart es nicht schon sagt (für Stellen, die die Art daneben zeigen)."""
        return "" if self.von.casefold() in self.vorlagenart.casefold() else self.von

    @property
    def teile(self) -> tuple[str, ...]:
        """Was die Spalte „Von“ neben der Spalte „Typ“ zeigt, als Text (leer: Spalte entfällt, ``hat_wert``)."""
        return tuple(k.text for k in self.kuerzel) + ((self.von_zusatz,) if self.von_zusatz else ())

    def __bool__(self) -> bool:
        return bool(self.kuerzel or self.von)

    @property
    def neben_art(self) -> bool:
        """Gibt es neben der angezeigten Vorlagenart etwas zu zeigen?"""
        return bool(self.kuerzel or self.von_zusatz)


class Urheber(Protocol):
    """Was ``absender`` von einer antragstellenden Fraktion braucht (``VorgangUrheber``)."""

    bezeichnung: str
    kurzform: str
    art: str


# =============================================================================
# Bausteine eines Absenders
# =============================================================================

_I = re.IGNORECASE

#: Kürzel einer örtlichen Gruppe: zwei bis sechs Buchstaben, davon mindestens zwei groß („BBB“, „AfB“, „MOVE“)
_GRUPPENKUERZEL: Final = re.compile(r"(?<![\w])(?=[a-zäöü]*[A-ZÄÖÜ][a-zäöü]*[A-ZÄÖÜ])[A-Za-zÄÖÜäöü]{2,6}(?![\w])")
#: Großgeschriebene Wörter, die kein Kürzel einer Gruppe sind
_KEIN_KUERZEL: Final = frozenset(
    {
        "die", "der", "das", "des", "den", "dem", "und", "von", "vom", "zur", "zum", "im", "in", "an", "auf",
        "top", "bv", "ob", "eu", "nrw", "go", "gemo", "ag", "nr", "abs", "ds", "vo", "gmbh", "kg", "ev", "ua", "ha",
    }
)  # fmt: skip
_FRAKTIONSWORT: Final = re.compile(
    r"(?<![\w])(?:rats|bezirks|kreistags|stadtrats|gemeinderats|regionalrats)?(?:fraktion(?:en)?|gruppen?)(?![\w])", _I
)
_GRUPPENWORT: Final = re.compile(r"(?<![\w])(?:kooperation|koalition|interfraktionell\w*)(?![\w])", _I)
#: Wörter und Zeichen, die einen Absender nicht unterscheiden
_FUELLE: Final = re.compile(
    rf"{_FRAKTIONSWORT.pattern}|{_GRUPPENWORT.pattern}"
    r"|(?<![\w])(?:gemeinsame[rsn]?|die|der|des|den|dem|von|und|sowie|aus|seitens)(?![\w])"
    r"|[-–—/,;&+.'’]",
    _I,
)
#: Ortszusatz am Ende eines Fraktionsnamens („… im Rat der Stadt Bonn“)
_ORTSZUSATZ: Final = re.compile(
    r"\s+(?:im|in\s+der|in\s+dem)\s+(?:rat|kreistag|bezirksvertretung|stadtrat|gemeinderat|regionalrat)\b.*$", _I
)


@dataclass(frozen=True)
class _Baustein:
    anfang: int
    ende: int
    text: str
    schluessel: str = ""
    #: Kürzel einer örtlichen Gruppe (``_GRUPPENKUERZEL``), nicht aus der Liste der Parteien
    gruppe: bool = False


def _fasse(text: str | None) -> str:
    """Unicode vereinheitlichen und Leerraum zusammenfassen (wie die Erkennung)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def _bausteine(text: str) -> tuple[list[_Baustein], str]:
    """Parteien und Gruppenkürzel in ``text`` (nach Stellung) und was danach ohne Füllwörter übrig bleibt."""
    funde: list[_Baustein] = []
    belegt = [False] * len(text)
    for schluessel, muster in erkennung.PARTEIEN.items():
        for treffer in muster.finditer(text):
            if any(belegt[treffer.start() : treffer.end()]):
                continue
            partei = PARTEIEN.get(schluessel)
            if schluessel == "fw" and not re.search(r"w(?:ä|ae)hler", treffer.group(0), _I):
                # „FWG“, „FWV“: oft eine örtliche Wählergemeinschaft, nicht die Partei – so, wie geschrieben, grau
                funde.append(_Baustein(treffer.start(), treffer.end(), treffer.group(0)))
            else:
                funde.append(
                    _Baustein(treffer.start(), treffer.end(), partei.kurz if partei else treffer.group(0), schluessel)
                )
            belegt[treffer.start() : treffer.end()] = [True] * (treffer.end() - treffer.start())
    for treffer in _GRUPPENKUERZEL.finditer(text):
        if any(belegt[treffer.start() : treffer.end()]) or treffer.group(0).casefold() in _KEIN_KUERZEL:
            continue
        funde.append(_Baustein(treffer.start(), treffer.end(), treffer.group(0), gruppe=True))
        belegt[treffer.start() : treffer.end()] = [True] * (treffer.end() - treffer.start())
    funde.sort(key=lambda b: b.anfang)
    rest = "".join(" " if belegt[i] else zeichen for i, zeichen in enumerate(text))
    return funde, _fasse(_FUELLE.sub(" ", rest))


def _farbe(bausteine: Iterable[_Baustein]) -> str:
    """Farbe der ersten Partei mit Farbe, sonst grau."""
    for baustein in bausteine:
        partei = PARTEIEN.get(baustein.schluessel)
        if partei is not None and partei.farbe != GRAU:
            return partei.farbe
    return GRAU


def _verbunden(text: str, links: _Baustein, rechts: _Baustein) -> bool:
    """Gehören zwei Bausteine zu einem Namen („BSW/SBI“, „Grüne/GAL“)?"""
    return re.fullmatch(r"\s*/\s*", text[links.ende : rechts.anfang]) is not None


def _kuerzel_im_absender(text: str) -> tuple[Kuerzel, ...]:
    """Kürzel aus einem Absender im Titel: je Fraktion eines, per „/“ verbundene Namen als eines."""
    bausteine, _rest = _bausteine(text)
    gruppen: list[list[_Baustein]] = []
    for baustein in bausteine:
        if gruppen and _verbunden(text, gruppen[-1][-1], baustein):
            gruppen[-1].append(baustein)
        else:
            gruppen.append([baustein])
    ergebnis: dict[str, Kuerzel] = {}
    for gruppe in gruppen:
        kurz = "/".join(dict.fromkeys(b.text for b in gruppe))
        ergebnis.setdefault(kurz.casefold(), Kuerzel(kurz, _farbe(gruppe), HINWEIS_TITEL))
    return tuple(ergebnis.values())


def kuerzel_der_fraktion(bezeichnung: str, kurzform: str = "", art: str = "") -> Kuerzel | None:
    """
    Kürzel einer bekannten Fraktion (``VorgangUrheber``, Mitgliedschaft): Parteien aus der Tabelle, sonst die Kurzform
    der Quelle, sonst ein Kürzel in Klammern im Namen, sonst der Name ohne Füllwörter, wenn er kurz ist; ``None``, wenn
    nichts davon kurz genug ist.

    „SPD-Fraktion im Rat der Stadt Bonn“ → „SPD“, Kurzform „B90/Grüne“ → „Grüne“, „Fraktion SPD/Volt“ → „SPD/Volt“,
    „Fraktion Mitte“ (Kurzform „Mitte“) → „Mitte“ in Grau, „Bürger Bund Bonn (BBB)“ → „BBB“ in Grau.
    """
    name = _fasse(bezeichnung)
    kurz = _fasse(kurzform)
    titel = name
    if art == erkennung.GRUPPE:
        titel = f"{name} (Ratsgruppe)"
    elif art == erkennung.EINZELMANDAT:
        titel = f"{name} (Einzelmandat)"
    for text in (kurz, _ORTSZUSATZ.sub("", name)):
        bausteine, rest = _bausteine(text)
        if bausteine and not rest:
            return Kuerzel("/".join(dict.fromkeys(b.text for b in bausteine)), _farbe(bausteine), titel)
    if kurz and len(kurz) <= KUERZEL_MAX and not _FRAKTIONSWORT.search(kurz):
        return Kuerzel(kurz, GRAU, titel)
    bausteine, rest = _bausteine(_ORTSZUSATZ.sub("", name))
    klammer = re.search(r"\(([^()\s]{2,12})\)", name)
    if klammer:
        return Kuerzel(klammer.group(1), _farbe(bausteine), titel)
    if bausteine:
        return Kuerzel("/".join(dict.fromkeys(b.text for b in bausteine)), _farbe(bausteine), titel)
    if 2 <= len(rest) <= KUERZEL_MAX:
        return Kuerzel(rest, GRAU, titel)
    return None


# =============================================================================
# Absender im Titel
# =============================================================================

_ART: Final = (
    r"(?:(?:dringlichkeits|änderungs|aenderungs|ergänzungs|ergaenzungs|zusatz|haushalts|prüf|pruef|sach|eil|"
    r"ersetzungs|tagesordnungs|resolutions)?antrag|(?:große|grosse|kleine|schriftliche|mündliche)\s+anfrage|anfrage|"
    r"prüfauftrag|pruefauftrag|resolution)"
)
_VON: Final = r"(?:der|des|von\s+der|von\s+den|von|aus\s+der|seitens\s+der)"
_TRENNER: Final = r"\s*(?::|\s[-–—]\s)\s*"
_ZEICHEN: Final = r"[^:;„“\"()\[\]«»]"
_DATUM: Final = r"(?:\s+vom\s+\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{4}|\d{2}))?"

#: Bürgeranträge, Einwohnerfragen, Anregungen: statt eines Kürzels steht das Wort daneben
_EINWOHNER: Final = re.compile(
    r"^(?P<art>bürger(?:innen)?antrag|einwohner(?:innen)?antrag|einwohner(?:innen)?(?:an)?frage|"
    r"bürger(?:innen)?(?:an)?frage|anregung(?:en)?(?:\s+und\s+beschwerden?)?)"
    r"(?:\s+(?:nach|gem\.?|gemäß)?\s*§\s*\d+\s*[a-z]?(?:\s*abs\.?\s*\d+)?(?:\s*[a-zäöü]{2,8}\.?)?(?:\s*nrw)?)?"
    rf"(?:\s*[-–]\s*öffentlich)?{_TRENNER}(?P<rest>.+)$",
    _I,
)
#: „Interfraktioneller Antrag: …“
_INTERFRAKTIONELL: Final = re.compile(rf"^interfraktionelle[rsn]?\s+(?P<art>{_ART}){_TRENNER}(?P<rest>.+)$", _I)
#: „Antrag der Fraktion …: …“, „Anfrage der SPD-Fraktion vom 12.03.2026 – …“
_ART_VON: Final = re.compile(
    rf"^(?:(?:gemeinsame[rsn]?|interfraktionelle[rsn]?)\s+)?(?P<art>{_ART})(?:\s+{_VON})?\s+"
    rf"(?P<von>{_ZEICHEN}{{2,120}}?){_DATUM}{_TRENNER}(?P<rest>.+)$",
    _I,
)
#: „Antrag der Fraktionen … betreffend „…““
_ART_VON_BETREFF: Final = re.compile(
    rf"^(?:(?:gemeinsame[rsn]?|interfraktionelle[rsn]?)\s+)?(?P<art>{_ART})(?:\s+{_VON})?\s+"
    rf"(?P<von>{_ZEICHEN}{{2,120}}?){_DATUM}\s+betr(?:effend|\.)\s*[„\"»‚](?P<rest>[^“\"«‘]+)[“\"«‘]\s*$",
    _I,
)
#: „Grüne-Antrag: …“, „BBB-Große Anfrage: …“, „CDU-SPD-FDP-Kooperation-Antrag: …“
_VON_ART: Final = re.compile(
    rf"^(?P<von>{_ZEICHEN}{{1,60}}?)(?:\s*[-–]\s*|\s+)(?P<art>{_ART}){_TRENNER}(?P<rest>.+)$", _I
)
#: „CDU-Fraktion: …“, „Fraktion DIE LINKE – …“
_NUR_VON: Final = re.compile(rf"^(?P<von>{_ZEICHEN}{{2,60}}?){_TRENNER}(?P<rest>.+)$", _I)
#: „Radweg Hauptstraße (Antrag der SPD-Fraktion)“
_KLAMMER: Final = re.compile(
    rf"^(?P<rest>.+?)\s*\((?:(?:gemeinsame[rsn]?|interfraktionelle[rsn]?)\s+)?(?P<art>{_ART})(?:\s+{_VON})?\s+"
    r"(?P<von>[^()]{2,80})\)\s*$",
    _I,
)
_FORMEN: Final = (_EINWOHNER, _INTERFRAKTIONELL, _ART_VON_BETREFF, _ART_VON, _VON_ART, _NUR_VON, _KLAMMER)


@dataclass(frozen=True)
class Fund:
    """Ein Absender im Titel: abgetrennter Teil, Stelle des verbleibenden Titels und was der Absender nennt."""

    #: Der ganze Titel (gefasst), auf den sich ``anfang``/``ende`` beziehen
    titel: str
    abgetrennt: str
    anfang: int
    ende: int
    #: Art laut Absender („Dringlichkeitsantrag“), leer bei Bürgeranträgen und „CDU-Fraktion: …“
    art: str = ""
    kuerzel: tuple[Kuerzel, ...] = ()
    von: str = ""
    #: Der Absender besteht ganz aus Parteien, Gruppenkürzeln und Füllwörtern
    vollstaendig: bool = False
    #: Der Absender nennt eine Fraktion oder Gruppe (für die Ausnahme mit bekannter Fraktion)
    fraktionswort: bool = False

    @property
    def rest(self) -> str:
        return self.titel[self.anfang : self.ende]

    def art_davor(self, paper_type: str = "") -> str:
        """Art, die vor dem Titel bleibt, weil die Vorlagenart sie nicht nennt („Dringlichkeitsantrag: “), sonst leer."""
        if not self.art or self.art.casefold() in _fasse(paper_type).casefold():
            return ""
        return f"{self.art[:1].upper()}{self.art[1:]}: "

    def anzeige(self, paper_type: str = "") -> str:
        return f"{self.art_davor(paper_type)}{self.rest}"


def _einwohner_wort(art: str) -> str:
    art = art.casefold()
    if art.startswith("anregung"):
        return "Anregung"
    if "frage" in art:
        return "Einwohnerfrage" if art.startswith("einwohner") else "Bürgerfrage"
    return "Einwohnerantrag" if art.startswith("einwohner") else "Bürgerantrag"


def _ist_titel(text: str) -> bool:
    return len(text.strip()) >= REST_MIN and re.search(r"[A-Za-zÄÖÜäöüß]{2}", text) is not None


def _unbestaetigt(bausteine: Sequence[_Baustein], bekannt: Collection[str]) -> bool:
    """
    Steht im Absender ein Gruppenkürzel, das keine bekannte Fraktion trägt? Ohne Fraktions- oder Gruppenwort ist so ein
    Kürzel oft ein Thema oder ein Verein („WLAN-Antrag:“, „OGS-Antrag:“, „DRK-Antrag:“), keine örtliche Gruppe.
    """
    return any(b.gruppe and b.text.casefold() not in bekannt for b in bausteine)


def praefix_finden(titel: str, bekannt: Collection[str] = frozenset()) -> Fund | None:
    """
    Absender am Anfang (oder als Klammer am Ende) des Titels nach den Formen der Moduldokumentation.

    ``bekannt``: Kürzel der Fraktionen, die die Erkennung (#1138) für den Vorgang kennt (``casefold``); nur sie
    bestätigen ein Gruppenkürzel in einem Absender ohne Fraktions- oder Gruppenwort („BBB-Große Anfrage:“).

    Liefert auch Absender, die nicht ganz aufgehen (``vollstaendig`` falsch); ob abgetrennt wird, entscheidet
    ``absender``. ``None`` ohne passende Form.
    """
    titel = _fasse(titel)
    if not titel:
        return None
    for form in _FORMEN:
        treffer = form.match(titel)
        if treffer is None or not _ist_titel(treffer.group("rest")):
            continue
        anfang, ende = treffer.span("rest")
        if form is _KLAMMER:
            abgetrennt = titel[treffer.end("rest") :].strip(" ()")
        else:
            abgetrennt = re.sub(r"\s*[:–—-]\s*$", "", titel[:anfang]).strip(' „"»‚')
        if form is _EINWOHNER:
            return Fund(titel, abgetrennt, anfang, ende, von=_einwohner_wort(treffer.group("art")), vollstaendig=True)
        art = "" if form is _KLAMMER else _fasse(treffer.groupdict().get("art"))
        if form is _INTERFRAKTIONELL:
            return Fund(titel, abgetrennt, anfang, ende, art=art, von="interfraktionell", vollstaendig=True)
        von = _ORTSZUSATZ.sub("", treffer.group("von"))
        if form is _NUR_VON and not _FRAKTIONSWORT.search(von):
            continue
        bausteine, rest = _bausteine(von)
        kuerzel = _kuerzel_im_absender(von)
        gruppe = _GRUPPENWORT.search(von)
        vom_wort = ""
        if not kuerzel and gruppe:
            wort = gruppe.group(0).casefold()
            vom_wort = "interfraktionell" if wort.startswith("interfraktionell") else wort.capitalize()
        fraktionswort = bool(_FRAKTIONSWORT.search(von))
        if not (fraktionswort or gruppe) and _unbestaetigt(bausteine, bekannt):
            bausteine, kuerzel = [], ()
            rest = rest or von
        vollstaendig = not rest and bool(bausteine or vom_wort)
        fund = Fund(
            titel,
            abgetrennt,
            anfang,
            ende,
            art=art,
            kuerzel=kuerzel,
            von=vom_wort,
            vollstaendig=vollstaendig,
            fraktionswort=fraktionswort,
        )
        if vollstaendig or fraktionswort:
            return fund
    return None


#: Wörter, nach denen ein Absender mit Fraktionswort trotzdem nicht abgetrennt wird (Antwort, Person, Verwaltung)
_KEIN_ABSENDER: Final = re.compile(
    r"(?<![\w])(?:stellungnahme|antwort|beantwortung|mitteilung|bericht|verwaltung|zum|zur|zu|betr\.?|betreffend|hier|"
    r"ratsmitglied\w*|ratsherr\w*|ratsfrau\w*|mitglied\w*|herrn?|frau|bürgermeister\w*|oberbürgermeister\w*)(?![\w])",
    _I,
)


def absender(name: str | None, paper_type: str | None = "", urheber: Sequence[Urheber] = ()) -> Absender:
    """
    Titel zur Anzeige und Kürzel eines Vorgangs.

    ``urheber``: gespeicherte antragstellende Fraktionen (Issue #1138); sie gehen vor dem Absender im Titel.
    """
    titel = _fasse(name)
    art = _fasse(paper_type)
    eigene: dict[str, Kuerzel] = {}
    for u in urheber:
        kuerzel = kuerzel_der_fraktion(u.bezeichnung, u.kurzform, u.art)
        if kuerzel is not None:
            eigene.setdefault(kuerzel.text.casefold(), kuerzel)
    bekannt = tuple(eigene.values())
    # Einzelne Kürzel der bekannten Fraktionen („SPD/Volt“ → „spd“, „volt“); nur sie bestätigen ein Gruppenkürzel
    bestaetigt = frozenset(teil.casefold() for k in bekannt for teil in k.text.split("/"))
    fund = None if erkennung.ist_antwort(erkennung.Vorlage(paper_type=art)) else praefix_finden(titel, bestaetigt)
    if fund is None:
        return Absender(titel=titel, kuerzel=bekannt, vorlagenart=art)
    if bekannt and (fund.vollstaendig or (fund.fraktionswort and not _KEIN_ABSENDER.search(fund.abgetrennt))):
        return Absender(
            titel=fund.anzeige(art), kuerzel=bekannt, abgetrennt=fund.abgetrennt, vorlagenart=art, fund=fund
        )
    if fund.vollstaendig and not bekannt:
        return Absender(
            titel=fund.anzeige(art),
            kuerzel=fund.kuerzel,
            von=fund.von,
            abgetrennt=fund.abgetrennt,
            vorlagenart=art,
            fund=fund,
        )
    return Absender(titel=titel, kuerzel=bekannt, vorlagenart=art)


# =============================================================================
# Mit Daten: Vorgangslisten und Suchtreffer
# =============================================================================


def anhaengen(vorgaenge: Iterable[Any]) -> None:
    """
    ``absender`` an jeden Vorgang hängen (Liste, Startseite, Vorgangsseite), eine Abfrage für alle.

    Vorgänge ohne Kennung bleiben unberührt; ``None`` in der Folge wird übersprungen.
    """
    from .vorgang_urheber import urheber_je_vorgang

    liste = [v for v in vorgaenge if v is not None and getattr(v, "pk", None) is not None]
    if not liste:
        return
    je_vorgang = urheber_je_vorgang({v.pk for v in liste})
    for vorgang in liste:
        vorgang.absender = absender(vorgang.name, vorgang.paper_type, je_vorgang.get(vorgang.pk, []))


def treffer_anreichern(treffer: Iterable[dict[str, Any]]) -> None:
    """
    Suchtreffer des Bürgerportals (``search_presentation.present_groups``): Titel ohne Absender, Kürzel als
    ``absender``. Die Hervorhebung des Suchdienstes im Titel bleibt erhalten; eine Abfrage für alle Treffer.

    Braucht je Vorgang ``titel_roh`` und ``art_roh`` (Titel und Vorlagenart laut Suchindex); Work ruft das nicht auf.
    """
    import uuid

    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    from .vorgang_urheber import urheber_je_vorgang

    vorgaenge: list[tuple[uuid.UUID, dict[str, Any]]] = []
    for t in treffer:
        if t.get("kind") != "vorgang":
            continue
        try:
            vorgaenge.append((uuid.UUID(str(t.get("pk"))), t))
        except ValueError:
            continue
    if not vorgaenge:
        return
    je_vorgang = urheber_je_vorgang({kennung for kennung, _t in vorgaenge})
    for kennung, t in vorgaenge:
        roh, art = str(t.get("titel_roh") or ""), str(t.get("art_roh") or "")
        ergebnis = absender(roh, art, je_vorgang.get(kennung, []))
        t["absender"] = ergebnis
        fund = ergebnis.fund
        if fund is None:
            continue
        gekuerzt = _markiert_kuerzen(str(t.get("title") or ""), fund)
        if gekuerzt is None:
            t["title"] = escape(ergebnis.titel)
        else:
            # Beide Teile sind maskiert: die Art per escape, der Titel in _markiert_kuerzen (nur <mark> bleibt HTML)
            t["title"] = mark_safe(f"{escape(fund.art_davor(art))}{gekuerzt}")  # noqa: S308


def _markiert_kuerzen(markiert: str, fund: Fund) -> str | None:
    """
    Den abgetrennten Absender aus einem hervorgehobenen Titel schneiden; ``<mark>`` bleibt, der Text bleibt maskiert.

    ``markiert`` ist die sichere Ausgabe von ``search_service._safe_highlight`` (oder der maskierte Titel). Stimmt ihr
    sichtbarer Text nicht mit dem Titel überein (anders gesäubert, gekürzt), ``None``: Dann gilt der Titel ohne
    Hervorhebung.
    """
    from html import unescape

    from django.utils.html import escape

    from .search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE

    teile = re.split(f"({re.escape(HIGHLIGHT_PRE)}|{re.escape(HIGHLIGHT_POST)})", markiert)
    if "".join(unescape(t) for t in teile if t not in (HIGHLIGHT_PRE, HIGHLIGHT_POST)) != fund.titel:
        return None
    ausgabe: list[str] = []
    stelle = 0
    in_markierung = False  # in der Quelle
    offen = False  # in der Ausgabe
    for teil in teile:
        if teil == HIGHLIGHT_PRE:
            in_markierung = True
        elif teil == HIGHLIGHT_POST:
            in_markierung = False
            if offen:
                ausgabe.append(HIGHLIGHT_POST)
                offen = False
        else:
            text = unescape(teil)
            von, bis = max(fund.anfang, stelle), min(fund.ende, stelle + len(text))
            if von < bis:
                if in_markierung and not offen:
                    ausgabe.append(HIGHLIGHT_PRE)
                    offen = True
                ausgabe.append(str(escape(text[von - stelle : bis - stelle])))
                if offen and bis < stelle + len(text):
                    ausgabe.append(HIGHLIGHT_POST)
                    offen = False
            stelle += len(text)
    if offen:
        ausgabe.append(HIGHLIGHT_POST)
    return "".join(ausgabe)
