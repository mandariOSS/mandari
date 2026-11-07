# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlüsse als Liste der Entscheidungen (Issue #1270).

Die Seite „Beschlüsse“ zeigt die Tagesordnungspunkte mit Ergebnis, nach Sitzung gruppiert und neueste zuerst; Formalien
entfallen, Stellungnahmen der Verwaltung stehen als Satz unter ihrem Antrag, viele gleiche Vertagungen als eine Zeile.
Umschalter und Filter stehen in der Adresse, die Liste je Kommune lässt sich ohne Sitzung teilen. Die Beschlusskontrolle
bleibt als Reiter „Umsetzung“ erreichbar, wo die Verwaltung Daten liefert. Die Zahl der Abfragen hängt nicht von der Zahl
der Punkte ab.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
    VorgangUrheber,
)
from insight_core.services import beschlussliste
from insight_core.services.beschlussliste import Auswahl, gremiengruppe, ist_formalie

pytestmark = pytest.mark.django_db

RIS = "https://ris.beschlussstadt.example/oparl"
HTMX = {"HX-Request": "true"}


class Welt:
    """Kleiner RIS-Bestand einer Kommune: Gremien, Sitzungen, Punkte, Vorgänge und Beratungen."""

    def __init__(self, body: OParlBody) -> None:
        self.body = body
        self.zaehler = 0

    def _kennung(self, art: str) -> str:
        self.zaehler += 1
        return f"{RIS}/{art}/{self.zaehler}"

    def gremium(self, name: str, **felder: Any) -> OParlOrganization:
        return OParlOrganization.objects.create(external_id=self._kennung("org"), body=self.body, name=name, **felder)

    def sitzung(self, gremium: OParlOrganization, tage: float, **felder: Any) -> OParlMeeting:
        sitzung = OParlMeeting.objects.create(
            external_id=self._kennung("meeting"),
            body=self.body,
            name=f"Sitzung {gremium.name}",
            start=timezone.now() - timedelta(days=tage),
            **felder,
        )
        sitzung.organizations.add(gremium)
        return sitzung

    def vorgang(self, name: str, art: str = "Antrag", nummer: str = "", **felder: Any) -> OParlPaper:
        return OParlPaper.objects.create(
            external_id=self._kennung("paper"), body=self.body, name=name, paper_type=art, reference=nummer, **felder
        )

    def punkt(
        self, sitzung: OParlMeeting, name: str | None, ergebnis: str | None, *vorgaenge: OParlPaper, **felder: Any
    ) -> OParlAgendaItem:
        punkt = OParlAgendaItem.objects.create(
            external_id=self._kennung("item"),
            meeting=sitzung,
            name=name,
            result=ergebnis,
            number=str(self.zaehler),
            order=self.zaehler,
            **felder,
        )
        for vorgang in vorgaenge:
            OParlConsultation.objects.create(
                external_id=self._kennung("consultation"),
                body=self.body,
                paper=vorgang,
                agenda_item_external_id=punkt.external_id,
                meeting_external_id=sitzung.external_id,
            )
        return punkt


@pytest.fixture
def welt() -> Welt:
    source = OParlSource.objects.create(name="RIS Beschlussstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Stadt Beschlussstadt", slug="beschlussstadt"
    )
    return Welt(body)


@pytest.fixture
def besucher(client: Client, welt: Welt) -> Client:
    client.get(f"/insight/kommune/{welt.body.id}/")
    return client


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _liste(html: str) -> str:
    """Nur die Ergebnisse (ohne Kopfband und Navigation)."""
    treffer = re.search(r'<div id="beschluesse-ergebnis"[^>]*>(.*?)</section>\s*</main>', html, re.S)
    return treffer.group(1) if treffer else html


# =============================================================================
# Regeln
# =============================================================================


