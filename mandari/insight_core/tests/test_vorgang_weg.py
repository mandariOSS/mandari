# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weg eines Vorgangs im Bürgerportal (Issue #1273): Zeitleiste mit Folgevorlagen und benannten Lücken, federführende
Stelle aus ``underDirectionOf``, Stand einer Anfrage und „Gefragt und geantwortet“ im Wortlaut der Dokumente.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.template import engines
from django.test import Client

from hub.ris import selectors as ris_selectors
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)
from insight_core.services import fragen_antworten, vorgang_weg
from insight_core.services.ergebnis import ANTWORT_OFFEN, BEANTWORTET, KENNTNIS
from insight_core.services.paper_status import LATEST, PAST, UPCOMING, PaperStatus, paper_status

BERLIN = ZoneInfo("Europe/Berlin")
JETZT = datetime(2026, 11, 7, 12, 0, tzinfo=BERLIN)
BASIS = "https://ris.beispielstadt.example/oparl/"

ANFRAGE_TEXT = """Anfrage der Fraktion
Sehr geehrter Herr Oberbürgermeister,
wir bitten um Beantwortung folgender Fragen:
1. Wann beginnt die Sanierung der Grund-
schule Nord?
2. Welche Kosten sind im Haushalt eingeplant?
Begründung:
1. Die Schule ist alt.
Mit freundlichen Grüßen
"""
ANTWORT_TEXT = """Stellungnahme der Verwaltung
Zu 1.:
Die Sanierung beginnt voraussichtlich im März 2027.
- 2 -
Zu 2.:
Im Haushalt sind 4,2 Mio. € eingeplant, 12 % mehr als geplant.
Im Auftrag
"""


def tag(jahr: int, monat: int, t: int) -> datetime:
    return datetime(jahr, monat, t, 17, 0, tzinfo=BERLIN)


def eintrag(
    datum: datetime | None, gremium: str = "Bezirksvertretung Mitte", ergebnis: str | None = None, **felder: Any
) -> dict[str, Any]:
    """Eintrag wie aus ``hub.ris.selectors.consultation_history``."""
    return {
        "consultation": None,
        "meeting": SimpleNamespace(cancelled=felder.get("abgesagt", False), id=uuid.uuid4()),
        "agenda_item": None,
        "date": datum,
        "organization_name": gremium,
        "organization_count": None,
        "agenda_number": felder.get("top"),
        "result": ergebnis,
        "public": felder.get("public", True),
        "role": felder.get("rolle"),
        "authoritative": False,
    }


def vorlage(name: str = "Sanierung der Grundschule Nord", **felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "pk": uuid.uuid4(),
        "name": name,
        "reference": None,
        "paper_type": None,
        "date": None,
        "raw_json": {},
    }
    werte.update(felder)
    return SimpleNamespace(**werte)


