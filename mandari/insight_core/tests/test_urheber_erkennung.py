# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erkennung der antragstellenden Fraktionen ohne Datenbank (Issue #1138, ``services/urheber_erkennung.py``).

Die Titel stammen in ihrer Form aus echten Ratsinformationssystemen (Köln, Bonn, Düsseldorf, Aachen, Neuss,
Münster); Namen von Personen sind erfunden.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from insight_core.services import urheber_erkennung as ue
from insight_core.services.urheber_erkennung import Fraktion, Kommune, Vorlage, Zugehoerigkeit

CDU = Fraktion.aus_name("CDU-Fraktion im Rat der Stadt Beispielstadt", organisation_id="cdu")
SPD = Fraktion.aus_name("SPD-Fraktion", organisation_id="spd")
GRUENE = Fraktion.aus_name("Fraktion Bündnis 90/Die Grünen", organisation_id="gruene")
LINKE = Fraktion.aus_name("Fraktion DIE LINKE.", organisation_id="linke")
VOLT = Fraktion.aus_name("Volt", organisation_id="volt", art=ue.GRUPPE)
BUERGERLISTE = Fraktion.aus_name("Fraktion Bürgerliste", organisation_id="bl")
FWK = Fraktion.aus_name("Freie Wähler Köln (FWK)", organisation_id="fwk")
SPD_BEZIRK = Fraktion.aus_name("SPD Bezirksfraktion Nord", organisation_id="spd-nord")

KOMMUNE = Kommune(fraktionen=[CDU, SPD, GRUENE, LINKE, VOLT, BUERGERLISTE, FWK, SPD_BEZIRK])


def _titel(name: str, paper_type: str = "", datum: date | None = date(2026, 3, 1)) -> list[str]:
    ergebnis = ue.ermitteln(Vorlage(name=name, paper_type=paper_type, datum=datum), KOMMUNE)
    return [str(f.organisation_id) for f in ergebnis.fraktionen] if ergebnis.herkunft == ue.TITLE else []


# --- Schreibvarianten ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "schluessel"),
    [
        ("Bündnis 90/Die Grünen", {"gruene"}),
        ("BÜNDNIS 90 / DIE GRÜNEN", {"gruene"}),
        ("B90/Grüne", {"gruene"}),
        ("GRÜNE", {"gruene"}),
        ("Gruene", {"gruene"}),
        ("DIE LINKE.", {"linke"}),
        ("Linksfraktion", {"linke"}),
        ("F.D.P.", {"fdp"}),
        ("Freie Demokratische Partei", {"fdp"}),
        ("Christlich Demokratische Union (CDU)", {"cdu"}),
        ("Sozialdemokratische Partei Deutschlands", {"spd"}),
        ("Alternative für Deutschland", {"afd"}),
        ("ÖDP", {"oedp"}),
        ("Die PARTEI", {"partei"}),
        ("die Partei der Mitte", set()),
        ("Freie Wähler", {"fw"}),
        ("FWG", {"fw"}),
        ("BSW", {"bsw"}),
        ("SPD/Volt", {"spd", "volt"}),
        ("Grünfläche am Hafen", set()),
    ],
)
def test_schreibvarianten(text: str, schluessel: set[str]) -> None:
    assert ue.parteien_in(text) == schluessel


@pytest.mark.parametrize(
    ("name", "art", "einordnung", "erwartet"),
    [
        ("SPD-Fraktion", "", "", True),
        ("Ratsfraktion UWG/Freie Wähler", "", "Gremium", True),
        ("Volt", "", "Gruppen", True),
        ("Sozialdemokratische Partei Deutschlands", "", "Fraktion", True),
        ("Fraktionsgeschäftsstelle CDU", "Amt", "Ebene 3", False),
        ("Fraktionsvorsitzendenkonferenz", "Gremium", "", False),
        ("fraktionslos", "Fraktion", "Fraktion", False),
        ("AfD", "Fraktion", "Einzelmandate", False),
        ("Ausschuss für Umwelt", "", "Ausschuss", False),
        ("CDU", "Partei", "", False),
    ],
)
def test_nur_fraktionen_und_ratsgruppen(name: str, art: str, einordnung: str, erwartet: bool) -> None:
    assert ue.ist_fraktion(name, art, einordnung) is erwartet


