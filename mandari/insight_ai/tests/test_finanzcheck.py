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
Teilfinanzplan
 Nr. Bezeichnung Haush.- jahr Betrag € Bemerkungen
Produktgruppe 1201 Bereitstellung von Verkehrsflä-
chen und -anlagen
Investitionsmaßnah-
me
4243 Velorouten Stadtregion
Auszahlungen    2028
2029
2030
2031
2.000.000
2.000.000
1.500.000
700.000
Einzahlungen   2028
2029
2030
2031
1.000.000
1.000.000
1.000.000
1.650.000
Saldo 1.550.000
Die zur Finanzierung erforderlichen Ermächtigungen sind im Haushaltsplan 2026/2027 bei den o.g.
Investitionsmaßnahmen und den u. g. Produktgruppen nur teilweise veranschlagt. Die bisher nicht
veranschlagten investiven Einzahlungen und Auszahlungen für die Jahre 2028 und 2031 werden zum
Haushaltsplanentwurf 2028 angemeldet. Dabei werden die nicht durch
Einzahlungen gedeckten Mehrbedarfe innerhalb des investiven Budgets des Dezernates für Planung, Bau und Wirtschaft
aus der Investitionsmaßnahme 0007 ,,Verkehrsflächen, Neubau und Erneuerung" bereitgestellt.
Zeile             02 Zuwendungen und allgemeine 2032 ff. 116.250 Folgeertrag
                  13      Aufwendungen für     Sach-  und 2032 ff.    62.000 Folgeaufwand
                  14 Bilanzielle Abschreibungen            2032 ff. 155.000 Folgeaufwand
                  20      Zinsen und   sonst.  Finanzauf- 2032 ff.    23.250 Folgeaufwand
