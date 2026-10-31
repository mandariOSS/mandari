# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erkennung der antragstellenden Fraktionen einer Vorlage (Issue #1138), ohne Datenbank und ohne KI.

Dieses Modul rechnet nur: Es bekommt die Vorlage und die Fraktionen ihrer Kommune als einfache Daten und liefert,
welche Fraktion(en) die Vorlage gestellt haben und auf welchem Weg das erkannt wurde. Laden und Speichern übernimmt
``vorgang_urheber``. Es braucht nur die Standardbibliothek, damit wir die Trefferquote auch außerhalb von Django auf
einer Kopie der Daten messen können.

**Wege, in dieser Reihenfolge; der erste mit Ergebnis gilt:**

1. ``source``: Die Quelle nennt ``originatorOrganization``.
2. ``title``: Vorlagenart oder Titel nennen die Fraktion beim Antrag („Antrag der CDU-Fraktion: …“,
   „(Antrag SPD-Fraktion)“, „SPD-Bezirksfraktion-Antrag:“, Vorlagenart „Antrag CDU“). Nur in dieser Stellung: Ein
   Titel wie „Stellungnahme zum Antrag der CDU“ oder eine Mitteilung der Verwaltung zählt nicht.
3. ``person``: Die Quelle nennt ``originatorPerson``; deren Fraktion am Datum der Vorlage.
4. ``signature``: Im Unterschriftenblock des Hauptdokuments stehen Vor- und Nachname von Personen mit Fraktion am
   Datum der Vorlage.

Die Wege 3 und 4 gelten nur für Anträge und Anfragen: Bei einer Verwaltungsvorlage nennt ``originatorPerson`` oft
den Bürgermeister, und der gehört einer Fraktion an, ohne dass sie die Vorlage gestellt hätte.

**Abgleich nur gegen die Fraktionen und Ratsgruppen der eigenen Kommune**, über Schreibvarianten
(``PARTEIEN``: „Grüne“, „Bündnis 90/Die Grünen“, „GRÜNE“ sind eine; „Linke“, „DIE LINKE“ …). Eine Fraktion, deren Name
keine bekannte Partei nennt („Bürgerliste“), wird über ihren Namen selbst gefunden. Gemeinsame Anträge ergeben
mehrere Fraktionen. **Mehrdeutiges wird nicht geraten:** Lässt sich eine genannte Fraktion nicht eindeutig einer
Fraktion der Kommune zuordnen (zwei passende, keine passende), liefert der Weg nichts und der nächste kommt dran.
Eine Person mit zwei Fraktionen am Stichtag zählt ebenso nicht.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

#: Version der Erkennung; steigt, wenn sich für dieselben Daten das Ergebnis ändern kann. Der Bestandslauf ermittelt
#: Vorlagen mit älterer Version neu.
VERSION: Final = 1

_ORTSZEIT: Final = ZoneInfo("Europe/Berlin")

FRAKTION: Final = "fraktion"
GRUPPE: Final = "gruppe"
EINZELMANDAT: Final = "einzelmandat"

SOURCE: Final = "source"
TITLE: Final = "title"
PERSON: Final = "person"
SIGNATURE: Final = "signature"
HERKUNFT: Final = (SOURCE, TITLE, PERSON, SIGNATURE)

# =============================================================================
# Parteien und ihre Schreibweisen
# =============================================================================

_I = re.IGNORECASE
_UE = "(?:ü|ue)"

