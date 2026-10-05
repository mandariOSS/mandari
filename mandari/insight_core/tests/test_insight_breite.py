# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight auf breiten Bildschirmen (Issue #841): fließende Breite, Zusatzspalten, keine Spalte voller „—“.

- Die Spalte „Funktion“ der Personenliste kommt aus den laufenden Mitgliedschaften – auch wenn das Hauptorgan nicht
  „Rat“ heißt und das RIS andere Rollennamen führt (vorher stand bei allen Personen nur „—“).
- Spalten, für die keine Zeile einen Wert hat, entfallen (Personen, Gremien, Sitzungen, Vorgänge, Mitgliedschaften).
- Die Übersicht trägt eine dritte Liste „Zuletzt beschlossen“, nur mit Ergebnissen.
- Bänder, Kopfbänder, Inhaltsrahmen und Fuß nutzen die fließende Obergrenze statt fester 72rem.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.template import Context, Template
from django.test import Client
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services.personen_liste import angaben_fuer, funktion_aus
from insight_core.templatetags.insight_listen import hat_wert, ist_leer

pytestmark = pytest.mark.django_db

RIS = "https://ris.breite.example/oparl"
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Breite-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Beispiel", slug="beispiel")


def _org(body: OParlBody, key: str, name: str, **extra: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/organization/{key}", body=body, name=name, **extra)


def _person(body: OParlBody, key: str, name: str, **extra: Any) -> OParlPerson:
    vorname, nachname = name.split(" ", 1)
    return OParlPerson.objects.create(
        external_id=f"{RIS}/person/{key}", body=body, name=name, given_name=vorname, family_name=nachname, **extra
    )


def _mitglied(person: OParlPerson, org: OParlOrganization, rolle: str | None, **extra: Any) -> OParlMembership:
    return OParlMembership.objects.create(
        external_id=f"{RIS}/membership/{person.pk}-{org.pk}", person=person, organization=org, role=rolle, **extra
    )


@pytest.fixture
def rat(body: OParlBody) -> dict[str, Any]:
    """Hauptorgan „Rat der Stadt Beispiel“ mit Rollennamen, die die alte feste Liste nicht kannte."""
    rat = _org(body, "rat", "Rat der Stadt Beispiel", classification="Rat", organization_type="Gremium")
    fraktion = _org(body, "f1", "Fraktion Mitte", short_name="Mitte", organization_type="Fraktion")
    ausschuss = _org(body, "a1", "Hauptausschuss", classification="Ausschuss", organization_type="Gremium")
    jugend = _org(body, "a2", "Jugendhilfeausschuss", classification="Ausschuss", organization_type="Gremium")
    ob = _person(body, "ob", "Olga Beispiel", email="olga@example.org")
    vorsitz = _person(body, "fv", "Fritz Vorsitz")
    mitglied = _person(body, "m", "Mara Mitglied")
    buerger = _person(body, "sb", "Sven Sachkundig")
    ehemalig = _person(body, "eh", "Erika Ehemalig")
    _mitglied(ob, rat, "Oberbürgermeisterin")
    _mitglied(vorsitz, rat, "Mitglied")
    _mitglied(vorsitz, fraktion, "Vorsitzender")
    _mitglied(mitglied, rat, "")
    _mitglied(mitglied, fraktion, "Mitglied")
    _mitglied(mitglied, ausschuss, "Mitglied")
    _mitglied(mitglied, jugend, "Mitglied")
    _mitglied(buerger, ausschuss, "Sachkundiger Bürger")
    _mitglied(ehemalig, rat, "Ratsmitglied", end_date=date(2020, 1, 1))
    return {
        "body": body,
        "rat": rat,
        "fraktion": fraktion,
        "ausschuss": ausschuss,
        "ob": ob,
        "vorsitz": vorsitz,
        "mitglied": mitglied,
        "buerger": buerger,
        "ehemalig": ehemalig,
    }


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


def _kopfzeile(html: str) -> list[str]:
    """Spaltenköpfe der ersten Tabelle (ohne Bildschirmleser-Texte)."""
    thead = re.search(r"<thead>(.*?)</thead>", html, re.S)
    assert thead, "keine Tabelle"
    return [re.sub(r"<[^>]+>", "", th).strip() for th in re.findall(r"<th\b[^>]*>(.*?)</th>", thead.group(1), re.S)]


def _ausschnitt(muster: str, html: str) -> str:
    treffer = re.search(muster, html, re.S)
    assert treffer, muster
    return treffer.group(0)


def _tabelle(html: str) -> str:
    tabelle = re.search(r"<table.*?</table>", html, re.S)
    assert tabelle, "keine Tabelle"
    return tabelle.group(0)


# =============================================================================
# Bausteine
# =============================================================================


class TestHatWert:
    def test_platzhalter_gelten_als_leer(self) -> None:
        leer: list[object] = [None, "", " ", "—", "-", "–", 0, [], False]
        belegt: list[object] = ["Ratsmitglied", 3, ["Hauptausschuss"], date(2026, 1, 1)]
        for wert in leer:
            assert ist_leer(wert), wert
        for wert in belegt:
            assert not ist_leer(wert), wert

    def test_punkte_fuer_tiefe_und_schluessel(self) -> None:
        zeilen = [{"angaben": {"fraktion": None}}, {"angaben": {"fraktion": "Mitte"}}]
        assert hat_wert(zeilen, "angaben.fraktion")
        assert not hat_wert(zeilen[:1], "angaben.fraktion")
        assert not hat_wert([], "angaben.fraktion")

    def test_im_template(self) -> None:
        html = Template('{% load insight_listen %}{% if zeilen|hat_wert:"rolle" %}Spalte{% endif %}').render(
            Context({"zeilen": [{"rolle": "—"}, {"rolle": ""}]})
        )
        assert html == ""


class TestFunktion:
    def test_rangfolge_aus_den_mitgliedschaften(self, rat: dict[str, Any]) -> None:
        angaben = angaben_fuer([rat["ob"], rat["vorsitz"], rat["mitglied"], rat["buerger"], rat["ehemalig"]])
        assert angaben[rat["ob"].pk].funktion == "Oberbürgermeisterin"
        # Besondere Rolle in der Fraktion schlägt das Mandat
        assert angaben[rat["vorsitz"].pk].funktion == "Vorsitzender"
        # Mandat ohne eigene Rolle: „Ratsmitglied“ (Hauptorgan heißt nicht „Rat“)
        assert angaben[rat["mitglied"].pk].funktion == "Ratsmitglied"
        assert angaben[rat["buerger"].pk].funktion == "Sachkundiger Bürger"
        # Beendete Mitgliedschaften zählen nicht
        assert angaben[rat["ehemalig"].pk].funktion == ""

    def test_fraktion_und_gremien(self, rat: dict[str, Any]) -> None:
        angaben = angaben_fuer([rat["mitglied"]])[rat["mitglied"].pk]
        assert angaben.fraktion == rat["fraktion"]
        assert angaben.gremien == ["Hauptausschuss", "Jugendhilfeausschuss"]
        assert angaben.gremien_kurz == "Hauptausschuss, Jugendhilfeausschuss"

    def test_viele_gremien_gekuerzt(self, body: OParlBody) -> None:
        person = _person(body, "x", "Viel Beschaeftigt")
        for nummer in range(4):
            _mitglied(person, _org(body, f"g{nummer}", f"Ausschuss {nummer}"), "Mitglied")
        assert angaben_fuer([person])[person.pk].gremien_kurz == "Ausschuss 0, Ausschuss 1 und 2 weitere"

    def test_vorsitz_im_ausschuss_mit_gremium(self, body: OParlBody) -> None:
        person = _person(body, "v", "Vera Vorsitz")
        _mitglied(person, _org(body, "s", "Schulausschuss"), "Vorsitzende")
        assert funktion_aus(person.memberships.select_related("organization")) == "Vorsitzende, Schulausschuss"


# =============================================================================
# Seiten
# =============================================================================


class TestPersonenliste:
    def test_funktion_und_fraktion_statt_strichen(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get("/insight/personen/").content.decode()
        assert _kopfzeile(html) == ["Name", "Fraktion", "Funktion", "Gremien", "Kontakt", "Merken"]
        for text in ("Oberbürgermeisterin", "Vorsitzender", "Ratsmitglied", "Sachkundiger Bürger", "Mitte"):
            assert text in html
        assert "—" not in _tabelle(html)

    def test_spalten_ohne_werte_entfallen(self, body: OParlBody) -> None:
        _person(body, "a", "Anna Ohnealles")
        html = _client(body).get("/insight/personen/").content.decode()
        assert _kopfzeile(html) == ["Name", "Merken"]

    def test_htmx_suche_liefert_dieselben_spalten(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get("/insight/personen/?q=Mara", headers={"HX-Request": "true"}).content.decode()
        assert "Ratsmitglied" in html and "Mitte" in html

    def test_seitenwahl_behaelt_suche_und_hat_namen(self, body: OParlBody) -> None:
        for nummer in range(55):
            _person(body, f"p{nummer}", f"Paula Muster{nummer:02d}")
        html = _client(body).get("/insight/personen/?q=Paula Muster").content.decode()
        seiten = _ausschnitt(r'<nav aria-label="Seiten".*?</nav>', html)
        assert 'href="?q=Paula+Muster&amp;page=2"' in seiten
        assert 'aria-label="Nächste Seite"' in seiten
        assert 'aria-current="page"' in seiten


class TestGremienliste:
    def test_art_und_mitglieder(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get("/insight/gremien/?tab=all").content.decode()
        assert _kopfzeile(html) == ["Gremium", "Art", "Mitglieder", "Merken"]
        # Rat: drei laufende Mitgliedschaften (die beendete zählt nicht)
        zeile = _ausschnitt(r"Rat der Stadt Beispiel.*?</tr>", html)
        assert re.search(r">\s*3\s*<", zeile)
        assert "—" not in _tabelle(html)

    def test_sitzungsspalten_nur_mit_sitzungen(self, rat: dict[str, Any]) -> None:
        jetzt = timezone.now()
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1", body=rat["body"], name="Sitzung", start=jetzt + timedelta(days=3)
        )
        sitzung.organizations.add(rat["rat"])
        html = _client(rat["body"]).get("/insight/gremien/").content.decode()
        assert "Nächste Sitzung" in _kopfzeile(html)
        assert "Letzte Sitzung" not in _kopfzeile(html)


class TestSitzungenUndVorgaenge:
    def test_tagesordnung_und_ohne_ort(self, rat: dict[str, Any]) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1", body=rat["body"], name="Sitzung", start=timezone.now() + timedelta(days=2)
        )
        for nummer in range(3):
            OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/{nummer}", meeting=sitzung, number=str(nummer + 1), name="TOP"
            )
        html = _client(rat["body"]).get("/insight/termine/").content.decode()
        assert _kopfzeile(html) == ["Sitzung", "Datum", "Uhrzeit", "Tagesordnung", "Merken"]
        assert "3 Punkte" in html

    def test_stand_der_vorgaenge(self, rat: dict[str, Any]) -> None:
        paper = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/1", body=rat["body"], name="Neue Radwege", reference="V/1", date=date(2026, 3, 1)
        )
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/9", body=rat["body"], name="Sitzung", start=timezone.now() - timedelta(days=9)
        )
        sitzung.organizations.add(rat["rat"])
        top = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/item/9", meeting=sitzung, number="1", name="Radwege", result="beschlossen"
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/1",
            body=rat["body"],
            paper=paper,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
        html = _client(rat["body"]).get("/insight/vorgaenge/").content.decode()
        assert _kopfzeile(html) == ["Vorgang", "Vorlagen-Nr.", "Datum", "Stand", "Merken"]
        assert "beschlossen" in html


