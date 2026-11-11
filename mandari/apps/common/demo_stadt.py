# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Musterstadt im Vollausbau: gemeinsamer Katalog der Demo-Daten und der daraus berechnete Sitzungsplan (Issue #99).

Die Basisdemo (``setup_demo_environment``) erzählt wenige, von Hand geschriebene Geschichten (Trinkwasserbrunnen,
Jugendbeirat, Spielplatz). Damit Rat, Ausschüsse, Fraktionen und Verwaltung so voll aussehen wie in einer echten
Stadt, ergänzt dieser Katalog einen **Bestand**: sechs Fraktionen und Gruppen, gut 50 Personen, elf Gremien,
Ämter, Orte und rund 200 Vorgänge über ein Jahr. ``stadtplan`` berechnet daraus deterministisch Sitzungen,
Tagesordnungen, Beratungsfolgen, Ergebnisse und Abstimmungen – relativ zum Aufbautag, wie die Basisdemo.

Hier stehen nur Daten, keine Modelle: Insight (``setup_demo_insight``), Session (``setup_demo_session``) und Work
(``setup_demo_work_bestand``) legen ihren Teil jeweils im eigenen Modul an. So passen die drei Teile zueinander
(gleiche Personen, Gremien, Termine, Drucksachennummern), ohne dass sich die Fachmodule kennen
(Schichtenmodell, ``[tool.importlinter]``).

Kennungen: Alles aus dem Bestand trägt das Präfix ``b-`` in den Demo-Kennungen (``demo_ext(art, "b-…")``). Ein
wiederholter Lauf entfernt den Bestand über dieses Präfix und legt ihn neu an; die Geschichten der Basisdemo
bleiben unberührt. Alle Namen, Orte und Vorgänge sind erfunden.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from apps.common.demo_daten import DEMO_EMAIL_DOMAIN

#: Präfix aller Bestandskennungen (Insight-``external_id``, Session- und Work-Schlüssel)
BESTAND = "b-"
#: Umfang des Bestands: ``voll`` für die öffentliche Demo, ``klein`` für Tests (Einstellung ``DEMO_UMFANG``)
UMFAENGE = ("voll", "klein")
#: Zeitraum des Bestands in Tagen relativ zum Aufbautag
VON_TAG = -365
BIS_TAG = 95
#: Im Umfang ``klein`` nur Sitzungen in diesem Fenster
KLEIN_VON_TAG = -60
KLEIN_BIS_TAG = 60
#: Takt der Ratssitzungen (Tage) und Anker: die kommende Ratssitzung der Basisdemo (rat-3, +14 Tage)
RAT_TAKT = 42
RAT_ANKER = 14
#: Ladungsfrist: Ein Vorgang kommt frühestens so viele Tage nach seinem Datum auf eine Tagesordnung
LADUNGSFRIST = 7


def stabil(*teile: object) -> int:
    """Stabile Zahl aus beliebigen Teilen (``hash()`` wechselt je Prozess, sha256 nicht)."""
    roh = "|".join(str(t) for t in teile).encode()
    return int(hashlib.sha256(roh).hexdigest()[:12], 16)


# ---------------------------------------------------------------------------
# Fraktionen, Personen, Gremien, Ämter, Orte
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fraktion:
    key: str  # Schlüssel der OParl-Organisation (wie in der Basisdemo: fraktion-bunte-liste, fraktion-zukunft)
    name: str
    kurz: str
    partei: str
    art: str = "Fraktion"  # oder "Gruppe"
    #: aus der Basisdemo (setup_demo_environment legt die Organisation an)
    basis: bool = False


FRAKTIONEN: tuple[Fraktion, ...] = (
    Fraktion("fraktion-bunte-liste", "Fraktion Bunte Liste (Demo)", "BL", "Bunte Liste", basis=True),
    Fraktion("fraktion-zukunft", "Fraktion Zukunft (Demo)", "ZUK", "Zukunft Musterstadt", basis=True),
    Fraktion("fraktion-buergerforum", "Fraktion Bürgerforum Musterstadt (Demo)", "BFM", "Bürgerforum Musterstadt"),
    Fraktion("fraktion-neue-mitte", "Fraktion Neue Mitte (Demo)", "NM", "Neue Mitte"),
    Fraktion("fraktion-gfm", "Fraktion Gemeinsam für Musterstadt (Demo)", "GfM", "Gemeinsam für Musterstadt"),
    Fraktion("gruppe-ul", "Gruppe Unabhängige Liste (Demo)", "UL", "Unabhängige Liste", art="Gruppe"),
)
FRAKTION = {f.key: f for f in FRAKTIONEN}


@dataclass(frozen=True)
class Person:
    key: str
    vorname: str
    nachname: str
    geschlecht: str  # OParl: female, male
    fraktion: str | None  # Schlüssel der Fraktion, None = fraktionslos bzw. Bürgermeisterin
    rat: bool = True  # False: sachkundige Bürgerin/sachkundiger Bürger (nur Ausschüsse)
    titel: str = ""
    beruf: str = ""
    #: aus der Basisdemo (setup_demo_environment legt Person, Rat- und Fraktionsmitgliedschaft an)
    basis: bool = False

    @property
    def name(self) -> str:
        return f"{self.titel} {self.vorname} {self.nachname}".strip()

    @property
    def email(self) -> str:
        return f"{self.key}@{DEMO_EMAIL_DOMAIN}"


BUERGERMEISTERIN = "miriam-mahler"

PERSONEN: tuple[Person, ...] = (
    # Basisdemo (Reihenfolge und Schlüssel wie setup_demo_environment)
    Person("anna-amberg", "Anna", "Amberg", "female", "fraktion-bunte-liste", beruf="Lehrerin", basis=True),
    Person("bernd-birkholz", "Bernd", "Birkholz", "male", "fraktion-bunte-liste", beruf="Elektromeister", basis=True),
    Person("clara-cornelsen", "Clara", "Cornelsen", "female", "fraktion-bunte-liste", beruf="Architektin", basis=True),
    Person("dieter-dahl", "Dieter", "Dahl", "male", "fraktion-bunte-liste", beruf="Rentner", basis=True),
    Person("elif-erden", "Elif", "Erden", "female", "fraktion-zukunft", beruf="Juristin", basis=True),
    Person("frank-feldmann", "Frank", "Feldmann", "male", "fraktion-zukunft", beruf="Bauingenieur", basis=True),
    Person("gisela-grote", "Gisela", "Grote", "female", "fraktion-zukunft", beruf="Apothekerin", basis=True),
    Person("hakan-heller", "Hakan", "Heller", "male", "fraktion-zukunft", beruf="Unternehmer", basis=True),
    # Bürgermeisterin (Vorsitz im Rat, stimmberechtigt)
    Person(BUERGERMEISTERIN, "Miriam", "Mahler", "female", None, titel="Dr.", beruf="Bürgermeisterin"),
    # Bunte Liste (12 Sitze)
    Person("jana-jacobs", "Jana", "Jacobs", "female", "fraktion-bunte-liste", beruf="Sozialarbeiterin"),
    Person("karim-kaya", "Karim", "Kaya", "male", "fraktion-bunte-liste", beruf="Informatiker"),
    Person("lea-lorenz", "Lea", "Lorenz", "female", "fraktion-bunte-liste", beruf="Studentin"),
    Person("moritz-meyer", "Moritz", "Meyer", "male", "fraktion-bunte-liste", beruf="Tischler"),
    Person("nadine-novak", "Nadine", "Novak", "female", "fraktion-bunte-liste", beruf="Pflegefachkraft"),
    Person("ole-olsen", "Ole", "Olsen", "male", "fraktion-bunte-liste", beruf="Landschaftsgärtner"),
    Person("petra-pohl", "Petra", "Pohl", "female", "fraktion-bunte-liste", beruf="Buchhändlerin"),
    Person("ruben-roth", "Ruben", "Roth", "male", "fraktion-bunte-liste", beruf="Verkehrsplaner"),
    # Zukunft (11 Sitze)
    Person("sabine-schulte", "Sabine", "Schulte", "female", "fraktion-zukunft", beruf="Steuerberaterin"),
    Person("tobias-thiel", "Tobias", "Thiel", "male", "fraktion-zukunft", beruf="Polizeibeamter"),
    Person("ursula-ulrich", "Ursula", "Ulrich", "female", "fraktion-zukunft", beruf="Landwirtin"),
    Person("volker-vogt", "Volker", "Vogt", "male", "fraktion-zukunft", beruf="Bankkaufmann"),
    Person("wiebke-wolff", "Wiebke", "Wolff", "female", "fraktion-zukunft", beruf="Ärztin"),
    Person("yusuf-yilmaz", "Yusuf", "Yılmaz", "male", "fraktion-zukunft", beruf="Gastronom"),
    Person("zoe-zander", "Zoe", "Zander", "female", "fraktion-zukunft", beruf="Erzieherin"),
    # Bürgerforum (8 Sitze)
    Person("achim-albers", "Achim", "Albers", "male", "fraktion-buergerforum", beruf="Schulleiter a. D."),
    Person("birgit-brandt", "Birgit", "Brandt", "female", "fraktion-buergerforum", beruf="Verwaltungswirtin"),
    Person("christian-claasen", "Christian", "Claasen", "male", "fraktion-buergerforum", beruf="Handwerksmeister"),
    Person("doris-dreher", "Doris", "Dreher", "female", "fraktion-buergerforum", beruf="Hebamme"),
    Person("emil-engel", "Emil", "Engel", "male", "fraktion-buergerforum", beruf="Kaufmann"),
    Person("franziska-fuchs", "Franziska", "Fuchs", "female", "fraktion-buergerforum", beruf="Journalistin"),
    Person("georg-gerber", "Georg", "Gerber", "male", "fraktion-buergerforum", beruf="Förster"),
    Person("heike-hansen", "Heike", "Hansen", "female", "fraktion-buergerforum", beruf="Bürokauffrau"),
    # Neue Mitte (6 Sitze)
    Person("ingo-imhoff", "Ingo", "Imhoff", "male", "fraktion-neue-mitte", beruf="Rechtsanwalt"),
    Person("julia-jansen", "Julia", "Jansen", "female", "fraktion-neue-mitte", beruf="Unternehmensberaterin"),
    Person("konrad-kessler", "Konrad", "Kessler", "male", "fraktion-neue-mitte", beruf="Immobilienkaufmann"),
    Person("laura-lange", "Laura", "Lange", "female", "fraktion-neue-mitte", beruf="Ingenieurin"),
    Person("marco-mertens", "Marco", "Mertens", "male", "fraktion-neue-mitte", beruf="Einzelhändler"),
    Person("nina-neumann", "Nina", "Neumann", "female", "fraktion-neue-mitte", beruf="Physiotherapeutin"),
    # Gemeinsam für Musterstadt (4 Sitze)
    Person("otto-ostermann", "Otto", "Ostermann", "male", "fraktion-gfm", beruf="Rentner"),
    Person("paula-peters", "Paula", "Peters", "female", "fraktion-gfm", beruf="Lehrerin"),
    Person("quentin-quast", "Quentin", "Quast", "male", "fraktion-gfm", beruf="Schreiner"),
    Person("rosa-reimann", "Rosa", "Reimann", "female", "fraktion-gfm", beruf="Floristin"),
    # Gruppe Unabhängige Liste (2 Sitze) und ein fraktionsloses Mitglied
    Person("stefan-stein", "Stefan", "Stein", "male", "gruppe-ul", beruf="Selbstständig"),
    Person("tanja-tewes", "Tanja", "Tewes", "female", "gruppe-ul", beruf="Kauffrau"),
    Person("uwe-unger", "Uwe", "Unger", "male", None, beruf="Schlosser"),
    # Sachkundige Bürgerinnen und Bürger (nur Ausschüsse); Sofia Sachkundig ist auch Demo-Nutzerin in Work
    Person(
        "sofia-sachkundig", "Sofia", "Sachkundig", "female", "fraktion-bunte-liste", rat=False, beruf="Stadtplanerin"
    ),
    Person("tim-tessmer", "Tim", "Tessmer", "male", "fraktion-bunte-liste", rat=False, beruf="Sozialpädagoge"),
    Person("anja-arndt", "Anja", "Arndt", "female", "fraktion-zukunft", rat=False, beruf="Sporttrainerin"),
    Person("benedikt-bauer", "Benedikt", "Bauer", "male", "fraktion-zukunft", rat=False, beruf="Energieberater"),
    Person("carla-conrad", "Carla", "Conrad", "female", "fraktion-buergerforum", rat=False, beruf="Museumspädagogin"),
    Person("dennis-dorn", "Dennis", "Dorn", "male", "fraktion-buergerforum", rat=False, beruf="Busfahrer"),
    Person("eva-ebert", "Eva", "Ebert", "female", "fraktion-neue-mitte", rat=False, beruf="Kinderärztin"),
    Person("felix-franke", "Felix", "Franke", "male", "fraktion-neue-mitte", rat=False, beruf="Kaufmann"),
    Person("gabi-glaser", "Gabi", "Glaser", "female", "fraktion-gfm", rat=False, beruf="Seniorenbetreuerin"),
    Person("henrik-holm", "Henrik", "Holm", "male", "gruppe-ul", rat=False, beruf="Landwirt"),
)
PERSON = {p.key: p for p in PERSONEN}