#: Schlüssel → Muster. Ein Name kann mehrere Schlüssel tragen („SPD/Volt-Fraktion“, „CDU/CSU“). Kürzel, die auch als
#: Wort vorkommen, sind nur in Großschreibung gemeint („Die PARTEI“, „FW“).
PARTEIEN: Final[dict[str, re.Pattern[str]]] = {
    "gruene": re.compile(
        rf"b{_UE}ndnis\s*'?\s*90\s*/?\s*(?:die\s+)?gr{_UE}nen?|\bb\s*90\s*/\s*(?:die\s+)?gr{_UE}nen?\b"
        rf"|\bgr{_UE}nen?\b",
        _I,
    ),
    "spd": re.compile(r"\bSPD\b|\bsozialdemokratische\s+partei\b", _I),
    "cdu": re.compile(r"\bCDU\b|\bchristlich\s+demokratische\s+union\b", _I),
    "csu": re.compile(r"\bCSU\b", _I),
    "fdp": re.compile(r"\bF\.\s?D\.\s?P\.|\bFDP\b|\bfreien?\s+demokrat(?:en|ische\s+partei)\b", _I),
    "linke": re.compile(r"\blinke[n]?\b|\blinksfraktion\b", _I),
    "afd": re.compile(r"\bAfD\b|\balternative\s+für\s+deutschland\b", _I),
    "volt": re.compile(r"\bvolt\b", _I),
    "oedp": re.compile(r"(?<!\w)(?:ÖDP|ödp|OEDP)\b"),
    "partei": re.compile(r"\b(?:Die|DIE)\s+PARTEI\b"),
    "fw": re.compile(r"(?i:\bfreien?\s+w(?:ä|ae)hler(?:gemeinschaft)?\b)|\bFW[GV]?\b|\bF\.W\.G\."),
    "bsw": re.compile(rf"\bBSW\b|\bb{_UE}ndnis\s+sahra\s+wagenknecht\b", _I),
    "piraten": re.compile(r"\bpiraten(?:partei)?\b", _I),
    "tierschutz": re.compile(r"\btierschutzpartei\b", _I),
}
#: Wörter, die in Namen von Fraktionen stehen, aber keine Fraktion unterscheiden
_FUELLWOERTER: Final = re.compile(
    r"\b(?:rats|kreistags|bezirks|regionalrats|stadtrats|gemeinderats|landschaftsversammlungs)?"
    r"(?:fraktion(?:en)?|gruppe)\b|\b(?:der|die|das|im|in|des|rat|stadt|gemeinde|kreistag|und)\b|[-–—/&,.:()]",
    _I,
)
_ORTSZUSATZ: Final = re.compile(r"\b(?:im|in\s+der|in\s+dem)\s+(?:rat|kreistag|bezirksvertretung|stadtrat)\b.*$", _I)


def _fasse(text: str) -> str:
    """Unicode vereinheitlichen und Leerraum (auch Zeilenwechsel, geschützte Leerzeichen) zusammenfassen."""
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parteien_in(text: str) -> set[str]:
    """Schlüssel der Parteien, die ``text`` nennt."""
    return {schluessel for schluessel, muster in PARTEIEN.items() if muster.search(text)}


def ist_gruppe(name: str, einordnung: str = "") -> bool:
    """Ratsgruppe (keine Fraktion): Name oder Einordnung der Quelle nennen eine Gruppe und keine Fraktion."""
    if re.search(r"^\s*(?:rats|kreistags|bezirks)?gruppen?\s*$", einordnung or "", _I):
        return True
    return bool(re.search(r"\b(?:rats|kreistags|bezirks)?gruppe\b", name, _I)) and not re.search(r"fraktion", name, _I)


_KEIN_ZUSAMMENSCHLUSS: Final = re.compile(
    r"fraktionslos|parteilos|einzel(?:mandat|vertret|mitglied)|geschäftsstelle|geschaeftsstelle|sachkundige", _I
)
_ANDERE_ART: Final = re.compile(r"gremium|verwaltung|\bamt\b|dienststelle|ausschuss|organisationseinheit|partei", _I)