@pytest.mark.parametrize(
    "name",
    [
        "Eröffnung der Sitzung",
        "Eröffnung, Begrüßung und Feststellung der Beschlussfähigkeit",
        "Feststellung der ordnungsgemäßen Einberufung",
        "Feststellung der Tagesordnung",
        "Genehmigung der Niederschrift über die 12. Sitzung vom 01.10.2026",
        "Genehmigung des Protokolls",
        "Niederschrift der Sitzung vom 03.09.2026",
        "Änderung der Tagesordnung",
        "Tagesordnung",
        "Bestellung einer Schriftführerin",
        "Einführung und Verpflichtung von Ratsmitgliedern",
        "Schließung der Sitzung",
        "Einwohnerfragestunde",
        "Mitteilungen der Verwaltung",
        "Verschiedenes",
        "Bekanntgabe der in nichtöffentlicher Sitzung gefassten Beschlüsse",
        "1. Eröffnung der Sitzung",
        "TOP 2: Feststellung der Tagesordnung",
        "Ö 1.1 Genehmigung der Niederschrift",
    ],
)
def test_formalien(name: str) -> None:
    assert ist_formalie(name)


@pytest.mark.parametrize(
    "name",
    [
        "Feststellung des Jahresabschlusses 2025 der Stadtwerke",
        "Genehmigung einer Dringlichkeitsentscheidung",
        "Eröffnungsbilanz der Stadt",
        "Eröffnung des Hallenbads Nord",
        "Mitteilung zum Radwegebau an der Hauptstraße",
        "Verpflichtungsermächtigung für den Haushalt 2027",
        "Bestellung von Mitgliedern des Aufsichtsrats",
        "Niederschlagswassergebühren 2027",
        "Öffentliche Toiletten am Markt",
        "Antrag zur Änderung der Hauptsatzung",
        "",
    ],
)
def test_keine_formalien(name: str) -> None:
    assert not ist_formalie(name)


def test_formalien_auch_in_der_abfrage(welt: Welt) -> None:
    rat = welt.gremium("Rat")
    sitzung = welt.sitzung(rat, 3)
    welt.punkt(sitzung, "Genehmigung der Niederschrift", "genehmigt")
    welt.punkt(sitzung, "Feststellung des Jahresabschlusses 2025", "beschlossen")
    welt.punkt(sitzung, None, "beschlossen")
    gremien = beschlussliste.gremien_der_kommune(welt.body)
    zeit = beschlussliste.zeitraum("", wahlperiode=None)
    namen = set(beschlussliste.punkte(welt.body, Auswahl(), zeit=zeit, gremien=gremien).values_list("name", flat=True))
    assert namen == {"Feststellung des Jahresabschlusses 2025", None}


@pytest.mark.parametrize(
    ("name", "klasse", "gruppe"),
    [
        ("Rat", "", "rat"),
        ("Rat der Stadt Bonn", "", "rat"),
        ("Hauptausschuss", "", "rat"),
        ("Ausschuss für Mobilität und Verkehr", "", "rat"),
        ("Jugendhilfeausschuss", "", "rat"),
        ("Kreistag", "", "rat"),
        ("Bezirksvertretung Bad Godesberg", "", "bezirk"),
        ("Bezirksvertretung 1 (Innenstadt)", "", "bezirk"),
        ("Stadtbezirksrat Mitte", "", "bezirk"),
        ("Ortsbeirat 3", "", "bezirk"),
        ("Bezirksausschuss 1", "", "bezirk"),
        ("BV Beuel", "", "bezirk"),
        ("Gremium Süd", "Bezirksvertretung", "bezirk"),
        ("Integrationsrat", "", ""),
        ("Seniorenbeirat", "", ""),
        ("Aufsichtsrat der Stadtwerke GmbH", "", ""),
        ("Ausschuss der Gesellschafterversammlung GmbH", "", ""),
    ],
)
def test_gremiengruppe(name: str, klasse: str, gruppe: str) -> None:
    assert gremiengruppe(name, klasse) == gruppe


def test_auswahl_aus_der_adresse_ohne_unbekanntes() -> None:
    auswahl = Auswahl.aus_anfrage({"gruppe": "x", "ergebnis": "gewonnen", "zeitraum": "ewig", "von": " SPD "})
    assert auswahl == Auswahl(von="SPD")
    assert Auswahl(gruppe="rat", ergebnis="abgelehnt").adresse() == "?gruppe=rat&ergebnis=abgelehnt"
    assert Auswahl().adresse() == "" and not Auswahl().gefiltert
    assert Auswahl(zeitraum="woche", gremium="1").weitere_filter == 2


