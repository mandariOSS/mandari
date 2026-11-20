# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Finanzcheck einer Vorlage (Issue #1513) am Beispiel Münster V/0452/2026 (Veloroute 12, Bohlweg bis Dieckstraße).

- Die KI liest nur aus; jede Zahl braucht eine Fundstelle, die im Dokumenttext steht und die Zahl enthält.
- Ein Kernbetrag ohne tragende Fundstelle verwirft den ganzen Finanzcheck; Tabellenzeilen entfallen einzeln.
- Eigenanteil und jährliche Belastung rechnet das Programm; die Sätze kommen aus festen Bausteinen ohne Fachsprache.
- Die Vorgangsseite zeigt den Block mit Anker #finanzcheck, die Description nennt die Kosten.
- Der Zeitplan fragt die KI nur bei Texten mit Geldbeträgen; neuer Text verwirft den Finanzcheck.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse

from insight_ai.models import TagesbatchKommune
from insight_ai.services import finanzcheck
from insight_ai.tests.test_zusammenfassung_claude import _sauber, _vorgang  # noqa: F401 (_sauber: Fixture)
from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.services import summary_store

pytestmark = pytest.mark.django_db

#: Auszug aus der Beschlussvorlage und der Folgelastenberechnung (Text wie aus dem PDF erkannt)
DOKUMENT = """Beschlussvorschlag:
I. Sachentscheidung:
Der Planung und der baulichen Ausführung wird zugestimmt.
II. Finanzielle Auswirkungen:
Es wird zur Kenntnis genommen, dass der Stadt Münster Baukosten in Höhe von ca. 6.200.000 €
entstehen. Dem gegenüber stehen Einzahlungen in Höhe von ca. 4.650.000 €.
Teilfinanzplan        Nr.     Bezeichnung                   Haush.-  Betrag
Auszahlungen               chen und -anlagen
Einzahlungen          4243 Velorouten Stadtregion
                                                            2028     2.000.000
                                                            2029     2.000.000
                                                            2030     1.500.000
                                                            2031      700.000
Saldo                                                               1.550.000
Die zur Finanzierung erforderlichen Ermächtigungen sind im Haushaltsplan 2026/2027 bei den o.g.
Investitionsmaßnahmen und den u. g. Produktgruppen nur teilweise veranschlagt. Dabei werden die nicht durch
Einzahlungen gedeckten Mehrbedarfe innerhalb des investiven Budgets des Dezernates für Planung, Bau und Wirtschaft
aus der Investitionsmaßnahme 0007 ,,Verkehrsflächen, Neubau und Erneuerung" bereitgestellt.
Zeile             02 Zuwendungen und allgemeine 2032 ff. 116.250 Folgeertrag
                  13      Aufwendungen für     Sach-  und 2032 ff.    62.000 Folgeaufwand
                  14 Bilanzielle Abschreibungen            2032 ff. 155.000 Folgeaufwand
                  20      Zinsen und   sonst.  Finanzauf- 2032 ff.    23.250 Folgeaufwand
Der Baubeginn ist derzeit für das Jahr 2028 vorgesehen. Die Bauzeit wird voraussichtlich 42 Monate betragen.
Für die Maßnahme werden Fördermittel nach den Förderrichtlinien Nahmobilität bzw. kommunaler Straßenbau beantragt.
Folgelastenberechnung
Nutzungsdauer:                                                40 Jahre
H Folgelasten (D-E-F+G) pro Jahr                                    Zeitpunkt        Euro
           insgesamt                                                                   124.000
"""

