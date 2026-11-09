# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Test- und Systemkonten, Fraktionsauswahl der laufenden Wahlperiode, Fraktion aus der gepflegten Liste (Issue #1258).

- Testkonten („ratsinfotest“, „34 Dummy_34“) und Systemrollen („Informationssystem“) erkennt dieselbe Regel in Python
  und in der Datenbank; echte Personen mit Vornamen bleiben sichtbar („Hans Test“).
- Gremienseite, Gremienliste, Personenliste, Personenseite und Sitemap blenden sie aus und zählen sie nicht mit; die
  Daten bleiben im Bestand.
- Der Fraktionsfilter der Vorgangsliste zeigt nur Fraktionen der laufenden Wahlperiode; frühere Namen derselben
  Fraktion gehören dazu, Reste früherer Wahlperioden nicht.
- Die Spalte „Fraktion“ der Personenliste nutzt die gepflegte Liste für Mandate im Hauptorgan.

Nur erfundene Namen.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from insight_core.models import (
    FraktionsBezeichnung,
    FraktionsZugehoerigkeit,
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    VorgangUrheber,
)
from insight_core.services import testkonten, vorgang_urheber
from insight_core.services.indexierung import mit_laufender_mitgliedschaft
from insight_core.services.personen_liste import angaben_fuer

pytestmark = pytest.mark.django_db

RIS = "https://ris.konten.example/oparl"
_nummer = count()


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Konten-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=quelle, name="Stadt Beispiel", slug="beispiel", is_listed=True
    )