class TestArten:
    @pytest.mark.parametrize(
        ("art", "titel", "erwartet"),
        [
            ("Große Anfrage", "Sanierung", True),
            ("Anfrage nach § 17 GeschO", "", True),
            ("Einwohnerfrage", "", True),
            ("Bauvoranfrage", "Neubau am Markt", False),
            ("Beschlussvorlage", "Anfrage der SPD", False),
            ("Antrag", "Anfrage zur Kita", False),
            ("Beantwortung einer Anfrage", "", False),
            ("Anträge und Anfragen", "SPD-Anfrage: Radwege", True),
            ("Anträge und Anfragen", "Antrag der SPD: Radwege", False),
            ("Mitteilungen/Anfragen", "Anfrage der CDU zur Kita", True),
            ("Mitteilungen/Anfragen", "Antwort auf die Anfrage der CDU", False),
            (None, "Fraktions-Große Anfrage: Hauptstraße", True),
            (None, "Bauvoranfrage Hauptstraße 3", False),
        ],
    )
    def test_anfrage(self, art: str | None, titel: str, erwartet: bool) -> None:
        assert vorgang_weg.ist_anfrage(art, titel) is erwartet

    @pytest.mark.parametrize(
        ("art", "titel", "erwartet"),
        [
            ("Stellungnahme der Verwaltung", "", True),
            ("Mitteilungsvorlage", "", True),
            ("Beantwortung einer Anfrage", "", True),
            ("Vorlage", "Antwort der Verwaltung auf die Anfrage", True),
            ("Änderungsantrag", "Stellungnahme", False),
            ("Beschlussvorlage", "Sanierung", False),
        ],
    )
    def test_antwort(self, art: str, titel: str, erwartet: bool) -> None:
        assert vorgang_weg.ist_antwort(art, titel) is erwartet

    @pytest.mark.parametrize(
        ("art", "titel", "verwandt", "erwartet"),
        [
            # Folgevorlage: Art oder Titel genügt
            ("Mitteilungsvorlage", "Radverkehr in der Innenstadt", False, True),
            ("Information", "Radverkehr in der Innenstadt", False, True),
            # Nur verwandt: Eine Mitteilung zum selben Thema ist keine Antwort
            ("Mitteilungsvorlage", "Radverkehr in der Innenstadt", True, False),
            ("Information", "Sachstand Schulsanierungen 2026", True, False),
            ("Beschlussvorlage", "Stellungnahme zum Regionalplan", True, False),
            # … sie muss sich als Antwort ausweisen oder auf die Anfrage beziehen
            ("Stellungnahme der Verwaltung", "Radverkehr in der Innenstadt", True, True),
            ("Mitteilungsvorlage", "Mitteilung zur Anfrage der Fraktion", True, True),
            ("Mitteilungsvorlage", "Beantwortung: Grundschule Nord", True, True),
            ("Mitteilungsvorlage", "Information zu A/0142/2026", True, True),
            ("Mitteilungsvorlage", "Sanierung der Grundschule Nord", True, True),
        ],
    )
    def test_antwort_auf_die_anfrage(self, art: str, titel: str, verwandt: bool, erwartet: bool) -> None:
        anfrage = vorlage(paper_type="Anfrage", reference="A/0142/2026", date=date(2026, 8, 18))
        kandidat = vorlage(titel, paper_type=art, date=date(2026, 10, 9))
        assert vorgang_weg.antwort_auf(anfrage, kandidat, verwandt=verwandt) is erwartet

    def test_antwort_nicht_vor_der_anfrage(self) -> None:
        anfrage = vorlage(paper_type="Anfrage", date=date(2026, 8, 18))
        frueher = vorlage(paper_type="Stellungnahme der Verwaltung", date=date(2026, 8, 1))
        assert not vorgang_weg.antwort_auf(anfrage, frueher)
        assert vorgang_weg.antwort_auf(anfrage, vorlage(paper_type="Stellungnahme der Verwaltung"))

    def test_antwort_als_dokument(self) -> None:
        assert vorgang_weg.ist_antwort_datei("Antwort der Verwaltung.pdf")
        assert vorgang_weg.ist_antwort_datei("Beantwortung")
        assert vorgang_weg.ist_antwort_datei("Anlage 1 – Antwort")
        assert not vorgang_weg.ist_antwort_datei("Anfrage.pdf")
        assert not vorgang_weg.ist_antwort_datei("Anfrage zur Stellungnahme des Landes")