Der Baubeginn ist derzeit für das Jahr 2028 vorgesehen. Die Bauzeit wird voraussichtlich 42 Monate betragen.
Für die Maßnahme werden Fördermittel nach den Förderrichtlinien Nahmobilität bzw. kommunaler Straßenbau beantragt.
Folgelastenberechnung
V/0479/2025 Aufgestellt am: 29.07.2025
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
    "auszahlungen": [
        {"jahr": 2028, "euro": 2000000, "beleg": ""},
        {"jahr": 2029, "euro": 2000000, "beleg": ""},
        {"jahr": 2030, "euro": 1500000, "beleg": ""},
        {"jahr": 2031, "euro": 700000, "beleg": ""},
    ],
    "auszahlungen_beleg": "Auszahlungen 2028 2029 2030 2031 2.000.000 2.000.000 1.500.000 700.000",
    "einzahlungen": [
        {"jahr": 2028, "euro": 1000000, "beleg": ""},
        {"jahr": 2029, "euro": 1000000, "beleg": ""},
        {"jahr": 2030, "euro": 1000000, "beleg": ""},
        {"jahr": 2031, "euro": 1650000, "beleg": ""},
    ],
    "einzahlungen_beleg": "Einzahlungen 2028 2029 2030 2031 1.000.000 1.000.000 1.000.000 1.650.000",
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
    "deckung_aus": "Verkehrsflächen, Neubau und Erneuerung",
    "nachmeldung": {
        "jahr": 2028,
        "beleg": "für die Jahre 2028 und 2031 werden zum Haushaltsplanentwurf 2028 angemeldet",
    },
    "kostenstand": {"datum": "29.07.2025", "beleg": "V/0479/2025 Aufgestellt am: 29.07.2025"},
    "preissteigerung": "unklar",
    "preissteigerung_beleg": "",
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
        # Teilfinanzplan mit Jahren und Beträgen getrennt untereinander: beide Richtungen ganz, Summen gehen auf
        assert [(z["jahr"], z["richtung"]) for z in daten["zahlungen"]] == [
            *((jahr, "ausgabe") for jahr in (2028, 2029, 2030, 2031)),
            *((jahr, "einnahme") for jahr in (2028, 2029, 2030, 2031)),
        ]
        assert daten["zahlungen_vollstaendig"] and not daten["zahlungen_unstimmig"]
        assert len(daten["laufend"]) == 4 and daten["nutzungsdauer"]["jahre"] == 40
        assert daten["kostenstand"]["datum"] == "29.07.2025" and daten["nachmeldung"]["jahr"] == 2028
        assert daten["deckung_aus"] == "Verkehrsflächen, Neubau und Erneuerung"
        assert daten["preissteigerung"] == "unklar"
        # Hinweise der KI entfallen: Sie wären ungeprüfter Text
        assert "hinweise" not in daten

    def test_erfundene_fundstelle_verwirft_alles(self) -> None:
        antwort = _antwort(gesamtkosten={"euro": 9800000, "beleg": "Baukosten in Höhe von ca. 9.800.000 €"})
        ergebnis = finanzcheck.pruefen(antwort, DOKUMENT)
        assert ergebnis.status == finanzcheck.STATUS_VERWORFEN and "gesamtkosten" in ergebnis.grund

    def test_zahl_passt_nicht_zur_fundstelle(self) -> None:
        antwort = _antwort(foerderung={"euro": 5000000, "beleg": "Einzahlungen in Höhe von ca. 4.650.000 €"})
        assert finanzcheck.pruefen(antwort, DOKUMENT).status == finanzcheck.STATUS_VERWORFEN

    def test_reihe_die_nicht_zur_tabelle_passt_fehlt_ganz(self) -> None:
        auszahlungen = [*ANTWORT["auszahlungen"], {"jahr": 2032, "euro": 300000, "beleg": "2032 300"}]
        ergebnis = finanzcheck.pruefen(_antwort(auszahlungen=auszahlungen), DOKUMENT)
        assert ergebnis.status == finanzcheck.STATUS_OK
        assert ergebnis.daten["zahlungen"] == [] and ergebnis.daten["zahlungen_unstimmig"]

    def test_summen_muessen_aufgehen(self) -> None:
        # Die Tabelle stimmt, aber die Gesamtkosten nicht: keine Jahre, Hinweis statt Diagramm
        text = DOKUMENT.replace("ca. 6.200.000 €", "ca. 7.200.000 €")
        antwort = _antwort(gesamtkosten={"euro": 7200000, "beleg": "Baukosten in Höhe von ca. 7.200.000 €"})
        daten = finanzcheck.pruefen(antwort, text).daten
        assert daten["zahlungen"] == [] and daten["zahlungen_unstimmig"]

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
    def _anzeige(
        self, antwort: dict[str, Any] = ANTWORT, name: str = "Stadt Münster", mit_aenderungen: bool = False
    ) -> finanzcheck.Anzeige:
        ergebnis = finanzcheck.pruefen(antwort, DOKUMENT)
        paper = SimpleNamespace(
            pk=1,
            date=date(2026, 9, 2),
            body=SimpleNamespace(name=name),
            finanzcheck={"status": ergebnis.status, "daten": ergebnis.daten},
        )
        anzeige = finanzcheck.anzeige(paper, mit_aenderungen=mit_aenderungen)
        assert anzeige is not None
        return anzeige

    def test_bau_und_wer_zahlt(self) -> None:
        bau = self._anzeige().karten.bau  # type: ignore[union-attr]
        assert bau is not None and (bau.titel, bau.wert) == ("Bau, 2028–2031", "6,2 Mio. €")
        assert [(s.wert, s.text, s.art) for s in bau.segmente] == [
            ("4,65 Mio. €", "Fördergeld (beantragt, noch nicht zugesagt)", "foerderung_offen"),
            ("1,55 Mio. €", "zahlt Münster", "stadt"),
        ]
        # Fördergeld kommt teils erst 2031: Münster streckt bis zu 2,5 Mio. € vor (aus dem Teilfinanzplan gerechnet)
        assert "bis zu 2,5 Mio. € vor" in bau.hinweis

    def test_haushalt_ohne_doppelzaehlung(self) -> None:
        """Der Anteil am Bau steckt in der jährlichen Belastung (Abschreibung) und steht nie zusätzlich daneben."""
        anzeige = self._anzeige()
        haushalt = anzeige.karten.haushalt  # type: ignore[union-attr]
        assert haushalt is not None
        assert (haushalt.titel, haushalt.wert, haushalt.einheit) == (
            "Belastung für den Haushalt, ab 2032",
            "124.000 €",
            "pro Jahr",
        )
        assert haushalt.text == "Im Schnitt, 40 Jahre lang. Zusammen rund 5 Mio. €."
        assert [(s.wert, s.text) for s in haushalt.segmente] == [
            ("38.750 €", "Anteil am Bau, auf 40 Jahre verteilt"),
            ("85.250 €", "Unterhalt und Zinsen"),
        ]
        text = " ".join(anzeige.saetze)
        assert "der Anteil am Bau ist darin enthalten" in text and "Danach jedes Jahr" not in text
        for wort in ("Abschreibung", "Sonderposten", "Ergebnisplan", "Finanzplan", "veranschlagt", "Aufwand"):
            assert wort not in text

    def test_wann_das_geld_fliesst(self) -> None:
        karten = self._anzeige().karten
        assert karten is not None and karten.mit_einnahmen
        assert [(s.jahr, s.aus, s.ein) for s in karten.saeulen] == [
            (2028, "2 Mio. €", "1 Mio. €"),
            (2029, "2 Mio. €", "1 Mio. €"),
            (2030, "1,5 Mio. €", "1 Mio. €"),
            (2031, "700.000 €", "1,65 Mio. €"),
        ]

    def test_was_offen_ist(self) -> None:
        karten = self._anzeige(mit_aenderungen=True).karten
        assert karten is not None and karten.stimmt and karten.stand == "Stand: Vorlage vom 02.09.2026"
        assert karten.offen == [
            "Das Fördergeld ist beantragt, aber noch nicht zugesagt. Kommt weniger, zahlt Münster mehr",
            "Das Geld ist erst teilweise im Haushalt eingeplant, der Rest soll in den Haushalt 2028 kommen. "
            "Fehlendes Geld soll aus „Verkehrsflächen, Neubau und Erneuerung“ kommen",
            "Kostenstand: 29.07.2025",
            "Ein Gremium hat mit Änderungen beschlossen, die Zahlen können abweichen",
        ]

    def test_eigenanteil_errechnet_und_summe_errechnet(self) -> None:
        karten = self._anzeige(_antwort(eigenanteil=None, laufend_gesamt=None)).karten
        assert karten is not None and karten.bau is not None and karten.haushalt is not None
        assert karten.bau.segmente[1].text == "zahlt Münster, Kosten minus Fördergeld"
        assert karten.haushalt.wert == "124.000 €" and "Aus den Einzelposten zusammengerechnet." in karten.haushalt.text

    def test_kreis_und_gemeinde(self) -> None:
        assert "Landkreis Rotenburg (Wümme) zahlt selbst" in " ".join(
            self._anzeige(name="Landkreis Rotenburg (Wümme)").saetze
        )
        assert "Kreuzau zahlt selbst" in " ".join(self._anzeige(name="Gemeinde Kreuzau").saetze)

    def test_seo_satz(self) -> None:
        assert self._anzeige().seo == (
            "Kosten laut Vorlage: 6,2 Mio. € (Bau 2028–2031), davon Münster 1,55 Mio. €, "
            "ab 2032 im Schnitt 124.000 € pro Jahr im Haushalt."
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
        assert 'id="finanzcheck"' in html and "Was kostet das?" in html and "KI-Kostencheck" in html
        assert "1,55 Mio. €" in html and "Wann das Geld fließt" in html and "Belastung für den Haushalt" in html
        assert 'fill="url(#fc-schraffur)"' in html and "Summen nachgerechnet" in html
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
            auszahlungen=[],
            einzahlungen=[],
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

    def test_zahlungen_zeilenweise_und_doppelte_posten(self) -> None:
        text = "Baukosten 2.000.000 €\nAuszahlung 2026: 500.000 €\nAuszahlung 2027: 1.500.000 €\n"
        leer: dict[str, Any] = {"foerderung": None, "eigenanteil": None, "einzahlungen": [], "laufend_gesamt": None}
        zeilen = [
            {"jahr": 2026, "euro": 500000, "beleg": "Auszahlung 2026: 500.000 €"},
            {"jahr": 2027, "euro": 1500000, "beleg": "Auszahlung 2027: 1.500.000 €"},
        ]
        antwort = _antwort(
            gesamtkosten={"euro": 2000000, "beleg": "Baukosten 2.000.000 €"},
            auszahlungen=zeilen,
            auszahlungen_beleg="",
            laufend=[],
            **leer,
        )
        daten = finanzcheck.pruefen(antwort, text).daten
        assert [(z["jahr"], z["euro"]) for z in daten["zahlungen"]] == [(2026, 500000), (2027, 1500000)]
        # Eine Zeile mit zwei Jahren ordnet nichts eindeutig zu: die ganze Reihe fehlt
        zeilen[1] = {"jahr": 2027, "euro": 1500000, "beleg": "2026: 500.000 € Auszahlung 2027: 1.500.000 €"}
        daten = finanzcheck.pruefen(antwort, text).daten
        assert daten["zahlungen"] == [] and daten["zahlungen_unstimmig"]

        laufend = [*ANTWORT["laufend"], ANTWORT["laufend"][1]]
        assert len(finanzcheck.pruefen(_antwort(laufend=laufend), DOKUMENT).daten["laufend"]) == 4

        text = "Die Kosten stehen noch nicht fest."
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg="Die Kosten stehen noch nicht fest.")
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_KEINE
        text = "Finanzielle Auswirkungen: keine"
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg="Finanzielle Auswirkungen: keine")
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK

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
            auszahlungen=[],
            einzahlungen=[],
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
        assert anzeige.saetze == ["Das belastet den Haushalt jedes Jahr mit 50.000 Euro."]
        assert anzeige.seo == "Kosten laut Vorlage: 50.000 € pro Jahr im Haushalt."
        haushalt = anzeige.karten.haushalt  # type: ignore[union-attr]
        assert haushalt is not None and haushalt.titel == "Belastung für den Haushalt, jedes Jahr"

    def test_errechnete_jahressumme_gekennzeichnet(self) -> None:
        ergebnis = finanzcheck.pruefen(_antwort(laufend_gesamt=None, laufend_ab=None), DOKUMENT)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None and anzeige.karten.haushalt is not None
        assert anzeige.karten.haushalt.wert == "124.000 €"
        assert anzeige.karten.haushalt.titel == "Belastung für den Haushalt, jedes Jahr"
        assert "Aus den Einzelposten zusammengerechnet." in anzeige.karten.haushalt.text


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
            auszahlungen=[],
            einzahlungen=[],
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
            auszahlungen=[],
            einzahlungen=[],
            deckung="unklar",
            im_haushalt="unklar",
        )
        ergebnis = finanzcheck.pruefen(antwort, text)
        assert ergebnis.status == finanzcheck.STATUS_OK and ergebnis.daten["eigenanteil"]["euro"] == 0
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and "Münster zahlt selbst nichts dazu." in anzeige.saetze

    def test_unvollstaendige_aufteilung_ohne_summe(self) -> None:
        laufend = [*ANTWORT["laufend"][:3], {"posten": "zinsen", "euro": 99999, "beleg": "erfunden 99.999"}]
        ergebnis = finanzcheck.pruefen(_antwort(laufend=laufend, laufend_gesamt=None), DOKUMENT)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None
        assert anzeige.karten is not None and anzeige.karten.haushalt is None

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