def _org(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/org/{next(_nummer)}", body=body, name=name, **felder)


def _person(body: OParlBody, name: str = "", vorname: str | None = None, nachname: str | None = None) -> OParlPerson:
    return OParlPerson.objects.create(
        external_id=f"{RIS}/person/{next(_nummer)}", body=body, name=name, given_name=vorname, family_name=nachname
    )


def _echt(body: OParlBody, vorname: str, nachname: str) -> OParlPerson:
    return _person(body, f"{vorname} {nachname}", vorname, nachname)


def _mitglied(person: OParlPerson, org: OParlOrganization, rolle: str | None = "Mitglied", **felder: Any) -> None:
    OParlMembership.objects.create(
        external_id=f"{RIS}/membership/{next(_nummer)}", person=person, organization=org, role=rolle, **felder
    )


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


# --- Regeln -------------------------------------------------------------------------------------

#: (Name, Vorname, Nachname) aus echten RIS-Beständen (Muster, nicht die Personen)
TESTKONTEN = [
    (" ratsinfotest", None, "ratsinfotest"),
    (" ms-r-test", None, "ms-r-test"),
    ("Test Beuel", None, None),
    ("Test Person", "Test", "Person"),
    ("34 Dummy_34", "34", "Dummy_34"),
    (" CDU-Ausleihe1", None, "CDU-Ausleihe1"),
    ("Gast", None, "Gast"),
    (" session", None, "session"),
    ("Presse#1", None, "Presse#1"),
    ("Tischvorlagen HA #40", None, None),
    ("Max Mustermann", "Max", "Mustermann"),
    ("Erika Mustermann", None, None),
]
ECHTE_PERSONEN = [
    ("Hans Test", "Hans", "Test"),
    ("Alice Gast", "Alice", "Gast"),
    ("Daniel Pawlak-Gast", None, None),
    ("Anna Testa", None, None),
    ("Jonas Alt-Mustermann", "Jonas", "Alt-Mustermann"),
    ("Sabine Holtzmann", None, None),
    ("Person 1", None, None),
    ("Sessionsdienst Meier", None, None),
    (" Fraktion DIE LINKE", None, "Fraktion DIE LINKE"),
    ("", None, None),
]


@pytest.mark.parametrize(("name", "vorname", "nachname"), TESTKONTEN)
def test_testkonten_werden_erkannt(name: str, vorname: str | None, nachname: str | None) -> None:
    assert testkonten.ist_testkonto(SimpleNamespace(name=name, given_name=vorname, family_name=nachname))


@pytest.mark.parametrize(("name", "vorname", "nachname"), ECHTE_PERSONEN)
def test_echte_personen_bleiben(name: str, vorname: str | None, nachname: str | None) -> None:
    assert not testkonten.ist_testkonto(SimpleNamespace(name=name, given_name=vorname, family_name=nachname))


def test_datenbank_und_python_urteilen_gleich(body: OParlBody) -> None:
    personen = {_person(body, *zeile).pk: True for zeile in TESTKONTEN}
    personen |= {_person(body, *zeile).pk: False for zeile in ECHTE_PERSONEN}

    regel = testkonten.testkonto_q()
    in_der_datenbank = set(OParlPerson.objects.filter(regel).values_list("pk", flat=True))
    ohne = set(OParlPerson.objects.exclude(regel).values_list("pk", flat=True))

    assert in_der_datenbank == {pk for pk, test in personen.items() if test}
    assert ohne == {pk for pk, test in personen.items() if not test}, "exclude behält Personen mit leeren Feldern"
    for person in OParlPerson.objects.all():
        assert testkonten.ist_testkonto(person) is personen[person.pk]


@pytest.mark.parametrize(
    ("rolle", "erwartet"),
    [
        ("Informationssystem", True),
        (" informationssystem ", True),
        ("Zugriff auf das Ratsinfo", True),
        ("Vollzugriff", True),
        ("Digitaler Druck", True),
        ("Schriftführung", False),
        ("Gast", False),
        ("Mitarbeiter/in der Verwaltung (infosystem + VS + AL)", False),
        ("-", False),
        ("", False),
        (None, False),
    ],
)
def test_systemrollen(body: OParlBody, rolle: str | None, erwartet: bool) -> None:
    assert testkonten.ist_systemrolle(rolle) is erwartet
    rat = _org(body, "Rat")
    _mitglied(_echt(body, "Erika", "Beispiel"), rat, rolle)
    assert OParlMembership.objects.filter(testkonten.systemrolle_q()).exists() is erwartet
    assert OParlMembership.objects.exclude(testkonten.systemrolle_q()).exists() is not erwartet


# --- Listen und Zählungen -----------------------------------------------------------------------


@pytest.fixture
def bezirk(body: OParlBody) -> dict[str, Any]:
    """Bezirksvertretung mit drei Sitzen, einem Testkonto und einer Verwaltungskraft mit RIS-Zugang."""
    bv = _org(body, "Bezirksvertretung Mitte", classification="Bezirksvertretung")
    rat = _org(body, "Rat", classification="Rat")
    sitze = [_echt(body, "Anna", "Eins"), _echt(body, "Bernd", "Zwei"), _echt(body, "Clara", "Drei")]
    for person in sitze:
        _mitglied(person, bv, "Bezirksvertreter/in")
    testkonto = _person(body, " ratsinfotest", None, "ratsinfotest")
    _mitglied(testkonto, bv, "Informationssystem")
    _mitglied(testkonto, rat, "-")
    verwaltung = _echt(body, "Vera", "Verwaltung")
    _mitglied(verwaltung, bv, "Informationssystem")
    _mitglied(verwaltung, rat, "Informationssystem")
    ehemals_test = _person(body, "Test Beuel", None, None)
    _mitglied(ehemals_test, bv, "Mitglied", end_date=date(2020, 1, 1))
    gestern = timezone.now() - timedelta(days=1)
    sitzung = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Sitzung", start=gestern)
    sitzung.organizations.add(bv)
    return {"bv": bv, "rat": rat, "sitze": sitze, "testkonto": testkonto, "verwaltung": verwaltung}


def test_gremienseite_ohne_test_und_systemkonten(body: OParlBody, bezirk: dict[str, Any]) -> None:
    html = _client(body).get(f"/insight/gremien/{bezirk['bv'].pk}/").content.decode()

    assert "3 Mitglieder" in html
    assert "Aktive Mitglieder (3)" in html
    for name in ("Anna Eins", "Bernd Zwei", "Clara Drei"):
        assert name in html
    for versteckt in ("ratsinfotest", "Vera Verwaltung", "Informationssystem", "Test Beuel"):
        assert versteckt not in html, versteckt
    # Die Daten bleiben im Bestand
    assert OParlMembership.objects.filter(organization=bezirk["bv"]).count() == 6


def test_gremienliste_zaehlt_nur_sitze(body: OParlBody, bezirk: dict[str, Any]) -> None:
    html = _client(body).get("/insight/gremien/?tab=all").content.decode()

    zeile = re.search(r"Bezirksvertretung Mitte.*?</tr>", html, re.S)
    assert zeile and re.search(r">\s*3\s*<", zeile.group(0))


def test_personenliste_ohne_testkonten_und_ohne_systemrolle_als_funktion(
    body: OParlBody, bezirk: dict[str, Any]
) -> None:
    html = _client(body).get("/insight/personen/").content.decode()

    assert "ratsinfotest" not in html and "Test Beuel" not in html
    assert "Vera Verwaltung" in html, "echte Personen bleiben in der Liste"
    assert "Informationssystem" not in html
    assert angaben_fuer([bezirk["verwaltung"]])[bezirk["verwaltung"].pk].funktion == ""


def test_personenseite_ohne_systemrolle_und_testkonto_ohne_index(body: OParlBody, bezirk: dict[str, Any]) -> None:
    client = _client(body)
    verwaltung = client.get(f"/insight/personen/{bezirk['verwaltung'].pk}/").content.decode()
    assert "Informationssystem" not in verwaltung
    assert '<meta name="robots" content="noindex, follow"' in verwaltung, "ohne Sitz kein Index"

    testkonto = client.get(f"/insight/personen/{bezirk['testkonto'].pk}/")
    assert testkonto.status_code == 200, "ausblenden statt löschen: die Seite bleibt erreichbar"
    assert '<meta name="robots" content="noindex, follow"' in testkonto.content.decode()


def test_sitemap_ohne_test_und_systemkonten(body: OParlBody, bezirk: dict[str, Any]) -> None:
    laufend = set(mit_laufender_mitgliedschaft(OParlPerson.objects.all(), timezone.localdate()))

    assert laufend == set(bezirk["sitze"])


# --- Fraktionsauswahl ---------------------------------------------------------------------------


def _vorgang(body: OParlBody, name: str, *urheber: tuple[str, str, OParlOrganization | None]) -> OParlPaper:
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/{next(_nummer)}", body=body, name=name)
    for bezeichnung, kurzform, org in urheber:
        VorgangUrheber.objects.create(
            paper_id=vorlage.pk,
            body_id=body.pk,
            organisation_id=org.pk if org else None,
            bezeichnung=bezeichnung,
            kurzform=kurzform,
            herkunft=VorgangUrheber.TITLE,
        )
    return vorlage


def _auswahl(body: OParlBody, stichtag: date) -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        (f.wert, f.anzeige, f.bezeichnungen) for f in vorgang_urheber.fraktionen_der_kommune(body.pk, stichtag=stichtag)
    ]