#: Antwort der KI, wie sie das Schema verlangt
ANTWORT: dict[str, Any] = {
    "finanzen": "kosten",
    "art": "bau_oder_anschaffung",
    "gesamtkosten": {"euro": 6200000, "beleg": "Baukosten in Höhe von ca. 6.200.000 €"},
    "foerderung": {"euro": 4650000, "beleg": "Dem gegenüber stehen Einzahlungen in Höhe von ca. 4.650.000 €."},
    "foerderung_stand": "beantragt",
    "foerderung_stand_beleg": "Für die Maßnahme werden Fördermittel nach den Förderrichtlinien Nahmobilität bzw. "
    "kommunaler Straßenbau beantragt.",
    "foerderung_von": "",
    "eigenanteil": {"euro": 1550000, "beleg": "Saldo 1.550.000"},
    "zahlungen": [
        {"jahr": 2028, "euro": 2000000, "richtung": "ausgabe", "beleg": "2028 2.000.000"},
        {"jahr": 2029, "euro": 2000000, "richtung": "ausgabe", "beleg": "2029 2.000.000"},
        {"jahr": 2030, "euro": 1500000, "richtung": "ausgabe", "beleg": "2030 1.500.000"},
        {"jahr": 2031, "euro": 700000, "richtung": "ausgabe", "beleg": "2031 700.000"},
    ],
    "laufend": [
        {"posten": "foerderanteil", "euro": 116250, "beleg": "02 Zuwendungen und allgemeine 2032 ff. 116.250"},
        {"posten": "unterhalt", "euro": 62000, "beleg": "Aufwendungen für Sach- und 2032 ff. 62.000"},
        {"posten": "wertverlust", "euro": 155000, "beleg": "14 Bilanzielle Abschreibungen 2032 ff. 155.000"},
        {"posten": "zinsen", "euro": 23250, "beleg": "20 Zinsen und sonst. Finanzauf- 2032 ff. 23.250"},
    ],
    "laufend_gesamt": {"euro": 124000, "beleg": "insgesamt 124.000"},
    "laufend_ab": {"jahr": 2032, "beleg": "Bilanzielle Abschreibungen 2032 ff. 155.000"},
    "nutzungsdauer": {"jahre": 40, "beleg": "Nutzungsdauer: 40 Jahre"},
    "keine_kosten_beleg": "",
    "im_haushalt": "teilweise",
    "im_haushalt_beleg": "Produktgruppen nur teilweise veranschlagt",
    "deckung": "umschichtung",
    "deckung_beleg": 'aus der Investitionsmaßnahme 0007 ,,Verkehrsflächen, Neubau und Erneuerung" bereitgestellt',
    "hinweise": [
        {
            "text": "Die Bauzeit beträgt voraussichtlich 42 Monate.",
            "beleg": "Die Bauzeit wird voraussichtlich 42 Monate betragen.",
        },
        {"text": "Die Stadt rechnet mit 99 neuen Stellplätzen.", "beleg": "Die Bauzeit wird voraussichtlich 42"},
    ],
}


def _antwort(**aenderungen: Any) -> dict[str, Any]:
    antwort = copy.deepcopy(ANTWORT)
    antwort.update(aenderungen)
    return antwort


def _kommune(name: str = "Stadt Münster") -> OParlBody:
    quelle = OParlSource.objects.create(name="RIS", url="https://ris.example.org/oparl/system")
    return OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name=name)


class _Anbieter:
    zusammenfassung_hinweis = "Claude von Anthropic (claude-opus-5-5); Verarbeitungsort: USA"

    def __init__(self, antwort: dict[str, Any]) -> None:
        self.antwort = antwort
        self.aufrufe: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    def chat_completion(
        self, messages: Any, max_tokens: int = 0, temperature: float = 0, schema: Any = None, effort: str | None = None
    ) -> Any:
        self.aufrufe.append({"nachrichten": messages, "schema": schema, "effort": effort})
        return SimpleNamespace(content=json.dumps(self.antwort), model="claude-opus-5-5")