@dataclass(frozen=True)
class Gremium:
    key: str  # Schlüssel der OParl-Organisation (rat, hauptausschuss, bauausschuss wie in der Basisdemo)
    name: str
    kurz: str
    art: str  # OParl-Klassifikation: Rat, Ausschuss
    session_art: str  # Session: council, committee
    raum: str
    sitze: int  # Sitze der Fraktionen und Gruppen (ohne Bürgermeisterin)
    versatz: int  # Tage vor der Ratssitzung im selben Turnus
    nur_jeder_zweite_turnus: bool = False
    nichtoeffentlich: bool = False  # Tagesordnung hat in der Regel einen nicht-öffentlichen Teil
    basis: bool = False

    @property
    def genitiv(self) -> str:
        """„des Hauptausschusses“, „des Ausschusses für Bauen und Verkehr“, „des Rates der Stadt Musterstadt“."""
        if self.key == "rat":
            return "des Rates der Stadt Musterstadt"
        if self.name.startswith("Ausschuss "):
            return "des Ausschusses " + self.name.removeprefix("Ausschuss ")
        return f"des {self.name}es"


GREMIEN: tuple[Gremium, ...] = (
    Gremium(
        "rat",
        "Rat der Stadt Musterstadt",
        "Rat",
        "Rat",
        "council",
        "Ratssaal",
        44,
        0,
        nichtoeffentlich=True,
        basis=True,
    ),
    Gremium(
        "hauptausschuss",
        "Hauptausschuss",
        "HA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 1",
        15,
        -7,
        nichtoeffentlich=True,
        basis=True,
    ),
    Gremium(
        "finanzausschuss",
        "Ausschuss für Finanzen und Beteiligungen",
        "FA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 1",
        13,
        -10,
        nichtoeffentlich=True,
    ),
    Gremium(
        "bauausschuss",
        "Ausschuss für Bauen und Verkehr",
        "BauA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 2",
        13,
        -21,
        nichtoeffentlich=True,
        basis=True,
    ),
    Gremium(
        "stadtentwicklungsausschuss",
        "Ausschuss für Stadtentwicklung und Wirtschaft",
        "StEA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 2",
        13,
        -17,
    ),
    Gremium(
        "umweltausschuss",
        "Ausschuss für Umwelt und Klimaschutz",
        "UKA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 2",
        13,
        -24,
    ),
    Gremium(
        "schulausschuss", "Ausschuss für Schule und Sport", "SchA", "Ausschuss", "committee", "Sitzungssaal 1", 13, -14
    ),
    Gremium("jugendhilfeausschuss", "Jugendhilfeausschuss", "JHA", "Ausschuss", "committee", "Sitzungssaal 1", 13, -28),
    Gremium(
        "sozialausschuss",
        "Ausschuss für Soziales, Gesundheit und Integration",
        "SozA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 1",
        13,
        -31,
    ),
    Gremium(
        "kulturausschuss",
        "Ausschuss für Kultur und Tourismus",
        "KA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 2",
        11,
        -35,
    ),
    Gremium(
        "rpa",
        "Rechnungsprüfungsausschuss",
        "RPA",
        "Ausschuss",
        "committee",
        "Sitzungssaal 3",
        9,
        -12,
        nur_jeder_zweite_turnus=True,
        nichtoeffentlich=True,
    ),
)
GREMIUM = {g.key: g for g in GREMIEN}

#: Ausschussbesetzung der Basisdemo (setup_demo_environment, ``ausschuss_zuordnung``): steht schon in der
#: Datenbank und wird beim Auffüllen der Sitze mitgezählt, aber nicht noch einmal angelegt.
BASIS_BESETZUNG: tuple[tuple[str, str, str], ...] = (
    ("anna-amberg", "hauptausschuss", "Vorsitzende"),
    ("elif-erden", "hauptausschuss", "Stellv. Vorsitzende"),
    ("bernd-birkholz", "hauptausschuss", "Mitglied"),
    ("frank-feldmann", "bauausschuss", "Vorsitzender"),
    ("clara-cornelsen", "bauausschuss", "Stellv. Vorsitzende"),
    ("hakan-heller", "bauausschuss", "Mitglied"),
)


@dataclass(frozen=True)
class Amt:
    key: str
    name: str
    kurz: str


AEMTER: tuple[Amt, ...] = (
    Amt("amt-10", "Haupt- und Personalamt", "10"),
    Amt("amt-14", "Rechnungsprüfungsamt", "14"),
    Amt("amt-20", "Kämmerei", "20"),
    Amt("amt-32", "Ordnungsamt", "32"),
    Amt("amt-37", "Feuerwehr", "37"),
    Amt("amt-40", "Amt für Schule und Sport", "40"),
    Amt("amt-41", "Kulturamt", "41"),
    Amt("amt-50", "Sozialamt", "50"),
    Amt("amt-51", "Jugendamt", "51"),
    Amt("amt-61", "Stadtplanungsamt", "61"),
    Amt("amt-65", "Gebäudewirtschaft", "65"),
    Amt("amt-66", "Tiefbauamt", "66"),
    Amt("amt-67", "Umweltamt", "67"),
    Amt("amt-80", "Wirtschaftsförderung", "80"),
)
AMT = {a.key: a for a in AEMTER}

#: Orte im fiktiven Stadtgebiet um die Kartenmitte (51.5, 7.5): Bezeichnung, Breite, Länge
ORTE: dict[str, tuple[str, float, float]] = {
    "marktplatz": ("Marktplatz", 51.5009, 7.4996),
    "rathaus": ("Rathausplatz", 51.5004, 7.5009),
    "bahnhof": ("Bahnhofsvorplatz", 51.4969, 7.5031),
    "stadtpark": ("Stadtpark", 51.5036, 7.4957),
    "lindenstrasse": ("Lindenstraße", 51.4987, 7.4921),
    "hauptstrasse": ("Hauptstraße", 51.5018, 7.5038),
    "kirchplatz": ("Kirchplatz", 51.5013, 7.4979),
    "am-anger": ("Am Anger", 51.4993, 7.5079),
    "musterweg": ("Musterweg", 51.4958, 7.4949),
    "gewerbegebiet-ost": ("Gewerbegebiet Ost", 51.4991, 7.5243),
    "industriestrasse": ("Industriestraße", 51.4978, 7.5196),
    "nordring": ("Nordring", 51.5098, 7.5012),
    "suedring": ("Südring", 51.4902, 7.5007),
    "westfalenallee": ("Westfalenallee", 51.5021, 7.4852),
    "mühlenteich": ("Mühlenteich", 51.5069, 7.4893),
    "sportpark": ("Sportpark Nord", 51.5112, 7.4961),
    "hallenbad": ("Hallenbad am Wall", 51.5047, 7.5068),
    "gesamtschule": ("Gesamtschule Musterstadt", 51.5081, 7.5104),
    "gymnasium": ("Gymnasium am Stadtpark", 51.5042, 7.4921),
    "grundschule-nord": ("Grundschule Nord", 51.5101, 7.5052),
    "grundschule-sued": ("Grundschule Süd", 51.4915, 7.4978),
    "kita-sonnenschein": ("Kita Sonnenschein", 51.4949, 7.5101),
    "kita-regenbogen": ("Kita Regenbogen", 51.5077, 7.4938),
    "jugendzentrum": ("Jugendzentrum Alte Post", 51.4998, 7.5058),
    "stadtbibliothek": ("Stadtbibliothek", 51.5007, 7.5024),
    "museum": ("Stadtmuseum im Kornspeicher", 51.5016, 7.4962),
    "stadthalle": ("Stadthalle", 51.5029, 7.5049),
    "friedhof": ("Hauptfriedhof", 51.5134, 7.4887),
    "feuerwache": ("Feuerwache Nord", 51.5072, 7.5023),
    "klaeranlage": ("Kläranlage Ruhrwiesen", 51.4867, 7.5162),
    "wertstoffhof": ("Wertstoffhof Industriestraße", 51.4972, 7.5221),
    "freibad": ("Freibad Am Mühlenteich", 51.5062, 7.4874),
    "radweg-ruhr": ("Radweg an der Ruhr", 51.4881, 7.4949),
    "wohngebiet-sued": ("Wohngebiet Südhang", 51.4889, 7.4911),
    "altstadt": ("Altstadt", 51.5011, 7.5003),
    "bruecke": ("Brücke Mühlenweg", 51.5053, 7.4902),
    "seniorenzentrum": ("Seniorenzentrum Lindenhof", 51.4979, 7.4943),
    "obdach": ("Unterkunft Am Wall", 51.5039, 7.5083),
    "busbahnhof": ("Zentraler Omnibusbahnhof", 51.4974, 7.5046),
    "parkhaus": ("Parkhaus Altstadt", 51.5023, 7.5016),
    "dorfplatz": ("Dorfplatz Oberheide", 51.5189, 7.4796),
    "waldweg": ("Stadtwald Heidekamp", 51.5161, 7.5157),
}

# ---------------------------------------------------------------------------
# Vorgänge
# ---------------------------------------------------------------------------

#: Beratungswege: Gremien in Beratungsfolge, das letzte entscheidet bzw. nimmt zur Kenntnis
WEGE: dict[str, tuple[str, ...]] = {
    "rat": ("hauptausschuss", "rat"),
    "haushalt": ("finanzausschuss", "hauptausschuss", "rat"),
    "finanzen": ("finanzausschuss", "hauptausschuss"),
    "finanzen-rat": ("finanzausschuss", "rat"),
    "bau": ("bauausschuss",),
    "bau-rat": ("bauausschuss", "rat"),
    "planung": ("stadtentwicklungsausschuss", "rat"),
    "planung-ausschuss": ("stadtentwicklungsausschuss",),
    "wirtschaft": ("stadtentwicklungsausschuss", "hauptausschuss"),
    "umwelt": ("umweltausschuss",),
    "umwelt-rat": ("umweltausschuss", "hauptausschuss", "rat"),
    "schule": ("schulausschuss",),
    "schule-rat": ("schulausschuss", "rat"),
    "jugend": ("jugendhilfeausschuss",),
    "jugend-rat": ("jugendhilfeausschuss", "rat"),
    "soziales": ("sozialausschuss",),
    "soziales-rat": ("sozialausschuss", "rat"),
    "kultur": ("kulturausschuss",),
    "kultur-rat": ("kulturausschuss", "rat"),
    "rpa": ("rpa", "rat"),
    "haupt": ("hauptausschuss",),
}

#: Vorlagenarten: OParl-Art, Präfix der Drucksachennummer, Session-Art
ARTEN: dict[str, tuple[str, str, str]] = {
    "vorlage": ("Beschlussvorlage", "V", "proposal"),
    "satzung": ("Beschlussvorlage", "V", "bylaw"),
    "antrag": ("Antrag", "A", "motion"),
    "anfrage": ("Anfrage", "AF", "inquiry"),
    "mitteilung": ("Mitteilungsvorlage", "M", "report"),
}


@dataclass(frozen=True)
class Vorgang:
    key: str  # b-001 …
    art: str  # Schlüssel aus ARTEN
    titel: str
    urheber: str  # Amt- oder Fraktionsschlüssel
    weg: tuple[str, ...]
    tag: int  # Datum der Drucksache relativ zum Aufbautag
    sachverhalt: tuple[str, ...]
    beschluss: str
    kosten: int | None = None  # Euro brutto
    orte: tuple[str, ...] = ()
    ergebnis: str = "approved"  # Endergebnis, wenn entschieden: approved, rejected, withdrawn, noted
    vertagt: bool = False  # erste Entscheidungssitzung vertagt, danach Ergebnis
    nummer: str = ""  # Drucksachennummer, setzt stadtplan()

    @property
    def oparl_art(self) -> str:
        return ARTEN[self.art][0]

    @property
    def session_art(self) -> str:
        return ARTEN[self.art][2]

    @property
    def von_fraktion(self) -> bool:
        return self.urheber in FRAKTION

    @property
    def urheber_name(self) -> str:
        if self.urheber in FRAKTION:
            return FRAKTION[self.urheber].name
        return AMT[self.urheber].name


def _eur(betrag: int) -> str:
    return f"{betrag:,}".replace(",", ".") + " Euro brutto"