class TestFragenUndAntworten:
    def test_zu_nummern_mit_fragen_aus_der_anfrage(self) -> None:
        paare = fragen_antworten.paare(ANFRAGE_TEXT, ANTWORT_TEXT)
        assert paare is not None
        assert [(p.nummer, p.frage) for p in paare] == [
            (1, "Wann beginnt die Sanierung der Grundschule Nord?"),
            (2, "Welche Kosten sind im Haushalt eingeplant?"),
        ]
        assert paare[0].antwort == "Die Sanierung beginnt voraussichtlich im März 2027."
        assert paare[1].antwort == "Im Haushalt sind 4,2 Mio. € eingeplant, 12 % mehr als geplant."

    def test_fragen_aus_dem_vorspann_der_antwort(self) -> None:
        antwort = "1. Wann beginnt es?\n2. Was kostet es?\nZu Frage 1: Im März.\nZu Frage 2: Nichts."
        paare = fragen_antworten.paare("Text ohne nummerierte Fragen.", antwort)
        assert paare is not None and [p.antwort for p in paare] == ["Im März.", "Nichts."]

    def test_frage_und_antwort_im_wechsel(self) -> None:
        antwort = "Frage 1: Wann beginnt es?\nAntwort: Im März.\nFrage 2: Was kostet es?\nAntwort:\n4 Mio. €"
        paare = fragen_antworten.paare(None, antwort)
        assert paare is not None
        assert [(p.frage, p.antwort) for p in paare] == [
            ("Wann beginnt es?", "Im März."),
            ("Was kostet es?", "4 Mio. €"),
        ]

    @pytest.mark.parametrize(
        "antwort",
        [
            "Die Verwaltung beantwortet die Anfrage zusammenfassend: Alles läuft nach Plan.",
            "Zu 1.: Im März.",  # eine Antwort, aber zwei Fragen
            "Zu 1.: Im März.\nZu 3.: Später.",  # Lücke in den Nummern
            "1. Wann beginnt die Sanierung?\n2. Welche Kosten?\nDie Verwaltung antwortet gesammelt.",
        ],
    )
    def test_lieber_nichts_als_falsch(self, antwort: str) -> None:
        assert fragen_antworten.paare(ANFRAGE_TEXT, antwort) is None

    def test_aufzaehlung_in_der_antwort_bricht_nicht_falsch_ab(self) -> None:
        # Eine Aufzählung in der wiederholenden Antwort beginnt einen neuen Lauf; die Fragenzahl der Anfrage passt dann
        # nicht mehr, also keine Paare statt nur der ersten Frage
        antwort = "1. Wann beginnt die Sanierung?\nIn drei Schritten:\n1. Planung\n2. Bau\n2. Welche Kosten?\nViele."
        assert fragen_antworten.paare(ANFRAGE_TEXT, antwort) is None

    def test_lange_antwort_gekuerzt(self) -> None:
        lang = " ".join(["Die Verwaltung prüft das sorgfältig."] * 60)
        paare = fragen_antworten.paare("1. Wann?", f"Zu 1.: {lang}")
        assert paare is not None and paare[0].gekuerzt
        assert len(paare[0].antwort) <= fragen_antworten.MAX_ANTWORT_ZEICHEN + 2
        assert paare[0].antwort.endswith(". …")

    def test_unterpunkte_und_jahreszahlen_sind_keine_fragen(self) -> None:
        assert fragen_antworten.fragen("1.1 Unterpunkt?\n2026. Jahr\n1) Erste Frage?\n2) Zweite Frage?") == [
            "Erste Frage?",
            "Zweite Frage?",
        ]


class TestKernzahlen:
    def render(self, text: str) -> str:
        return engines["django"].from_string("{% load insight_listen %}{{ text|kernzahlen }}").render({"text": text})

    def test_betraege_und_prozente_fett(self) -> None:
        html = self.render("Es sind 4,2 Mio. € und 12.500 Euro, also 12 % bzw. 3 Prozent; 2027 beginnt der Bau.")
        assert '<strong class="font-semibold text-gray-900 dark:text-white">4,2 Mio. €</strong>' in html
        assert ">12.500 Euro</strong>" in html and ">12 %</strong>" in html and ">3 Prozent</strong>" in html
        assert ">2027" not in html

    def test_text_wird_escaped(self) -> None:
        html = self.render('<script>alert("x")</script> 5 €')
        assert "<script>" not in html and "&lt;script&gt;" in html and ">5 €</strong>" in html


def _status(verlauf: list[dict[str, Any]]) -> PaperStatus:
    return paper_status(verlauf, JETZT)