class TestPruefung:
    def test_beispiel_besteht(self) -> None:
        ergebnis = finanzcheck.pruefen(ANTWORT, DOKUMENT)
        assert ergebnis.status == finanzcheck.STATUS_OK, ergebnis.grund
        daten = ergebnis.daten
        assert daten["gesamtkosten"]["euro"] == 6200000 and daten["foerderung"]["euro"] == 4650000
        assert daten["foerderung_stand"] == "beantragt" and daten["im_haushalt"] == "teilweise"
        assert [z["jahr"] for z in daten["zahlungen"]] == [2028, 2029, 2030, 2031]
        assert len(daten["laufend"]) == 4 and daten["nutzungsdauer"]["jahre"] == 40
        # Der zweite Hinweis nennt eine Zahl, die nirgends steht: Er entfällt
        assert [h["text"] for h in daten["hinweise"]] == ["Die Bauzeit beträgt voraussichtlich 42 Monate."]

    def test_erfundene_fundstelle_verwirft_alles(self) -> None:
        antwort = _antwort(gesamtkosten={"euro": 9800000, "beleg": "Baukosten in Höhe von ca. 9.800.000 €"})
        ergebnis = finanzcheck.pruefen(antwort, DOKUMENT)
        assert ergebnis.status == finanzcheck.STATUS_VERWORFEN and "gesamtkosten" in ergebnis.grund

    def test_zahl_passt_nicht_zur_fundstelle(self) -> None:
        antwort = _antwort(foerderung={"euro": 5000000, "beleg": "Einzahlungen in Höhe von ca. 4.650.000 €"})
        assert finanzcheck.pruefen(antwort, DOKUMENT).status == finanzcheck.STATUS_VERWORFEN

    def test_zeile_ohne_fundstelle_entfaellt_einzeln(self) -> None:
        zahlungen = [*ANTWORT["zahlungen"], {"jahr": 2032, "euro": 300000, "richtung": "ausgabe", "beleg": "2032 300"}]
        ergebnis = finanzcheck.pruefen(_antwort(zahlungen=zahlungen), DOKUMENT)
        assert ergebnis.status == finanzcheck.STATUS_OK
        assert [z["jahr"] for z in ergebnis.daten["zahlungen"]] == [2028, 2029, 2030, 2031]

    def test_einordnung_ohne_fundstelle_wird_unklar(self) -> None:
        ergebnis = finanzcheck.pruefen(
            _antwort(foerderung_stand="zugesagt", foerderung_stand_beleg="bewilligt"), DOKUMENT
        )
        assert ergebnis.daten["foerderung_stand"] == "unklar"

    def test_keine_kosten_mit_fundstelle(self) -> None:
        text = "Finanzielle Auswirkungen: Es entstehen keine Kosten für den Haushalt."
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg="Es entstehen keine Kosten für den Haushalt.")
        ergebnis = finanzcheck.pruefen(antwort, text)
        assert ergebnis.status == finanzcheck.STATUS_OK and ergebnis.daten["finanzen"] == "keine_kosten"

    def test_keine_angaben(self) -> None:
        assert finanzcheck.pruefen({"finanzen": "keine_angaben"}, DOKUMENT).status == finanzcheck.STATUS_KEINE
        assert finanzcheck.pruefen("kein JSON", DOKUMENT).status == finanzcheck.STATUS_VERWORFEN