def test_zeitraum_texte() -> None:
    jetzt = timezone.make_aware(datetime(2026, 11, 7, 12, 0))
    assert beschlussliste.zeitraum("woche", wahlperiode=None, jetzt=jetzt).text == "vom 2. bis 8. November"
    assert beschlussliste.zeitraum("", wahlperiode=None, jetzt=jetzt).text == "vom 8. Oktober bis 7. November"
    seit = beschlussliste.zeitraum("wahlperiode", wahlperiode=date(2025, 11, 1), jetzt=jetzt)
    assert seit.text == "seit 1. November 2025" and seit.beginn.date() == date(2025, 11, 1)
    assert beschlussliste.zeitraum("wahlperiode", wahlperiode=None, jetzt=jetzt).text == "in den letzten 12 Monaten"


# =============================================================================
# Seite
# =============================================================================


class TestListe:
    def test_nach_sitzung_neueste_zuerst_ohne_formalien(self, besucher: Client, welt: Welt) -> None:
        rat = welt.gremium("Rat")
        haupt = welt.gremium("Hauptausschuss")
        alt = welt.sitzung(rat, 10)
        neu = welt.sitzung(haupt, 2)
        welt.punkt(alt, "Radweg am Kanal", "einstimmig beschlossen", welt.vorgang("Radweg am Kanal"))
        welt.punkt(neu, "Genehmigung der Niederschrift", "genehmigt")
        welt.punkt(neu, "Neue Bäume am Markt", "abgelehnt", welt.vorgang("Neue Bäume am Markt"))
        welt.punkt(neu, "Ohne Ergebnis", None)
        welt.punkt(neu, "Nichtöffentlich", "beschlossen", public=False)
        welt.punkt(welt.sitzung(rat, 1, cancelled=True), "Abgesagt", "beschlossen")
        welt.punkt(welt.sitzung(rat, -3), "Zukunft", "beschlossen")
        welt.punkt(welt.sitzung(rat, 40), "Zu alt", "beschlossen")

        antwort = besucher.get("/insight/beschluesse/")

        assert antwort.status_code == 200
        html = antwort.content.decode()
        liste = _liste(html)
        assert liste.index("Hauptausschuss") < liste.index("Rat") < liste.index("Radweg am Kanal")
        assert liste.index("Neue Bäume am Markt") < liste.index("Radweg am Kanal")
        for fehlt in ("Genehmigung der Niederschrift", "Ohne Ergebnis", "Nichtöffentlich", "Abgesagt", "Zukunft"):
            assert fehlt not in liste, fehlt
        assert "Zu alt" not in liste, "Voreinstellung: letzte 30 Tage"
        assert "2 Ergebnisse in 2 Sitzungen vom" in _text(liste)
        assert 'data-ergebnis="beschlossen"' in liste and 'data-ergebnis="abgelehnt"' in liste
        assert "einstimmig beschlossen" in liste, "Rohtext als Erklärung am Etikett"
        assert f'href="/insight/termine/{neu.pk}/"' in liste and "Zur Sitzung" in liste
        assert '<meta name="robots" content="index, follow">' in html

    def test_stellungnahmen_unter_dem_antrag(self, besucher: Client, welt: Welt) -> None:
        bv = welt.gremium("Bezirksvertretung Nord")
        sitzung = welt.sitzung(bv, 3)
        antrag = welt.vorgang("Gefahrenstelle Radweg", "Bürgerantrag", "260745")
        welt.punkt(sitzung, "Gefahrenstelle Radweg", "vertagt", antrag)
        stellungnahme = welt.vorgang("Gefahrenstelle Radweg", "Stellungnahme der Verwaltung", "260745-01 ST")
        welt.punkt(sitzung, "Gefahrenstelle Radweg: Stellungnahme", "zur Kenntnis genommen", stellungnahme)
        # Über superordinatedPaper der Quelle und am selben Punkt
        anfrage = welt.vorgang("Bushaltestelle Siemensstraße", "Anfrage", "261553")
        antwort = welt.vorgang(
            "Antwort zur Bushaltestelle",
            "Beantwortung einer Anfrage",
            "A-77",
            raw_json={"superordinatedPaper": [anfrage.external_id]},
        )
        welt.punkt(sitzung, "Bushaltestelle Siemensstraße", "zur Kenntnis genommen", anfrage)
        welt.punkt(sitzung, "Antwort zur Bushaltestelle", "zur Kenntnis genommen", antwort)
        hallenbad = welt.vorgang("Hallenbad", "Antrag", "100")
        welt.punkt(sitzung, "Hallenbad", "beschlossen", hallenbad, welt.vorgang("Hallenbad", "Stellungnahme", "100-1"))
        # Ohne Antrag in der Sitzung bleibt die Antwort eine eigene Zeile
        welt.punkt(sitzung, "Antwort zum Spielplatz", "zur Kenntnis genommen", welt.vorgang("Spielplatz", "Antwort"))

        liste = _liste(besucher.get("/insight/beschluesse/").content.decode())

        assert liste.count("Gefahrenstelle Radweg") == 1, "die Stellungnahme ist keine eigene Zeile"
        assert liste.count("Mit Stellungnahme der Verwaltung.") == 2
        assert "Antwort zur Bushaltestelle" not in liste and "Mit Antwort der Verwaltung." in liste
        assert "Spielplatz" in liste, "ohne Antrag in der Sitzung eine eigene Zeile"
        assert f'href="/insight/vorgaenge/{antrag.pk}/"' in liste
        assert "Bürgerantrag" in liste and "Verwaltung" in liste, "Absender nach der Vorlagenart"

    def test_viele_vertagungen_als_eine_zeile(self, besucher: Client, welt: Welt) -> None:
        haupt = welt.gremium("Hauptausschuss")
        sitzung = welt.sitzung(haupt, 3)
        welt.punkt(sitzung, "Tarifkorridor Taxi", "vertagt", welt.vorgang("Tarifkorridor Taxi"))
        for nummer in range(6):
            welt.punkt(
                sitzung, f"Anfrage Nummer {nummer}", "vertagt", welt.vorgang(f"Anfrage {nummer}", "Große Anfrage")
            )

        liste = _liste(besucher.get("/insight/beschluesse/").content.decode())

        assert "6 Punkte vertagt" in liste and "Vorlagenart: Große Anfrage." in liste
        assert "<summary" in liste and "Welche Punkte?" in liste and "Anfrage 5" in liste
        assert "Tarifkorridor Taxi" in liste, "ein einzelner vertagter Antrag bleibt eine Zeile"
        assert "7 Ergebnisse in 1 Sitzung" in _text(liste)

    def test_abfragen_unabhaengig_von_der_zahl_der_punkte(self, besucher: Client, welt: Welt) -> None:
        rat = welt.gremium("Rat")
        fraktion = welt.gremium("SPD-Fraktion", classification="Fraktion")
        vorgang = welt.vorgang("Radweg")
        VorgangUrheber.objects.create(
            paper_id=vorgang.pk, body_id=welt.body.pk, bezeichnung="SPD-Fraktion", kurzform="SPD", herkunft="title"
        )
        welt.punkt(welt.sitzung(rat, 2), "Radweg", "beschlossen", vorgang)
        del fraktion

        def zaehlen() -> int:
            with CaptureQueriesContext(connection) as erfasst:
                assert besucher.get("/insight/beschluesse/", headers=HTMX).status_code == 200
            return len(erfasst.captured_queries)

        wenige = zaehlen()
        for tag in range(3, 9):
            sitzung = welt.sitzung(rat, tag)
            for nummer in range(5):
                v = welt.vorgang(f"Vorgang {tag}-{nummer}", nummer=f"V/{tag}/{nummer}")
                VorgangUrheber.objects.create(
                    paper_id=v.pk, body_id=welt.body.pk, bezeichnung="SPD-Fraktion", kurzform="SPD", herkunft="title"
                )
                welt.punkt(sitzung, f"Punkt {tag}-{nummer}", "beschlossen", v, welt.vorgang("ST", "Stellungnahme"))
        assert zaehlen() == wenige