def ist_fraktion(name: str, art: str = "", einordnung: str = "") -> bool:
    """
    Fraktion oder Ratsgruppe einer Organisation des RIS (keine Partei, kein Ausschuss, keine Einzelmandate).

    Nach Art bzw. Einordnung der Quelle, sonst nach dem Namen; der Name allein genügt nur, wenn die Art nichts anderes
    sagt. „Ratsfraktion …“ gilt auch mit der Art „Gremium“; „Fraktionsgeschäftsstelle“ (Amt) und
    „Fraktionsvorsitzendenkonferenz“ (Gremium) sind keine Fraktionen.
    """
    if _KEIN_ZUSAMMENSCHLUSS.search(f"{name} {einordnung}"):
        return False
    angabe = f"{art or ''} {einordnung or ''}"
    if re.search(r"fraktion|\b(?:rats)?gruppen?\b", angabe, _I) or re.search(r"fraktion(?!s)", name, _I):
        return True
    if _ANDERE_ART.search(angabe):
        return False
    return bool(re.search(r"\b(?:rats|kreistags|bezirks)?gruppe\b", name, _I))


# =============================================================================
# Daten
# =============================================================================


def _tag(wert: date | None) -> date | None:
    """
    Nur das Datum eines Werts.

    Bestandsdatenbanken führen die Datumsfelder der Ratsdaten als Zeitstempel. Der Tag zählt in Ortszeit, weil Quellen
    sowohl Mitternacht UTC als auch Mitternacht Ortszeit liefern.
    """
    if isinstance(wert, datetime):
        return wert.astimezone(_ORTSZEIT).date() if wert.tzinfo else wert.date()
    return wert


def _im_zeitraum(beginn: date | None, ende: date | None, stichtag: date) -> bool:
    tag = _tag(stichtag)
    von, bis = _tag(beginn), _tag(ende)
    assert tag is not None
    return (von is None or von <= tag) and (bis is None or bis >= tag)


@dataclass(frozen=True)
class Fraktion:
    """
    Eine Fraktion oder Ratsgruppe der Kommune: aus OParl (``organisation_id``) oder aus der gepflegten Liste.

    ``art``: ``fraktion``, ``gruppe`` (Ratsgruppe) oder ``einzelmandat`` (gepflegte Liste: Mandat ohne
    Fraktionsstatus, etwa „FDP“ mit zwei Sitzen). ``kurzform`` ist der Name für Leser („Grüne“, „SPD“).
    """

    bezeichnung: str
    organisation_id: object | None = None
    art: str = "fraktion"
    kurzform: str = ""
    beginn: date | None = None
    ende: date | None = None
    #: Schlüssel der Parteien im Namen; leer bei Namen ohne bekannte Partei (dann gilt ``eigenname``)
    schluessel: frozenset[str] = field(default_factory=frozenset)
    #: Unterscheidender Rest des Namens („Bürgerliste“), wenn er keine bekannte Partei nennt
    eigenname: str = ""
    #: Fraktion in einer Bezirksvertretung („SPD-Bezirksfraktion Nord“)
    bezirk: bool = False
    #: Kürzel aus dem Namen („Freie Wähler Köln (FWK)“) oder dem Kurznamen der Quelle; nur in dieser Schreibung
    kuerzel: tuple[str, ...] = ()

    @classmethod
    def aus_name(
        cls,
        bezeichnung: str,
        *,
        organisation_id: object | None = None,
        art: str | None = None,
        beginn: date | None = None,
        ende: date | None = None,
        kurzname: str = "",
        kurzform: str = "",
    ) -> Fraktion:
        name = _fasse(bezeichnung)
        kuerzel = [k for k in re.findall(r"\(([^()]{2,20})\)", name) if not re.search(r"\s", k.strip())]
        kurz = _fasse(kurzname)
        if kurz and kurz.casefold() != name.casefold() and len(kurz) <= 20 and " " not in kurz:
            kuerzel.append(kurz)
        kern = _ORTSZUSATZ.sub("", name)
        schluessel = frozenset(parteien_in(kern))
        eigenname = "" if schluessel else _fasse(_FUELLWOERTER.sub(" ", kern))
        return cls(
            bezeichnung=name,
            organisation_id=organisation_id,
            art=art or (GRUPPE if ist_gruppe(name) else FRAKTION),
            kurzform=_fasse(kurzform),
            beginn=beginn,
            ende=ende,
            schluessel=schluessel,
            eigenname=eigenname if len(eigenname) >= 2 else "",
            bezirk=bool(re.search(r"bezirk", name, _I)),
            kuerzel=tuple(dict.fromkeys(k.strip() for k in kuerzel if len(k.strip()) >= 2)),
        )

    def gilt_am(self, stichtag: date | None) -> bool:
        if stichtag is None:
            return True
        return _im_zeitraum(self.beginn, self.ende, stichtag)

    def erkennbar(self) -> bool:
        return bool(self.schluessel or self.eigenname or self.kuerzel)