class TestAnzeige:
    def _anzeige(self, antwort: dict[str, Any] = ANTWORT, name: str = "Stadt Münster") -> finanzcheck.Anzeige:
        ergebnis = finanzcheck.pruefen(antwort, DOKUMENT)
        paper = SimpleNamespace(
            pk=1,
            body=SimpleNamespace(name=name),
            finanzcheck={"status": ergebnis.status, "daten": ergebnis.daten},
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None
        return anzeige

    def test_kennzahlen_und_saetze(self) -> None:
        anzeige = self._anzeige()
        assert [(k.titel, k.wert, k.zusatz) for k in anzeige.kennzahlen] == [
            ("Kosten insgesamt", "6,2 Mio. €", ""),
            ("Davon Fördergeld", "4,65 Mio. €", "beantragt, noch nicht zugesagt"),
            ("Zahlt die Stadt selbst", "1,55 Mio. €", ""),
            ("Jedes Jahr", "124.000 €", "ab 2032"),
        ]
        text = " ".join(anzeige.saetze)
        assert "Das Vorhaben kostet laut Vorlage insgesamt 6,2 Millionen Euro." in text
        assert "Das Fördergeld ist beantragt, aber noch nicht zugesagt." in text
        assert "Die Stadt zahlt selbst 1,55 Millionen Euro." in text
        assert "Gezahlt wird in den Jahren 2028 bis 2031." in text
        assert "Ab 2032 kostet das die Stadt jedes Jahr 124.000 Euro." in text
        assert "(hier 40 Jahre)" in text and "Der Anteil des Fördergelds wird dagegengerechnet." in text
        assert "Das Geld ist erst teilweise im Haushalt der Stadt eingeplant." in text
        assert "kein zusätzliches Geld" in text
        # Keine Fachsprache des Haushaltsrechts in den Sätzen
        for wort in ("Abschreibung", "Sonderposten", "Ergebnisplan", "Finanzplan", "veranschlagt", "Aufwand"):
            assert wort not in text
        assert len(anzeige.absaetze) == 3

    def test_rechnung_aufgeschluesselt(self) -> None:
        anzeige = self._anzeige()
        assert anzeige.laufend_summe == "124.000 €"
        assert [z.entlastung for z in anzeige.laufend] == [False, False, False, True]
        assert not anzeige.mit_einnahmen
        assert [(j.jahr, j.ausgaben) for j in anzeige.jahre][0] == (2028, "2 Mio. €")

    def test_eigenanteil_errechnet_und_summe_errechnet(self) -> None:
        anzeige = self._anzeige(_antwort(eigenanteil=None, laufend_gesamt=None))
        eigen = next(k for k in anzeige.kennzahlen if k.titel == "Zahlt die Stadt selbst")
        assert (eigen.wert, eigen.zusatz) == ("1,55 Mio. €", "errechnet")
        assert next(k for k in anzeige.kennzahlen if k.titel == "Jedes Jahr").wert == "124.000 €"
        assert "(Kosten minus Fördergeld)" in " ".join(anzeige.saetze)

    def test_kreis_und_gemeinde(self) -> None:
        text = " ".join(self._anzeige(name="Landkreis Rotenburg (Wümme)").saetze)
        assert "Der Kreis zahlt selbst" in text and "kostet das den Kreis" in text and "Haushalt des Kreises" in text

    def test_seo_satz(self) -> None:
        assert (
            self._anzeige().seo
            == "Kosten laut Vorlage: 6,2 Mio. €, davon Stadt 1,55 Mio. €, jährlich 124.000 € ab 2032."
        )

    def test_nicht_bestanden_wird_nicht_gezeigt(self) -> None:
        paper = SimpleNamespace(pk=1, body=None, finanzcheck={"status": finanzcheck.STATUS_VERWORFEN, "daten": {}})
        assert finanzcheck.anzeige(paper) is None

    @pytest.mark.parametrize(
        ("euro", "kurz", "satz"),
        [
            (6200000, "6,2 Mio. €", "6,2 Millionen Euro"),
            (1000000, "1 Mio. €", "1 Million Euro"),
            (1234567, "1,23 Mio. €", "rund 1,23 Millionen Euro"),
            (124000, "124.000 €", "124.000 Euro"),
        ],
    )
    def test_betraege(self, euro: int, kurz: str, satz: str) -> None:
        assert finanzcheck.euro_kurz(euro) == kurz and finanzcheck.euro_satz(euro) == satz


class TestVorgangsseite:
    def test_block_und_description(self, client: Client) -> None:
        kommune = _kommune()
        vorgang = _vorgang(kommune, text=DOKUMENT, reference="V/0452/2026", paper_type="Vorlagen")
        ergebnis = finanzcheck.pruefen(ANTWORT, DOKUMENT)
        OParlPaper.objects.filter(pk=vorgang.pk).update(
            finanzcheck={"status": ergebnis.status, "fassung": finanzcheck.FASSUNG, "daten": ergebnis.daten}
        )
        html = client.get(reverse("insight_core:insight:paper_detail", args=[vorgang.pk])).content.decode()
        assert 'id="finanzcheck"' in html and "Was kostet das?" in html and "KI-Finanzcheck" in html
        assert "1,55 Mio. €" in html and "Rechnung und Fundstellen" in html
        beschreibung = html.split('<meta name="description" content="', 1)[1].split('"', 1)[0]
        assert "Kosten laut Vorlage: 6,2 Mio. €" in beschreibung

    def test_ohne_finanzcheck_kein_block(self, client: Client) -> None:
        vorgang = _vorgang(_kommune())
        html = client.get(reverse("insight_core:insight:paper_detail", args=[vorgang.pk])).content.decode()
        assert 'id="finanzcheck"' not in html


class TestNachziehen:
    def test_erstellt_und_speichert(self) -> None:
        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        vorgang = _vorgang(kommune, text=DOKUMENT)
        anbieter = _Anbieter(ANTWORT)
        assert finanzcheck.nachziehen(provider=anbieter) == 1
        assert anbieter.aufrufe[0]["schema"] is finanzcheck.SCHEMA and anbieter.aufrufe[0]["effort"] == "medium"
        vorgang.refresh_from_db()
        gespeichert = cast(dict[str, Any], vorgang.finanzcheck)
        assert gespeichert["status"] == "ok" and gespeichert["fassung"] == finanzcheck.FASSUNG
        assert "claude-opus-5-5" in gespeichert["hinweis"]
        # Ein zweiter Lauf fragt nicht erneut
        assert finanzcheck.nachziehen(provider=anbieter) == 0 and len(anbieter.aufrufe) == 1

    def test_ohne_geldbetraege_keine_ki(self) -> None:
        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        vorgang = _vorgang(kommune, text="Der Rat nimmt den Bericht zur Kenntnis.")
        anbieter = _Anbieter(ANTWORT)
        assert finanzcheck.nachziehen(provider=anbieter) == 0 and anbieter.aufrufe == []
        vorgang.refresh_from_db()
        assert cast(dict[str, Any], vorgang.finanzcheck)["status"] == finanzcheck.STATUS_KEINE

    def test_nur_kommunen_im_tagesbatch(self) -> None:
        _vorgang(_kommune(), text=DOKUMENT)
        anbieter = _Anbieter(ANTWORT)
        assert finanzcheck.nachziehen(provider=anbieter) == 0 and anbieter.aufrufe == []

    def test_neuer_text_verwirft(self) -> None:
        kommune = _kommune()
        vorgang = _vorgang(kommune, text=DOKUMENT)
        OParlPaper.objects.filter(pk=vorgang.pk).update(finanzcheck={"status": "ok", "daten": {}})
        datei = vorgang.files.get()
        assert summary_store.discard_for_files([datei.pk]) == 1
        vorgang.refresh_from_db()
        assert vorgang.finanzcheck is None

    def test_vorfilter(self) -> None:
        assert finanzcheck.kommt_in_frage(DOKUMENT)
        assert not finanzcheck.kommt_in_frage("Der Rat nimmt den Bericht zur Kenntnis.")
        assert not finanzcheck.kommt_in_frage("Münster hat 320.000 Einwohnerinnen und Einwohner.")


class TestBefundePruefung:
    """Befunde der Prüfung von PR #1515: Zahlgrenzen, Einheit T€, Paare aus Jahr und Betrag, „keine Kosten“."""

    def test_fundstelle_mitten_in_einer_zahl(self) -> None:
        text = "Die Kosten betragen 1.200.000 € und werden vollständig von der Stadt getragen."
        antwort = _antwort(
            gesamtkosten={"euro": 200000, "beleg": "200.000 € und werden vollständig"},
            foerderung=None,
            eigenanteil=None,
            laufend_gesamt=None,
            laufend=[],
            zahlungen=[],
        )
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN

    def test_tabelle_in_tausend_euro(self) -> None:
        text = "Kosten der Maßnahme (in T€)\nBaukosten 1.200\nFolgekosten 62 T€ pro Jahr"
        richtig = {"euro": 1200000, "beleg": "Baukosten 1.200"}
        falsch = {"euro": 1200, "beleg": "Baukosten 1.200"}
        leer: dict[str, Any] = {"foerderung": None, "eigenanteil": None, "laufend": [], "zahlungen": []}
        assert finanzcheck.pruefen(_antwort(gesamtkosten=richtig, laufend_gesamt=None, **leer), text).status == "ok"
        assert finanzcheck.pruefen(_antwort(gesamtkosten=falsch, laufend_gesamt=None, **leer), text).status == (
            finanzcheck.STATUS_VERWORFEN
        )
        klein = {"euro": 62, "beleg": "Folgekosten 62 T€ pro Jahr"}
        assert finanzcheck.pruefen(_antwort(gesamtkosten=richtig, laufend_gesamt=klein, **leer), text).status == (
            finanzcheck.STATUS_VERWORFEN
        )

    def test_zahlung_mit_zwei_jahren_und_doppelte_zeilen(self) -> None:
        text = DOKUMENT + "Auszahlungen 2026 (500.000 €) und 2027 (1.500.000 €)\n"
        zahlungen = [
            {"jahr": 2026, "euro": 1500000, "richtung": "ausgabe", "beleg": "2026 (500.000 €) und 2027 (1.500.000 €)"},
            {"jahr": 2028, "euro": 2000000, "richtung": "ausgabe", "beleg": "2028 2.000.000"},
            {"jahr": 2028, "euro": 2000000, "richtung": "ausgabe", "beleg": "2028 2.000.000"},
        ]
        laufend = [*ANTWORT["laufend"], ANTWORT["laufend"][1]]
        daten = finanzcheck.pruefen(_antwort(zahlungen=zahlungen, laufend=laufend), text).daten
        assert [(z["jahr"], z["euro"]) for z in daten["zahlungen"]] == [(2028, 2000000)]
        assert len(daten["laufend"]) == 4

    def test_kosten_stehen_noch_nicht_fest(self) -> None:
        text = "Die Kosten stehen noch nicht fest."
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg="Die Kosten stehen noch nicht fest.")
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_KEINE
        text = "Finanzielle Auswirkungen: keine"
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg="Finanzielle Auswirkungen: keine")
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK


class TestBefundeAnzeige:
    @pytest.mark.parametrize(
        ("name", "wer"),
        [
            ("Kreisstadt Homberg (Efze)", "die Stadt"),
            ("Stadt Köln, kreisfreie Stadt", "die Stadt"),
            ("Kreis Steinfurt", "der Kreis"),
            ("Samtgemeinde Sittensen", "die Gemeinde"),
        ],
    )
    def test_traeger(self, name: str, wer: str) -> None:
        assert finanzcheck.traeger(SimpleNamespace(body=SimpleNamespace(name=name)))["nom"] == wer

    def test_laufende_kosten_ohne_bau(self) -> None:
        text = "Der Zuschuss an den Verein beträgt jährlich 50.000 € und wird aus dem Kulturbudget gezahlt."
        antwort = _antwort(
            art="laufende_kosten",
            gesamtkosten=None,
            foerderung=None,
            eigenanteil=None,
            laufend_gesamt={"euro": 50000, "beleg": "Zuschuss an den Verein beträgt jährlich 50.000 €"},
            laufend=[],
            zahlungen=[],
            laufend_ab=None,
            nutzungsdauer=None,
            im_haushalt="unklar",
            deckung="unklar",
            hinweise=[],
        )
        ergebnis = finanzcheck.pruefen(antwort, text)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None
        assert anzeige.saetze == ["Jedes Jahr kostet das die Stadt 50.000 Euro."]
        assert anzeige.seo == "Kosten laut Vorlage: jährlich 50.000 €."

    def test_errechnete_jahressumme_gekennzeichnet(self) -> None:
        ergebnis = finanzcheck.pruefen(_antwort(laufend_gesamt=None, laufend_ab=None), DOKUMENT)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None
        jedes_jahr = next(k for k in anzeige.kennzahlen if k.titel == "Jedes Jahr")
        assert (jedes_jahr.wert, jedes_jahr.zusatz) == ("124.000 €", "errechnet")