# Verwaltungsvorlagen: (Weg, Amt, Titel, Anlass, Beschlussvorschlag, Kosten, Orte)
_VORLAGEN: tuple[tuple[str, str, str, str, str, int | None, tuple[str, ...]], ...] = (
    (
        "haushalt",
        "amt-20",
        "Haushaltssatzung und Haushaltsplan für das kommende Haushaltsjahr – Einbringung",
        "Die Kämmerin bringt den Entwurf der Haushaltssatzung ein. Der Ergebnisplan weist ein Defizit von 2,4 Mio. "
        "Euro aus, das aus der Ausgleichsrücklage gedeckt wird.",
        "Der Rat nimmt den Entwurf zur Kenntnis und verweist ihn zur Beratung in die Fachausschüsse.",
        None,
        (),
    ),
    (
        "haushalt",
        "amt-20",
        "Jahresabschluss des Vorjahres und Entlastung der Bürgermeisterin",
        "Der Jahresabschluss schließt mit einem Jahresüberschuss von 812.000 Euro. Das Rechnungsprüfungsamt hat "
        "einen uneingeschränkten Bestätigungsvermerk erteilt.",
        "Der Rat stellt den Jahresabschluss fest und erteilt der Bürgermeisterin Entlastung.",
        None,
        (),
    ),
    (
        "haushalt",
        "amt-20",
        "Hebesätze der Grund- und Gewerbesteuer",
        "Mit der Grundsteuerreform müssen die Hebesätze neu festgesetzt werden. Die Verwaltung schlägt einen "
        "aufkommensneutralen Hebesatz vor.",
        "Der Rat beschließt die Hebesatzsatzung in der vorgelegten Fassung.",
        None,
        (),
    ),
    (
        "finanzen-rat",
        "amt-20",
        "Beteiligungsbericht der Stadt Musterstadt",
        "Der Bericht stellt die wirtschaftliche Lage der Stadtwerke, der Wohnungsbaugesellschaft und der "
        "Bädergesellschaft dar.",
        "Der Rat nimmt den Beteiligungsbericht zur Kenntnis.",
        None,
        (),
    ),
    (
        "finanzen",
        "amt-20",
        "Überplanmäßige Aufwendung für den Winterdienst",
        "Der lange Winter hat den Ansatz für Streusalz und Bereitschaftsdienste um 140.000 Euro überschritten.",
        "Der Hauptausschuss stimmt der überplanmäßigen Aufwendung zu.",
        140000,
        (),
    ),
    (
        "finanzen",
        "amt-20",
        "Überplanmäßige Auszahlung für die Dachsanierung der Stadthalle",
        "Bei der Dachsanierung wurden Schäden an der Tragkonstruktion festgestellt. Die Mehrkosten betragen "
        "95.000 Euro.",
        "Der Hauptausschuss stimmt der überplanmäßigen Auszahlung zu.",
        95000,
        ("stadthalle",),
    ),
    (
        "finanzen",
        "amt-20",
        "Außerplanmäßige Aufwendung für Energiekosten der Schulen",
        "Die Abschlagszahlungen für Strom und Wärme liegen deutlich über dem Planansatz.",
        "Der Hauptausschuss stimmt der außerplanmäßigen Aufwendung in Höhe von 210.000 Euro zu.",
        210000,
        (),
    ),
    (
        "finanzen-rat",
        "amt-20",
        "Gebührenkalkulation Abwasser für das kommende Jahr",
        "Die Kalkulation berücksichtigt die Investitionen in die Kläranlage Ruhrwiesen. Die Schmutzwassergebühr "
        "steigt um 0,12 Euro je Kubikmeter.",
        "Der Rat beschließt die Gebührensatzung für die Abwasserbeseitigung.",
        None,
        ("klaeranlage",),
    ),
    (
        "finanzen-rat",
        "amt-20",
        "Gebührenkalkulation Abfallentsorgung",
        "Durch die neue Ausschreibung der Sammlung sinken die Kosten. Die Gebühr für die 120-Liter-Tonne sinkt "
        "um 4 Euro im Jahr.",
        "Der Rat beschließt die Abfallgebührensatzung.",
        None,
        ("wertstoffhof",),
    ),
    (
        "finanzen-rat",
        "amt-20",
        "Kreditermächtigung für Investitionen in Schulgebäude",
        "Für das Schulbauprogramm ist eine Kreditaufnahme von 6,5 Mio. Euro vorgesehen.",
        "Der Rat ermächtigt die Verwaltung zur Aufnahme der Kredite.",
        6500000,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Stellenplan der Stadtverwaltung",
        "Der Stellenplan sieht acht zusätzliche Stellen vor, unter anderem für Kitas, Digitalisierung und Klimaschutz.",
        "Der Rat beschließt den Stellenplan.",
        None,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Änderung der Hauptsatzung: digitale Bekanntmachungen",
        "Öffentliche Bekanntmachungen sollen künftig rechtsverbindlich auf der Internetseite der Stadt erfolgen.",
        "Der Rat beschließt die Änderung der Hauptsatzung.",
        None,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Änderung der Geschäftsordnung des Rates: hybride Sitzungen",
        "Ausschussmitglieder sollen in begründeten Fällen per Video an Sitzungen teilnehmen können.",
        "Der Rat beschließt die Änderung der Geschäftsordnung.",
        None,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Wahl einer Schiedsperson für den Schiedsamtsbezirk Musterstadt-Mitte",
        "Die Amtszeit der bisherigen Schiedsperson endet. Es liegt eine Bewerbung vor.",
        "Der Rat wählt die vorgeschlagene Person zur Schiedsperson.",
        None,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Benennung von Vertreterinnen und Vertretern im Aufsichtsrat der Stadtwerke",
        "Nach dem Ausscheiden eines Mitglieds ist ein Sitz im Aufsichtsrat neu zu besetzen.",
        "Der Rat benennt das vorgeschlagene Mitglied.",
        None,
        (),
    ),
    (
        "rat",
        "amt-10",
        "Digitalisierungsstrategie der Stadtverwaltung",
        "Die Strategie beschreibt, welche Verwaltungsleistungen bis Ende des übernächsten Jahres online angeboten "
        "werden, und setzt auf offene Software.",
        "Der Rat beschließt die Digitalisierungsstrategie.",
        350000,
        ("rathaus",),
    ),
    (
        "rat",
        "amt-37",
        "Beschaffung eines Hilfeleistungslöschfahrzeugs",
        "Das Löschfahrzeug der Feuerwache Nord ist 24 Jahre alt und nicht mehr wirtschaftlich zu reparieren.",
        "Der Rat beschließt die Beschaffung eines HLF 20.",
        620000,
        ("feuerwache",),
    ),
    (
        "haupt",
        "amt-32",
        "Ordnungsbehördliche Verordnung über die Sperrzeit in der Altstadt",
        "Anwohnende beklagen nächtlichen Lärm. Die Verwaltung schlägt eine Sperrzeit ab 2 Uhr am Wochenende vor.",
        "Der Hauptausschuss empfiehlt dem Rat den Erlass der Verordnung.",
        None,
        ("altstadt",),
    ),
    (
        "haupt",
        "amt-32",
        "Neuordnung der Parkraumbewirtschaftung in der Innenstadt",
        "Die Parkgebühren wurden seit zwölf Jahren nicht angepasst. Bewohnerparken soll ausgeweitet werden.",
        "Der Hauptausschuss beschließt die neue Gebührenordnung für das Parken.",
        None,
        ("parkhaus", "altstadt"),
    ),
    (
        "haupt",
        "amt-32",
        "Weihnachtsmarkt: Vergabe der Standplätze und Sicherheitskonzept",
        "Der Weihnachtsmarkt findet wieder auf dem Marktplatz statt. Das Sicherheitskonzept wurde mit der Polizei "
        "abgestimmt.",
        "Der Hauptausschuss stimmt dem Sicherheitskonzept zu.",
        38000,
        ("marktplatz",),
    ),
    (
        "bau-rat",
        "amt-66",
        "Grundhafte Erneuerung der Hauptstraße",
        "Fahrbahn, Gehwege und Leitungen der Hauptstraße sind stark sanierungsbedürftig. Die Stadtwerke erneuern "
        "gleichzeitig die Wasserleitung.",
        "Der Rat beschließt die Erneuerung der Hauptstraße nach Variante 2 (mit Radfahrstreifen).",
        2850000,
        ("hauptstrasse",),
    ),
    (
        "bau",
        "amt-66",
        "Sanierung der Brücke Mühlenweg",
        "Die Brückenprüfung hat die Zustandsnote 3,0 ergeben. Eine Sanierung ist innerhalb von zwei Jahren nötig.",
        "Der Ausschuss beauftragt die Verwaltung mit der Ausführungsplanung.",
        780000,
        ("bruecke",),
    ),
    (
        "bau",
        "amt-66",
        "Ausbau der Lindenstraße zur Fahrradstraße",
        "Die Lindenstraße ist eine wichtige Verbindung zwischen Wohngebieten und Schulen.",
        "Der Ausschuss beschließt die Einrichtung einer Fahrradstraße.",
        120000,
        ("lindenstrasse",),
    ),
    (
        "bau",
        "amt-66",
        "Barrierefreier Umbau von zwölf Bushaltestellen",
        "Bis Ende nächsten Jahres sollen alle Haltestellen im Kernbereich barrierefrei sein. Das Land fördert 90 "
        "Prozent der Kosten.",
        "Der Ausschuss beschließt das Bauprogramm.",
        540000,
        ("busbahnhof", "hauptstrasse"),
    ),
    (
        "bau",
        "amt-66",
        "Verkehrsberuhigung im Musterweg",
        "Messungen zeigen überhöhte Geschwindigkeiten im Musterweg. Vorgeschlagen werden Aufpflasterungen und "
        "Tempo 30.",
        "Der Ausschuss beschließt die Maßnahmen zur Verkehrsberuhigung.",
        45000,
        ("musterweg",),
    ),
    (
        "bau",
        "amt-66",
        "Straßenbeleuchtung: Umstellung auf LED, dritter Bauabschnitt",
        "Mit dem dritten Abschnitt sind 85 Prozent der Leuchten umgestellt. Der Stromverbrauch sinkt um 60 Prozent.",
        "Der Ausschuss beschließt die Umsetzung des dritten Bauabschnitts.",
        310000,
        ("nordring", "suedring"),
    ),
    (
        "bau",
        "amt-66",
        "Neubau eines Kreisverkehrs Nordring / Westfalenallee",
        "Die Kreuzung ist ein Unfallschwerpunkt. Ein Kreisverkehr erhöht die Sicherheit und den Verkehrsfluss.",
        "Der Ausschuss beschließt die Vorplanung des Kreisverkehrs.",
        1150000,
        ("nordring", "westfalenallee"),
    ),
    (
        "bau",
        "amt-66",
        "Sanierung des Parkhauses Altstadt",
        "Betonschäden an den Parkdecks erfordern eine Sanierung. Während der Arbeiten bleibt das Parkhaus "
        "teilweise geöffnet.",
        "Der Ausschuss beschließt die Sanierung in zwei Bauabschnitten.",
        980000,
        ("parkhaus",),
    ),
    (
        "bau-rat",
        "amt-65",
        "Neubau der Feuerwache Süd – Vorplanung",
        "Die Hilfsfrist im Süden wird nicht eingehalten. Der Brandschutzbedarfsplan sieht einen neuen Standort vor.",
        "Der Rat beschließt die Vorplanung und beauftragt die Entwurfsplanung.",
        7800000,
        ("suedring",),
    ),
    (
        "bau",
        "amt-65",
        "Sanierung der Sanitäranlagen im Hallenbad",
        "Duschen und Umkleiden entsprechen nicht mehr den hygienischen Anforderungen.",
        "Der Ausschuss beschließt die Sanierung in der Sommerschließzeit.",
        260000,
        ("hallenbad",),
    ),
    (
        "bau",
        "amt-65",
        "Photovoltaik auf städtischen Dächern – Programm für die nächsten drei Jahre",
        "Auf 14 städtischen Gebäuden sollen Photovoltaikanlagen mit zusammen 1,2 Megawatt Leistung entstehen.",
        "Der Ausschuss beschließt das Ausbauprogramm.",
        1600000,
        ("gesamtschule", "stadthalle", "rathaus"),
    ),
    (
        "bau",
        "amt-66",
        "Winterdienst: Neuvergabe der Räum- und Streuleistungen",
        "Der Vertrag mit dem bisherigen Dienstleister endet. Die Verwaltung schlägt eine Vergabe für vier Jahre vor.",
        "Der Ausschuss stimmt der Ausschreibung zu.",
        420000,
        (),
    ),
    (
        "planung",
        "amt-61",
        "Integriertes Stadtentwicklungskonzept Innenstadt",
        "Das Konzept bündelt Maßnahmen für Handel, Wohnen, Grün und Verkehr in der Innenstadt und ist Grundlage "
        "für Fördermittel.",
        "Der Rat beschließt das Stadtentwicklungskonzept.",
        None,
        ("altstadt", "marktplatz"),
    ),
    (
        "planung",
        "amt-61",
        "Wohnraumversorgungskonzept",
        "Bis 2035 werden rund 1.400 zusätzliche Wohnungen benötigt, davon ein Drittel gefördert.",
        "Der Rat beschließt das Konzept und eine Quote von 30 Prozent gefördertem Wohnraum.",
        None,
        ("wohngebiet-sued",),
    ),
    (
        "planung",
        "amt-61",
        "Einzelhandels- und Zentrenkonzept – Fortschreibung",
        "Das Konzept legt fest, wo großflächiger Einzelhandel zulässig ist, und schützt die Innenstadt.",
        "Der Rat beschließt die Fortschreibung.",
        None,
        ("hauptstrasse",),
    ),
    (
        "planung-ausschuss",
        "amt-61",
        "Gestaltungssatzung Altstadt – Entwurf",
        "Fassaden, Werbeanlagen und Dachformen in der Altstadt sollen einheitlichen Regeln folgen.",
        "Der Ausschuss beschließt die öffentliche Auslegung des Entwurfs.",
        None,
        ("altstadt", "kirchplatz"),
    ),
    (
        "wirtschaft",
        "amt-80",
        "Erweiterung des Gewerbegebiets Ost",
        "Mehrere ansässige Betriebe wollen wachsen. Die Fläche ist im Flächennutzungsplan bereits dargestellt.",
        "Der Hauptausschuss beauftragt die Verwaltung mit Grundstücksverhandlungen.",
        None,
        ("gewerbegebiet-ost",),
    ),
    (
        "wirtschaft",
        "amt-80",
        "Leerstandsmanagement in der Innenstadt",
        "Elf Ladenlokale stehen leer. Ein Zwischennutzungsfonds soll Pop-up-Läden und Kultur ermöglichen.",
        "Der Hauptausschuss beschließt den Zwischennutzungsfonds.",
        60000,
        ("hauptstrasse", "altstadt"),
    ),
    (
        "wirtschaft",
        "amt-80",
        "Wochenmarkt: neue Marktsatzung",
        "Der Wochenmarkt soll zusätzlich donnerstags am Nachmittag stattfinden.",
        "Der Hauptausschuss empfiehlt die neue Marktsatzung.",
        None,
        ("marktplatz",),
    ),
    (
        "umwelt-rat",
        "amt-67",
        "Kommunale Wärmeplanung – Abschlussbericht",
        "Die Wärmeplanung zeigt, wo Fernwärme ausgebaut wird und wo Wärmepumpen die beste Lösung sind.",
        "Der Rat beschließt den kommunalen Wärmeplan.",
        180000,
        ("altstadt", "wohngebiet-sued"),
    ),
    (
        "umwelt-rat",
        "amt-67",
        "Klimaschutzkonzept: Maßnahmenprogramm",
        "Das Programm enthält 42 Maßnahmen, um die Emissionen der Stadt bis 2035 um 65 Prozent zu senken.",
        "Der Rat beschließt das Maßnahmenprogramm.",
        450000,
        (),
    ),
    (
        "umwelt",
        "amt-67",
        "Hitzeaktionsplan",
        "Der Plan nennt kühle Orte, Trinkwasserstellen und Hilfen für ältere Menschen an Hitzetagen.",
        "Der Ausschuss beschließt den Hitzeaktionsplan.",
        75000,
        ("stadtpark", "marktplatz", "seniorenzentrum"),
    ),
    (
        "umwelt",
        "amt-67",
        "Baumpflanzprogramm: 500 Stadtbäume",
        "In den nächsten fünf Jahren sollen 500 klimaresistente Bäume gepflanzt werden, vor allem in dicht bebauten "
        "Quartieren.",
        "Der Ausschuss beschließt das Pflanzprogramm.",
        400000,
        ("altstadt", "wohngebiet-sued", "musterweg"),
    ),
    (
        "umwelt",
        "amt-67",
        "Renaturierung des Mühlenbachs",
        "Der Mühlenbach wird naturnah umgestaltet. Das Land fördert die Maßnahme mit 80 Prozent.",
        "Der Ausschuss beschließt die Entwurfsplanung.",
        690000,
        ("mühlenteich",),
    ),
    (
        "umwelt",
        "amt-67",
        "Starkregenrisikomanagement: Gefahrenkarte",
        "Die Gefahrenkarte zeigt, welche Straßen bei Starkregen überflutet werden können.",
        "Der Ausschuss nimmt die Karte zur Kenntnis und beauftragt ein Handlungskonzept.",
        55000,
        ("am-anger",),
    ),
    (
        "umwelt",
        "amt-67",
        "Pflege der städtischen Grünflächen: insektenfreundliche Mahd",
        "Auf 18 Hektar Straßenbegleitgrün soll nur noch zweimal im Jahr gemäht werden.",
        "Der Ausschuss beschließt das Pflegekonzept.",
        None,
        ("westfalenallee", "nordring"),
    ),
    (
        "umwelt",
        "amt-67",
        "Stadtwald Heidekamp: Wiederbewaldung nach Borkenkäferschäden",
        "Auf 9 Hektar wird ein Mischwald mit Eichen, Buchen und Douglasien gepflanzt.",
        "Der Ausschuss stimmt dem Wiederbewaldungskonzept zu.",
        160000,
        ("waldweg",),
    ),
    (
        "schule-rat",
        "amt-40",
        "Schulentwicklungsplan Primarstufe",
        "Die Schülerzahlen an den Grundschulen steigen bis 2030 um 14 Prozent. Die Grundschule Nord braucht "
        "zusätzliche Räume.",
        "Der Rat beschließt den Schulentwicklungsplan.",
        None,
        ("grundschule-nord", "grundschule-sued"),
    ),
    (
        "schule-rat",
        "amt-40",
        "Erweiterung der Grundschule Nord für den Ganztag",
        "Ab 2026 gilt der Rechtsanspruch auf Ganztagsbetreuung. Die Grundschule Nord braucht eine Mensa und vier "
        "Gruppenräume.",
        "Der Rat beschließt den Erweiterungsbau.",
        3400000,
        ("grundschule-nord",),
    ),
    (
        "schule",
        "amt-40",
        "Digitale Ausstattung der Schulen: Medienentwicklungsplan",
        "Alle Klassenräume erhalten interaktive Tafeln, die Schulen ein stabiles WLAN.",
        "Der Ausschuss beschließt den Medienentwicklungsplan.",
        890000,
        ("gesamtschule", "gymnasium"),
    ),
    (
        "schule",
        "amt-40",
        "Schulwegsicherung an der Grundschule Süd",
        "Elterntaxis gefährden die Kinder. Vorgeschlagen werden Hol- und Bringzonen und ein Schulwegplan.",
        "Der Ausschuss beschließt die Maßnahmen.",
        35000,
        ("grundschule-sued",),
    ),
    (
        "schule",
        "amt-40",
        "Sanierung der Sporthalle der Gesamtschule",
        "Hallenboden und Lüftung müssen erneuert werden.",
        "Der Ausschuss beschließt die Sanierung in den Sommerferien.",
        720000,
        ("gesamtschule",),
    ),
    (
        "schule",
        "amt-40",
        "Sportstättenentwicklungsplan",
        "Der Plan zeigt, welche Sportstätten bis 2035 saniert oder neu gebaut werden müssen.",
        "Der Ausschuss beschließt den Sportstättenentwicklungsplan.",
        None,
        ("sportpark", "freibad", "hallenbad"),
    ),
    (
        "schule",
        "amt-40",
        "Kunstrasenplatz im Sportpark Nord",
        "Der Tennenplatz ist bei Regen kaum bespielbar. Vier Vereine nutzen die Anlage.",
        "Der Ausschuss beschließt den Bau eines Kunstrasenplatzes mit Kork-Einstreu.",
        850000,
        ("sportpark",),
    ),
    (
        "schule",
        "amt-40",
        "Freibad Am Mühlenteich: Saisonzeiten und Eintrittspreise",
        "Die Saison soll um zwei Wochen verlängert werden. Familienkarten werden günstiger.",
        "Der Ausschuss beschließt die neuen Saisonzeiten und Entgelte.",
        None,
        ("freibad",),
    ),
    (
        "jugend-rat",
        "amt-51",
        "Kita-Bedarfsplanung für das kommende Kindergartenjahr",
        "Es fehlen 120 Plätze für Kinder unter drei Jahren. Zwei neue Einrichtungen sind geplant.",
        "Der Rat beschließt die Bedarfsplanung.",
        None,
        ("kita-sonnenschein", "kita-regenbogen"),
    ),
    (
        "jugend-rat",
        "amt-51",
        "Neubau der Kita am Südhang (sechs Gruppen)",
        "Ein freier Träger baut und betreibt die Einrichtung, die Stadt übernimmt die Investitionskosten anteilig.",
        "Der Rat stimmt dem Investitionszuschuss zu.",
        1950000,
        ("wohngebiet-sued",),
    ),
    (
        "jugend",
        "amt-51",
        "Kinder- und Jugendförderplan",
        "Der Plan legt Schwerpunkte der offenen Jugendarbeit für fünf Jahre fest.",
        "Der Ausschuss beschließt den Kinder- und Jugendförderplan.",
        240000,
        ("jugendzentrum",),
    ),
    (
        "jugend",
        "amt-51",
        "Ferienprogramm und Ferienfreizeiten",
        "Im Sommer werden 1.100 Plätze in 85 Angeboten bereitgestellt.",
        "Der Ausschuss stimmt dem Programm zu.",
        68000,
        ("jugendzentrum", "sportpark"),
    ),
    (
        "jugend",
        "amt-51",
        "Spielflächenkonzept",
        "Von 46 Spielplätzen sind elf sanierungsbedürftig. Das Konzept priorisiert die Sanierungen.",
        "Der Ausschuss beschließt das Spielflächenkonzept.",
        520000,
        ("stadtpark", "musterweg"),
    ),
    (
        "jugend",
        "amt-51",
        "Schulsozialarbeit an allen Grundschulen",
        "Bisher gibt es Schulsozialarbeit nur an zwei von fünf Grundschulen.",
        "Der Ausschuss empfiehlt die Ausweitung auf alle Grundschulen.",
        190000,
        ("grundschule-nord",),
    ),
    (
        "soziales-rat",
        "amt-50",
        "Unterbringung geflüchteter Menschen: Konzept",
        "Die Stadt muss in diesem Jahr voraussichtlich 180 weitere Menschen aufnehmen.",
        "Der Rat beschließt das Unterbringungskonzept.",
        1300000,
        ("obdach",),
    ),
    (
        "soziales",
        "amt-50",
        "Seniorenplan der Stadt Musterstadt",
        "Der Plan beschreibt Angebote für Begegnung, Pflege und Wohnen im Alter.",
        "Der Ausschuss beschließt den Seniorenplan.",
        None,
        ("seniorenzentrum",),
    ),
    (
        "soziales",
        "amt-50",
        "Kommunaler Aktionsplan Inklusion",
        "Der Aktionsplan enthält 35 Maßnahmen, etwa barrierefreie Gebäude und Leichte Sprache.",
        "Der Ausschuss beschließt den Aktionsplan.",
        90000,
        ("rathaus",),
    ),
    (
        "soziales",
        "amt-50",
        "Integrationskonzept – Fortschreibung",
        "Sprachkurse, Arbeitsmarktzugang und Begegnung stehen im Mittelpunkt.",
        "Der Ausschuss beschließt die Fortschreibung.",
        None,
        (),
    ),
    (
        "soziales",
        "amt-50",
        "Wohnungslosenhilfe: Notunterkunft im Winter",
        "In den Wintermonaten werden zusätzlich 20 Notschlafplätze benötigt.",
        "Der Ausschuss stimmt der Anmietung zu.",
        85000,
        ("obdach",),
    ),
    (
        "soziales",
        "amt-50",
        "Gesundheitskiosk im Musterviertel",
        "Ein niedrigschwelliges Beratungsangebot soll den Zugang zu Gesundheitsleistungen erleichtern.",
        "Der Ausschuss beschließt ein Modellprojekt für drei Jahre.",
        210000,
        ("musterweg",),
    ),
    (
        "kultur",
        "amt-41",
        "Kulturentwicklungsplan",
        "Der Plan wurde mit 300 Kulturschaffenden in Werkstätten erarbeitet.",
        "Der Ausschuss beschließt den Kulturentwicklungsplan.",
        None,
        ("museum", "stadthalle"),
    ),
    (
        "kultur",
        "amt-41",
        "Stadtbibliothek: Öffnung als Dritter Ort mit Open-Library-Zeiten",
        "Mit Selbstverbuchung kann die Bibliothek auch abends und sonntags öffnen.",
        "Der Ausschuss beschließt die Einführung der Open Library.",
        120000,
        ("stadtbibliothek",),
    ),
    (
        "kultur-rat",
        "amt-41",
        "Stadtmuseum: Neukonzeption der Dauerausstellung",
        "Die Ausstellung ist 30 Jahre alt. Das neue Konzept erzählt die Stadtgeschichte digital und barrierefrei.",
        "Der Rat beschließt die Neukonzeption.",
        1450000,
        ("museum",),
    ),
    (
        "kultur",
        "amt-41",
        "Sommerkino im Stadtpark",
        "An acht Abenden zeigt der Kulturverein Filme unter freiem Himmel.",
        "Der Ausschuss stimmt einem Zuschuss zu.",
        12000,
        ("stadtpark",),
    ),
    (
        "kultur",
        "amt-41",
        "Städtepartnerschaft: Jubiläumsprogramm",
        "Die Partnerschaft mit der fiktiven Stadt Exempelville besteht seit 40 Jahren.",
        "Der Ausschuss beschließt das Programm.",
        25000,
        ("rathaus",),
    ),
    (
        "kultur",
        "amt-41",
        "Tourismuskonzept Musterstadt",
        "Rad- und Wandertourismus an der Ruhr sollen gestärkt werden.",
        "Der Ausschuss beschließt das Tourismuskonzept.",
        40000,
        ("radweg-ruhr",),
    ),
    (
        "rpa",
        "amt-14",
        "Prüfbericht des Rechnungsprüfungsamtes zum Jahresabschluss",
        "Die Prüfung hat keine wesentlichen Beanstandungen ergeben. Hinweise betreffen die Inventur.",
        "Der Rat nimmt den Prüfbericht zur Kenntnis.",
        None,
        (),
    ),
    (
        "rpa",
        "amt-14",
        "Vergabeprüfung: Bericht über das Prüfjahr",
        "Geprüft wurden 214 Vergaben. In drei Fällen fehlte die Dokumentation der Wertung.",
        "Der Rat nimmt den Bericht zur Kenntnis.",
        None,
        (),
    ),
)