@dataclass(frozen=True)
class Zugehoerigkeit:
    """Person → Fraktion für einen Zeitraum: aus OParl-Mitgliedschaften oder aus der gepflegten Liste."""

    fraktion: Fraktion
    person_id: object | None = None
    vorname: str = ""
    nachname: str = ""
    beginn: date | None = None
    ende: date | None = None

    def gilt_am(self, stichtag: date | None) -> bool:
        if stichtag is None:
            return self.ende is None
        return _im_zeitraum(self.beginn, self.ende, stichtag)


@dataclass
class Kommune:
    """Was die Erkennung über eine Körperschaft wissen muss."""

    fraktionen: list[Fraktion] = field(default_factory=list)
    zugehoerigkeiten: list[Zugehoerigkeit] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._je_person: dict[object, list[Zugehoerigkeit]] = {}
        for z in self.zugehoerigkeiten:
            if z.person_id is not None:
                self._je_person.setdefault(z.person_id, []).append(z)
        self._namen: list[tuple[Zugehoerigkeit, re.Pattern[str]]] = []
        for z in self.zugehoerigkeiten:
            muster = _namensmuster(z.vorname, z.nachname)
            if muster is not None:
                self._namen.append((z, muster))

    def fraktion_der_person(self, person_id: object, stichtag: date | None) -> Fraktion | None:
        """Die eine Fraktion der Person am Stichtag; ``None`` ohne oder bei mehreren."""
        return _eine(z.fraktion for z in self._je_person.get(person_id, []) if z.gilt_am(stichtag))


@dataclass
class Vorlage:
    """Was die Erkennung über eine Vorlage wissen muss."""

    name: str = ""
    paper_type: str = ""
    datum: date | None = None
    #: Organisationen, die die Quelle als ``originatorOrganization`` nennt (schon im Bestand aufgelöst)
    quelle: list[Fraktion] = field(default_factory=list)
    #: Kennungen der Personen aus ``originatorPerson`` (im Bestand aufgelöst)
    personen: list[object] = field(default_factory=list)
    #: Ende des Haupttexts (der Unterschriftenblock steht am Schluss)
    text: str = ""


@dataclass(frozen=True)
class Ergebnis:
    herkunft: str | None = None
    fraktionen: tuple[Fraktion, ...] = ()


KEIN_ERGEBNIS: Final = Ergebnis()


def _eine(fraktionen: Iterable[Fraktion]) -> Fraktion | None:
    """Genau eine Fraktion (gleiche Bezeichnung zählt einmal), sonst ``None``."""
    eindeutig = {f.bezeichnung.casefold(): f for f in fraktionen}
    return next(iter(eindeutig.values())) if len(eindeutig) == 1 else None


# =============================================================================
# Fraktionen im Text finden
# =============================================================================


def _aufloesen(text: str, kommune: Kommune, stichtag: date | None) -> list[Fraktion] | None:
    """
    Fraktionen der Kommune, die ``text`` nennt (nur die Nennung von Fraktionen, kein Fließtext).

    ``None``, wenn eine Nennung nicht eindeutig einer Fraktion der Kommune zuzuordnen ist; leere Liste ohne Nennung.
    """
    gueltig = [f for f in kommune.fraktionen if f.erkennbar() and f.gilt_am(stichtag)]
    # Bezirksvertretung: „Bezirksfraktion“, „BV“, „BV6“
    bezirk = bool(re.search(r"(?i:bezirk)|\bBV\s?\d*\b", text))
    ergebnis: dict[str, Fraktion] = {}
    for schluessel in sorted(parteien_in(text)):
        passend = [f for f in gueltig if f.schluessel == {schluessel}] or [
            f for f in gueltig if schluessel in f.schluessel
        ]
        # Bezirksfraktion nur, wenn der Text von einer spricht (und umgekehrt)
        passend = [f for f in passend if f.bezirk == bezirk] or passend
        fraktion = _eine(passend)
        if fraktion is None:
            return None
        ergebnis[fraktion.bezeichnung.casefold()] = fraktion
    bereinigt = _fasse(_FUELLWOERTER.sub(" ", text))
    for fraktion in gueltig:
        eigen = fraktion.eigenname and re.search(rf"(?<!\w){re.escape(fraktion.eigenname)}(?!\w)", bereinigt, _I)
        if eigen or any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text) for k in fraktion.kuerzel):
            ergebnis.setdefault(fraktion.bezeichnung.casefold(), fraktion)
    return list(ergebnis.values())