def test_fraktionen_frueherer_wahlperioden_fehlen(body: OParlBody) -> None:
    """Wie Wuppertal: Im RIS gibt es nur Ratsgruppen von 1999–2004, 2004–2009 und eine von 2026 ohne Mitglieder."""
    pds = _org(body, "Ratsgruppe PDS", short_name="PDS", start_date=date(1999, 10, 1), end_date=date(2004, 10, 13))
    rep = _org(body, "REP-Ratsgruppe", start_date=date(2004, 10, 14), end_date=date(2009, 10, 20))
    bsw = _org(body, "Ratsgruppe BSW", start_date=date(2026, 1, 1))
    ohne_ende = _org(body, "Fraktion Alt", short_name="Alt")
    _mitglied(_echt(body, "Otto", "Alt"), ohne_ende, start_date=date(2014, 6, 1), end_date=date(2020, 10, 31))
    laufend = _org(body, "Fraktion Neu", short_name="Neu")
    _mitglied(_echt(body, "Nina", "Neu"), laufend, start_date=date(2025, 11, 1))
    _mitglied(_echt(body, "Paul", "Vorher"), laufend, start_date=date(2014, 6, 1), end_date=date(2020, 10, 31))
    for org in (pds, rep, bsw, ohne_ende, laufend):
        _vorgang(body, f"Antrag {org.name}", (org.name or "", org.short_name or "", org))

    assert _auswahl(body, date(2026, 11, 7)) == [
        ("Fraktion Neu", "Neu", ("Fraktion Neu",)),
        ("Ratsgruppe BSW", "Ratsgruppe BSW", ("Ratsgruppe BSW",)),
    ]
    assert _auswahl(body, date(2003, 1, 1)) == [("Ratsgruppe PDS", "PDS", ("Ratsgruppe PDS",))]