class TestUebersicht:
    def test_zuletzt_beschlossen_mit_vorgang(self, rat: dict[str, Any]) -> None:
        paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/2", body=rat["body"], name="Spielplatz am Park")
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/2", body=rat["body"], name="Sitzung", start=timezone.now() - timedelta(days=5)
        )
        sitzung.organizations.add(rat["rat"])
        top = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/item/2", meeting=sitzung, number="4", name="TOP 4", result="einstimmig beschlossen"
        )
        OParlAgendaItem.objects.create(external_id=f"{RIS}/item/3", meeting=sitzung, number="5", name="Ohne Ergebnis")
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/2",
            body=rat["body"],
            paper=paper,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
        html = _client(rat["body"]).get("/insight/").content.decode()
        assert "Zuletzt beschlossen" in html
        assert "Spielplatz am Park" in html and "einstimmig beschlossen" in html
        assert "Ohne Ergebnis" not in html
        assert f"/insight/vorgaenge/{paper.pk}/" in html
        assert "min-[1600px]:grid-cols-3" in html

    def test_ohne_ergebnisse_keine_dritte_spalte(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get("/insight/").content.decode()
        assert "Zuletzt beschlossen" not in html
        assert "min-[1600px]:grid-cols-3" not in html


class TestDetailseiten:
    def test_gremium_mit_fraktionen_und_randspalte(self, rat: dict[str, Any]) -> None:
        rat["rat"].website = "https://rat.example.org"
        rat["rat"].save()
        html = _client(rat["body"]).get(f"/insight/gremien/{rat['rat'].pk}/").content.decode()
        assert 'aria-label="Termine und Angaben"' in html
        assert "3 Mitglieder" in html
        mitglieder = _ausschnitt(r"Aktive Mitglieder.*?</table>", html)
        assert "Mitte" in mitglieder
        assert "—" not in mitglieder

    def test_person_mit_funktion_und_randspalte(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get(f"/insight/personen/{rat['ob'].pk}/").content.decode()
        assert 'aria-label="Kontakt und Termine"' in html
        assert "Oberbürgermeisterin" in html
        assert "olga@example.org" in html


class TestBreite:
    @pytest.mark.parametrize(
        "datei",
        [
            "cotton/insight/band.html",
            "cotton/insight/kopfband.html",
            "components/insight_footer.html",
            "pages/portal/home.html",
            "partials/search_ortsband.html",
        ],
    )
    def test_fliessende_obergrenze_statt_72rem(self, datei: str) -> None:
        inhalt = (TEMPLATES / datei).read_text(encoding="utf-8")
        assert "max-w-insight" in inhalt
        assert "max-w-6xl" not in inhalt

    def test_inhaltsrahmen_im_layout(self) -> None:
        assert 'class="max-w-insight mx-auto px-4 sm:px-8 py-6 lg:py-8"' in (TEMPLATES / "base_insight.html").read_text(
            encoding="utf-8"
        )


# =============================================================================
# Nachbesserung nach der Prüfung von PR #848
# =============================================================================


def _randspalte(html: str) -> str:
    return _ausschnitt(r'<aside class="min-w-0 space-y-10".*?</aside>', html)


def _ohne_kopf(html: str) -> str:
    """Seiteninhalt ohne Kopfzeile, Seitenleiste und Fuß (dort stehen Kommune und Bereich ohnehin)."""
    return _ausschnitt(r"<main.*?</main>", html)


class TestFraktionNurSichtbar:
    """Gelöschte und von Session zurückgenommene Fraktionen erscheinen nirgends als Fraktion einer Person."""

    def _alte_fraktionen(self, rat: dict[str, Any]) -> None:
        geloescht = _org(
            rat["body"], "f-alt", "Fraktion Geloescht", short_name="Geloescht", organization_type="Fraktion"
        )
        geloescht.deleted = True
        geloescht.save()
        zurueck = OParlOrganization.objects.create(
            external_id="https://mandari.example/session/x/api/oparl/organization/9/",
            body=rat["body"],
            name="Fraktion Zurueckgenommen",
            short_name="Zurueckgenommen",
            organization_type="Fraktion",
            deleted=True,
        )
        # Alphabetisch vor „Fraktion Mitte“: ohne Filter gewinnen sie
        for org in (geloescht, zurueck):
            _mitglied(rat["mitglied"], org, "Mitglied")
            _mitglied(rat["buerger"], org, "Mitglied")

    def test_fraktionshilfen_filtern(self, rat: dict[str, Any]) -> None:
        from insight_core.services import question_service

        self._alte_fraktionen(rat)
        assert question_service.get_faction(rat["mitglied"]) == rat["fraktion"]
        assert question_service.get_faction(rat["buerger"]) is None
        karte = question_service.get_faction_map([rat["mitglied"], rat["buerger"]])
        assert karte == {rat["mitglied"].pk: rat["fraktion"]}

    def test_gremienseite_ohne_alte_fraktionen(self, rat: dict[str, Any]) -> None:
        self._alte_fraktionen(rat)
        html = _client(rat["body"]).get(f"/insight/gremien/{rat['ausschuss'].pk}/").content.decode()
        mitglieder = _ausschnitt(r"Aktive Mitglieder.*?</table>", html)
        assert "Mitte" in mitglieder
        assert "Geloescht" not in mitglieder and "Zurueckgenommen" not in mitglieder

    def test_personenseite_nennt_die_fraktion_nur_im_kopf(self, rat: dict[str, Any]) -> None:
        self._alte_fraktionen(rat)
        html = _client(rat["body"]).get(f"/insight/personen/{rat['mitglied'].pk}/").content.decode()
        kopf = _ausschnitt(r'<header class="rounded-2xl.*?</header>', html)
        assert kopf.count(">Mitte</a>") == 1
        assert "Zurueckgenommen" not in kopf and "Geloescht" not in kopf
        # Außerhalb des Kopfs nur als Zeile der Mitgliedschaften („Fraktion Mitte“), nicht noch einmal als Angabe
        rest = _ohne_kopf(html).replace(kopf, "").replace("Fraktion Mitte", "")
        assert "Mitte" not in rest
        # Gelöschte Fraktionen auch nicht als laufende Mitgliedschaft
        assert "Geloescht" not in _ohne_kopf(html) and "Zurueckgenommen" not in _ohne_kopf(html)


class TestKeineDoppelungen:
    def test_person_randspalte_wiederholt_den_kopf_nicht(self, rat: dict[str, Any]) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/r1", body=rat["body"], name="Sitzung", start=timezone.now() + timedelta(days=3)
        )
        sitzung.organizations.add(rat["ausschuss"])
        html = _client(rat["body"]).get(f"/insight/personen/{rat['mitglied'].pk}/").content.decode()
        rand = _randspalte(html)
        assert "Nächste Sitzungen" in rand and "Hauptausschuss" in rand
        for doppelt in ("Ratsmitglied", "Mitte", "aktuell", "Funktion", "Kommune"):
            assert doppelt not in rand, doppelt

    def test_person_ohne_inhalte_ohne_randspalte(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get(f"/insight/personen/{rat['ehemalig'].pk}/").content.decode()
        assert '<aside class="min-w-0 space-y-10"' not in html
        assert "min-[1440px]:grid-cols" not in html

    def test_gremium_randspalte_wiederholt_den_kopf_nicht(self, rat: dict[str, Any]) -> None:
        rat["rat"].start_date = date(2024, 7, 1)
        rat["rat"].save()
        html = _client(rat["body"]).get(f"/insight/gremien/{rat['rat'].pk}/").content.decode()
        rand = _randspalte(html)
        assert "01.07.2024" in rand
        for doppelt in (">Art<", ">Mitglieder<", ">Kommune<", "Stadt Beispiel"):
            assert doppelt not in rand, doppelt

    def test_sitzung_ohne_beginn_und_ort_in_den_angaben(self, rat: dict[str, Any]) -> None:
        start = timezone.now() + timedelta(days=4)
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/d1",
            body=rat["body"],
            name="Sitzung",
            start=start,
            end=start + timedelta(hours=2),
            location_name="Ratssaal",
            location_address="Markt 1",
        )
        html = _client(rat["body"]).get(f"/insight/termine/{sitzung.pk}/").content.decode()
        assert ">Beginn</dt>" not in html and ">Ort</dt>" not in html
        assert ">Ende</dt>" in html and "Markt 1" in html
        assert _ohne_kopf(html).count("Ratssaal") == 1

    def test_rueckmeldung_nennt_die_loeschfrist_einmal(self) -> None:
        aside = (TEMPLATES / "pages/feedback.html").read_text(encoding="utf-8")
        formular = (TEMPLATES / "partials/page_feedback.html").read_text(encoding="utf-8")
        assert "zwölf Monaten" in formular
        assert "zwölf Monaten" not in aside


class TestBreiteNachPruefung:
    def test_randspalten_erst_ab_1440(self, rat: dict[str, Any]) -> None:
        rat["rat"].website = "https://rat.example.org"
        rat["rat"].save()
        client = _client(rat["body"])
        for pfad in (f"/insight/gremien/{rat['rat'].pk}/", f"/insight/personen/{rat['ob'].pk}/"):
            html = client.get(pfad).content.decode()
            assert "min-[1440px]:grid-cols-[minmax(0,1fr)_20rem]" in html, pfad
            assert "xl:grid-cols-[minmax(0,1fr)_21rem]" not in html, pfad

    @pytest.mark.parametrize(
        "datei",
        [
            "partials/person_list_items.html",
            "partials/organization_list_items.html",
            "partials/meeting_list_items.html",
            "partials/paper_list_items.html",
        ],
    )
    def test_scrollbehaelter_haelt_sr_only_kopf(self, datei: str) -> None:
        """Ohne ``relative`` am Scrollbehälter entkommt der sr-only-Kopf „Merken“, die Seite scrollt seitlich."""
        inhalt = (TEMPLATES / datei).read_text(encoding="utf-8")
        assert '<div class="relative overflow-x-auto">' in inhalt
        assert 'whitespace-nowrap">{{ person.email' not in inhalt

    def test_suche_lesebreite_und_sortierung_neben_den_filtern(self) -> None:
        leiste = (TEMPLATES / "cotton/suche/filterleiste.html").read_text(encoding="utf-8")
        seite = (TEMPLATES / "partials/search_page.html").read_text(encoding="utf-8")
        assert "ml-auto" not in leiste
        # Seit der Nachmessung wächst die Trefferspalte mit (test_insight_breite_rahmen.py), die Filter stehen daneben
        assert "2xl:grid-cols-[minmax(0,1fr)_18rem]" in seite


class TestZuletztBeschlossen:
    def _sitzung(self, rat: dict[str, Any], key: str, tage: int) -> OParlMeeting:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/{key}",
            body=rat["body"],
            name="Sitzung",
            start=timezone.now() - timedelta(days=tage),
        )
        sitzung.organizations.add(rat["rat"])
        return sitzung

    def test_zwei_abfragen_ohne_korrelierte_unterabfrage(self, rat: dict[str, Any]) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from insight_core.views.home import zuletzt_beschlossen

        sitzung = self._sitzung(rat, "q", 3)
        for nummer in range(3):
            top = OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/q{nummer}",
                meeting=sitzung,
                number=str(nummer),
                order=nummer,
                result="beschlossen",
            )
            paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/q{nummer}", body=rat["body"], name=f"V{nummer}")
            OParlConsultation.objects.create(
                external_id=f"{RIS}/consultation/q{nummer}",
                body=rat["body"],
                paper=paper,
                agenda_item_external_id=top.external_id,
            )
        with CaptureQueriesContext(connection) as abfragen:
            punkte: list[Any] = list(zuletzt_beschlossen(rat["body"]))
        # Punkte, dann Beratungen per agenda_item_external_id__in – keine Unterabfrage je Zeile
        assert len(abfragen.captured_queries) == 2
        assert "oparl_consultations" not in abfragen.captured_queries[0]["sql"]
        assert [p.vorgang.name for p in punkte] == ["V0", "V1", "V2"]
        assert all(p.gremium == "Rat der Stadt Beispiel" for p in punkte)

    def test_ohne_vertagt_und_kenntnisnahme(self, rat: dict[str, Any]) -> None:
        from insight_core.views.home import zuletzt_beschlossen

        sitzung = self._sitzung(rat, "k", 2)
        ergebnisse = ["vertagt", "zur Kenntnis genommen", "Kenntnisnahme", "beantwortet", "mehrheitlich beschlossen"]
        for nummer, ergebnis in enumerate(ergebnisse):
            OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/k{nummer}",
                meeting=sitzung,
                number=str(nummer),
                order=nummer,
                name=f"TOP {ergebnis}",
                result=ergebnis,
            )
        assert [p.result for p in zuletzt_beschlossen(rat["body"])] == ["mehrheitlich beschlossen"]