# --- Weg 2: Vorlagenart und Titel ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "paper_type", "erwartet"),
    [
        ("Antrag der CDU-Fraktion: Radweg an der Hafenstraße", "", ["cdu"]),
        ("Schlaglöcher am Markt (Antrag SPD-Fraktion)", "Antrag", ["spd"]),
        ("SPD-Bezirksfraktion-Antrag: Ampel an der Schule", "", ["spd-nord"]),
        ("Antrag CDU-Fraktion und Fraktion Bündnis 90/Die Grünen: Parkplätze", "", ["cdu", "gruene"]),
        ("Grüne/SPD/u.w.-Antrag: Haltestelle Zeisigweg", "Antrag", ["gruene", "spd"]),
        ("Hinweisschilder zum Wochenmarkt - Antrag der CDU Fraktion -", "Antrag", ["cdu"]),
        ("Anfrage der Fraktion DIE LINKE vom 12.03.2026 zur Wohnungsnot", "Anfrage", ["linke"]),
        ("Antrag der Fraktion Bürgerliste betr. Spielplatz", "Antrag", ["bl"]),
        ("Gemeinsamer Antrag von CDU, SPD und Volt", "", ["cdu", "spd", "volt"]),
        ("Radweg Hafenstraße", "Antrag CDU", ["cdu"]),
        ("Sicherheit der Brücken", "Anfrage nach § 4 BV6 (SPD)", ["spd-nord"]),
        ("Nachnutzung der Sparkasse", "Antrag nach § 3 BV2 (FWK)", ["fwk"]),
        ("CDU-Fraktion: Bänke am Ufer", "Antrag", ["cdu"]),
    ],
)
def test_titel_und_vorlagenart(name: str, paper_type: str, erwartet: list[str]) -> None:
    assert sorted(_titel(name, paper_type)) == sorted(erwartet)


@pytest.mark.parametrize(
    ("name", "paper_type"),
    [
        # Die Verwaltung antwortet oder berichtet: Der Antrag ist nur Gegenstand
        ("Antrag der CDU-Fraktion: Radweg", "Stellungnahme der Verwaltung"),
        ("Gefährliche Orte – Anfrage der SPD-Fraktion vom 18.06.2018", "Beantwortung einer Anfrage (Rat bzw. HA)"),
        ("Spielplatzoffensive", "Mitteilung der Verwaltung"),
        ("Stellungnahme zum Antrag der CDU-Fraktion", ""),
        ("Beantwortung der Anfrage der SPD", ""),
        ("Radweg Hauptstraße; hier: Antrag der CDU-Fraktion vom 09.06.2021", "Kenntnisnahme"),
        # Kein Antragsteller genannt
        ("Antrag zur Sanierung der Grundschule", "Antrag"),
        ("Antrag an die BV Mitte", "Antrag an die BV Mitte"),
        ("Grüne Welle auf der Ringstraße", "Antrag"),
    ],
)
def test_kein_antragsteller_im_titel(name: str, paper_type: str) -> None:
    assert _titel(name, paper_type) == []


def test_verwaltungsvorlage_zaehlt_nur_mit_dem_antrag_vorn() -> None:
    """Bezirksregierung Köln: „Sitzungsvorlage“ mit der Anfrage der Fraktion als Titel."""
    assert _titel("Anfrage der CDU-Fraktion vom 13.04.2023 Digitale Beteiligung", "Sitzungsvorlage RR") == ["cdu"]


def test_unbekannte_fraktion_macht_den_weg_ungueltig() -> None:
    """Nennt der Titel eine Fraktion, die es in der Kommune nicht gibt, wird nichts geraten, auch nicht teilweise."""
    assert ue.aus_titel(Vorlage(name="Antrag der Fraktionen SPD und AfD: Bänke"), KOMMUNE) is None
    assert ue.ermitteln(Vorlage(name="Antrag der Fraktionen SPD und AfD: Bänke"), KOMMUNE) == ue.KEIN_ERGEBNIS


def test_zwei_passende_fraktionen_sind_mehrdeutig() -> None:
    kommune = Kommune(fraktionen=[CDU, Fraktion.aus_name("CDU im Braunkohlenausschuss", organisation_id="cdu2")])
    assert ue.aus_titel(Vorlage(name="Antrag der CDU-Fraktion: Tagebau"), kommune) is None