# Bebauungspläne: (Nummer, Name, Ort, Stufe)
_BPLAENE: tuple[tuple[int, str, str, str], ...] = (
    (13, "Wohnen am Südhang", "wohngebiet-sued", "Aufstellungsbeschluss"),
    (13, "Wohnen am Südhang", "wohngebiet-sued", "Beschluss über die öffentliche Auslegung"),
    (14, "Gewerbegebiet Ost – Erweiterung", "gewerbegebiet-ost", "Aufstellungsbeschluss"),
    (15, "Bahnhofsquartier", "bahnhof", "Frühzeitige Beteiligung"),
    (15, "Bahnhofsquartier", "bahnhof", "Satzungsbeschluss"),
    (16, "Alte Gärtnerei", "lindenstrasse", "Satzungsbeschluss"),
    (17, "Nahversorgung Nordring", "nordring", "Aufstellungsbeschluss"),
    (18, "Dorfplatz Oberheide", "dorfplatz", "Beschluss über die öffentliche Auslegung"),
    (19, "Sportpark Nord", "sportpark", "Aufstellungsbeschluss"),
    (20, "Westfalenallee / Industriestraße", "industriestrasse", "Frühzeitige Beteiligung"),
)

# Zuschüsse: (Weg, Amt, Empfänger, Zweck, Betrag, Ort)
_ZUSCHUESSE: tuple[tuple[str, str, str, str, int, str], ...] = (
    (
        "kultur",
        "amt-41",
        "Heimatverein Musterstadt",
        "die Restaurierung der historischen Stadtmühle",
        18000,
        "mühlenteich",
    ),
    (
        "kultur",
        "amt-41",
        "Musikschule Musterstadt e. V.",
        "Instrumente für Grundschulklassen",
        9500,
        "grundschule-sued",
    ),
    ("kultur", "amt-41", "Theatergruppe Bühne frei e. V.", "das Sommertheater auf dem Kirchplatz", 7000, "kirchplatz"),
    ("schule", "amt-40", "TuS Musterstadt 1898 e. V.", "die Erneuerung der Flutlichtanlage", 42000, "sportpark"),
    ("schule", "amt-40", "Schwimmverein Delfin e. V.", "Schwimmkurse für Grundschulkinder", 15000, "hallenbad"),
    ("schule", "amt-40", "Reitverein Oberheide e. V.", "eine barrierefreie Aufstiegshilfe", 6000, "dorfplatz"),
    ("soziales", "amt-50", "Tafel Musterstadt e. V.", "ein Kühlfahrzeug", 28000, "industriestrasse"),
    ("soziales", "amt-50", "Seniorenbeirat", "das Programm „Digital fit im Alter“", 5500, "seniorenzentrum"),
    ("soziales", "amt-50", "Verein Nachbarschaft Musterviertel e. V.", "das Stadtteilcafé", 16000, "musterweg"),
    ("jugend", "amt-51", "Jugendring Musterstadt", "die Jugendleiter-Ausbildung", 8000, "jugendzentrum"),
    ("jugend", "amt-51", "Verein Spielmobil e. V.", "Spielaktionen in den Sommerferien", 11000, "stadtpark"),
    ("kultur", "amt-41", "Kunstverein Musterstadt e. V.", "die Ausstellungsreihe „Junge Kunst“", 6500, "museum"),
)