class TestStationen:
    def test_vorlage_folgevorlagen_beratungen_in_zeitlicher_folge(self) -> None:
        paper = vorlage(paper_type="Beschlussvorlage", date=date(2026, 9, 1))
        aenderung = vorlage("Änderungsantrag der SPD", paper_type="Änderungsantrag", reference="V/1/2026-1")
        aenderung.date = date(2026, 9, 20)
        verlauf = [
            eintrag(tag(2026, 9, 10), "Bauausschuss", "einstimmig empfohlen", top="5"),
            eintrag(tag(2026, 10, 1), "Rat der Stadt", "beschlossen", top="8"),
            eintrag(tag(2026, 12, 3), "Rat der Stadt"),
        ]
        status = _status(verlauf)
        stationen = vorgang_weg.stationen(paper, verlauf, status, folgevorlagen=[aenderung], now=JETZT)
        assert [s.titel for s in stationen] == [
            "Beschlussvorlage",
            "Bauausschuss",
            "Änderungsantrag",
            "Rat der Stadt",
            "Rat der Stadt",
        ]
        assert [s.zustand for s in stationen] == [PAST, PAST, PAST, LATEST, UPCOMING]
        assert stationen[1].ergebnis is not None and stationen[1].ergebnis.text == "beschlossen"
        assert stationen[1].ergebnis_text == "einstimmig empfohlen"
        assert stationen[2].detail == "V/1/2026-1, „Änderungsantrag der SPD“"
        assert stationen[2].href == f"/insight/vorgaenge/{aenderung.pk}/"

    def test_fehlendes_ergebnis_wird_benannt(self) -> None:
        paper = vorlage(date=date(2026, 9, 1))
        verlauf = [eintrag(tag(2026, 10, 20), top="3"), eintrag(tag(2025, 3, 1), top="4")]
        stationen = vorgang_weg.stationen(paper, verlauf, _status(verlauf), now=JETZT)
        details = {s.datum: s.detail for s in stationen if s.art == vorgang_weg.BERATUNG}
        assert details[tag(2026, 10, 20)] == "TOP 3, Ergebnis noch nicht veröffentlicht"
        assert details[tag(2025, 3, 1)] == "TOP 4, kein Ergebnis angegeben"

    def test_langes_ergebnis_ohne_begriff_steht_ausgeschrieben(self) -> None:
        roh = "Die Angelegenheit wird in einer gemeinsamen Sitzung mit dem Fachausschuss weiter erörtert"
        verlauf = [eintrag(tag(2026, 10, 1), ergebnis=roh)]
        station = vorgang_weg.stationen(vorlage(), verlauf, _status(verlauf), now=JETZT)[0]
        assert station.ergebnis is not None and not station.ergebnis.bekannt
        assert station.ergebnis_text == roh

    def test_offene_anfrage_hat_eine_luecke_bei_heute(self) -> None:
        paper = vorlage(paper_type="Anfrage", date=date(2026, 10, 1))
        verlauf = [eintrag(tag(2026, 12, 1))]
        status = _status(verlauf)
        anfrage = vorgang_weg.anfrage_stand(paper, [], [], verlauf, status, JETZT)
        assert anfrage.schluessel == ANTWORT_OFFEN and anfrage.etikett is not None
        assert anfrage.etikett.text == "Antwort steht aus"
        stationen = vorgang_weg.stationen(paper, verlauf, status, anfrage=anfrage, now=JETZT)
        assert [(s.art, s.zustand) for s in stationen] == [
            (vorgang_weg.VORLAGE, PAST),
            (vorgang_weg.LUECKE, vorgang_weg.OFFEN),
            (vorgang_weg.BERATUNG, UPCOMING),
        ]
        assert stationen[1].titel == "Antwort der Verwaltung" and stationen[1].detail == "Steht noch aus"