def test_zeitraum_der_fraktion_entscheidet() -> None:
    alt = Fraktion.aus_name("FDP-Fraktion", organisation_id="alt", ende=date(2025, 10, 31))
    neu = Fraktion.aus_name("FDP/KSG-Fraktion", organisation_id="neu", beginn=date(2025, 11, 1))
    kommune = Kommune(fraktionen=[alt, neu])
    vorlage = Vorlage(name="Antrag der FDP-Fraktion: Bänke")
    for datum, erwartet in ((date(2024, 1, 1), "alt"), (date(2026, 1, 1), "neu")):
        vorlage.datum = datum
        assert [f.organisation_id for f in ue.aus_titel(vorlage, kommune) or []] == [erwartet]


# --- Weg 1: Quelle ------------------------------------------------------------------------------


def test_quelle_geht_vor() -> None:
    vorlage = Vorlage(name="Antrag der CDU-Fraktion: Bänke", quelle=[SPD])
    assert ue.ermitteln(vorlage, KOMMUNE) == ue.Ergebnis(ue.SOURCE, (SPD,))


# --- Weg 3: originatorPerson --------------------------------------------------------------------


def _kommune_mit_personen() -> Kommune:
    return Kommune(
        fraktionen=[CDU, SPD],
        zugehoerigkeiten=[
            Zugehoerigkeit(fraktion=CDU, person_id="p1", vorname="Petra", nachname="Beispiel", ende=date(2025, 10, 31)),
            Zugehoerigkeit(
                fraktion=SPD, person_id="p1", vorname="Petra", nachname="Beispiel", beginn=date(2025, 11, 1)
            ),
            Zugehoerigkeit(fraktion=SPD, person_id="p2", vorname="Max", nachname="Muster"),
        ],
    )


def test_person_mit_fraktion_am_vorlagedatum() -> None:
    kommune = _kommune_mit_personen()
    vor = Vorlage(name="Bänke am Ufer", paper_type="Antrag", datum=date(2024, 5, 1), personen=["p1"])
    nach = Vorlage(name="Bänke am Ufer", paper_type="Antrag", datum=date(2026, 5, 1), personen=["p1"])
    assert ue.ermitteln(vor, kommune) == ue.Ergebnis(ue.PERSON, (CDU,))
    assert ue.ermitteln(nach, kommune) == ue.Ergebnis(ue.PERSON, (SPD,))


def test_person_zaehlt_nicht_bei_verwaltungsvorlagen() -> None:
    """Bei einer Beschlussvorlage nennt originatorPerson oft den Bürgermeister; der hat eine Fraktion, stellt aber nicht."""
    vorlage = Vorlage(name="Haushalt 2027", paper_type="Beschlussvorlage", datum=date(2026, 5, 1), personen=["p2"])
    assert ue.ermitteln(vorlage, _kommune_mit_personen()) == ue.KEIN_ERGEBNIS


def test_person_mit_zwei_fraktionen_am_stichtag_wird_nicht_geraten() -> None:
    kommune = Kommune(
        fraktionen=[CDU, SPD],
        zugehoerigkeiten=[Zugehoerigkeit(fraktion=CDU, person_id="p1"), Zugehoerigkeit(fraktion=SPD, person_id="p1")],
    )
    assert ue.aus_personen(Vorlage(paper_type="Antrag", personen=["p1"]), kommune) is None


# --- Weg 4: Unterschriften ----------------------------------------------------------------------

BRIEF = """
Sehr geehrter Herr Oberbürgermeister,
die Fraktionen beantragen, der Rat möge beschließen: Am Hafenweg werden Bänke aufgestellt.
Begründung: Herr Max Muster hat im Ausschuss bereits darauf hingewiesen.

Mit freundlichen Grüßen

gez. Dr. Petra Beispiel                 gez. Jonas Alt-Mustermann
Fraktionsvorsitzende                    Fraktionsvorsitzender
"""


def _kommune_mit_unterschriften() -> Kommune:
    return Kommune(
        fraktionen=[CDU, SPD, GRUENE],
        zugehoerigkeiten=[
            Zugehoerigkeit(fraktion=CDU, vorname="Petra Maria", nachname="Beispiel"),
            Zugehoerigkeit(fraktion=GRUENE, vorname="Jonas", nachname="Alt-Mustermann"),
            Zugehoerigkeit(fraktion=SPD, vorname="Max", nachname="Muster"),
        ],
    )