# Fraktionsanträge: (Fraktion, Weg, Titel, Beschlussvorschlag, Begründung, Ergebnis, Orte)
_ANTRAEGE: tuple[tuple[str, str, str, str, str, str, tuple[str, ...]], ...] = (
    (
        "fraktion-bunte-liste",
        "bau",
        "Sichere Radwege zu allen Schulen",
        "Die Verwaltung legt bis zum Ende des Schuljahres ein Konzept für sichere Radwege zu allen Schulen vor.",
        "Immer mehr Kinder fahren mit dem Rad zur Schule; an mehreren Stellen fehlen sichere Wege.",
        "approved",
        ("lindenstrasse", "gesamtschule"),
    ),
    (
        "fraktion-bunte-liste",
        "umwelt",
        "Mehr Bänke und Schatten in der Innenstadt",
        "Die Verwaltung prüft, wo zusätzliche Sitzgelegenheiten und Bäume die Aufenthaltsqualität verbessern.",
        "Gerade ältere Menschen brauchen Pausenmöglichkeiten im Schatten.",
        "approved",
        ("marktplatz", "hauptstrasse"),
    ),
    (
        "fraktion-bunte-liste",
        "rat",
        "Livestream der Ratssitzungen",
        "Öffentliche Sitzungen des Rates werden ab dem nächsten Jahr im Internet übertragen und als Video "
        "bereitgestellt.",
        "Transparenz stärkt das Vertrauen in die Kommunalpolitik.",
        "approved",
        (),
    ),
    (
        "fraktion-bunte-liste",
        "soziales",
        "Kostenlose Menstruationsartikel in öffentlichen Gebäuden",
        "In Schulen, Rathaus und Bibliothek werden kostenlose Menstruationsartikel bereitgestellt.",
        "Periodenarmut betrifft viele junge Menschen.",
        "approved",
        ("rathaus", "stadtbibliothek"),
    ),
    (
        "fraktion-bunte-liste",
        "jugend",
        "Jugendbeteiligung bei der Spielplatzplanung",
        "Kinder und Jugendliche werden bei der Planung jedes neuen Spielplatzes beteiligt.",
        "Wer einen Spielplatz nutzt, weiß am besten, was er braucht.",
        "approved",
        ("stadtpark",),
    ),
    (
        "fraktion-bunte-liste",
        "bau-rat",
        "Tempo 30 auf der gesamten Hauptstraße",
        "Die Verwaltung beantragt bei der Straßenverkehrsbehörde Tempo 30 auf der gesamten Hauptstraße.",
        "Lärm und Unfallgefahr würden deutlich sinken.",
        "rejected",
        ("hauptstrasse",),
    ),
    (
        "fraktion-bunte-liste",
        "umwelt-rat",
        "Klimacheck für alle Beschlussvorlagen",
        "Jede Beschlussvorlage enthält künftig eine Einschätzung der Auswirkungen auf das Klima.",
        "Klimafolgen sollen bei Entscheidungen sichtbar sein.",
        "approved",
        (),
    ),
    (
        "fraktion-zukunft",
        "wirtschaft",
        "Wirtschaftsförderung: Gründerzentrum am Bahnhof",
        "Die Verwaltung prüft die Einrichtung eines Gründerzentrums im ehemaligen Güterbahnhof.",
        "Junge Unternehmen brauchen günstige Räume und Beratung.",
        "approved",
        ("bahnhof",),
    ),
    (
        "fraktion-zukunft",
        "haushalt",
        "Senkung der Gewerbesteuer um zehn Hebesatzpunkte",
        "Der Hebesatz der Gewerbesteuer wird ab dem nächsten Haushaltsjahr um zehn Punkte gesenkt.",
        "Die Stadt soll für Betriebe attraktiver werden.",
        "rejected",
        (),
    ),
    (
        "fraktion-zukunft",
        "bau",
        "Mehr Parkplätze am Bahnhof",
        "Die Verwaltung prüft den Bau eines Parkdecks mit 150 Stellplätzen am Bahnhof.",
        "Pendlerinnen und Pendler finden morgens keinen Parkplatz.",
        "approved",
        ("bahnhof",),
    ),
    (
        "fraktion-zukunft",
        "haupt",
        "Sicherheit in der Altstadt: Ordnungsdienst am Wochenende",
        "Der kommunale Ordnungsdienst wird freitags und samstags bis 2 Uhr eingesetzt.",
        "Anwohnende fühlen sich nachts unsicher.",
        "approved",
        ("altstadt",),
    ),
    (
        "fraktion-zukunft",
        "schule",
        "Sanierungsprogramm für Schultoiletten",
        "Die Verwaltung legt ein Programm zur Sanierung aller Schultoiletten bis Ende nächsten Jahres vor.",
        "Viele Kinder meiden die Toiletten in der Schule.",
        "approved",
        ("gymnasium", "grundschule-sued"),
    ),
    (
        "fraktion-zukunft",
        "finanzen",
        "Prüfung von Einsparungen bei Dienstfahrzeugen",
        "Die Verwaltung prüft ein gemeinsames Fahrzeugpool-Modell für alle Ämter.",
        "Fahrzeuge stehen oft ungenutzt.",
        "withdrawn",
        (),
    ),
    (
        "fraktion-buergerforum",
        "bau",
        "Sanierung der Gehwege im Ortsteil Oberheide",
        "Die Gehwege in Oberheide werden in das Sanierungsprogramm aufgenommen.",
        "Viele Gehwege sind uneben und für Rollatoren gefährlich.",
        "approved",
        ("dorfplatz",),
    ),
    (
        "fraktion-buergerforum",
        "rat",
        "Bürgerhaushalt einführen",
        "Ab dem übernächsten Haushalt können Bürgerinnen und Bürger über 100.000 Euro mitentscheiden.",
        "Beteiligung schafft Akzeptanz.",
        "approved",
        (),
    ),
    (
        "fraktion-buergerforum",
        "kultur",
        "Öffentliche Toilette am Stadtpark",
        "Die Verwaltung errichtet eine barrierefreie öffentliche Toilette am Stadtpark.",
        "Besucherinnen und Besucher des Parks finden keine Toilette.",
        "approved",
        ("stadtpark",),
    ),
    (
        "fraktion-buergerforum",
        "soziales",
        "Seniorentaxi für mobilitätseingeschränkte Menschen",
        "Ältere Menschen erhalten vergünstigte Taxifahrten zu Arztterminen.",
        "In Oberheide gibt es keinen Arzt und kaum Busse.",
        "deferred",
        ("dorfplatz",),
    ),
    (
        "fraktion-buergerforum",
        "umwelt",
        "Hundekotbeutel-Spender in allen Parks",
        "In allen öffentlichen Parks werden Spender und Abfallbehälter aufgestellt.",
        "Sauberkeit in den Grünanlagen.",
        "approved",
        ("stadtpark", "mühlenteich"),
    ),
    (
        "fraktion-neue-mitte",
        "rat",
        "Digitales Bürgerbüro mit Terminbuchung",
        "Alle Dienstleistungen des Bürgerbüros sind online buchbar, Wartezeiten werden angezeigt.",
        "Lange Wartezeiten ärgern die Menschen.",
        "approved",
        ("rathaus",),
    ),
    (
        "fraktion-neue-mitte",
        "wirtschaft",
        "Kostenloses WLAN in der Innenstadt",
        "Die Stadtwerke bauen ein frei zugängliches WLAN in der Fußgängerzone auf.",
        "Ein attraktives Zentrum braucht digitale Angebote.",
        "approved",
        ("hauptstrasse", "marktplatz"),
    ),
    (
        "fraktion-neue-mitte",
        "haushalt",
        "Haushaltssicherung: externe Organisationsuntersuchung",
        "Eine externe Untersuchung soll Einsparpotenziale in der Verwaltung aufzeigen.",
        "Das strukturelle Defizit muss abgebaut werden.",
        "rejected",
        (),
    ),
    (
        "fraktion-neue-mitte",
        "schule",
        "Schwimmfähigkeit: Schwimmunterricht für alle Drittklässler",
        "Die Stadt stellt ausreichend Wasserzeiten bereit, damit jedes Kind in der dritten Klasse schwimmen lernt.",
        "Immer weniger Kinder können sicher schwimmen.",
        "approved",
        ("hallenbad",),
    ),
    (
        "fraktion-gfm",
        "bau",
        "Zebrastreifen an der Westfalenallee",
        "Auf Höhe der Bushaltestelle Westfalenallee wird ein Fußgängerüberweg eingerichtet.",
        "Viele Schulkinder queren hier die Straße.",
        "approved",
        ("westfalenallee",),
    ),
    (
        "fraktion-gfm",
        "kultur",
        "Heimatpreis der Stadt Musterstadt",
        "Die Stadt lobt jährlich einen Heimatpreis für ehrenamtliches Engagement aus.",
        "Ehrenamt verdient Anerkennung.",
        "approved",
        (),
    ),
    (
        "fraktion-gfm",
        "umwelt",
        "Blühstreifen an Feldwegen",
        "Die Stadt legt auf eigenen Flächen an Feldwegen Blühstreifen an.",
        "Insekten brauchen Nahrung.",
        "approved",
        ("waldweg",),
    ),
    (
        "gruppe-ul",
        "rat",
        "Verkleinerung des Rates zur nächsten Wahl",
        "Die Verwaltung prüft, wie der Rat zur nächsten Kommunalwahl verkleinert werden kann.",
        "Ein kleinerer Rat spart Kosten.",
        "rejected",
        (),
    ),
    (
        "gruppe-ul",
        "bau",
        "Ampelschaltung Nordring optimieren",
        "Die Ampeln am Nordring werden zu einer grünen Welle koordiniert.",
        "Staus und Abgase würden reduziert.",
        "approved",
        ("nordring",),
    ),
    (
        "fraktion-bunte-liste",
        "jugend",
        "Jugendzentrum: längere Öffnungszeiten am Wochenende",
        "Das Jugendzentrum Alte Post öffnet freitags und samstags bis 22 Uhr.",
        "Jugendliche brauchen sichere Treffpunkte am Abend.",
        "approved",
        ("jugendzentrum",),
    ),
    (
        "fraktion-bunte-liste",
        "soziales-rat",
        "Sozialticket für Bus und Bahn",
        "Menschen mit geringem Einkommen erhalten ein vergünstigtes Monatsticket.",
        "Mobilität ist Voraussetzung für Teilhabe.",
        "approved",
        ("busbahnhof",),
    ),
    (
        "fraktion-zukunft",
        "planung",
        "Bauland für junge Familien",
        "Die Verwaltung entwickelt ein Baulandmodell mit Vergabe nach sozialen Kriterien.",
        "Junge Familien ziehen weg, weil Bauland fehlt.",
        "approved",
        ("wohngebiet-sued",),
    ),
    (
        "fraktion-buergerforum",
        "schule",
        "Hitzeschutz in Klassenräumen",
        "Die Verwaltung prüft außenliegenden Sonnenschutz für alle Klassenräume nach Süden.",
        "Im Sommer wird es in vielen Klassen über 30 Grad warm.",
        "approved",
        ("gymnasium", "grundschule-nord"),
    ),
    (
        "fraktion-neue-mitte",
        "bau",
        "Baustellenkoordination und -information",
        "Baustellen werden in einer öffentlichen Karte mit Dauer und Umleitungen angezeigt.",
        "Viele Baustellen überraschen die Menschen.",
        "approved",
        ("hauptstrasse",),
    ),
)

