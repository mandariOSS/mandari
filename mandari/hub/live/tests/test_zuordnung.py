# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zuordnung (Issue #915, #47): TOP-Nummer und Titel → Tagesordnungspunkt derselben Sitzung (Lesarten bei verlorenen
Punkten, Titelprüfung), gelesener Name → Person (eindeutig, unsicher, keine).
"""

from __future__ import annotations

from datetime import date

import pytest

from hub.live.models import SectionConfidence, SpeechAssignment
from hub.live.tests.conftest import TAGESORDNUNG, Tagesordnung, Welt, kennung, mitglied, person
from hub.live.zuordnung import (
    aehnlichkeit,
    normalisiere_nummer,
    nummer_varianten,
    person_zuordnen,
    tagesordnungspunkt,
    top_zuordnen,
    vergleichsname,
)
from insight_core.models import OParlAgendaItem, OParlOrganization

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("roh", "nummer"),
    [("5", "5"), ("5.", "5"), ("Ö 5", "5"), ("TOP 5", "5"), ("1.1", "1.1"), (" 12 ", "12"), ("Ö", None), (None, None)],
)
def test_nummer_normalisieren(roh: str | None, nummer: str | None) -> None:
    assert normalisiere_nummer(roh) == nummer


def test_tagesordnungspunkt_der_sitzung(welt: Welt) -> None:
    assert tagesordnungspunkt(welt.sitzung.pk, "5") == welt.top5, "„Ö 5“ im RIS"
    assert tagesordnungspunkt(welt.sitzung.pk, "5.1") == welt.top51
    assert tagesordnungspunkt(welt.sitzung.pk, "9") is None
    welt.top5.public = False
    welt.top5.save()
    assert tagesordnungspunkt(welt.sitzung.pk, "5") is None, "nichtöffentliche TOPs nie"


def test_titelaehnlichkeit() -> None:
    # verglichen wird mit dem Anfang des Titels im RIS: Ein abgeschnittener Titel kostet kaum etwas
    assert aehnlichkeit("Neubau einer Grundschule", "Neubau einer Grundschule am Musterweg") == 0.94
    assert aehnlichkeit(None, "x") is None
    assert (aehnlichkeit("Haushalt", "Spielplatz sanieren") or 0) < 0.5


@pytest.mark.parametrize(
    ("gelesen", "im_ris"),
    [
        ("ericht der Verwaltung zum Radverkehrskonzept", "Bericht der Verwaltung zum Radverkehrskonzept"),
        ("Bebauungsplan Nr. 47 Am Muhlenbach …", "Bebauungsplan Nr. 47 Am Mühlenbach und Umgebung"),
        ("ebauungsplan Nr. 47 Am Mu\ufffdhlenbach", "Bebauungsplan Nr. 47 Am Mühlenbach"),
        ("Bericht der Verwaltung zum", "Bericht der Verwaltung zum Radverkehrskonzept"),
    ],
)
def test_titelaehnlichkeit_verzeiht_lesefehler(gelesen: str, im_ris: str) -> None:
    """Fehlender erster Buchstabe, „…“ am Ende, Umlautfehler, abgeschnittener Titel: weiter klar ähnlich."""
    assert (aehnlichkeit(gelesen, im_ris) or 0) >= 0.9


@pytest.mark.parametrize(
    ("gelesen", "lesarten"),
    [
        ("11", ["11", "1.1"]),
        ("12", ["12", "1.2"]),
        ("111", ["111", "11.1", "1.11", "1.1.1"]),
        ("TOP 1.1", ["1.1"]),
        ("5", ["5"]),
        (None, []),
    ],
)
def test_nummer_varianten(gelesen: str | None, lesarten: list[str]) -> None:
    assert nummer_varianten(gelesen) == lesarten


def _zuordnen(t: Tagesordnung, nummer: str | None, titel: str | None) -> tuple[str | None, str, str]:
    """(Nummer des gewählten TOP im RIS, Nummer des Abschnitts, Sicherheit)."""
    treffer = top_zuordnen(t.sitzung.pk, nummer, titel)
    assert treffer is not None
    punkt = treffer.punkt
    assert punkt is None or punkt.meeting_id == t.sitzung.pk, "nur Punkte der Sitzung der Übertragung"
    return (punkt.number if punkt else None), treffer.nummer, treffer.sicherheit


def test_punktverlust_der_titel_entscheidet(tagesordnung: Tagesordnung) -> None:
    """„TOP 1.1“ als „11“ gelesen, Titel von 1.1 eingeblendet: TOP 1.1, nicht TOP 11."""
    t = tagesordnung
    assert _zuordnen(t, "11", TAGESORDNUNG["1.1"]) == ("1.1", "1.1", SectionConfidence.NUMMER_UND_TITEL)
    assert _zuordnen(t, "12", TAGESORDNUNG["1.2"]) == ("1.2", "1.2", SectionConfidence.NUMMER_UND_TITEL)
    # die gelesene Nummer bleibt erhalten, die Ähnlichkeit zum gewählten TOP wird gespeichert
    treffer = top_zuordnen(t.sitzung.pk, "TOP 11", TAGESORDNUNG["1.1"])
    assert treffer is not None and treffer.gelesen == "11" and (treffer.titel_aehnlichkeit or 0) >= 0.95
    # passt der Titel zu TOP 11, bleibt es TOP 11
    assert _zuordnen(t, "11", TAGESORDNUNG["11"]) == ("11", "11", SectionConfidence.NUMMER_UND_TITEL)


def test_fehlender_erster_buchstabe_und_abgeschnittener_titel(tagesordnung: Tagesordnung) -> None:
    t = tagesordnung
    assert _zuordnen(t, "11", "ericht der Verwaltung zum Radverkehrs…")[0] == "1.1"
    assert _zuordnen(t, "12", "ebauungsplan Nr. 47 Am Muhlenbach")[0] == "1.2"


def test_titel_passt_nur_zu_anderer_nummer(tagesordnung: Tagesordnung) -> None:
    """Keine Lesart der Nummer passt zum Titel: Der Titel allein entscheidet (ab 0,75)."""
    t = tagesordnung
    assert _zuordnen(t, "2", TAGESORDNUNG["12"]) == ("12", "12", SectionConfidence.TITEL)
    assert _zuordnen(t, "7", "Anfrage zur Straßenbeleuchtung im Ortsteil") == ("11.1", "11.1", SectionConfidence.TITEL)


def test_ohne_passenden_titel_nur_die_nummer(tagesordnung: Tagesordnung) -> None:
    """Titel fehlt oder passt zu keinem TOP: die gelesene Nummer, mit niedriger Sicherheit."""
    t = tagesordnung
    assert _zuordnen(t, "11", "Lorem ipsum dolor sit amet") == ("11", "11", SectionConfidence.NUMMER)
    assert _zuordnen(t, "11", None) == ("11", "11", SectionConfidence.NUMMER)
    assert _zuordnen(t, "1.2", None) == ("1.2", "1.2", SectionConfidence.NUMMER)
    # die gelesene Nummer gibt es nicht, aber genau eine Lesart: „111“ → 11.1
    assert _zuordnen(t, "111", None) == ("11.1", "11.1", SectionConfidence.NUMMER)
    assert _zuordnen(t, "9", None) == (None, "9", SectionConfidence.KEINE)
    assert top_zuordnen(t.sitzung.pk, "TOP", "Anfragen der Fraktionen") is None, "ohne Ziffern keine Zuordnung"


def test_gleich_lautende_titel_entscheiden_nicht_allein(tagesordnung: Tagesordnung) -> None:
    t = tagesordnung
    for nummer in ("4", "14"):
        OParlAgendaItem.objects.create(
            external_id=kennung("agendaitems"), meeting=t.sitzung, number=nummer, order=20, name="Verschiedenes"
        )
    assert _zuordnen(t, "9", "Verschiedenes") == (None, "9", SectionConfidence.KEINE)
    assert _zuordnen(t, "14", "Verschiedenes") == ("14", "14", SectionConfidence.NUMMER_UND_TITEL)


def test_kandidaten_nur_aus_der_sitzung_der_uebertragung(tagesordnung: Tagesordnung) -> None:
    """Eine andere Sitzung mit denselben Nummern und Titeln zählt nie; nichtöffentliche Punkte auch nicht."""
    t = tagesordnung
    OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=t.andere, number="7", order=8, name="Sondersitzung Hallenbad"
    )
    for nummer, titel in TAGESORDNUNG.items():
        treffer = top_zuordnen(t.sitzung.pk, nummer.replace(".", ""), titel)
        assert treffer is not None and treffer.punkt == t.punkte[nummer], nummer
    assert _zuordnen(t, "7", "Sondersitzung Hallenbad") == (None, "7", SectionConfidence.KEINE)
    t.punkte["1.1"].public = False
    t.punkte["1.1"].save()
    assert _zuordnen(t, "11", TAGESORDNUNG["1.1"])[0] == "11", "nichtöffentlicher TOP 1.1 zählt nicht"


def test_schwellen_aus_dem_profil(tagesordnung: Tagesordnung) -> None:
    t = tagesordnung
    titel = "Bericht der Verwaltung zum Radweg"
    assert _zuordnen(t, "11", titel)[0] == "1.1"
    streng = top_zuordnen(t.sitzung.pk, "11", titel, titel_zur_nummer=0.99, titel_allein=0.99)
    assert streng is not None and streng.punkt == t.punkte["11"] and streng.sicherheit == SectionConfidence.NUMMER


def test_vergleichsname_ohne_titel() -> None:
    assert vergleichsname("Dr. Erika Muster") == "erika muster"
    assert vergleichsname("Prof. Dr.-Ing. Max Beispiel") == "max beispiel"
    assert vergleichsname("Jörg Groß") == "jorg gross"


def test_eindeutig_unter_den_mitgliedern(welt: Welt) -> None:
    treffer = person_zuordnen("Erika Muster", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (welt.muster, SpeechAssignment.EINDEUTIG)
    treffer = person_zuordnen("Dr. Erika Muster", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert treffer.person == welt.muster


def test_ehemaliges_mitglied_zaehlt_nicht_im_gremium_aber_in_der_kommune(welt: Welt) -> None:
    ehemalig = person(welt.body, "Paula", "Probe")
    mitglied(ehemalig, welt.rat, end_date=date(2019, 12, 31))
    treffer = person_zuordnen("Paula Probe", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (ehemalig, SpeechAssignment.EINDEUTIG), "über die Kommune"


def test_namensgleiche_sind_unsicher(welt: Welt) -> None:
    zweite = person(welt.body, "Max", "Beispiel")
    mitglied(zweite, welt.rat)
    treffer = person_zuordnen("Max Beispiel", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (None, SpeechAssignment.UNSICHER)


def test_lesefehler_ist_unsicher(welt: Welt) -> None:
    treffer = person_zuordnen("Erika Mustor", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (welt.muster, SpeechAssignment.UNSICHER)


def test_unbekannt_und_andere_kommune(welt: Welt) -> None:
    assert person_zuordnen("Gisela Gast", meeting=welt.sitzung, organization_id=welt.rat.pk).zuordnung == (
        SpeechAssignment.KEINE
    )
    andere = OParlOrganization.objects.create(
        external_id="https://andere.example/o/1", body=welt.body, name="Ausschuss"
    )
    treffer = person_zuordnen("Erika Muster", meeting=welt.sitzung, organization_id=andere.pk)
    assert treffer.person == welt.muster, "ohne Mitgliedschaft über die Kommune"
    assert person_zuordnen("ab", meeting=welt.sitzung, organization_id=welt.rat.pk).zuordnung == SpeechAssignment.KEINE