def test_unterschriften_im_schluss_ergeben_gemeinsamen_antrag() -> None:
    """Titel ignoriert, Zweitname erlaubt; Namen im Fließtext vor der Grußformel zählen nicht."""
    vorlage = Vorlage(name="Bänke am Hafenweg", paper_type="Antrag", text=BRIEF)
    assert ue.ermitteln(vorlage, _kommune_mit_unterschriften()) == ue.Ergebnis(ue.SIGNATURE, (CDU, GRUENE))


def test_gleichnamige_personen_in_zwei_fraktionen_werden_nicht_geraten() -> None:
    kommune = _kommune_mit_unterschriften()
    kommune = Kommune(
        fraktionen=kommune.fraktionen,
        zugehoerigkeiten=[
            *kommune.zugehoerigkeiten,
            Zugehoerigkeit(fraktion=SPD, vorname="Petra", nachname="Beispiel"),
        ],
    )
    assert ue.aus_unterschriften(Vorlage(paper_type="Antrag", text=BRIEF), kommune) is None


def test_unterschrift_mit_datum_und_akzent() -> None:
    kommune = Kommune(
        fraktionen=[LINKE],
        zugehoerigkeiten=[
            Zugehoerigkeit(fraktion=LINKE, vorname="Jacob", nachname="Bohé", beginn=date(2025, 11, 1)),
        ],
    )
    text = "Wir beantragen …\nMit freundlichen Grüßen\nJacob Bohe\nFraktion Die Linke"
    assert ue.aus_unterschriften(Vorlage(paper_type="Antrag", datum=date(2026, 1, 1), text=text), kommune) == [LINKE]
    assert ue.aus_unterschriften(Vorlage(paper_type="Antrag", datum=date(2025, 1, 1), text=text), kommune) == []


def test_reihenfolge_titel_vor_person_vor_unterschrift() -> None:
    kommune = _kommune_mit_unterschriften()
    kommune = Kommune(
        fraktionen=kommune.fraktionen,
        zugehoerigkeiten=[*kommune.zugehoerigkeiten, Zugehoerigkeit(fraktion=SPD, person_id="p9")],
    )
    vorlage = Vorlage(name="Antrag der SPD-Fraktion: Bänke", paper_type="Antrag", personen=["p9"], text=BRIEF)
    assert ue.ermitteln(vorlage, kommune).herkunft == ue.TITLE
    vorlage.name = "Bänke am Hafenweg"
    assert ue.ermitteln(vorlage, kommune).herkunft == ue.PERSON
    vorlage.personen = []
    assert ue.ermitteln(vorlage, kommune).herkunft == ue.SIGNATURE


@pytest.mark.parametrize(
    "stichtag",
    [
        date(2026, 3, 1),
        datetime(2026, 3, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=ZoneInfo("Europe/Berlin")),
        datetime(2026, 3, 1),
    ],
)
def test_zeitraum_vertraegt_datum_und_zeitstempel(stichtag: date) -> None:
    """Bestandsdatenbanken liefern für ``date``, ``start_date`` und ``end_date`` Zeitstempel (Vorfall 31.10.2026)."""
    gepflegt = Fraktion.aus_name("SPD-Fraktion", beginn=date(2026, 3, 1), ende=date(2026, 3, 1))
    aus_oparl = Fraktion.aus_name(
        "SPD-Fraktion", beginn=datetime(2026, 3, 1, tzinfo=UTC), ende=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert gepflegt.gilt_am(stichtag)
    assert aus_oparl.gilt_am(stichtag)
    assert Zugehoerigkeit(fraktion=gepflegt, beginn=date(2026, 3, 1), ende=date(2026, 3, 1)).gilt_am(stichtag)
    assert Zugehoerigkeit(
        fraktion=aus_oparl, beginn=datetime(2026, 3, 1, tzinfo=UTC), ende=datetime(2026, 3, 1, tzinfo=UTC)
    ).gilt_am(stichtag)


def test_zeitraum_grenzen_mit_zeitstempel() -> None:
    fraktion = Fraktion.aus_name("SPD-Fraktion", ende=datetime(2026, 3, 1, tzinfo=UTC))

    assert fraktion.gilt_am(date(2026, 3, 1))
    assert not fraktion.gilt_am(datetime(2026, 3, 2, tzinfo=UTC))