@pytest.fixture
def liste(body: OParlBody) -> date:
    """Wie Münster: keine Fraktionen im RIS, dafür die gepflegte Liste mit Umbenennung und neuer Fraktion."""
    heute = timezone.localdate()
    beginn, wechsel = heute - timedelta(days=370), heute - timedelta(days=37)

    def eintrag(
        nachname: str, vorname: str, fraktion: str, kurzform: str, art: str = "fraktion", **felder: Any
    ) -> None:
        felder.setdefault("gueltig_ab", beginn)
        FraktionsZugehoerigkeit.objects.create(
            body=body, nachname=nachname, vorname=vorname, fraktion=fraktion, kurzform=kurzform, art=art, **felder
        )

    eintrag("Beispiel", "Petra", "Fraktion GRÜNE", "Grüne")
    eintrag("Muster", "Max", "FDP", "FDP", "einzelmandat", gueltig_bis=wechsel - timedelta(days=1))
    eintrag("Muster", "Max", "FDP-Ratsfraktion", "FDP", gueltig_ab=wechsel)
    eintrag("Probe", "Lars", "Die PARTEI & MOVE Beispielstadt", "PARTEI & MOVE", "gruppe")
    eintrag("Weg", "Wanda", "Wählergruppe Gestern", "Gestern", gueltig_bis=wechsel - timedelta(days=1))
    for name, kurz, ab, bis in (
        ("Die PARTEI/ÖDP", "PARTEI/ÖDP", beginn, wechsel - timedelta(days=1)),
        ("Die PARTEI & MOVE Beispielstadt", "PARTEI & MOVE", wechsel, None),
    ):
        FraktionsBezeichnung.objects.create(
            body=body,
            fraktion="Die PARTEI & MOVE Beispielstadt",
            bezeichnung=name,
            kurzform=kurz,
            gueltig_ab=ab,
            gueltig_bis=bis,
        )
    _vorgang(body, "Radweg am Kanal", ("Fraktion GRÜNE", "Grüne", None))
    _vorgang(body, "Bänke am Hafen", ("FDP", "FDP", None))
    _vorgang(body, "Kunst im Tunnel", ("Die PARTEI/ÖDP", "PARTEI/ÖDP", None))
    _vorgang(body, "Alte Anfrage", ("Wählergruppe Gestern", "Gestern", None))
    return heute


def test_gepflegte_liste_mit_frueheren_namen(body: OParlBody, liste: date) -> None:
    assert _auswahl(body, liste) == [
        ("FDP-Ratsfraktion", "FDP", ("FDP",)),
        ("Fraktion GRÜNE", "Grüne", ("Fraktion GRÜNE",)),
        ("Die PARTEI & MOVE Beispielstadt", "PARTEI & MOVE", ("Die PARTEI/ÖDP",)),
    ]


