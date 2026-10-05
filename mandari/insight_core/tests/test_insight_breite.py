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
        html = _client(rat["body"]).get(f"/insight/gremien/{rat['rat'].pk}/").content.decode()
        assert 'aria-label="Termine und Angaben"' in html
        assert "3 Mitglieder" in html
        mitglieder = _ausschnitt(r"Aktive Mitglieder.*?</table>", html)
        assert "Mitte" in mitglieder
        assert "—" not in mitglieder

    def test_person_mit_funktion_und_randspalte(self, rat: dict[str, Any]) -> None:
        html = _client(rat["body"]).get(f"/insight/personen/{rat['ob'].pk}/").content.decode()
        assert 'aria-label="Kontakt und Angaben"' in html
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
        assert 'class="max-w-insight px-4 sm:px-8 py-6 lg:py-8"' in (TEMPLATES / "base_insight.html").read_text(
            encoding="utf-8"
        )