# Anfragen: (Fraktion, Gremium, Thema)
_ANFRAGEN: tuple[tuple[str, str, str], ...] = (
    ("fraktion-bunte-liste", "bauausschuss", "Zustand der Radwege im Stadtgebiet"),
    ("fraktion-bunte-liste", "umweltausschuss", "Baumfällungen im vergangenen Jahr"),
    ("fraktion-bunte-liste", "jugendhilfeausschuss", "Wartelisten in den Kitas"),
    ("fraktion-bunte-liste", "sozialausschuss", "Wohnungslosigkeit in Musterstadt"),
    ("fraktion-bunte-liste", "hauptausschuss", "Barrierefreiheit der Internetseite der Stadt"),
    ("fraktion-bunte-liste", "schulausschuss", "Unterrichtsausfall durch Lehrkräftemangel"),
    ("fraktion-zukunft", "hauptausschuss", "Wartezeiten im Bürgerbüro"),
    ("fraktion-zukunft", "finanzausschuss", "Entwicklung der Gewerbesteuereinnahmen"),
    ("fraktion-zukunft", "bauausschuss", "Dauer der Baugenehmigungsverfahren"),
    ("fraktion-zukunft", "stadtentwicklungsausschuss", "Leerstand von Gewerbeflächen"),
    ("fraktion-zukunft", "hauptausschuss", "Krankenstand in der Stadtverwaltung"),
    ("fraktion-buergerforum", "bauausschuss", "Schlaglöcher nach dem Winter"),
    ("fraktion-buergerforum", "kulturausschuss", "Besucherzahlen des Stadtmuseums"),
    ("fraktion-buergerforum", "sozialausschuss", "Ärztliche Versorgung im Ortsteil Oberheide"),
    ("fraktion-buergerforum", "umweltausschuss", "Wilder Müll an Glascontainern"),
    ("fraktion-buergerforum", "hauptausschuss", "Kosten externer Gutachten"),
    ("fraktion-neue-mitte", "finanzausschuss", "Zinsentwicklung der städtischen Kredite"),
    ("fraktion-neue-mitte", "stadtentwicklungsausschuss", "Glasfaserausbau in den Ortsteilen"),
    ("fraktion-neue-mitte", "schulausschuss", "Ausstattung der Schulen mit Tablets"),
    ("fraktion-neue-mitte", "hauptausschuss", "Stand der Online-Dienste nach dem Onlinezugangsgesetz"),
    ("fraktion-gfm", "bauausschuss", "Beleuchtung von Fußwegen in Oberheide"),
    ("fraktion-gfm", "kulturausschuss", "Nutzung der Stadthalle durch Vereine"),
    ("fraktion-gfm", "umweltausschuss", "Bekämpfung des Eichenprozessionsspinners"),
    ("gruppe-ul", "finanzausschuss", "Kosten der Ratsarbeit"),
    ("gruppe-ul", "bauausschuss", "Unfallschwerpunkte im Stadtgebiet"),
    ("fraktion-bunte-liste", "kulturausschuss", "Förderung freier Kulturinitiativen"),
    ("fraktion-zukunft", "jugendhilfeausschuss", "Personalsituation in den städtischen Kitas"),
    ("fraktion-buergerforum", "schulausschuss", "Busverbindungen zu den weiterführenden Schulen"),
    ("fraktion-neue-mitte", "sozialausschuss", "Pflegeplätze in Musterstadt"),
    ("fraktion-bunte-liste", "umweltausschuss", "Fortschritt beim Ausbau der Photovoltaik"),
    ("fraktion-zukunft", "bauausschuss", "Sanierungsstau bei städtischen Brücken"),
    ("fraktion-gfm", "sozialausschuss", "Angebote für pflegende Angehörige"),
)

# Mitteilungen: (Amt, Gremium, Titel, Inhalt)
_MITTEILUNGEN: tuple[tuple[str, str, str, str], ...] = (
    (
        "amt-20",
        "finanzausschuss",
        "Haushaltsbericht zum ersten Quartal",
        "Die Erträge liegen 1,2 Prozent über dem Plan, die Aufwendungen im Rahmen.",
    ),
    (
        "amt-20",
        "finanzausschuss",
        "Haushaltsbericht zum zweiten Quartal",
        "Die Gewerbesteuer entwickelt sich besser als erwartet; Personalkosten steigen durch den Tarifabschluss.",
    ),
    (
        "amt-20",
        "finanzausschuss",
        "Haushaltsbericht zum dritten Quartal",
        "Zum Jahresende wird ein um 400.000 Euro besseres Ergebnis erwartet.",
    ),
    (
        "amt-61",
        "stadtentwicklungsausschuss",
        "Sachstand Bahnhofsquartier",
        "Der städtebauliche Wettbewerb ist entschieden, die Bürgerbeteiligung beginnt im Frühjahr.",
    ),
    (
        "amt-66",
        "bauausschuss",
        "Baustellenübersicht für das laufende Jahr",
        "Im Stadtgebiet sind 34 Baumaßnahmen geplant, davon zehn mit Vollsperrung.",
    ),
    (
        "amt-66",
        "bauausschuss",
        "Bericht zur Straßenzustandserfassung",
        "Ein Viertel der Straßen ist in schlechtem Zustand; der Sanierungsbedarf liegt bei 18 Mio. Euro.",
    ),
    (
        "amt-67",
        "umweltausschuss",
        "Treibhausgasbilanz der Stadt",
        "Die Emissionen sind seit 1990 um 38 Prozent gesunken, im Verkehr kaum.",
    ),
    (
        "amt-67",
        "umweltausschuss",
        "Bericht zur Luftqualität am Nordring",
        "Die Grenzwerte für Stickstoffdioxid wurden im vergangenen Jahr eingehalten.",
    ),
    (
        "amt-40",
        "schulausschuss",
        "Anmeldezahlen an den weiterführenden Schulen",
        "Die Gesamtschule musste 31 Kinder abweisen, das Gymnasium hat freie Plätze.",
    ),
    (
        "amt-40",
        "schulausschuss",
        "Bericht zur Offenen Ganztagsschule",
        "An allen Grundschulen nehmen inzwischen mehr als 70 Prozent der Kinder am Ganztag teil.",
    ),
    (
        "amt-51",
        "jugendhilfeausschuss",
        "Bericht der Jugendgerichtshilfe",
        "Die Zahl der Verfahren ist leicht gesunken, Präventionsprojekte zeigen Wirkung.",
    ),
    (
        "amt-51",
        "jugendhilfeausschuss",
        "Sachstand Kita-Ausbau",
        "Die Kita am Südhang wird im Herbst fertig; zwei Gruppen starten vorzeitig in einem Provisorium.",
    ),
    (
        "amt-50",
        "sozialausschuss",
        "Bericht zur Unterbringung geflüchteter Menschen",
        "In städtischen Unterkünften leben derzeit 412 Menschen, die Belegungsquote liegt bei 87 Prozent.",
    ),
    (
        "amt-50",
        "sozialausschuss",
        "Armutsbericht Musterstadt",
        "Jedes fünfte Kind lebt in einem Haushalt mit Sozialleistungen.",
    ),
    (
        "amt-41",
        "kulturausschuss",
        "Jahresbericht der Stadtbibliothek",
        "Die Ausleihen sind um zwölf Prozent gestiegen, E-Medien machen ein Drittel aus.",
    ),
    (
        "amt-41",
        "kulturausschuss",
        "Bilanz des Stadtfestes",
        "Rund 25.000 Besucherinnen und Besucher kamen; es gab keine größeren Zwischenfälle.",
    ),
    (
        "amt-10",
        "hauptausschuss",
        "Bericht zur Personalentwicklung",
        "38 Stellen sind unbesetzt, vor allem in technischen Berufen und in den Kitas.",
    ),
    (
        "amt-10",
        "hauptausschuss",
        "Sachstand Digitalisierung der Verwaltung",
        "Inzwischen sind 64 Verwaltungsleistungen online verfügbar.",
    ),
    (
        "amt-32",
        "hauptausschuss",
        "Bericht des kommunalen Ordnungsdienstes",
        "Die meisten Einsätze betrafen Lärm, Müll und Parkverstöße.",
    ),
    (
        "amt-37",
        "hauptausschuss",
        "Jahresbericht der Feuerwehr",
        "Die Feuerwehr rückte 1.180-mal aus; die Hilfsfrist wurde in 84 Prozent der Fälle eingehalten.",
    ),
    (
        "amt-80",
        "stadtentwicklungsausschuss",
        "Bericht der Wirtschaftsförderung",
        "17 Betriebe haben sich neu angesiedelt, 230 Arbeitsplätze sind entstanden.",
    ),
    (
        "amt-65",
        "bauausschuss",
        "Energiebericht der städtischen Gebäude",
        "Der Wärmeverbrauch ist um neun Prozent gesunken, der Stromverbrauch um vier Prozent.",
    ),
    (
        "amt-14",
        "rpa",
        "Bericht über die laufenden Prüfungen",
        "Das Rechnungsprüfungsamt hat 18 Prüfungen abgeschlossen; sieben laufen noch.",
    ),
    (
        "amt-66",
        "bauausschuss",
        "Sachstand Hauptstraße",
        "Der erste Bauabschnitt ist abgeschlossen. Die Umleitung über den Nordring bleibt bis Herbst.",
    ),
)