class TestRollen:
    def test_schreibweisen_und_allgemeine_rollen(self, body: OParlBody) -> None:
        rat = _org(body, "r", "Rat der Stadt Beispiel", classification="Rat")
        ausschuss = _org(
            body, "a", "Ausschuss für Schule und Sport", short_name="Ausschuss", classification="Ausschuss"
        )
        sb = _person(body, "sb", "Sina Buerger")
        _mitglied(sb, ausschuss, "Sachk. Bürger/in (mit Stimmr.)")
        am = _person(body, "am", "Anton Ausschuss")
        _mitglied(am, ausschuss, "Ausschussmitglied")
        st = _person(body, "st", "Stella Stellv")
        _mitglied(st, ausschuss, "Stellv. Mitglied")
        vo = _person(body, "vo", "Viktor Vorsitz")
        _mitglied(vo, rat, "Vorsitz")
        angaben = angaben_fuer([sb, am, st, vo])
        assert angaben[sb.pk].funktion == "Sachk. Bürger/in (mit Stimmr.)"
        assert angaben[am.pk].funktion == ""
        assert angaben[st.pk].funktion == ""
        assert angaben[vo.pk].funktion == "Vorsitz, Rat der Stadt Beispiel"
        # Nichtssagender Kurzname („Ausschuss“): die Spalte zeigt den vollen Namen
        assert angaben[am.pk].gremien == ["Ausschuss für Schule und Sport"]

    def test_abgeschnittener_kurzname(self, body: OParlBody) -> None:
        voll = "Ausschuss für Soziales, Gesundheit und Arbeit"
        ausschuss = _org(body, "s", voll, short_name=voll[:40], classification="Ausschuss")
        person = _person(body, "v", "Vera Vorsitz")
        _mitglied(person, ausschuss, "Vorsitzende")
        angaben = angaben_fuer([person])[person.pk]
        assert angaben.funktion == f"Vorsitzende, {voll}"
        assert angaben.gremien == [voll]

    @pytest.mark.parametrize(
        ("gremium", "rolle", "erwartet"),
        [
            ("Rat der Stadt Beispiel", "Beratendes Mitglied", "Beratendes Mitglied, Rat der Stadt Beispiel"),
            ("Rat der Stadt Beispiel", "stellv. Mitglied", "stellv. Mitglied, Rat der Stadt Beispiel"),
            ("Rat der Stadt Beispiel", "Mitglied", "Ratsmitglied"),
            ("Regionalrat", "stellvertretendes Mitglied", "stellvertretendes Mitglied, Regionalrat"),
            ("Regionalrat", "Mitglied", "Mitglied Regionalrat"),
        ],
    )
    def test_hauptorgan_nur_schlichte_mitglieder_als_mandat(
        self, body: OParlBody, gremium: str, rolle: str, erwartet: str
    ) -> None:
        """Stellvertretende und beratende Mitglieder sind keine Ratsmitglieder (Gegenprüfung #848)."""
        organ = _org(body, "h", gremium, short_name=gremium, classification="Rat" if "Rat " in gremium else "")
        person = _person(body, "p", "Paula Beispiel")
        _mitglied(person, organ, rolle)
        assert angaben_fuer([person])[person.pk].funktion == erwartet