class TestBudgetreserve:
    def test_finanzcheck_laesst_den_zusammenfassungen_ihren_teil(self) -> None:
        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        _vorgang(kommune, text=DOKUMENT)
        anbieter = _Anbieter(ANTWORT)
        anbieter.endpunkt = SimpleNamespace(monatsbudget_usd=100)  # type: ignore[attr-defined]
        anbieter.budget_rest = lambda: 20  # type: ignore[attr-defined]
        assert finanzcheck.nachziehen(provider=anbieter) == 0 and anbieter.aufrufe == []


class TestBefundeCodeRabbit:
    """Befunde von CodeRabbit zu PR #1515."""

    def test_dezimalkomma_zaehlt(self) -> None:
        text = "Die Baukosten betragen 6,2 Mio. Euro."
        antwort = _antwort(
            gesamtkosten={"euro": 62000000, "beleg": "Die Baukosten betragen 62 Mio. Euro."},
            foerderung=None,
            eigenanteil=None,
            laufend_gesamt=None,
            laufend=[],
            zahlungen=[],
        )
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN
        antwort["gesamtkosten"] = {"euro": 6200000, "beleg": "Die Baukosten betragen 6,2 Mio. Euro."}
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK

    def test_eigenanteil_null(self) -> None:
        text = "Die Maßnahme kostet 500.000 € und wird voll gefördert, der Eigenanteil beträgt 0 €."
        antwort = _antwort(
            gesamtkosten={"euro": 500000, "beleg": "Die Maßnahme kostet 500.000 €"},
            foerderung=None,
            eigenanteil={"euro": 0, "beleg": "der Eigenanteil beträgt 0 €"},
            laufend_gesamt=None,
            laufend=[],
            zahlungen=[],
            deckung="unklar",
            im_haushalt="unklar",
        )
        ergebnis = finanzcheck.pruefen(antwort, text)
        assert ergebnis.status == finanzcheck.STATUS_OK and ergebnis.daten["eigenanteil"]["euro"] == 0
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and "Die Stadt zahlt selbst nichts dazu." in anzeige.saetze

    def test_unvollstaendige_aufteilung_ohne_summe(self) -> None:
        laufend = [*ANTWORT["laufend"][:3], {"posten": "zinsen", "euro": 99999, "beleg": "erfunden 99.999"}]
        ergebnis = finanzcheck.pruefen(_antwort(laufend=laufend, laufend_gesamt=None), DOKUMENT)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None
        assert not any(k.titel == "Jedes Jahr" for k in anzeige.kennzahlen) and anzeige.laufend == []

    def test_zwischendurch_zurueckgesetzter_text(self) -> None:
        kommune = _kommune()
        vorgang = _vorgang(kommune, text=DOKUMENT)

        class _Zuruecksetzer(_Anbieter):
            def chat_completion(self, *args: Any, **kwargs: Any) -> Any:
                vorgang.files.update(text_content=None, text_extracted_at=None, text_extraction_status="pending")
                return super().chat_completion(*args, **kwargs)

        finanzcheck.erstellen(vorgang, provider=_Zuruecksetzer(ANTWORT))
        vorgang.refresh_from_db()
        assert vorgang.finanzcheck is None

    def test_trocken_mit_nachziehen_abgelehnt(self) -> None:
        from django.core.management import CommandError, call_command

        with pytest.raises(CommandError):
            call_command("finanzcheck", "--nachziehen", "--trocken")

    def test_wartende_belegen_das_kontingent_nicht(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from insight_core.models import OParlFile

        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        bereit = _vorgang(kommune, tage=5, text=DOKUMENT)
        wartend = _vorgang(kommune, tage=1, text=DOKUMENT)
        OParlFile.objects.create(
            external_id=f"{wartend.external_id}/anlage-2",
            body=kommune,
            paper=wartend,
            name="Anlage 2",
            download_url=f"{wartend.external_id}/anlage-2.pdf",
            mime_type="application/pdf",
            text_extraction_status="pending",
        )
        monkeypatch.setattr(finanzcheck, "KANDIDATEN_JE_LAUF", 1)
        anbieter = _Anbieter(ANTWORT)
        assert finanzcheck.nachziehen(provider=anbieter) == 1
        bereit.refresh_from_db()
        wartend.refresh_from_db()
        assert bereit.finanzcheck is not None and wartend.finanzcheck is None


class TestBefundeCodeRabbit2:
    """Zweite Runde CodeRabbit zu PR #1515."""

    def test_jahr_nicht_aus_laengerer_zahl(self) -> None:
        assert finanzcheck._jahre_in("Zahlung 20281 Euro") == []
        assert finanzcheck._jahre_in("2028 2.000.000") == [2028]

    def test_tausend_nur_in_der_eigenen_anlage(self) -> None:
        from insight_ai.services.prompts import anlage_block

        text = "\n\n".join(
            [
                anlage_block("Vorlage", "Die Baukosten betragen insgesamt 1.200 Euro für das Schild."),
                anlage_block("Kostenplan", "Kosten (in T€)\nBaukosten 1.200"),
            ]
        )
        leer: dict[str, Any] = {"foerderung": None, "eigenanteil": None, "laufend": [], "zahlungen": []}
        eigene = {"euro": 1200, "beleg": "Die Baukosten betragen insgesamt 1.200 Euro für das Schild."}
        assert finanzcheck.pruefen(_antwort(gesamtkosten=eigene, laufend_gesamt=None, **leer), text).status == "ok"
        tabelle = {"euro": 1200000, "beleg": "Baukosten 1.200"}
        assert finanzcheck.pruefen(_antwort(gesamtkosten=tabelle, laufend_gesamt=None, **leer), text).status == "ok"

    def test_reserve_vor_jedem_aufruf(self) -> None:
        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        _vorgang(kommune, tage=1, text=DOKUMENT)
        _vorgang(kommune, tage=2, text=DOKUMENT)
        anbieter = _Anbieter(ANTWORT)
        rest = iter([40, 40, 20, 20])
        anbieter.endpunkt = SimpleNamespace(monatsbudget_usd=100)  # type: ignore[attr-defined]
        anbieter.budget_rest = lambda: next(rest)  # type: ignore[attr-defined]
        assert finanzcheck.nachziehen(provider=anbieter) == 1 and len(anbieter.aufrufe) == 1

    def test_geloeschter_vorgang_nicht_per_kennung(self) -> None:
        from insight_core.services import finanzcheck_store

        vorgang = _vorgang(_kommune(), text=DOKUMENT)
        OParlPaper.objects.filter(pk=vorgang.pk).update(deleted=True)
        with pytest.raises(LookupError):
            finanzcheck_store.vorgang_finden(str(vorgang.pk))