# -- Weg 2: Vorlagenart und Titel ---------------------------------------------------------------------------------

_ANTRAGSWORT: Final = re.compile(
    r"(?<!\w)(?:gemeinsame[rsn]?\s+|interfraktionelle[rsn]?\s+|dringliche[rsn]?\s+)?"
    r"(?:(?:dringlichkeits|änderungs|aenderungs|ergänzungs|ergaenzungs|zusatz|haushalts|prüf|pruef|sach|eil|"
    r"tagesordnungs|resolutions|ersetzungs)?antrag|(?:große|kleine|schriftliche|mündliche|grosse)?\s*anfrage|"
    r"prüfauftrag|resolution)(?!\w)",
    _I,
)
#: Steht eines dieser Wörter vor dem Antragswort, ist der Antrag nur Gegenstand der Vorlage
_VORHER_SPERRE: Final = re.compile(
    r"(?:(?:^|\W)(?:zum|zur|zu|den|dem|des|der|die|einen?|einer|eines|auf|über|betr\.?|betreffend|bzgl\.?|"
    r"bezüglich|gem\.?|gemäß|aufgrund|wegen|siehe)|hier\s*:?)\s*$",
    _I,
)
_SPERRWOERTER: Final = re.compile(
    r"\b(?:beantwortung|antwort|stellungnahme|mitteilung|bericht|sachstand|umsetzung|erledigung|ablehnung|"
    r"vorlage\s+der\s+verwaltung|verwaltungsvorlage|niederschrift|protokoll)\b",
    _I,
)
#: Hier endet die Nennung der Antragsteller
_ENDE: Final = re.compile(
    r"[:;()\[\]\"„“”«»]|\s[-–—]\s|\s(?:zu|zum|zur|betr\.?|betreffend|bzgl\.?|bezüglich|über|für|wegen|hinsichtlich|"
    r"vom|v\.|in|im|an|auf|nach|gem\.?|gemäß|bei|mit|zwecks|hier|ohne|gegen|als|wg\.)(?!\w)|\d{1,2}\.\d{1,2}\.",
    _I,
)
_ARTIKEL: Final = re.compile(r"^\s*(?:der|des|den|von\s+der|von\s+den|von|aus\s+der|seitens\s+der|durch\s+die)\s+", _I)
#: Antworten und Berichte der Verwaltung: Ihr Titel nennt den Antrag nur als Gegenstand
_ANTWORTART: Final = re.compile(
    r"beantwortung|antwort|stellungnahme|mitteilung|bericht|niederschrift|bekanntgabe|vorlage\s+der\s+verwaltung", _I
)
#: Vorlagen der Verwaltung: Der Titel zählt nur, wenn er mit dem Antrag beginnt („Anfrage der CDU-Fraktion vom …“),
#: nicht bei „Radweg Hauptstraße; hier: Antrag der CDU-Fraktion“
_VERWALTUNGSART: Final = re.compile(
    r"beschlussvorlage|informationsvorlage|verwaltungsvorlage|kenntnisnahme|entscheidungsvorlage|empfehlungsvorlage|"
    r"sitzungsvorlage",
    _I,
)
_ANTRAGSART: Final = re.compile(r"antrag|anfrage|resolution|prüfauftrag|pruefauftrag", _I)
#: So weit hinter dem Antragswort muss die erste Nennung einer Fraktion beginnen
_ABSTAND: Final = 30
#: Bis hierhin gilt eine Nennung als „vorn“ im Titel
_VORN: Final = 80