class TestAnfrageStand:
    def test_antwort_als_folgevorlage(self) -> None:
        paper = vorlage(paper_type="Anfrage", date=date(2026, 8, 18))
        antwort = vorlage(paper_type="Stellungnahme der Verwaltung", date=date(2026, 10, 9))
        stand = vorgang_weg.anfrage_stand(paper, [antwort], [], [], _status([]), JETZT)
        assert stand.schluessel == BEANTWORTET and stand.antwort_vorlage is antwort
        assert stand.satz == "Die Verwaltung hat am 09.10.2026 geantwortet."

    def test_antwort_als_dokument(self) -> None:
        datei = SimpleNamespace(name="Antwort der Verwaltung")
        stand = vorgang_weg.anfrage_stand(vorlage(paper_type="Anfrage"), [], [datei], [], _status([]), JETZT)
        assert stand.beantwortet and stand.antwort_datei is datei

    @pytest.mark.parametrize(
        "ergebnis",
        [
            "mündlich beantwortet",
            "schriftlich beantwortet",
            "Die Anfrage wurde von der Verwaltung beantwortet",
            "Beantwortung ist erfolgt",
            "erledigt durch Beantwortung",
            "zur Kenntnis genommen",
            "Kenntnisnahme",
        ],
    )
    def test_in_der_sitzung_beantwortet(self, ergebnis: str) -> None:
        verlauf = [eintrag(tag(2025, 3, 1), ergebnis=ergebnis)]
        paper = vorlage(paper_type="Anfrage", date=date(2025, 2, 1))
        stand = vorgang_weg.anfrage_stand(paper, [], [], verlauf, _status(verlauf), JETZT)
        assert stand.beantwortet and stand.in_sitzung and stand.satz == ""

    @pytest.mark.parametrize(
        "ergebnis",
        [
            "wird schriftlich beantwortet",
            "Beantwortung vertagt",
            "nicht beantwortet",
            "Kenntnis genommen; Antwort folgt schriftlich",
            "Die Anfrage konnte nicht abschließend beantwortet werden",
            "Beantwortung erfolgt schriftlich",
            "Die Antwort steht noch aus",
            "schriftliche Antwort zugesagt",
            "Die Verwaltung wird die Anfrage in der nächsten Sitzung beantworten",
            "an den Fachausschuss verwiesen",
            "von der Tagesordnung abgesetzt",
        ],
    )
    def test_verneinte_angekuendigte_oder_vertagte_antwort_steht_aus(self, ergebnis: str) -> None:
        # Eine frühere Kenntnisnahme ändert daran nichts: Es zählt die jüngste Beratung mit Ergebnis
        verlauf = [
            eintrag(tag(2026, 9, 15), "Bauausschuss", ergebnis="zur Kenntnis genommen"),
            eintrag(tag(2026, 10, 13), ergebnis=ergebnis),
            eintrag(tag(2026, 10, 20), "Rat der Stadt"),
        ]
        paper = vorlage(paper_type="Anfrage", date=date(2026, 9, 1))
        status = _status(verlauf)
        stand = vorgang_weg.anfrage_stand(paper, [], [], verlauf, status, JETZT)
        assert not stand.beantwortet and not stand.in_sitzung
        assert stand.schluessel == ANTWORT_OFFEN and stand.luecke
        assert stand.etikett is not None and stand.etikett.text == "Antwort steht aus"
        stationen = vorgang_weg.stationen(paper, verlauf, status, anfrage=stand, now=JETZT)
        assert [s.detail for s in stationen if s.art == vorgang_weg.LUECKE] == ["Steht noch aus"]
        # Das Etikett der Beratung sagt nicht „zur Kenntnis genommen“, wo die Antwort fehlt oder angekündigt ist
        beratung = next(s for s in stationen if s.datum == tag(2026, 10, 13))
        if "Kenntnis" not in ergebnis:
            assert beratung.ergebnis is not None and beratung.ergebnis.schluessel != KENNTNIS

    def test_angekuendigte_antwort_bei_alter_anfrage_liegt_nicht_vor(self) -> None:
        verlauf = [eintrag(tag(2019, 3, 1), ergebnis="wird schriftlich beantwortet")]
        paper = vorlage(paper_type="Anfrage", date=date(2019, 2, 1))
        stand = vorgang_weg.anfrage_stand(paper, [], [], verlauf, _status(verlauf), JETZT)
        assert stand.etikett is None and stand.luecke
        assert stand.satz == "Eine Antwort der Verwaltung liegt uns nicht vor."

    def test_alte_anfrage_ohne_spur_ohne_etikett(self) -> None:
        paper = vorlage(paper_type="Anfrage", date=date(2019, 2, 1))
        stand = vorgang_weg.anfrage_stand(paper, [], [], [], _status([]), JETZT)
        assert stand.etikett is None and stand.luecke
        assert stand.satz == "Eine Antwort der Verwaltung liegt uns nicht vor."

    def test_zurueckgezogen_ohne_luecke(self) -> None:
        verlauf = [eintrag(tag(2026, 10, 1), ergebnis="zurückgezogen")]
        paper = vorlage(paper_type="Anfrage", date=date(2026, 9, 1))
        stand = vorgang_weg.anfrage_stand(paper, [], [], verlauf, _status(verlauf), JETZT)
        assert stand.etikett is None and not stand.luecke