def test_gleiche_fundstelle_in_anlagen_mit_verschiedener_einheit_ist_mehrdeutig() -> None:
    from insight_ai.services.prompts import anlage_block

    text = "\n\n".join(
        [
            anlage_block("Kostenplan A", "Beträge in Euro\nBaukosten 1.200"),
            anlage_block("Kostenplan B", "Beträge in T€\nBaukosten 1.200"),
        ]
    )
    leer: dict[str, Any] = {
        "foerderung": None,
        "eigenanteil": None,
        "laufend": [],
        "auszahlungen": [],
        "einzahlungen": [],
        "laufend_gesamt": None,
    }
    for euro in (1200, 1200000):
        antwort = _antwort(gesamtkosten={"euro": euro, "beleg": "Baukosten 1.200"}, **leer)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN


def test_tabelle_spaltenweise_und_zeilenweise() -> None:
    material = finanzcheck._Material("Auszahlungen 2028\n2029\n2.000.000\n1.500.000\nRest")
    assert finanzcheck._tabelle(material, "Auszahlungen 2028 2029 2.000.000 1.500.000") == [
        (2028, 2000000),
        (2029, 1500000),
    ]
    material = finanzcheck._Material("Jahr Betrag in T€\n2028 1.200\n2029 800 €\nZeile 02 25,81")
    # „1.200“ im T€-Kopf ist 1,2 Mio. €; „800 €“ hat eine eigene Einheit; Prozente und Zeilennummern zählen nicht
    assert finanzcheck._tabelle(material, "2028 1.200 2029 800 €") == [(2028, 1200000), (2029, 800)]
    assert finanzcheck._tabelle(material, "2028 1.200 2029") is None