def _vorgaenge_roh() -> list[Vorgang]:
    """Alle Vorgänge des Bestands, noch ohne Datum und Nummer (das setzt ``stadtplan``)."""
    roh: list[Vorgang] = []

    def neu(**kwargs: object) -> None:
        roh.append(Vorgang(key=f"{BESTAND}{len(roh) + 1:03d}", tag=0, **kwargs))  # type: ignore[arg-type]

    for weg, amt, titel, anlass, beschluss, kosten, orte in _VORLAGEN:
        sachverhalt = [anlass]
        if kosten:
            sachverhalt.append(f"Die Kosten betragen voraussichtlich {_eur(kosten)}.")
        art = "satzung" if "satzung" in titel.lower() or "Satzung" in beschluss else "vorlage"
        neu(
            art=art,
            titel=titel,
            urheber=amt,
            weg=WEGE[weg],
            sachverhalt=tuple(sachverhalt),
            beschluss=beschluss,
            kosten=kosten,
            orte=orte,
            ergebnis="noted" if "zur Kenntnis" in beschluss else "approved",
        )
    for nummer, name, ort, stufe in _BPLAENE:
        bplan_weg = WEGE["planung"] if stufe == "Satzungsbeschluss" else WEGE["planung-ausschuss"]
        neu(
            art="satzung" if stufe == "Satzungsbeschluss" else "vorlage",
            titel=f"Bebauungsplan Nr. {nummer} „{name}“ – {stufe}",
            urheber="amt-61",
            weg=bplan_weg,
            sachverhalt=(
                f"Für den Bereich „{name}“ soll Planungsrecht geschaffen werden.",
                f"Verfahrensschritt: {stufe}. Die Stellungnahmen der Öffentlichkeit und der Behörden "
                "sind in der Abwägungstabelle zusammengefasst.",
            ),
            beschluss=f"Der {GREMIUM[bplan_weg[-1]].name} beschließt: {stufe} für den Bebauungsplan Nr. {nummer}.",
            orte=(ort,),
        )
    for weg, amt, empfaenger, zweck, betrag, ort in _ZUSCHUESSE:
        neu(
            art="vorlage",
            titel=f"Zuschuss an {empfaenger} für {zweck}",
            urheber=amt,
            weg=WEGE[weg],
            sachverhalt=(
                f"{empfaenger} beantragt einen Zuschuss für {zweck}.",
                f"Die Gesamtkosten betragen {_eur(betrag * 2)}, beantragt sind {_eur(betrag)}.",
            ),
            beschluss=f"Der Ausschuss gewährt einen Zuschuss in Höhe von {_eur(betrag)}.",
            kosten=betrag,
            orte=(ort,),
        )
    for i, (fraktion, weg, titel, beschluss, begruendung, ergebnis, orte) in enumerate(_ANTRAEGE):
        f = FRAKTION[fraktion]
        neu(
            art="antrag",
            titel=f"Antrag der {f.name.replace(' (Demo)', '')}: {titel}",
            urheber=fraktion,
            weg=WEGE[weg],
            sachverhalt=(f"Begründung: {begruendung}",),
            beschluss=beschluss,
            orte=orte,
            ergebnis="approved" if ergebnis == "deferred" else ergebnis,
            vertagt=ergebnis == "deferred" or i % 9 == 4,
        )
    for fraktion, gremium, thema in _ANFRAGEN:
        f = FRAKTION[fraktion]
        neu(
            art="anfrage",
            titel=f"Anfrage der {f.name.replace(' (Demo)', '')}: {thema}",
            urheber=fraktion,
            weg=(gremium,),
            sachverhalt=(
                f"Die {f.art} bittet um Auskunft zum Thema „{thema}“.",
                "1. Wie stellt sich die aktuelle Lage dar?",
                "2. Welche Maßnahmen plant die Verwaltung, und bis wann?",
                "3. Welche Kosten sind damit verbunden?",
            ),
            beschluss="Die Antwort der Verwaltung wird zur Kenntnis genommen.",
            ergebnis="noted",
        )
    for amt, gremium, titel, inhalt in _MITTEILUNGEN:
        neu(
            art="mitteilung",
            titel=f"Mitteilung: {titel}",
            urheber=amt,
            weg=(gremium,),
            sachverhalt=(inhalt, "Die Verwaltung erläutert den Bericht in der Sitzung."),
            beschluss="Der Ausschuss nimmt die Mitteilung zur Kenntnis.",
            ergebnis="noted",
        )
    return roh


# ---------------------------------------------------------------------------
# Plan: Sitzungen, Tagesordnungen, Beratungen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Top:
    nummer: str
    reihenfolge: int
    titel: str
    vorgang: str | None = None  # Schlüssel des Vorgangs
    oeffentlich: bool = True
    rolle: str = ""  # Vorberatung, Entscheidung, Kenntnisnahme
    ergebnis: str = ""  # leer = offen; approved, rejected, deferred, withdrawn, noted, recommended
    ergebnis_text: str = ""
    stimmen: tuple[int, int, int] | None = None  # ja, nein, Enthaltungen


@dataclass(frozen=True)
class Sitzung:
    key: str  # b-<gremium>-m028 / b-<gremium>-p056 oder Schlüssel der Basisdemo (rat-3 …)
    gremium: str
    tag: int
    tops: tuple[Top, ...]
    basis: bool = False  # Sitzung der Basisdemo: Sie steht schon in Insight, hier kommen nur TOPs dazu
    name: str = ""  # „12. Sitzung des Hauptausschusses“ (eindeutig je Gremium); leer bei der Basisdemo

    @property
    def vergangen(self) -> bool:
        return self.tag < 0


@dataclass(frozen=True)
class Mitgliedschaft:
    person: str
    gremium: str
    rolle: str  # Mitglied, Vorsitzende/r, Stellv. Vorsitzende/r, Sachkundige/r Bürger/in
    session_rolle: str  # chair, deputy_chair, member


@dataclass(frozen=True)
class Stadtplan:
    aufbautag: date
    umfang: str
    vorgaenge: tuple[Vorgang, ...]
    sitzungen: tuple[Sitzung, ...]
    besetzung: tuple[Mitgliedschaft, ...]  # nur Bestand (ohne BASIS_BESETZUNG, ohne Rat und Fraktionen)
    vorgang: dict[str, Vorgang] = field(default_factory=dict)
    #: Umsetzungsstand je entschiedenem Vorgang (Insight: implementation_status)
    umsetzung: dict[str, str] = field(default_factory=dict)

    def datum(self, tag: int) -> date:
        return self.aufbautag + timedelta(days=tag)


#: Sitzungen der Basisdemo (setup_demo_environment, ``sitzungen_def``): Schlüssel, Gremium, Tag, belegte TOPs
BASIS_SITZUNGEN: tuple[tuple[str, str, int, int], ...] = (
    ("rat-1", "rat", -84, 4),
    ("rat-2", "rat", -28, 4),
    ("rat-3", "rat", 14, 4),
    ("haupt-1", "hauptausschuss", -42, 3),
    ("haupt-2", "hauptausschuss", 7, 3),
    ("bau-1", "bauausschuss", -35, 3),
)
#: Abstand, in dem neben einer Sitzung der Basisdemo keine Bestandssitzung desselben Gremiums liegt
BASIS_ABSTAND = 14
#: Gleiche Sitzung im Session-Mandanten der Basisdemo (Name dort); andere Basissitzungen gibt es nur in Insight
BASIS_SITZUNG_SESSION: dict[str, str] = {
    "rat-3": "Ratssitzung (Demo, kommend)",
    "haupt-2": "Hauptausschuss (Demo, kommend)",
}
#: Laufende Nummern der Sitzungen beginnen so, als hätte die Wahlperiode schon einige Sitzungen gehabt
SITZUNGSNUMMER_START = 4

_NOE_TITEL = (
    "Grundstücksangelegenheit: Ankauf einer Teilfläche",
    "Vergabe von Bauleistungen",
    "Personalangelegenheit",
    "Stundung von Forderungen",
    "Vertragsangelegenheit Stadtwerke",
    "Mietvertrag für eine Unterkunft",
)


def _haeufigkeit(seed: int, *, ja: int, nein: int) -> bool:
    """Deterministischer Zufall: wahr mit Wahrscheinlichkeit ja/(ja+nein)."""
    return seed % (ja + nein) < ja