def _nennungen_nach(text: str, wort: re.Match[str]) -> str:
    """Text hinter dem Antragswort bis zum Ende der Nennung der Antragsteller."""
    rest = text[wort.end() : wort.end() + 160]
    rest = re.sub(r"^\s*(?:nr\.?\s*)?[\w/.-]*\d[\w/.-]*\s*", " ", rest, flags=_I)  # „Antrag Nr. 12/2024 der …“
    rest = _ARTIKEL.sub(" ", rest)
    ende = _ENDE.search(rest)
    return rest[: ende.start()] if ende else rest


def _beginnt_mit_nennung(fenster: str) -> bool:
    """Steht die erste Nennung einer Partei oder Fraktion gleich am Anfang (und nicht irgendwo im Satz)?"""
    anfang = fenster[:_ABSTAND]
    return bool(parteien_in(anfang) or re.search(r"fraktion|gruppe", anfang, _I))


def nennung_im_titel(text: str, *, nur_vorn: bool = False) -> list[str]:
    """
    Textstellen, in denen der Titel die Antragsteller nennt.

    ``nur_vorn``: nur, wenn der Titel mit dem Antrag beginnt (Vorlagen der Verwaltung).
    """
    text = _fasse(text)
    stellen: list[str] = []
    if not text or _SPERRWOERTER.search(text[:60]):
        return stellen
    for wort in _ANTRAGSWORT.finditer(text):
        davor = text[: wort.start()]
        if _VORHER_SPERRE.search(davor):
            continue
        vorn = wort.start() <= _VORN and not re.search(r"[:;]|\s[-–—]\s|[.!?]\s", davor)
        if nur_vorn and not vorn:
            continue
        # „SPD-Bezirksfraktion-Antrag:“, „CDU/SPD-Antrag zu …“: Die Nennung steht vor dem Antragswort
        if (
            davor.strip()
            and vorn
            and len(davor) <= 40
            and "(" not in davor
            and (davor.rstrip().endswith("-") or parteien_in(davor) or re.search(r"fraktion|gruppe", davor, _I))
        ):
            stellen.append(davor)
            continue
        fenster = _nennungen_nach(text, wort)
        if _beginnt_mit_nennung(fenster):
            stellen.append(fenster)
    # „CDU-Fraktion: Radweg …“, „Fraktion DIE LINKE – Anfrage …“: Die Fraktion steht vor dem Thema
    if not stellen:
        kopf = re.match(r"^(.{2,60}?)(?::|\s[-–—]\s)", text)
        if kopf and re.search(r"fraktion|gruppe", kopf.group(1), _I) and not _ANTRAGSWORT.search(kopf.group(1)):
            stellen.append(kopf.group(1))
    return stellen


def ist_antwort(vorlage: Vorlage) -> bool:
    """Antwort, Stellungnahme oder Bericht der Verwaltung laut Vorlagenart."""
    return bool(vorlage.paper_type and _ANTWORTART.search(vorlage.paper_type))


def ist_verwaltungsvorlage(vorlage: Vorlage) -> bool:
    return ist_antwort(vorlage) or bool(vorlage.paper_type and _VERWALTUNGSART.search(vorlage.paper_type))


def ist_antragsartig(vorlage: Vorlage) -> bool:
    """Antrag, Anfrage oder Resolution laut Vorlagenart (ohne Art: laut Titel)."""
    if ist_verwaltungsvorlage(vorlage):
        return False
    if vorlage.paper_type:
        return bool(_ANTRAGSART.search(vorlage.paper_type))
    wort = _ANTRAGSWORT.search(_fasse(vorlage.name)[:40])
    return wort is not None and not _VORHER_SPERRE.search(_fasse(vorlage.name)[: wort.start()])