def test_dokumente_lesen_ganz_und_gerecht_gekuerzt() -> None:
    from insight_core.models import OParlFile

    kommune = _kommune()
    vorgang = _vorgang(kommune, text="Beschlussvorlage " + "x" * 50)
    OParlFile.objects.create(
        external_id=f"{vorgang.external_id}/anlage-3",
        body=kommune,
        paper=vorgang,
        name="Anlage 3 Folgelastenberechnung",
        download_url=f"{vorgang.external_id}/anlage-3.pdf",
        mime_type="application/pdf",
        text_extraction_status="completed",
        text_content="Folgelasten " + "y" * 500,
    )
    ganz = finanzcheck.dokumente_lesen(vorgang.files.all())
    assert "Anlage 3 Folgelastenberechnung" in ganz and "y" * 500 in ganz and "gekürzt" not in ganz
    # Knappes Budget: die kleine Anlage ganz, die große mit dem Rest und Vermerk
    knapp = finanzcheck.dokumente_lesen(vorgang.files.all(), budget=200)
    assert "x" * 50 in knapp and "y" * 500 not in knapp and "[… gekürzt]" in knapp


def test_aeltere_fassung_wird_neu_erstellt() -> None:
    kommune = _kommune()
    TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
    vorgang = _vorgang(kommune, text=DOKUMENT)
    OParlPaper.objects.filter(pk=vorgang.pk).update(finanzcheck={"status": "ok", "fassung": 1, "daten": {}})
    assert [p.pk for p in finanzcheck.kandidaten()] == [vorgang.pk]
    OParlPaper.objects.filter(pk=vorgang.pk).update(
        finanzcheck={"status": "ok", "fassung": finanzcheck.FASSUNG, "daten": {}}
    )
    assert finanzcheck.kandidaten() == []