def test_filter_der_vorgangsliste(body: OParlBody, liste: date) -> None:
    client = Client()
    seite = client.get("/insight/k/beispiel/vorgaenge/").content.decode()
    auswahl = re.search(r'<select id="filter-fraktion".*?</select>', seite, re.S)
    assert auswahl
    optionen = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', auswahl.group(0))
    assert optionen == [
        ("", "Alle Fraktionen"),
        ("FDP-Ratsfraktion", "FDP"),
        ("Fraktion GRÜNE", "Grüne"),
        ("Die PARTEI &amp; MOVE Beispielstadt", "PARTEI &amp; MOVE"),
    ]

    heute = client.get("/insight/k/beispiel/vorgaenge/", {"fraktion": "Die PARTEI & MOVE Beispielstadt"})
    assert "Kunst im Tunnel" in heute.content.decode() and "Bänke am Hafen" not in heute.content.decode()
    # Ältere Links mit einem früheren Namen oder einer früheren Fraktion zeigen weiter deren Vorgänge
    frueher = client.get("/insight/k/beispiel/vorgaenge/", {"fraktion": "Die PARTEI/ÖDP"}).content.decode()
    assert "Kunst im Tunnel" in frueher
    assert re.search(r'<option value="Die PARTEI &amp; MOVE Beispielstadt"\s+selected', frueher)
    gestern = client.get("/insight/k/beispiel/vorgaenge/", {"fraktion": "Wählergruppe Gestern"}).content.decode()
    assert "Alte Anfrage" in gestern and "Radweg am Kanal" not in gestern
    fdp = client.get("/insight/k/beispiel/vorgaenge/", {"fraktion": "FDP-Ratsfraktion"}).content.decode()
    assert "Bänke am Hafen" in fdp


def test_ohne_antragstellende_fraktionen_eine_abfrage(body: OParlBody, django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(1):
        assert vorgang_urheber.fraktionen_der_kommune(body.pk) == []


# --- Fraktion aus der gepflegten Liste ----------------------------------------------------------


def test_personenliste_nutzt_die_gepflegte_liste_fuer_mandate(body: OParlBody, liste: date) -> None:
    rat = _org(body, "Rat", classification="Rat")
    ausschuss = _org(body, "Ausschuss für Umwelt", classification="Ausschuss")
    petra = _echt(body, "Petra", "Beispiel")
    _mitglied(petra, rat, "Ratsmitglied")
    lars = _echt(body, "Lars", "Probe")
    _mitglied(lars, rat, "Ratsmitglied")
    namensvetter = _echt(body, "Max", "Muster")
    _mitglied(namensvetter, ausschuss, "Sachkundiger Bürger")
    wanda = _echt(body, "Wanda", "Weg")
    _mitglied(wanda, rat, "Ratsmitglied")

    angaben = angaben_fuer([petra, lars, namensvetter, wanda])

    assert angaben[petra.pk].fraktion_name == "Grüne"
    assert angaben[petra.pk].fraktion_hinweis == "redaktionell gepflegt"
    assert angaben[lars.pk].fraktion_name == "PARTEI & MOVE", "der heutige Name"
    assert angaben[namensvetter.pk].fraktion_name == "", "ohne Mandat im Hauptorgan keine Zuordnung über den Namen"
    assert angaben[wanda.pk].fraktion_name == "", "nicht mehr in der Fraktion"

    html = _client(body).get("/insight/personen/").content.decode()
    assert "Fraktion" in html and "Grüne" in html and "redaktionell gepflegt" in html
    seite = _client(body).get(f"/insight/personen/{petra.pk}/").content.decode()
    assert "Grüne" in seite and "redaktionell gepflegt" in seite


def test_oparl_fraktion_geht_vor(body: OParlBody, liste: date) -> None:
    rat = _org(body, "Rat", classification="Rat")
    fraktion = _org(body, "Fraktion Mitte", short_name="Mitte", organization_type="Fraktion")
    petra = _echt(body, "Petra", "Beispiel")
    _mitglied(petra, rat, "Ratsmitglied")
    _mitglied(petra, fraktion)

    assert angaben_fuer([petra])[petra.pk].fraktion_name == "Mitte"