def aus_titel(vorlage: Vorlage, kommune: Kommune) -> list[Fraktion] | None:
    """
    Weg 2: Fraktionen aus der Vorlagenart, sonst aus dem Titel. ``None`` bei Mehrdeutigem, leer ohne Nennung.

    Die Vorlagenart zählt ganz, wenn sie ein Antrag ist („Antrag CDU“, „Anfrage nach § 4 BV6 (CDU)“).
    """
    if ist_antwort(vorlage):
        return []
    art = _fasse(vorlage.paper_type)
    stellen_je_text = []
    if art and _ANTRAGSART.search(art) and not ist_verwaltungsvorlage(vorlage):
        stellen_je_text.append([art])
    stellen_je_text.append(nennung_im_titel(vorlage.name, nur_vorn=ist_verwaltungsvorlage(vorlage)))
    for stellen in stellen_je_text:
        gefunden: dict[str, Fraktion] = {}
        for stelle in stellen:
            fraktionen = _aufloesen(stelle, kommune, vorlage.datum)
            if fraktionen is None:
                return None
            gefunden.update({f.bezeichnung.casefold(): f for f in fraktionen})
        if gefunden:
            return list(gefunden.values())
    return []


# -- Weg 3: originatorPerson ---------------------------------------------------------------------------------------


def aus_personen(vorlage: Vorlage, kommune: Kommune) -> list[Fraktion] | None:
    """Weg 3: Fraktion jeder genannten Person am Datum der Vorlage; ``None``, wenn eine Person nicht eindeutig ist."""
    if not vorlage.personen or not ist_antragsartig(vorlage):
        return []
    gefunden: dict[str, Fraktion] = {}
    for person_id in vorlage.personen:
        stichtag_gueltig = [z.fraktion for z in kommune._je_person.get(person_id, []) if z.gilt_am(vorlage.datum)]
        if not stichtag_gueltig:
            continue  # Person ohne Fraktion (Verwaltung, Einzelmitglied): zählt nicht
        fraktion = _eine(stichtag_gueltig)
        if fraktion is None:
            return None
        gefunden[fraktion.bezeichnung.casefold()] = fraktion
    return list(gefunden.values())


# -- Weg 4: Unterschriften -----------------------------------------------------------------------------------------

#: Beginn des Unterschriftenblocks
_GRUSS: Final = re.compile(
    r"\bgez\.|mit\s+freundliche[nm]\s+gr(?:ü|ue)(?:ß|ss)(?:en)?|\bfreundliche\s+gr(?:ü|ue)(?:ß|ss)e|"
    r"für\s+die\s+(?:\w+-?)?(?:fraktion|gruppe)|\bunterschrift|\bunterzeichne",
    _I,
)
#: Ohne Grußformel: so viele Zeichen am Ende gelten als Unterschriftenblock
SCHLUSS: Final = 800
#: So viele Zeichen vom Ende des Dokuments liest die Erkennung überhaupt (``vorgang_urheber`` lädt nur sie)
TEXTENDE: Final = 6000
_TITEL: Final = re.compile(
    r"\b(?:prof|dr|dipl|ing|med|phil|rer|nat|jur|habil|mag|ba|ma|msc|bsc)\.(?:\s*-\s*\w+\.)?\s*|\b(?:von|vom)\s+",
    _I,
)


def _ohne_titel(text: str) -> str:
    return _fasse(_TITEL.sub(" ", text or ""))


def _namensmuster(vorname: str, nachname: str) -> re.Pattern[str] | None:
    """Muster für „Vorname Nachname“ (auch nur einer der Vornamen), Titel ausgenommen."""
    nachname = _ohne_titel(nachname)
    vornamen = [v for v in re.split(r"[\s]+", _ohne_titel(vorname)) if len(v) >= 2]
    if len(nachname) < 2 or not vornamen:
        return None
    alle = [_muster(" ".join(vornamen))] + [_muster(v) for v in vornamen]
    return re.compile(rf"(?<!\w)(?:{'|'.join(dict.fromkeys(alle))})\s+{_muster(nachname)}(?!\w)", _I)