class TestFilter:
    @pytest.fixture
    def bestand(self, welt: Welt) -> dict[str, Any]:
        rat = welt.gremium("Rat")
        bv = welt.gremium("Bezirksvertretung Süd")
        welt.gremium("CDU-Fraktion", classification="Fraktion")
        cdu_antrag = welt.vorgang("Mehr Bänke", "Antrag")
        VorgangUrheber.objects.create(
            paper_id=cdu_antrag.pk, body_id=welt.body.pk, bezeichnung="CDU-Fraktion", kurzform="CDU", herkunft="title"
        )
        sitzung_rat = welt.sitzung(rat, 2)
        sitzung_bv = welt.sitzung(bv, 3)
        welt.punkt(sitzung_rat, "Mehr Bänke", "abgelehnt", cdu_antrag)
        welt.punkt(sitzung_rat, "Haushalt 2027", "beschlossen", welt.vorgang("Haushalt 2027", "Beschlussvorlage"))
        welt.punkt(sitzung_bv, "Spielplatz Süd", "einstimmig beschlossen", welt.vorgang("Spielplatz Süd", "Antrag"))
        welt.punkt(welt.sitzung(rat, 100), "Altes Thema", "beschlossen")
        return {"rat": rat, "bv": bv}

    def test_umschalter_und_gremium(self, besucher: Client, bestand: dict[str, Any]) -> None:
        html = besucher.get("/insight/beschluesse/").content.decode()
        assert 'name="gruppe" value="bezirk"' in html and "Bezirksvertretungen" in html
        assert "CDU-Fraktion</option>" not in html, "Fraktionen sind keine Gremien der Auswahl"

        bezirke = _liste(besucher.get("/insight/beschluesse/", {"gruppe": "bezirk"}).content.decode())
        assert "Spielplatz Süd" in bezirke and "Haushalt 2027" not in bezirke
        rat = _liste(besucher.get("/insight/beschluesse/", {"gruppe": "rat"}).content.decode())
        assert "Haushalt 2027" in rat and "Spielplatz Süd" not in rat
        gremium = _liste(besucher.get("/insight/beschluesse/", {"gremium": str(bestand["bv"].pk)}).content.decode())
        assert "Spielplatz Süd" in gremium and "Mehr Bänke" not in gremium

    def test_eingebracht_von_ergebnis_und_zeitraum(self, besucher: Client, bestand: dict[str, Any]) -> None:
        cdu = _liste(besucher.get("/insight/beschluesse/", {"von": "CDU-Fraktion"}).content.decode())
        assert "Mehr Bänke" in cdu and "Haushalt 2027" not in cdu and ">CDU<" in cdu
        verwaltung = _liste(besucher.get("/insight/beschluesse/", {"von": "verwaltung"}).content.decode())
        assert "Haushalt 2027" in verwaltung and "Mehr Bänke" not in verwaltung
        abgelehnt = _liste(besucher.get("/insight/beschluesse/", {"ergebnis": "abgelehnt"}).content.decode())
        assert "Mehr Bänke" in abgelehnt and "Spielplatz Süd" not in abgelehnt
        beschlossen = _liste(besucher.get("/insight/beschluesse/", {"ergebnis": "beschlossen"}).content.decode())
        assert "Spielplatz Süd" in beschlossen and "Mehr Bänke" not in beschlossen
        jahr = _liste(besucher.get("/insight/beschluesse/", {"zeitraum": "jahr"}).content.decode())
        assert "Altes Thema" in jahr and "in den letzten 12 Monaten" in jahr

    def test_htmx_liefert_nur_die_liste_mit_sauberer_adresse(self, besucher: Client, bestand: dict[str, Any]) -> None:
        antwort = besucher.get(
            "/insight/beschluesse/", {"gruppe": "", "ergebnis": "abgelehnt", "zeitraum": "jahr"}, headers=HTMX
        )
        assert antwort["HX-Push-Url"] == "/insight/beschluesse/?ergebnis=abgelehnt&zeitraum=jahr"
        html = antwort.content.decode()
        assert "<html" not in html and "Mehr Bänke" in html
        assert '<span id="filter-zahl" hx-swap-oob="true"> (2)</span>' in html
        teilen = re.search(r'<a href="([^"]+)" data-action="copy-link"', html)
        assert teilen and unescape(teilen.group(1)) == (
            "http://testserver/insight/k/beschlussstadt/beschluesse/?ergebnis=abgelehnt&zeitraum=jahr"
        )
        assert "Filter zurücksetzen" in html

    def test_leerzustand_schlaegt_laengeren_zeitraum_vor(self, besucher: Client, bestand: dict[str, Any]) -> None:
        html = _liste(
            besucher.get("/insight/beschluesse/", {"zeitraum": "woche", "ergebnis": "vertagt"}).content.decode()
        )
        assert "Keine Ergebnisse für diese Auswahl" in html
        assert re.search(r'href="/insight/beschluesse/\?ergebnis=vertagt"[^>]*>Letzte 30 Tage zeigen</a>', html)
        assert "alle Filter zurücksetzen" in html

    @pytest.mark.parametrize(
        "params",
        [{"gremium": "kaputt"}, {"von": "x" * 900}, {"page": "999"}, {"page": "-1"}, {"ergebnis": "<script>"}],
    )
    def test_unsinnige_parameter_ohne_fehler(self, besucher: Client, bestand: dict[str, Any], params: Any) -> None:
        assert besucher.get("/insight/beschluesse/", params).status_code == 200

    def test_wahlperiode_nur_mit_bekanntem_beginn(self, besucher: Client, welt: Welt, bestand: dict[str, Any]) -> None:
        assert 'value="wahlperiode"' not in besucher.get("/insight/beschluesse/").content.decode()
        OParlLegislativeTerm.objects.create(
            external_id=f"{RIS}/term/1", body=welt.body, name="2025–2030", start_date=date(2025, 11, 1)
        )
        assert 'value="wahlperiode"' in besucher.get("/insight/beschluesse/").content.decode()