class TestVerweise:
    @pytest.mark.parametrize(
        ("raw", "erwartet"),
        [
            ({"subordinatedPaper": ["a", "b", "a"]}, ["a", "b"]),
            ({"subordinatedPaper": [{"id": "a"}, {"name": "ohne Kennung"}, 3]}, ["a"]),
            ({"subordinatedPaper": "a"}, ["a"]),
            ({}, []),
            (None, []),
        ],
    )
    def test_oparl_verweise(self, raw: Any, erwartet: list[str]) -> None:
        assert ris_selectors.oparl_references(raw, "subordinatedPaper") == erwartet


@pytest.mark.django_db
class TestVorgangsseite:
    @pytest.fixture
    def kommune(self) -> OParlBody:
        source = OParlSource.objects.create(name="Beispielquelle", url=BASIS + "system")
        return OParlBody.objects.create(external_id=BASIS + "body/1", source=source, name="Beispielstadt")

    def _vorlage(self, kommune: OParlBody, nummer: str, **felder: Any) -> OParlPaper:
        return OParlPaper.objects.create(external_id=f"{BASIS}paper/{nummer}", body=kommune, **felder)

    def _seite(self, paper: OParlPaper) -> str:
        return Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()

    @pytest.fixture
    def anfrage(self, kommune: OParlBody) -> OParlPaper:
        OParlOrganization.objects.create(external_id=BASIS + "organization/66", body=kommune, name="Tiefbauamt")
        OParlOrganization.objects.create(external_id=BASIS + "organization/10", body=kommune, name="Ratsbüro")
        anfrage = self._vorlage(
            kommune,
            "1",
            name="Sanierung der Grundschule Nord",
            reference="A/0142/2026",
            paper_type="Große Anfrage",
            date=date(2026, 8, 18),
            raw_json={
                "underDirectionOf": [BASIS + "organization/10"],
                "subordinatedPaper": [BASIS + "paper/2", BASIS + "paper/3", BASIS + "paper/9"],
                "mainFile": {"id": BASIS + "file/1"},
            },
        )
        antwort = self._vorlage(
            kommune,
            "2",
            name="Sanierung der Grundschule Nord",
            reference="A/0142/2026-01",
            paper_type="Stellungnahme der Verwaltung",
            date=date(2026, 10, 9),
            raw_json={"underDirectionOf": [BASIS + "organization/66"]},
        )
        # Zurückgenommen bzw. gelöscht: erscheint nicht
        self._vorlage(kommune, "3", name="Alt", reference="A/0142/2026-02", paper_type="Ergänzung", deleted=True)
        OParlFile.objects.create(
            external_id=BASIS + "file/1", body=kommune, paper=anfrage, name="Große Anfrage", text_content=ANFRAGE_TEXT
        )
        OParlFile.objects.create(
            external_id=BASIS + "file/2", body=kommune, paper=antwort, name="Stellungnahme", text_content=ANTWORT_TEXT
        )
        sitzung = OParlMeeting.objects.create(
            external_id=BASIS + "meeting/1", body=kommune, name="Bezirksvertretung Mitte", start=tag(2026, 10, 13)
        )
        punkt = OParlAgendaItem.objects.create(
            external_id=BASIS + "agendaitem/1", meeting=sitzung, number="2.9", result="zur Kenntnis genommen"
        )
        OParlConsultation.objects.create(
            external_id=BASIS + "consultation/1",
            body=kommune,
            paper=anfrage,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=punkt.external_id,
        )
        return anfrage

    def test_anfrage_mit_antwort_zeitleiste_und_federfuehrung(self, anfrage: OParlPaper) -> None:
        seite = self._seite(anfrage)
        antwort = OParlPaper.objects.get(reference="A/0142/2026-01")
        assert 'data-ergebnis="beantwortet"' in seite
        assert "Die Verwaltung hat am 09.10.2026 geantwortet." in seite
        # Gefragt und geantwortet im Wortlaut, Kernzahlen fett
        assert 'data-testid="gefragt-geantwortet"' in seite
        assert "Wann beginnt die Sanierung der Grundschule Nord?" in seite
        assert "Die Sanierung beginnt voraussichtlich im März 2027." in seite
        assert ">4,2 Mio. €</strong>" in seite
        # Zeitleiste: Anfrage, Antwort mit federführendem Amt, Beratung mit Etikett; vor den Dokumenten
        assert (
            seite.index('id="gefragt-titel"') < seite.index('id="verlauf-titel"') < seite.index('id="dokumente-titel"')
        )
        verlauf = seite[seite.index('id="verlauf-titel"') : seite.index('id="dokumente-titel"')]
        assert (
            verlauf.index("Große Anfrage")
            < verlauf.index("Antwort der Verwaltung")
            < verlauf.index("Bezirksvertretung")
        )
        assert f'href="/insight/vorgaenge/{antwort.pk}/"' in verlauf
        assert "Stellungnahme der Verwaltung A/0142/2026-01, federführend: Tiefbauamt." in verlauf
        assert 'data-ergebnis="kenntnis"' in verlauf and "zuletzt" in verlauf
        assert "A/0142/2026-02" not in seite
        # Federführung der Anfrage selbst in den Angaben
        assert 'data-testid="vorgang-federfuehrung">Ratsbüro<' in seite

    def test_einwohnerfrage_ohne_wortlaut_und_ohne_hausnummer(self, anfrage: OParlPaper) -> None:
        """Einwohnerfragen (#1269): kein Wortlaut aus dem Schreiben, Titel verbundener Vorlagen ohne Hausnummer."""
        OParlPaper.objects.filter(pk=anfrage.pk).update(paper_type="Einwohnerfrage")
        OParlPaper.objects.filter(reference="A/0142/2026-01").update(name="Stellungnahme zur Musterstraße 12a")
        seite = self._seite(anfrage)
        assert 'data-ergebnis="beantwortet"' in seite
        assert "Wann beginnt die Sanierung der Grundschule Nord?" not in seite
        assert "Musterstraße 12a" not in seite
        assert "Musterstraße [Hausnummer ausgelassen]" in seite

    def test_ohne_text_nur_der_hinweis(self, anfrage: OParlPaper) -> None:
        OParlFile.objects.filter(paper__reference="A/0142/2026-01").update(text_content=None)
        seite = self._seite(anfrage)
        assert 'data-testid="gefragt-geantwortet"' in seite
        assert "Die Fragen stehen in der Anfrage, die Antwort der Verwaltung in" in seite
        assert ">Stellungnahme der Verwaltung A/0142/2026-01</a>" in seite
        assert "Wann beginnt die Sanierung der Grundschule Nord?" not in seite

    def test_offene_anfrage(self, kommune: OParlBody) -> None:
        paper = self._vorlage(
            kommune, "5", name="Radweg am Markt", paper_type="Anfrage", date=date.today(), raw_json={}
        )
        seite = self._seite(paper)
        assert 'data-ergebnis="antwort_offen"' in seite and "Antwort steht aus" in seite
        assert "Eine Antwort der Verwaltung liegt noch nicht vor." in seite
        assert 'id="verlauf-titel"' in seite and "Steht noch aus." in seite
        assert 'data-testid="gefragt-geantwortet"' not in seite

    def test_person_als_federfuehrung_wird_nie_gezeigt(self, kommune: OParlBody) -> None:
        paper = self._vorlage(
            kommune,
            "6",
            name="Spielplatz",
            paper_type="Beschlussvorlage",
            raw_json={"underDirectionOf": [BASIS + "person/7"]},
        )
        assert 'data-testid="vorgang-federfuehrung"' not in self._seite(paper)

    def test_folgevorlage_einer_anderen_kommune_fehlt(self, kommune: OParlBody) -> None:
        andere = OParlBody.objects.create(external_id=BASIS + "body/2", source=kommune.source, name="Nachbarort")
        fremd = OParlPaper.objects.create(external_id=BASIS + "paper/8", body=andere, name="Fremd", reference="X-1")
        paper = self._vorlage(
            kommune,
            "7",
            name="Spielplatz",
            paper_type="Beschlussvorlage",
            raw_json={"subordinatedPaper": [fremd.external_id]},
        )
        assert ris_selectors.related_public_papers(paper)["subordinatedPaper"] == []
        assert "X-1" not in self._seite(paper)

    def test_stellungnahme_zeigt_ihre_anfrage(self, anfrage: OParlPaper) -> None:
        antwort = OParlPaper.objects.get(reference="A/0142/2026-01")
        antwort.raw_json = {**antwort.raw_json, "superordinatedPaper": [anfrage.external_id]}
        antwort.save(update_fields=["raw_json"])
        seite = self._seite(antwort)
        verlauf = seite[seite.index('id="verlauf-titel"') : seite.index('id="dokumente-titel"')]
        assert f'href="/insight/vorgaenge/{anfrage.pk}/"' in verlauf and "Große Anfrage" in verlauf
        assert 'data-testid="gefragt-geantwortet"' not in seite

    def test_antwort_als_dokument_der_anfrage(self, kommune: OParlBody) -> None:
        paper = self._vorlage(kommune, "11", name="Grundschule Nord", paper_type="Anfrage", date=date(2026, 8, 18))
        for nummer, (name, text) in enumerate(
            [("Anfrage der Fraktion", ANFRAGE_TEXT), ("Antwort der Verwaltung", ANTWORT_TEXT)], start=11
        ):
            OParlFile.objects.create(
                external_id=f"{BASIS}file/{nummer}", body=kommune, paper=paper, name=name, text_content=text
            )
        seite = self._seite(paper)
        assert 'data-ergebnis="beantwortet"' in seite
        assert "Die Antwort der Verwaltung liegt als Dokument vor." in seite
        assert "Welche Kosten sind im Haushalt eingeplant?" in seite
        assert "Im Wortlaut automatisch aus den Dokumenten ausgelesen" in seite
        verlauf = seite[seite.index('id="verlauf-titel"') : seite.index('id="dokumente-titel"')]
        assert "Antwort der Verwaltung" in verlauf and "Dokument „Antwort der Verwaltung“." in verlauf

    def test_verwandte_mitteilung_zum_thema_ist_keine_antwort(self, kommune: OParlBody) -> None:
        mitteilung = self._vorlage(
            kommune,
            "21",
            name="Radverkehr in der Innenstadt",
            reference="M/0001/2026",
            paper_type="Mitteilungsvorlage",
            date=date.today(),
        )
        paper = self._vorlage(
            kommune,
            "20",
            name="Radweg am Markt",
            paper_type="Anfrage",
            date=date.today(),
            raw_json={"relatedPaper": [mitteilung.external_id]},
        )
        seite = self._seite(paper)
        assert 'data-ergebnis="antwort_offen"' in seite and "Antwort steht aus" in seite
        assert "Die Verwaltung hat" not in seite

    def test_texte_nur_fuer_die_gewaehlten_dokumente(self, anfrage: OParlPaper, kommune: OParlBody) -> None:
        haupt = OParlFile.objects.get(external_id=BASIS + "file/1")
        anlage = OParlFile.objects.create(
            external_id=BASIS + "file/5", body=kommune, paper=anfrage, name="Anlage", text_content="Lageplan"
        )
        geloescht = OParlFile.objects.create(
            external_id=BASIS + "file/6", body=kommune, paper=anfrage, name="Alt", text_content="Alt", deleted=True
        )
        texte = ris_selectors.text_starts_of_files([haupt.pk, geloescht.pk, "keine-kennung"], max_chars=12)
        assert texte == {haupt.pk: ANFRAGE_TEXT[:12]}
        assert anlage.pk not in texte
        assert ris_selectors.text_starts_of_files([], max_chars=12) == {}