def _stimmen(gremium: Gremium, ergebnis: str, seed: int) -> tuple[int, int, int] | None:
    if ergebnis not in ("approved", "rejected", "recommended"):
        return None
    sitze = gremium.sitze + (1 if gremium.key == "rat" else 0)
    fehlend = seed % 4
    anwesend = sitze - fehlend
    if ergebnis == "rejected":
        ja = anwesend // 2 - 1 - seed % 5
        enth = seed % 3
        return (max(ja, 1), anwesend - max(ja, 1) - enth, enth)
    if _haeufigkeit(seed, ja=2, nein=3):
        return (anwesend, 0, 0)  # einstimmig
    nein = 1 + seed % (anwesend // 3)
    enth = (seed // 7) % 3
    return (anwesend - nein - enth, nein, enth)


def _sitzungstage(umfang: str) -> list[tuple[str, int]]:
    """(Gremium, Tag) aller Bestandssitzungen, ohne Konflikte mit der Basisdemo."""
    von, bis = (VON_TAG, BIS_TAG) if umfang == "voll" else (KLEIN_VON_TAG, KLEIN_BIS_TAG)
    basis = [(g, t) for _k, g, t, _n in BASIS_SITZUNGEN]
    tage: list[tuple[str, int]] = []
    for turnus in range(-10, 4):
        rat_tag = RAT_ANKER + turnus * RAT_TAKT
        for gremium in GREMIEN:
            if gremium.nur_jeder_zweite_turnus and turnus % 2:
                continue
            tag = rat_tag + gremium.versatz
            if not von <= tag <= bis or -1 <= tag <= 1:  # nie am Aufbautag selbst (vergangen oder kommend?)
                continue
            if any(g == gremium.key and abs(t - tag) <= BASIS_ABSTAND for g, t in basis):
                continue
            tage.append((gremium.key, tag))
    return sorted(tage, key=lambda gt: (gt[1], gt[0]))


def _besetzung() -> tuple[Mitgliedschaft, ...]:
    """Ausschüsse nach d'Hondt besetzen; die Besetzung der Basisdemo zählt mit, wird aber nicht wiederholt."""
    rat_sitze = {f.key: sum(1 for p in PERSONEN if p.fraktion == f.key and p.rat) for f in FRAKTIONEN}
    ergebnis: list[Mitgliedschaft] = []
    belegt_je_person: dict[str, int] = {}
    for gremium in GREMIEN:
        if gremium.key == "rat":
            continue
        # d'Hondt über die Ratssitze der Fraktionen und Gruppen
        quoten: list[tuple[float, str]] = []
        for fraktion, sitze in rat_sitze.items():
            quoten.extend((sitze / teiler, fraktion) for teiler in range(1, gremium.sitze + 1))
        quoten.sort(key=lambda q: (-q[0], q[1]))
        anteile: dict[str, int] = {}
        for _q, fraktion in quoten[: gremium.sitze]:
            anteile[fraktion] = anteile.get(fraktion, 0) + 1
        basis = [(p, r) for p, g, r in BASIS_BESETZUNG if g == gremium.key]
        for p, _r in basis:
            fraktion = PERSON[p].fraktion or ""
            anteile[fraktion] = anteile.get(fraktion, 0) - 1
            belegt_je_person[p] = belegt_je_person.get(p, 0) + 1
        hat_vorsitz = any(r.startswith("Vorsitz") for _p, r in basis)
        neue: list[str] = []
        for fraktion, anzahl in sorted(anteile.items()):
            # Wer bisher in den wenigsten Ausschüssen sitzt, kommt zuerst; sachkundige Bürger nach den Ratsmitgliedern
            kandidaten = sorted(
                (p for p in PERSONEN if p.fraktion == fraktion and p.key not in {b for b, _ in basis}),
                key=lambda p: (belegt_je_person.get(p.key, 0) + (0 if p.rat else 2), stabil(gremium.key, p.key)),
            )
            for person in kandidaten[: max(anzahl, 0)]:
                neue.append(person.key)
                belegt_je_person[person.key] = belegt_je_person.get(person.key, 0) + 1
        for i, key in enumerate(sorted(neue, key=lambda k: stabil("vorsitz", gremium.key, k))):
            person = PERSON[key]
            weiblich = person.geschlecht == "female"
            if not hat_vorsitz and i == 0 and person.rat:
                rolle, srolle = ("Vorsitzende" if weiblich else "Vorsitzender"), "chair"
            elif not hat_vorsitz and i == 1 and person.rat:
                rolle, srolle = ("Stellv. Vorsitzende" if weiblich else "Stellv. Vorsitzender"), "deputy_chair"
            elif not person.rat:
                rolle, srolle = ("Sachkundige Bürgerin" if weiblich else "Sachkundiger Bürger"), "member"
            else:
                rolle, srolle = "Mitglied", "member"
            ergebnis.append(Mitgliedschaft(key, gremium.key, rolle, srolle))
    return tuple(ergebnis)


def _vorgangsdaten(roh: list[Vorgang], umfang: str) -> list[Vorgang]:
    """Datum je Vorgang verteilen (gleichmäßig über das Jahr, je Art versetzt) und im Umfang ``klein`` ausdünnen."""
    # Reihenfolge mischen, damit nicht alle Haushaltsvorlagen im selben Monat liegen
    verteilt = sorted(roh, key=lambda v: stabil("reihenfolge", v.key))
    spanne = (BIS_TAG - 30) - (VON_TAG + 20)
    ergebnis = []
    for i, vorgang in enumerate(verteilt):
        tag = VON_TAG + 20 + (i * spanne) // len(verteilt)
        if umfang == "klein" and not (KLEIN_VON_TAG - 30 <= tag <= KLEIN_BIS_TAG - 30 and i % 3 == 0):
            continue
        ergebnis.append(replace(vorgang, tag=tag))
    return ergebnis


def _nummern(vorgaenge: list[Vorgang], aufbautag: date) -> list[Vorgang]:
    """Drucksachennummern je Präfix und Jahr in Datumsfolge: V/2026/0101 (die Basisdemo nutzt D-001 …)."""
    zaehler: dict[tuple[str, int], int] = {}
    ergebnis = []
    for vorgang in sorted(vorgaenge, key=lambda v: (v.tag, v.key)):
        jahr = (aufbautag + timedelta(days=vorgang.tag)).year
        praefix = ARTEN[vorgang.art][1]
        lfd = zaehler.get((praefix, jahr), 100) + 1
        zaehler[(praefix, jahr)] = lfd
        ergebnis.append(replace(vorgang, nummer=f"{praefix}/{jahr}/{lfd:04d}"))
    return ergebnis


def _umsetzung(tag: int, seed: int) -> str:
    if tag < -180:
        return "done" if seed % 6 else "in_progress"
    if tag < -60:
        return ("done", "in_progress", "in_progress", "open", "deferred")[seed % 5]
    return "open" if seed % 3 else "in_progress"


#: Feste Tagesordnungspunkte am Anfang jeder Bestandssitzung
STANDARD_TOPS = (
    "Eröffnung, Feststellung der Ordnungsmäßigkeit und der Tagesordnung",
    "Genehmigung der Niederschrift über die letzte Sitzung",
    "Mitteilungen der Verwaltung",
)


def _anhaengen(tops: list[Top], belegt: int, titel: str, **felder: Any) -> None:
    """Öffentlichen TOP fortlaufend nummeriert anhängen (in Basissitzungen nach deren ``belegt`` TOPs)."""
    nummer = belegt + len(tops) + 1
    tops.append(Top(nummer=str(nummer), reihenfolge=nummer, titel=titel, **felder))


@lru_cache(maxsize=4)
def stadtplan(aufbautag: date, umfang: str = "voll") -> Stadtplan:
    """
    Den Bestand der Musterstadt für einen Aufbautag berechnen: Vorgänge mit Datum und Nummer, Sitzungen mit
    Tagesordnung und Ergebnissen, Ausschussbesetzung. Deterministisch – derselbe Tag liefert denselben Plan.

    Eine Sitzung liegt in der Vergangenheit, wenn ihr Tag negativ ist. Vergangene Beratungen haben ein Ergebnis,
    kommende sind offen. Ein Vorgang kommt frühestens ``LADUNGSFRIST`` Tage nach seinem Datum in die erste Sitzung
    seines Beratungswegs, jede weitere Station in die nächste Sitzung des nächsten Gremiums.
    """
    if umfang not in UMFAENGE:
        raise ValueError(f"Unbekannter Umfang {umfang!r}, erlaubt: {', '.join(UMFAENGE)}")
    vorgaenge = _nummern(_vorgangsdaten(_vorgaenge_roh(), umfang), aufbautag)

    # Sitzungen: Bestand plus die der Basisdemo (dort kommen TOPs dazu, die Sitzung selbst steht schon)
    termine: list[tuple[str, str, int, bool, int]] = [
        (f"{BESTAND}{g}-{'m' if t < 0 else 'p'}{abs(t):03d}", g, t, False, 0) for g, t in _sitzungstage(umfang)
    ]
    termine += [(k, g, t, True, belegt) for k, g, t, belegt in BASIS_SITZUNGEN]
    termine.sort(key=lambda s: (s[2], s[1]))
    je_gremium: dict[str, list[tuple[str, int]]] = {}
    for key, gremium_key, tag, _basis, _belegt in termine:
        je_gremium.setdefault(gremium_key, []).append((key, tag))

    # Beratungen den Sitzungen zuordnen
    beratungen: dict[str, list[tuple[Vorgang, str, str, int]]] = {}  # Sitzung → (Vorgang, Rolle, Ergebnis, Stufe)
    umsetzung: dict[str, str] = {}
    for vorgang in vorgaenge:
        frueheste = vorgang.tag + LADUNGSFRIST
        for stufe, gremium_key in enumerate(vorgang.weg):
            letzte = stufe == len(vorgang.weg) - 1
            kandidaten = [(k, t) for k, t in je_gremium.get(gremium_key, []) if t >= frueheste]
            if not kandidaten:
                break  # noch nicht terminiert: Vorgang in Vorbereitung
            sitzung, tag = kandidaten[0]
            if vorgang.art in ("anfrage", "mitteilung"):
                rolle, ergebnis = "Kenntnisnahme", "noted"
            elif letzte:
                rolle, ergebnis = "Entscheidung", vorgang.ergebnis
            else:
                rolle, ergebnis = "Vorberatung", "recommended"
            # Vertagt wird nur in einer vergangenen Sitzung; liegt die erste schon in der Zukunft, ist sie einfach offen
            if letzte and vorgang.vertagt and rolle == "Entscheidung" and tag < 0:
                beratungen.setdefault(sitzung, []).append((vorgang, rolle, "deferred" if tag < 0 else "", stufe))
                weiter = [(k, t) for k, t in kandidaten[1:]]
                if not weiter:
                    break
                sitzung, tag = weiter[0]
            if vorgang.ergebnis == "rejected" and rolle == "Vorberatung":
                ergebnis = "rejected"  # schon im Ausschuss ohne Mehrheit, der Rat entscheidet trotzdem
            beratungen.setdefault(sitzung, []).append((vorgang, rolle, ergebnis if tag < 0 else "", stufe))
            if letzte and tag < 0 and ergebnis == "approved":
                umsetzung[vorgang.key] = _umsetzung(tag, stabil("umsetzung", vorgang.key))
            frueheste = tag + 3

    sitzungen: list[Sitzung] = []
    laufend: dict[str, int] = {}
    for key, gremium_key, tag, basis, belegt in termine:
        gremium = GREMIUM[gremium_key]
        laufend[gremium_key] = laufend.get(gremium_key, SITZUNGSNUMMER_START) + 1
        name = "" if basis else f"{laufend[gremium_key]}. Sitzung {gremium.genitiv}"
        tops: list[Top] = []
        if not basis:
            for titel in STANDARD_TOPS:
                _anhaengen(tops, belegt, titel)
        eintraege = sorted(beratungen.get(key, []), key=lambda e: (ARTEN[e[0].art][1] != "V", e[0].nummer))
        for vorgang, rolle, ergebnis, _stufe in eintraege:
            seed = stabil("top", key, vorgang.key)
            titel = vorgang.titel + (" (Vorberatung)" if rolle == "Vorberatung" else "")
            _anhaengen(
                tops,
                belegt,
                titel,
                vorgang=vorgang.key,
                rolle=rolle,
                ergebnis=ergebnis,
                ergebnis_text=ERGEBNIS_TEXT.get(ergebnis, ""),
                stimmen=_stimmen(gremium, ergebnis, seed),
            )
        if not basis:
            _anhaengen(tops, belegt, "Anfragen und Verschiedenes")
            if gremium.nichtoeffentlich and (stabil("noe", key) % 3 or gremium.key == "rat"):
                for n in range(1 + stabil("noe-anzahl", key) % 2):
                    titel = _NOE_TITEL[(stabil("noe-titel", key) + n) % len(_NOE_TITEL)]
                    tops.append(
                        Top(
                            nummer=f"N{n + 1}",
                            reihenfolge=100 + n,
                            titel=titel,
                            oeffentlich=False,
                            ergebnis="approved" if tag < 0 else "",
                            ergebnis_text="Beschlossen" if tag < 0 else "",
                        )
                    )
        if basis and not tops:
            continue
        sitzungen.append(Sitzung(key=key, gremium=gremium_key, tag=tag, tops=tuple(tops), basis=basis, name=name))

    return Stadtplan(
        aufbautag=aufbautag,
        umfang=umfang,
        vorgaenge=tuple(vorgaenge),
        sitzungen=tuple(sitzungen),
        besetzung=_besetzung(),
        vorgang={v.key: v for v in vorgaenge},
        umsetzung=umsetzung,
    )


#: Ergebnistext der Tagesordnung (Insight ``result``)
ERGEBNIS_TEXT: dict[str, str] = {
    "approved": "Beschlossen",
    "rejected": "Abgelehnt",
    "deferred": "Vertagt",
    "withdrawn": "Zurückgezogen",
    "noted": "Zur Kenntnis genommen",
    "recommended": "Empfehlung an das beschließende Gremium",
}


def top_kennung(sitzung: Sitzung, top: Top) -> str:
    """Schlüssel eines Bestands-TOPs für ``demo_ext("agendaitem", …)`` – immer mit Präfix, auch in Basissitzungen."""
    return f"{BESTAND}{sitzung.key.removeprefix(BESTAND)}-{top.nummer}"


def beratung_kennung(sitzung: Sitzung, top: Top) -> str:
    """Schlüssel der Beratung (``demo_ext("consultation", …)``)."""
    return f"{top_kennung(sitzung, top)}-{top.vorgang}"


def mitgliedschaft_kennung(person: str, organisation: str) -> str:
    """Schlüssel einer Bestands-Mitgliedschaft (``demo_ext("membership", …)``); die Basisdemo nutzt ``person-org``."""
    return f"{BESTAND}{person}-{organisation}"


def drucksachentext(vorgang: Vorgang) -> list[str]:
    """Zeilen der Drucksache (PDF und Volltext): Kopf, Sachverhalt, Beschlussvorschlag, Finanzen."""
    zeilen = [
        f"{vorgang.oparl_art} {vorgang.nummer}",
        f"Urheber: {vorgang.urheber_name}",
        f"Beratungsfolge: {' → '.join(GREMIUM[g].name for g in vorgang.weg)}",
        "",
        "Sachverhalt" if not vorgang.von_fraktion else "Antragstext",
        *vorgang.sachverhalt,
        "",
        "Beschlussvorschlag",
        vorgang.beschluss,
    ]
    if vorgang.art in ("vorlage", "satzung"):
        zeilen += ["", "Finanzielle Auswirkungen", f"Ja, {_eur(vorgang.kosten)}." if vorgang.kosten else "Keine."]
    if vorgang.orte:
        zeilen += ["", "Betroffene Orte: " + ", ".join(ORTE[o][0] for o in vorgang.orte)]
    return zeilen


def demo_umfang() -> str:
    """Umfang des Bestands aus der Einstellung ``DEMO_UMFANG`` (``voll`` oder ``klein``)."""
    from django.conf import settings

    umfang = str(getattr(settings, "DEMO_UMFANG", "voll"))
    return umfang if umfang in UMFAENGE else "voll"