class TestAdresseUndReiter:
    def test_liste_je_kommune_ohne_sitzung(self, welt: Welt) -> None:
        welt.punkt(welt.sitzung(welt.gremium("Rat"), 2), "Radweg", "beschlossen")
        antwort = Client().get("/insight/k/beschlussstadt/beschluesse/", {"ergebnis": "beschlossen"})
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert "Radweg" in html
        assert '<link rel="canonical" href="' in html and "/insight/k/beschlussstadt/beschluesse/" in html
        assert '<meta name="robots" content="noindex, follow">' in html, "gefiltert nicht im Index"

    def test_umsetzung_als_reiter_nur_mit_daten(self, besucher: Client, welt: Welt) -> None:
        html = besucher.get("/insight/beschluesse/").content.decode()
        assert "Umsetzung</a>" not in html
        tenant = SessionTenant.objects.create(
            name="Verwaltung",
            slug="verwaltung",
            oparl_body=welt.body,
            insight_publish=True,
            implementation_publish=True,
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, organization=rat, name="Rat", start=timezone.now() - timedelta(days=7), is_public=True
        )
        SessionAgendaItem.objects.create(
            meeting=sitzung, number="1", name="Radweg Hafen", vote_result="approved", implementation_public=True
        )
        html = besucher.get("/insight/beschluesse/").content.decode()
        assert 'href="/insight/beschluesse/umsetzung/"' in html and 'aria-current="page"' in html
        umsetzung = besucher.get("/insight/beschluesse/umsetzung/").content.decode()
        assert "Radweg Hafen" in umsetzung and "Umsetzung steht an" in umsetzung
        assert 'href="/insight/beschluesse/"' in umsetzung, "Reiter zurück zu den Entscheidungen"

    def test_alte_adressen_der_beschlusskontrolle(self, besucher: Client) -> None:
        antwort = besucher.get("/insight/beschluesse/", {"status": "open", "gremium": "1"})
        assert antwort.status_code == 302
        assert antwort["Location"] == "/insight/beschluesse/umsetzung/?status=open&gremium=1"

    def test_beschluesse_immer_in_der_navigation(self, besucher: Client) -> None:
        html = besucher.get("/insight/vorgaenge/").content.decode()
        nav = re.search(r'<nav class="mt-5 px-3" aria-label="Bereiche">(.*?)</nav>', html, re.S)
        assert nav and 'href="/insight/beschluesse/"' in nav.group(1)
        assert nav.group(1).index("Sitzungen") < nav.group(1).index("Beschlüsse") < nav.group(1).index("Vorgänge")

    def test_startseite_fuehrt_zu_allen_beschluessen(self, besucher: Client, welt: Welt) -> None:
        welt.punkt(welt.sitzung(welt.gremium("Rat"), 2), "Radweg", "beschlossen")
        html = besucher.get("/insight/").content.decode()
        assert ">Alle Beschlüsse</a>" in html