class TestBefundeCodeRabbit3:
    """CodeRabbit zu PR #1526: engere Prüfung von Einheit, Feld, Einordnung und „keine Kosten“."""

    LEER: dict[str, Any] = {
        "foerderung": None,
        "eigenanteil": None,
        "laufend": [],
        "auszahlungen": [],
        "einzahlungen": [],
        "laufend_gesamt": None,
    }

    def test_keine_kosten_nur_als_feste_wendung_ohne_zahl(self) -> None:
        text = "Es entstehen keine zusätzlichen Mittel; die Kosten betragen 20.000 Euro."
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg=text)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_KEINE
        text = "Finanzielle Auswirkungen: keine"
        antwort = _antwort(finanzen="keine_kosten", keine_kosten_beleg=text)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK

    def test_keine_kosten_ohne_ki(self) -> None:
        ergebnis = finanzcheck._ohne_ki("Finanzielle Auswirkungen:\nEs entstehen der Stadt keine Kosten.\nBegründung")
        assert ergebnis.status == finanzcheck.STATUS_OK and ergebnis.grund == finanzcheck.OHNE_KI
        assert ergebnis.daten["keine_kosten_beleg"] == "Es entstehen der Stadt keine Kosten"
        assert finanzcheck._ohne_ki("Der Rat nimmt den Bericht zur Kenntnis.").status == finanzcheck.STATUS_KEINE

    def test_waehrung_der_ki_zaehlt_nicht(self) -> None:
        text = "Kostenplan in T€\nBaukosten 1.200\n"
        antwort = _antwort(gesamtkosten={"euro": 1200, "beleg": "Baukosten 1.200 €"}, **self.LEER)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN
        antwort = _antwort(gesamtkosten={"euro": 1200000, "beleg": "Baukosten 1.200 €"}, **self.LEER)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK

    def test_einheit_aus_dem_naechsten_tabellenkopf(self) -> None:
        text = "Übersicht in T€\nPlanung 500\nKostenaufstellung in Euro\nBaukosten 1.200.000\n"
        antwort = _antwort(gesamtkosten={"euro": 1200000, "beleg": "Baukosten 1.200.000"}, **self.LEER)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK
        # „500 T€“ hinter einer Zahl ist kein Tabellenkopf
        text = "Die Planung kostet rund 500 T€.\nBaukosten 1.200.000\n"
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_OK
        # Kein Kopf davor, „T€“ erst weiter unten: Einheit offen
        text = "Baukosten 1.200.000\nTabelle in T€\nPlanung 500\n"
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN

    def test_betrag_braucht_passendes_wort(self) -> None:
        text = "Das Land gibt Zuwendungen in Höhe von 4.650.000 €.\n"
        antwort = _antwort(gesamtkosten={"euro": 4650000, "beleg": "Zuwendungen in Höhe von 4.650.000 €"}, **self.LEER)
        assert finanzcheck.pruefen(antwort, text).status == finanzcheck.STATUS_VERWORFEN

    def test_einordnung_braucht_passenden_wortlaut(self) -> None:
        text = DOKUMENT + "Der Förderbescheid liegt noch nicht vor.\n"
        for beleg in (
            "Der Förderbescheid liegt noch nicht vor.",
            "Für die Maßnahme werden Fördermittel nach den Förderrichtlinien Nahmobilität bzw. kommunaler "
            "Straßenbau beantragt.",
        ):
            antwort = _antwort(foerderung_stand="zugesagt", foerderung_stand_beleg=beleg)
            daten = finanzcheck.pruefen(antwort, text).daten
            assert daten["foerderung_stand"] == "unklar" and daten["foerderung_stand_beleg"] == ""
        daten = finanzcheck.pruefen(ANTWORT, DOKUMENT).daten
        assert (daten["foerderung_stand"], daten["im_haushalt"], daten["deckung"]) == (
            "beantragt",
            "teilweise",
            "umschichtung",
        )

    def test_balken_nur_wenn_foerderung_in_die_kosten_passt(self) -> None:
        daten = {
            "finanzen": "kosten",
            "gesamtkosten": {"euro": 1000000, "beleg": ""},
            "foerderung": {"euro": 1005000, "beleg": ""},
            "eigenanteil": {"euro": 0, "beleg": ""},
            "zahlungen": [{"jahr": 2028, "euro": 1000000, "richtung": "ausgabe", "beleg": ""}],
            "zahlungen_vollstaendig": True,
        }
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None and anzeige.karten.bau is not None
        assert anzeige.karten.bau.segmente == []
        # Ein Zahlungsjahr: keine Säulen, das Jahr steht im Titel
        assert anzeige.karten.saeulen == [] and anzeige.karten.bau.titel == "Kosten, 2028"

    def test_vorgang_gesperrt_kein_zweiter_aufruf(self) -> None:
        from django.core.cache import cache

        vorgang = _vorgang(_kommune(), text=DOKUMENT)
        anbieter = _Anbieter(ANTWORT)
        cache.add(f"insight:finanzcheck:sperre:{vorgang.pk}", 1, 60)
        try:
            assert finanzcheck.erstellen(vorgang, provider=anbieter) is None and anbieter.aufrufe == []
        finally:
            cache.delete(f"insight:finanzcheck:sperre:{vorgang.pk}")
        assert finanzcheck.erstellen(vorgang, provider=anbieter) is not None and len(anbieter.aufrufe) == 1

    def test_verschobene_anlage_verwirft(self) -> None:
        kommune = _kommune()
        vorgang = _vorgang(kommune, text=DOKUMENT)
        anderer = _vorgang(kommune, text=None)

        class _Verschieber(_Anbieter):
            def chat_completion(self, *args: Any, **kwargs: Any) -> Any:
                vorgang.files.update(paper=anderer)
                return super().chat_completion(*args, **kwargs)

        finanzcheck.erstellen(vorgang, provider=_Verschieber(ANTWORT))
        vorgang.refresh_from_db()
        assert vorgang.finanzcheck is None

    def test_hoechstens_null_fragt_nicht(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from django.core.management import call_command

        aufrufe: list[int] = []

        def nachziehen(*, hoechstens: int) -> int:
            aufrufe.append(hoechstens)
            return 0

        monkeypatch.setattr(finanzcheck, "nachziehen", nachziehen)
        call_command("finanzcheck", "--nachziehen", "--hoechstens", "0")
        assert aufrufe == [0]


class TestBefundeCodeRabbit1545:
    """CodeRabbit zu PR #1545 (Kostencheck, Fassung 2)."""

    def test_zahl_mit_einheit_ist_kein_jahr(self) -> None:
        material = finanzcheck._Material("Auszahlungen 2028 2000 €")
        assert finanzcheck._tabelle(material, "Auszahlungen 2028 2000 €") == [(2028, 2000)]
        assert finanzcheck._jahre_in("Auszahlungen 2028 2000 €") == [2028]

    def test_tausend_ohne_tausenderpunkt_unter_tabellenkopf(self) -> None:
        material = finanzcheck._Material("Beträge in T€\nAuszahlungen 2028 2029\n500 1.200\n")
        assert finanzcheck._tabelle(material, "Auszahlungen 2028 2029 500 1.200") == [
            (2028, 500000),
            (2029, 1200000),
        ]

    def test_vertauschte_richtung_fehlt(self) -> None:
        antwort = _antwort(
            auszahlungen=ANTWORT["einzahlungen"],
            auszahlungen_beleg=ANTWORT["einzahlungen_beleg"],
            einzahlungen=ANTWORT["auszahlungen"],
            einzahlungen_beleg=ANTWORT["auszahlungen_beleg"],
            gesamtkosten=None,
            foerderung=None,
            eigenanteil=None,
        )
        daten = finanzcheck.pruefen(antwort, DOKUMENT).daten
        assert daten["zahlungen"] == [] and daten["zahlungen_unstimmig"]

    def test_doppelte_einzelzeile_zaehlt_einmal(self) -> None:
        text = "Baukosten 500.000 €\nAuszahlung 2028: 500.000 €\n"
        zeile = {"jahr": 2028, "euro": 500000, "beleg": "Auszahlung 2028: 500.000 €"}
        antwort = _antwort(
            gesamtkosten={"euro": 500000, "beleg": "Baukosten 500.000 €"},
            foerderung=None,
            eigenanteil=None,
            laufend=[],
            laufend_gesamt=None,
            auszahlungen=[zeile, dict(zeile)],
            auszahlungen_beleg="",
            einzahlungen=[],
        )
        daten = finanzcheck.pruefen(antwort, text).daten
        assert [(z["jahr"], z["euro"]) for z in daten["zahlungen"]] == [(2028, 500000)]

    def test_fehlende_einzahlungen_bei_foerderung(self) -> None:
        antwort = _antwort(einzahlungen=[], einzahlungen_beleg="", eigenanteil=None)
        ergebnis = finanzcheck.pruefen(antwort, DOKUMENT)
        assert ergebnis.daten["einzahlungen_fehlen"]
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None
        assert "Wann das Fördergeld kommt, ließ sich aus der Vorlage nicht ermitteln" in anzeige.karten.offen

    def test_nachmeldung_nur_mit_jahr_des_zielhaushalts(self) -> None:
        beleg = "für die Jahre 2028 und 2031 werden zum Haushaltsplanentwurf 2028 angemeldet"
        daten = finanzcheck.pruefen(_antwort(nachmeldung={"jahr": 2031, "beleg": beleg}), DOKUMENT).daten
        assert daten["nachmeldung"] is None

    def test_kostenstand_braucht_ganzes_datum_und_bezug(self) -> None:
        text = DOKUMENT + "Baubeginn am 11.02.2026.\n"
        for datum in ("1.02.2026", "11.02.2026"):
            antwort = _antwort(kostenstand={"datum": datum, "beleg": "Baubeginn am 11.02.2026."})
            assert finanzcheck.pruefen(antwort, text).daten["kostenstand"] is None

    def test_verneinung_der_preissteigerung_im_selben_satzteil(self) -> None:
        satz = "Preissteigerungen sind eingerechnet, nicht jedoch zusätzliche Planungskosten."
        text = DOKUMENT + satz + "\n"
        daten = finanzcheck.pruefen(_antwort(preissteigerung="nicht_enthalten", preissteigerung_beleg=satz), text).daten
        assert daten["preissteigerung"] == "unklar"
        daten = finanzcheck.pruefen(_antwort(preissteigerung="enthalten", preissteigerung_beleg=satz), text).daten
        assert daten["preissteigerung"] == "enthalten"

    def test_foerdergeld_auch_ohne_balken(self) -> None:
        daten = {
            "finanzen": "kosten",
            "gesamtkosten": {"euro": 500000, "beleg": ""},
            "foerderung": {"euro": 500000, "beleg": ""},
            "foerderung_stand": "zugesagt",
            "eigenanteil": {"euro": 0, "beleg": ""},
        }
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None and anzeige.karten.bau is not None
        assert anzeige.karten.bau.segmente == []
        assert anzeige.karten.bau.text == "Fördergeld 500.000 € (zugesagt). Münster zahlt selbst nichts dazu."

    def test_summe_ueber_jahre_nur_mit_belegtem_bauanteil(self) -> None:
        text = "Baukosten 1.000.000 €\nUnterhaltung jährlich 10.000 €\nNutzungsdauer: 40 Jahre\n"
        antwort = _antwort(
            gesamtkosten={"euro": 1000000, "beleg": "Baukosten 1.000.000 €"},
            foerderung=None,
            eigenanteil=None,
            auszahlungen=[],
            einzahlungen=[],
            laufend=[{"posten": "unterhalt", "euro": 10000, "beleg": "Unterhaltung jährlich 10.000 €"}],
            laufend_gesamt={"euro": 10000, "beleg": "Unterhaltung jährlich 10.000 €"},
            laufend_ab=None,
        )
        ergebnis = finanzcheck.pruefen(antwort, text)
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None and anzeige.karten.haushalt is not None
        assert "Zusammen" not in anzeige.karten.haushalt.text
        assert "Anteil am Bau" not in " ".join(anzeige.saetze)

    def test_kuerzen_nur_an_ganzen_zeilen(self) -> None:
        vorgang = _vorgang(_kommune(), text="Zeile eins\nBaukosten 1.200.000 €")
        text = finanzcheck.dokumente_lesen(vorgang.files.all(), budget=25)
        assert "Zeile eins" in text and "1.2" not in text and "[… gekürzt]" in text

    @pytest.mark.parametrize(
        ("ergebnis", "erwartet"),
        [
            ("einstimmig mit Änderungen beschlossen", True),
            ("nicht mit Änderungen beschlossen", False),
            ("ohne Änderungen beschlossen", False),
            ("abgelehnt", False),
            ("Mit Änderungen entsprechend der Empfehlung des Ausschusses beschlossen.", True),
            (
                "Der Rat hat die Vorlage mit Änderungen beschlossen. Die Empfehlung des Ausschusses wurde berücksichtigt.",
                True,
            ),
        ],
    )
    def test_mit_aenderungen_nur_bei_beschluss(self, ergebnis: str, erwartet: bool) -> None:
        assert finanzcheck.mit_aenderungen_beschlossen(ergebnis) is erwartet

    def test_ohne_ki_nennt_keine_ki(self) -> None:
        daten = {"finanzen": "keine_kosten", "keine_kosten_beleg": "Es entstehen keine Kosten"}
        paper = SimpleNamespace(
            pk=1,
            body=SimpleNamespace(name="Stadt Münster"),
            finanzcheck={"status": "ok", "grund": finanzcheck.OHNE_KI, "daten": daten},
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.keine_kosten and not anzeige.mit_ki


class TestBefundeCodeRabbit1545Runde2:
    """Zweite Runde CodeRabbit zu PR #1545."""

    def test_prozent_ist_kein_betrag_unter_tausend_kopf(self) -> None:
        material = finanzcheck._Material("Beträge in T€\nAuszahlungen 2028 2029 1.200 10 %\n")
        assert finanzcheck._tabelle(material, "Auszahlungen 2028 2029 1.200 10 %") is None

    def test_unberuecksichtigt_ist_nicht_eingerechnet(self) -> None:
        satz = "Preissteigerungen bleiben unberücksichtigt."
        text = DOKUMENT + satz + "\n"
        enthalten = _antwort(preissteigerung="enthalten", preissteigerung_beleg=satz)
        assert finanzcheck.pruefen(enthalten, text).daten["preissteigerung"] == "unklar"
        ausgeschlossen = _antwort(preissteigerung="nicht_enthalten", preissteigerung_beleg=satz)
        assert finanzcheck.pruefen(ausgeschlossen, text).daten["preissteigerung"] == "nicht_enthalten"
        nur_stichwort = "Die Preissteigerungen der letzten Jahre waren hoch."
        antwort = _antwort(preissteigerung="enthalten", preissteigerung_beleg=nur_stichwort)
        assert finanzcheck.pruefen(antwort, DOKUMENT + nur_stichwort + "\n").daten["preissteigerung"] == "unklar"

    def test_widerspruch_in_der_finanzierung(self) -> None:
        daten = {
            "finanzen": "kosten",
            "gesamtkosten": {"euro": 1000000, "beleg": ""},
            "foerderung": {"euro": 400000, "beleg": ""},
            "foerderung_stand": "zugesagt",
            "eigenanteil": {"euro": 700000, "beleg": ""},
        }
        paper = SimpleNamespace(
            pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": daten}
        )
        anzeige = finanzcheck.anzeige(paper)
        assert anzeige is not None and anzeige.karten is not None
        assert not anzeige.karten.stimmt
        assert "Fördergeld und Anteil von Münster ergeben laut Vorlage nicht die Gesamtkosten" in anzeige.karten.offen

    def test_nur_einzahlungen_bei_belegten_kosten_unvollstaendig(self) -> None:
        daten = finanzcheck.pruefen(_antwort(auszahlungen=[], auszahlungen_beleg=""), DOKUMENT).daten
        assert daten["zahlungen"] == [] and daten["zahlungen_unstimmig"]

    def test_empfehlung_ist_kein_beschluss_mit_aenderungen(self) -> None:
        assert not finanzcheck.mit_aenderungen_beschlossen(
            "Die Vorlage wird mit Änderungen zur Beschlussfassung empfohlen"
        )


class TestBefundeCodeRabbit1545Runde3:
    """Dritte Runde CodeRabbit zu PR #1545 (Folge-PR)."""

    def test_zeilennummer_im_ausschnitt_ist_kein_betrag(self) -> None:
        material = finanzcheck._Material("Beträge in T€\nAuszahlungen Zeile 02\n2028 2029\n500 1.200\n")
        assert finanzcheck._tabelle(material, "Auszahlungen Zeile 02 2028 2029 500 1.200") == [
            (2028, 500000),
            (2029, 1200000),
        ]

    def test_benachbarte_zeilen_mit_anderer_richtung(self) -> None:
        text = "Baukosten 600.000 €\nAuszahlungen 2028: 600.000 €\nEinzahlungen 2028: 100.000 €\n"
        material = finanzcheck._Material(text)
        assert finanzcheck._richtung_belegt(material, "Auszahlungen 2028: 600.000 €", "ausgabe")
        assert not finanzcheck._richtung_belegt(material, "Einzahlungen 2028: 100.000 €", "ausgabe")
        # Ohne Bezeichnung in der Fundstelle zählt die eigene Zeile, nicht die Zeile davor
        assert not finanzcheck._richtung_belegt(material, "2028: 100.000 €", "ausgabe")
        assert finanzcheck._richtung_belegt(material, "2028: 100.000 €", "einnahme")


def test_summe_ueber_jahre_nur_aus_derselben_folgekostenberechnung() -> None:
    """Nutzungsdauer und Folgekosten aus verschiedenen Dokumenten: keine Summe über alle Jahre."""
    from insight_ai.services.prompts import anlage_block

    text = "\n\n".join(
        [
            anlage_block("Beschlussvorlage", DOKUMENT.replace("Nutzungsdauer:", "Restlaufzeit:")),
            anlage_block("Anlage 4 Bauwerksbuch", "Nutzungsdauer: 40 Jahre"),
        ]
    )
    ergebnis = finanzcheck.pruefen(ANTWORT, text)
    paper = SimpleNamespace(
        pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
    )
    anzeige = finanzcheck.anzeige(paper)
    assert anzeige is not None and anzeige.karten is not None and anzeige.karten.haushalt is not None
    assert anzeige.karten.haushalt.wert == "124.000 €" and "Zusammen" not in anzeige.karten.haushalt.text
    # Aus derselben Folgelastenberechnung (hier ohne Anlagen: dasselbe Dokument) gilt die Summe
    assert "Zusammen rund 5 Mio. €" in TestAnzeige()._anzeige().karten.haushalt.text  # type: ignore[union-attr]


def _haushalt(text: str) -> finanzcheck.Kasten:
    ergebnis = finanzcheck.pruefen(ANTWORT, text)
    paper = SimpleNamespace(
        pk=1, body=SimpleNamespace(name="Stadt Münster"), finanzcheck={"status": "ok", "daten": ergebnis.daten}
    )
    anzeige = finanzcheck.anzeige(paper)
    assert anzeige is not None and anzeige.karten is not None and anzeige.karten.haushalt is not None
    return anzeige.karten.haushalt


class TestBefundeCodeRabbit1552:
    """CodeRabbit zu PR #1552."""

    def test_richtung_ueber_mehrere_zeilen_der_fundstelle(self) -> None:
        material = finanzcheck._Material("Einzahlungen 2028: 100.000 €\nAuszahlungen 2028\n600.000 €\n")
        assert finanzcheck._richtung_belegt(material, "2028 600.000 €", "ausgabe")
        assert not finanzcheck._richtung_belegt(material, "2028 600.000 €", "einnahme")

    def test_jahressumme_in_mehreren_anlagen(self) -> None:
        """Steht die Jahressumme auch in einer anderen Anlage, zählt die gemeinsame mit der Nutzungsdauer."""
        from insight_ai.services.prompts import anlage_block

        text = "\n\n".join(
            [
                anlage_block("Anlage 1 Kostenübersicht", "Folgelasten insgesamt 124.000 € pro Jahr"),
                anlage_block("Beschlussvorlage", DOKUMENT),
            ]
        )
        assert "Zusammen rund 5 Mio. €" in _haushalt(text).text

    def test_balken_ohne_belegte_dauer_ohne_jahre(self) -> None:
        from insight_ai.services.prompts import anlage_block

        getrennt = "\n\n".join(
            [
                anlage_block("Beschlussvorlage", DOKUMENT.replace("Nutzungsdauer:", "Restlaufzeit:")),
                anlage_block("Anlage 4 Bauwerksbuch", "Nutzungsdauer: 40 Jahre"),
            ]
        )
        assert [s.text for s in _haushalt(getrennt).segmente][0] == "Anteil am Bau"
        assert [s.text for s in _haushalt(DOKUMENT).segmente][0] == "Anteil am Bau, auf 40 Jahre verteilt"