class TestZuletztBeschlossenGegenpruefung:
    def test_beschluss_hat_vorrang_vor_kenntnis(self) -> None:
        from insight_core.services.paper_status import ist_beschluss

        assert ist_beschluss("beschlossen; Kenntnis der Stellungnahme")
        assert ist_beschluss("Einstimmig angenommen, im Übrigen zur Kenntnis genommen")
        assert not ist_beschluss("zur Kenntnis genommen")
        assert not ist_beschluss("vertagt")

    def test_viele_kenntnisnahmen_verdraengen_die_beschluesse_nicht(self, rat: dict[str, Any]) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from insight_core.views.home import zuletzt_beschlossen

        neu = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/viel",
            body=rat["body"],
            name="Sitzung",
            start=timezone.now() - timedelta(days=1),
        )
        alt = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/alt", body=rat["body"], name="Sitzung", start=timezone.now() - timedelta(days=9)
        )
        for nummer in range(40):
            OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/viel{nummer}",
                meeting=neu,
                number=str(nummer),
                order=nummer,
                result="zur Kenntnis genommen",
            )
        for nummer in range(4):
            OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/alt{nummer}",
                meeting=alt,
                number=str(nummer),
                order=nummer,
                result="beschlossen",
            )
        with CaptureQueriesContext(connection) as abfragen:
            punkte = zuletzt_beschlossen(rat["body"])
        assert [p.result for p in punkte] == ["beschlossen"] * 4
        assert len(abfragen.captured_queries) == 2


def test_kuerzel_bleibt_kurzname() -> None:
    """Echte Kürzel bleiben, nichtssagend kurze werden durch den vollen Namen ersetzt."""
    from insight_core.services.personen_liste import _org_name

    assert _org_name(OParlOrganization(name="SPD-Fraktion", short_name="SPD", organization_type="Fraktion")) == "SPD"
    assert _org_name(OParlOrganization(name="Regionalrat", short_name="RR")) == "Regionalrat"