#: Akzente außerhalb des Deutschen werden oft weggelassen („Bohé“ als „Bohe“)
_AKZENTE: Final = {
    "é": "e",
    "è": "e",
    "ê": "e",
    "á": "a",
    "à": "a",
    "â": "a",
    "ó": "o",
    "ò": "o",
    "ô": "o",
    "í": "i",
    "ì": "i",
    "ç": "c",
    "ñ": "n",
    "ø": "o",
    "š": "s",
    "č": "c",
    "ć": "c",
    "ž": "z",
}


def _muster(name: str) -> str:
    """Name als Muster: Leerraum und Bindestriche flexibel, Akzente wahlweise ohne."""
    teile = []
    for zeichen in name:
        basis = _AKZENTE.get(zeichen.lower())
        if basis:
            teile.append(f"[{basis}{zeichen.lower()}]")
        elif zeichen.isspace():
            teile.append(r"\s+")
        elif zeichen == "-":
            teile.append(r"\s*-\s*")
        else:
            teile.append(re.escape(zeichen))
    return "".join(teile)


def unterschriftenblock(text: str) -> str:
    """Schluss des Dokuments ab der Grußformel bzw. ``gez.``; ohne sie die letzten ``SCHLUSS`` Zeichen."""
    text = _fasse(text)
    # Die erste Grußformel in den letzten 2500 Zeichen: Mehrere Unterschriften („gez. …  gez. …“) gehören dazu
    beginn = next((m.start() for m in _GRUSS.finditer(text) if len(text) - m.start() <= 2500), None)
    if beginn is not None:
        return _ohne_titel(text[beginn:])
    return _ohne_titel(text[-SCHLUSS:])


def aus_unterschriften(vorlage: Vorlage, kommune: Kommune) -> list[Fraktion] | None:
    """Weg 4: Personen mit Fraktion am Stichtag im Unterschriftenblock; ``None``, wenn ein Name nicht eindeutig ist."""
    if not vorlage.text or not ist_antragsartig(vorlage):
        return []
    block = unterschriftenblock(vorlage.text)
    if not block:
        return []
    treffer: dict[tuple[int, int], list[Fraktion]] = {}
    for zugehoerigkeit, muster in kommune._namen:
        if not zugehoerigkeit.gilt_am(vorlage.datum) or not zugehoerigkeit.fraktion.gilt_am(vorlage.datum):
            continue
        # Je Fundstelle im Text: Passen dort zwei Personen verschiedener Fraktionen, ist der Name mehrdeutig
        for fund in muster.finditer(block):
            treffer.setdefault(fund.span(), []).append(zugehoerigkeit.fraktion)
    gefunden: dict[str, Fraktion] = {}
    for fraktionen in treffer.values():
        fraktion = _eine(fraktionen)
        if fraktion is None:
            return None  # Gleichnamige Personen in verschiedenen Fraktionen: nicht raten
        gefunden[fraktion.bezeichnung.casefold()] = fraktion
    return list(gefunden.values())


# =============================================================================
# Ablauf
# =============================================================================


def ermitteln(vorlage: Vorlage, kommune: Kommune, *, wege: Sequence[str] = HERKUNFT) -> Ergebnis:
    """Antragstellende Fraktionen der Vorlage; der erste Weg (in ``HERKUNFT``-Reihenfolge) mit Ergebnis gilt."""
    for weg in HERKUNFT:
        if weg not in wege:
            continue
        if weg == SOURCE:
            fraktionen: list[Fraktion] | None = list({f.bezeichnung.casefold(): f for f in vorlage.quelle}.values())
        elif weg == TITLE:
            fraktionen = aus_titel(vorlage, kommune)
        elif weg == PERSON:
            fraktionen = aus_personen(vorlage, kommune)
        else:
            fraktionen = aus_unterschriften(vorlage, kommune)
        if fraktionen:
            return Ergebnis(weg, tuple(sorted(fraktionen, key=lambda f: f.bezeichnung.casefold())))
    return KEIN_ERGEBNIS
