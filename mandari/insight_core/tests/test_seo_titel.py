# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Titel, Descriptions und strukturierte Daten der Insight-Detailseiten (Issue #914).

Gefunden werden Vorgänge über Betreff und Kommune, nicht über die Drucksachennummer: Der Titel nennt den Betreff,
Art und Nummer und die Kommune – genau einmal, auch im Bürgerportal einer Körperschaft, dessen Titelzusatz sonst
ebenfalls die Kommune ist. Sitzungen desselben Gremiums unterscheiden sich am Datum, Personen heißen nicht pauschal
„Ratsmitglied“, und die Übersicht einer Kommune heißt „Ratsinformationen <Kommune> – Sitzungen, Vorlagen,
Beschlüsse“. Zu jedem Objekt kommt als zweites JSON-LD-Element die Brotkrumenliste. Die Fußzeile verweist auf die
Seite über mandari als Ratsinformationssystem.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import uuid
from datetime import date, datetime
from typing import Any

import pytest
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
    PublicQuestion,
)
from insight_core.seo import kuerzen

pytestmark = pytest.mark.django_db

BASIS = "https://ris.muenster.example/oparl/"
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.S | re.I)
ZUSAMMENFASSUNG = (
    "Die Feuerwache Süd wird an der Musterstraße neu gebaut, weil das alte Gebäude zu klein ist. "
    "Der Bau kostet rund zwölf Millionen Euro und soll 2014 fertig sein."
)


def _zeit(jahr: int, monat: int, tag: int, stunde: int = 0, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime(jahr, monat, tag, stunde, minute))


def _id() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def muenster() -> OParlBody:
    source = OParlSource.objects.create(name="RIS Münster", url=BASIS + "system")
    return OParlBody.objects.create(
        external_id=BASIS + "body/1", source=source, name="Stadt Münster", display_name="Münster", slug="muenster"
    )


def _gremium(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{BASIS}organization/{_id()}", body=body, name=name, **felder)


def _sitzung(body: OParlBody, gremium: OParlOrganization | None, start: datetime, **felder: Any) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{BASIS}meeting/{_id()}",
        body=body,
        name=gremium.name if gremium else "Sitzung",
        start=start,
        **felder,
    )
    if gremium is not None:
        sitzung.organizations.add(gremium)
    return sitzung


def _vorgang(body: OParlBody, **felder: Any) -> OParlPaper:
    werte: dict[str, Any] = {
        "name": "Neubau der Feuerwache Süd",
        "reference": "V/0881/2011",
        "paper_type": "Vorlagen",
        "date": date(2011, 11, 15),
    }
    werte.update(felder)
    return OParlPaper.objects.create(external_id=f"{BASIS}paper/{_id()}", body=body, **werte)


def _beraten(paper: OParlPaper, sitzung: OParlMeeting, ergebnis: str) -> None:
    top = OParlAgendaItem.objects.create(
        external_id=f"{BASIS}agendaitem/{_id()}", meeting=sitzung, number="5", result=ergebnis
    )
    OParlConsultation.objects.create(
        external_id=f"{BASIS}consultation/{_id()}",
        body=paper.body,
        paper=paper,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=top.external_id,
        role="Entscheidung",
    )


def _person(body: OParlBody, name: str, *rollen: tuple[OParlOrganization, str], ehemals: bool = False) -> OParlPerson:
    person = OParlPerson.objects.create(external_id=f"{BASIS}person/{_id()}", body=body, name=name)
    for gremium, rolle in rollen:
        OParlMembership.objects.create(
            external_id=f"{BASIS}membership/{_id()}",
            person=person,
            organization=gremium,
            role=rolle,
            end_date=date(2020, 10, 31) if ehemals else None,
        )
    return person


def _seite(client: Client, url: str) -> str:
    antwort = client.get(url)
    assert antwort.status_code == 200, url
    return antwort.content.decode()


def _titel(seite: str) -> str:
    treffer = re.search(r"<title>(.*?)</title>", seite, re.S)
    assert treffer
    return html_lib.unescape(" ".join(treffer.group(1).split()))


def _meta(seite: str, attribut: str, name: str) -> str:
    treffer = re.search(rf'<meta {attribut}="{re.escape(name)}" content="([^"]*)"', seite)
    assert treffer, name
    return html_lib.unescape(treffer.group(1))


def _json_ld(seite: str) -> list[Any]:
    return [json.loads(inhalt) for attribute, inhalt in SCRIPT_RE.findall(seite) if "ld+json" in attribute.lower()]


def _fuss(seite: str) -> str:
    return re.sub(r"\s+", " ", seite[seite.index("<footer") : seite.index("</footer>")])


# =============================================================================
# Vorgang
# =============================================================================


class TestVorgang:
    def test_titel_mit_betreff_art_nummer_und_kommune(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster)
        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        assert _titel(seite) == "Neubau der Feuerwache Süd – Vorlage V/0881/2011, Münster | mandari Insight"
        assert _meta(seite, "property", "og:title") == "Neubau der Feuerwache Süd – Vorlage V/0881/2011, Münster"
        assert _meta(seite, "name", "twitter:title") == _meta(seite, "property", "og:title")

    def test_description_mit_stand_und_anfang_der_zusammenfassung(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, summary=ZUSAMMENFASSUNG)
        _beraten(paper, _sitzung(muenster, _gremium(muenster, "Rat"), _zeit(2011, 12, 6, 17)), "beschlossen")

        description = _meta(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"), "name", "description")

        assert description.startswith(
            "Münster: Vorlage V/0881/2011 vom 15.11.2011. Am 06.12.2011 im Rat beschlossen. Die Feuerwache Süd"
        )
        assert len(description) <= 160 and description.endswith("…")

    def test_ohne_beratung_folgt_die_zusammenfassung(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, paper_type="Antrag", reference="A/12/2026", summary="Kurz und klar.")

        description = _meta(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"), "name", "description")

        assert description == "Münster: Antrag A/12/2026 vom 15.11.2011. Kurz und klar."
        assert "Noch keine Beratung" not in description

    def test_langer_betreff_an_einer_wortgrenze_gekuerzt(self, muenster: OParlBody) -> None:
        betreff = (
            "Bebauungsplan Nr. 612: Südviertel – Erweiterung des Gewerbegebiets an der Weseler Straße "
            "und Anpassung der Erschließung"
        )
        paper = _vorgang(muenster, name=betreff)

        titel = _titel(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))

        gekuerzt, rest = titel.split(" – Vorlage ", 1)
        assert len(gekuerzt) <= 70 and gekuerzt.endswith("…")
        assert betreff.startswith(gekuerzt.removesuffix("…")) and betreff[len(gekuerzt) - 1] == " "
        assert rest == "V/0881/2011, Münster | mandari Insight"

    def test_ohne_betreff_art_nummer_und_kommune(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, name="V/0881/2011")

        assert (
            _titel(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))
            == "Vorlage V/0881/2011, Münster | mandari Insight"
        )

    @pytest.mark.parametrize(
        ("art", "kurz"),
        [
            ("Beschlussvorlage", "Vorlage"),
            ("Anträge", "Antrag"),
            ("Anträge und Anfragen", "Vorgang"),
            (None, "Vorgang"),
        ],
    )
    def test_art_kurz(self, muenster: OParlBody, art: str | None, kurz: str) -> None:
        paper = _vorgang(muenster, paper_type=art)

        assert f" – {kurz} V/0881/2011, Münster" in _titel(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))

    def test_json_ld_mit_kommune_und_brotkrumen(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster)
        daten = _json_ld(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))

        vorgang, krumen = daten
        assert vorgang["@type"] == "CreativeWork" and vorgang["name"] == "Neubau der Feuerwache Süd"
        assert vorgang["spatialCoverage"] == {"@type": "AdministrativeArea", "name": "Münster"}
        assert vorgang["about"]["name"] == "Münster" and vorgang["identifier"] == "V/0881/2011"
        assert None not in vorgang.values()
        assert krumen["@type"] == "BreadcrumbList"
        eintraege = krumen["itemListElement"]
        # Ohne Ebene „Vorgänge“: /insight/k/<slug>/vorgaenge/ leitet weiter (Issue #939)
        assert [e["name"] for e in eintraege] == ["mandari Insight", "Münster", "V/0881/2011"]
        assert [e["position"] for e in eintraege] == [1, 2, 3]
        pfade = [re.sub(r"^https?://[^/]+", "", e["item"]) for e in eintraege]
        assert pfade == ["/insight/", "/insight/k/muenster/", f"/insight/vorgaenge/{paper.id}/"]

    def test_brotkrumen_ohne_einstieg_der_kommune(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(slug=None)
        paper = _vorgang(muenster)

        krumen = _json_ld(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))[1]["itemListElement"]

        assert [e["name"] for e in krumen] == ["mandari Insight", "V/0881/2011"]
        assert krumen[1]["item"].endswith(f"/insight/vorgaenge/{paper.id}/")


# =============================================================================
# Sitzung
# =============================================================================


class TestSitzung:
    def test_sitzungen_desselben_gremiums_unterscheiden_sich_am_datum(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster, "Rat")
        erste = _sitzung(muenster, rat, _zeit(2030, 11, 5, 17))
        zweite = _sitzung(muenster, rat, _zeit(2030, 12, 3, 17))

        titel = [_titel(_seite(Client(), f"/insight/termine/{s.id}/")) for s in (erste, zweite)]

        assert titel == [
            "Rat am 05.11.2030 – Münster | mandari Insight",
            "Rat am 03.12.2030 – Münster | mandari Insight",
        ]

    def test_datum_in_ortszeit(self, muenster: OParlBody) -> None:
        # 23:30 Uhr in Münster ist in UTC schon der nächste Tag
        sitzung = _sitzung(muenster, _gremium(muenster, "Rat"), _zeit(2030, 11, 5, 23, 30))

        assert _titel(_seite(Client(), f"/insight/termine/{sitzung.id}/")).startswith("Rat am 05.11.2030 – ")

    def test_description_mit_ort_und_tagesordnung(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(
            muenster,
            _gremium(muenster, "Bezirksvertretung Mitte"),
            _zeit(2030, 11, 5, 17),
            location_name="Rathaus, Festsaal",
            location_address="Musterstraße 1, 48143 Münster",
        )
        for nummer in ("1", "2"):
            OParlAgendaItem.objects.create(
                external_id=f"{BASIS}agendaitem/{_id()}", meeting=sitzung, number=nummer, name=f"Punkt {nummer}"
            )
        seite = _seite(Client(), f"/insight/termine/{sitzung.id}/")
        description = _meta(seite, "name", "description")

        assert description.startswith("Münster: Sitzung in der Bezirksvertretung Mitte am ")
        assert "05.11.2030, 17:00 Uhr, Rathaus, Festsaal." in description
        assert description.endswith("Tagesordnung mit 2 Punkten.") and len(description) <= 160
        ereignis, krumen = _json_ld(seite)
        assert ereignis["@type"] == "Event" and ereignis["name"] == "Bezirksvertretung Mitte am 05.11.2030"
        assert ereignis["location"]["name"] == "Rathaus, Festsaal"
        assert ereignis["location"]["address"]["streetAddress"] == "Musterstraße 1"
        assert [e["name"] for e in krumen["itemListElement"]][1:] == [
            "Münster",
            "Bezirksvertretung Mitte am 05.11.2030",
        ]

    def test_abgesagte_sitzung(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster, "Rat"), _zeit(2030, 11, 5, 17), cancelled=True)

        description = _meta(_seite(Client(), f"/insight/termine/{sitzung.id}/"), "name", "description")

        assert "Die Sitzung ist abgesagt." in description


# =============================================================================
# Gremium
# =============================================================================


class TestGremium:
    def test_titel_und_description_mit_art_mitgliedern_und_naechster_sitzung(self, muenster: OParlBody) -> None:
        fraktion = _gremium(muenster, "GRÜNE", classification="Fraktion")
        _person(muenster, "Erika Muster", (fraktion, "Mitglied"))
        _person(muenster, "Max Beispiel", (fraktion, "Vorsitzender"))
        _sitzung(muenster, fraktion, _zeit(2030, 11, 5, 18))
        seite = _seite(Client(), f"/insight/gremien/{fraktion.id}/")

        assert _titel(seite) == "GRÜNE – Münster | mandari Insight"
        assert _meta(seite, "name", "description") == (
            "GRÜNE (Fraktion), Münster. 2 aktuelle Mitglieder, Sitzungstermine und Tagesordnungen. "
            "Nächste Sitzung am 05.11.2030."
        )
        gremium, krumen = _json_ld(seite)
        assert gremium["@type"] == "Organization" and gremium["name"] == "GRÜNE"
        assert gremium["parentOrganization"]["name"] == "Münster"
        assert [e["name"] for e in krumen["itemListElement"]] == ["mandari Insight", "Münster", "GRÜNE"]

    def test_art_nicht_doppelt_wenn_der_name_sie_traegt(self, muenster: OParlBody) -> None:
        ausschuss = _gremium(muenster, "Ausschuss für Umwelt und Klimaschutz", classification="Ausschuss")

        description = _meta(_seite(Client(), f"/insight/gremien/{ausschuss.id}/"), "name", "description")

        assert description.startswith("Ausschuss für Umwelt und Klimaschutz, Münster. Mitglieder, Sitzungstermine")

    def test_kommune_im_namen_nicht_doppelt(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster, "Rat der Stadt Münster", classification="Rat")

        seite = _seite(Client(), f"/insight/gremien/{rat.id}/")

        assert _titel(seite) == "Rat der Stadt Münster | mandari Insight"
        assert _meta(seite, "name", "description").startswith("Rat der Stadt Münster. ")


# =============================================================================
# Person
# =============================================================================


class TestPerson:
    def test_ratsmitglied_mit_fraktion_und_gremien(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster, "Rat")
        fraktion = _gremium(muenster, "Fraktion Bündnis 90/Die Grünen", short_name="GRÜNE", classification="Fraktion")
        bv = _gremium(muenster, "Bezirksvertretung Mitte")
        person = _person(muenster, "Erika Muster", (rat, "Mitglied"), (fraktion, "Mitglied"), (bv, "Mitglied"))
        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Erika Muster – Ratsmitglied, Münster | mandari Insight"
        assert _meta(seite, "name", "description") == (
            "Erika Muster, Ratsmitglied, Münster. Fraktion: GRÜNE. Mitglied in: Rat, Bezirksvertretung Mitte."
        )
        daten, krumen = _json_ld(seite)
        assert daten["@type"] == "Person" and daten["name"] == "Erika Muster" and daten["jobTitle"] == "Ratsmitglied"
        assert [o["name"] for o in daten["memberOf"]] == ["Rat", "Bezirksvertretung Mitte"]
        assert {o["name"] for o in daten["affiliation"]} == {"Münster", "Fraktion Bündnis 90/Die Grünen"}
        assert [e["name"] for e in krumen["itemListElement"]] == ["mandari Insight", "Münster", "Erika Muster"]

    def test_ohne_mandat_nicht_als_ratsmitglied(self, muenster: OParlBody) -> None:
        ausschuss = _gremium(muenster, "Ausschuss für Umwelt", classification="Ausschuss")
        person = _person(muenster, "Max Beispiel", (ausschuss, "Mitglied"))
        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Max Beispiel, Münster | mandari Insight"
        kopf = seite[: seite.index("</head>")]
        assert "Ratsmitglied" not in kopf
        assert "jobTitle" not in _json_ld(seite)[0]

    def test_besondere_rolle_im_rat(self, muenster: OParlBody) -> None:
        fraktion = _gremium(muenster, "Fraktion Bunte Liste", classification="Fraktion")
        person = _person(
            muenster, "Dr. Erika Muster", (_gremium(muenster, "Rat"), "Oberbürgermeisterin"), (fraktion, "Mitglied")
        )

        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Dr. Erika Muster – Oberbürgermeisterin, Münster | mandari Insight"
        assert _meta(seite, "name", "description").startswith(
            "Dr. Erika Muster, Oberbürgermeisterin, Münster. Fraktion: Bunte Liste. Mitglied in: Rat."
        )
        assert _json_ld(seite)[0]["jobTitle"] == "Oberbürgermeisterin"

    def test_ehemaliges_mitglied_ohne_funktion(self, muenster: OParlBody) -> None:
        person = _person(muenster, "Hans Früher", (_gremium(muenster, "Rat"), "Mitglied"), ehemals=True)

        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Hans Früher, Münster | mandari Insight"
        assert "Ratsmitglied" not in seite[: seite.index("</head>")]


# =============================================================================
# Kommune nur einmal im Titel – auch im Bürgerportal der Körperschaft
# =============================================================================


class TestKommuneNurEinmal:
    def test_detailseiten_im_buergerportal_der_koerperschaft(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster, "Rat")
        seiten = [
            f"/insight/vorgaenge/{_vorgang(muenster).id}/",
            f"/insight/termine/{_sitzung(muenster, rat, _zeit(2030, 11, 5, 17)).id}/",
            f"/insight/gremien/{rat.id}/",
            f"/insight/personen/{_person(muenster, 'Erika Muster', (rat, 'Mitglied')).id}/",
        ]
        client = Client()
        _seite(client, "/insight/k/muenster/")

        for url in seiten:
            titel = _titel(_seite(client, url))
            assert titel.count("Münster") == 1, titel
            assert titel.endswith(" | mandari Insight"), titel

    def test_listen_im_buergerportal_behalten_den_namen_des_portals(self, muenster: OParlBody) -> None:
        client = Client()
        _seite(client, "/insight/k/muenster/")

        assert _titel(_seite(client, "/insight/termine/")) == "Sitzungen | Münster"


# =============================================================================
# Übersicht der Kommune
# =============================================================================


class TestUebersichtDerKommune:
    def test_titel_und_description(self, muenster: OParlBody) -> None:
        seite = _seite(Client(), "/insight/k/muenster/")

        assert _titel(seite) == "Ratsinformationen Münster – Sitzungen, Vorlagen, Beschlüsse | mandari Insight"
        assert _meta(seite, "property", "og:title") == "Ratsinformationen Münster – Sitzungen, Vorlagen, Beschlüsse"
        description = _meta(seite, "name", "description")
        assert description.startswith("Ratsinformationen Münster: Sitzungen mit Tagesordnung, Vorlagen, Anträge")
        assert len(description) <= 160
        # Die Überschrift bleibt
        assert re.search(r"<h1[^>]*>Münster transparent</h1>", seite)

    def test_lange_kommunennamen_bleiben_unter_160_zeichen(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(display_name="Bezirksregierung Musterland-Südwest Nord")

        description = _meta(_seite(Client(), "/insight/k/muenster/"), "name", "description")

        assert len(description) <= 160 and description.endswith("…")


# =============================================================================
# Ratsfrage
# =============================================================================


def test_ratsfrage_mit_kommune(muenster: OParlBody) -> None:
    person = _person(muenster, "Erika Muster", (_gremium(muenster, "Rat"), "Mitglied"))
    frage = PublicQuestion.objects.create(
        body=muenster,
        recipient=person,
        questioner_name="Bert",
        questioner_email="bert@example.org",
        subject="Sanierung der Grundschule am Hafen",
        question_text="Wann wird die Grundschule saniert?",
        status="published",
        published_at=timezone.now(),
    )
    seite = _seite(Client(), f"/insight/fragen/{frage.id}/")

    assert _titel(seite) == "Sanierung der Grundschule am Hafen – Frage an Erika Muster, Münster | mandari Insight"
    assert _meta(seite, "property", "og:title") == "Sanierung der Grundschule am Hafen – Frage an Erika Muster, Münster"


# =============================================================================
# Kürzen an der Wortgrenze
# =============================================================================


@pytest.mark.parametrize(
    ("text", "limit", "gekuerzt"),
    [
        # Der Schnitt fällt genau auf das Ende eines Worts: Das ganze Wort bleibt
        ("Radweg an der Hafenstraße wird gebaut", 26, "Radweg an der Hafenstraße…"),
        # Der Schnitt fällt mitten in ein Wort: Es fällt weg
        ("Radweg an der Hafenstraße wird gebaut", 24, "Radweg an der…"),
        ("Radweg an der Hafenstraße", 25, "Radweg an der Hafenstraße"),
        ("Bebauungsplan,  Südviertel", 15, "Bebauungsplan…"),
    ],
)
def test_kuerzen_an_der_wortgrenze(text: str, limit: int, gekuerzt: str) -> None:
    assert kuerzen(text, limit) == gekuerzt
    assert len(kuerzen(text, limit)) <= limit


# =============================================================================
# Beschluss (Umsetzungsstand)
# =============================================================================


def test_beschluss_mit_gremium_kommune_und_stand(muenster: OParlBody) -> None:
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(
        name="Stadt Münster",
        slug="ms",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
        oparl_body=muenster,
    )
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss", organization=gremium, start=_zeit(2026, 9, 1, 17), is_public=True
    )
    beschluss = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg an der Hafenstraße",
        vote_result="approved",
        implementation_public=True,
    )
    seite = _seite(Client(), f"/insight/beschluesse/{beschluss.pk}/")

    assert _titel(seite) == "Radweg an der Hafenstraße – Umsetzungsstand, Münster | mandari Insight"
    assert _meta(seite, "name", "description").startswith("Münster: Beschluss im Hauptausschuss vom 01.09.2026.")
    daten, krumen = _json_ld(seite)
    assert daten["name"] == "Radweg an der Hafenstraße" and daten["spatialCoverage"]["name"] == "Münster"
    assert [e["name"] for e in krumen["itemListElement"]][1:] == ["Münster", "Radweg an der Hafenstraße"]


# =============================================================================
# Fußzeile
# =============================================================================


class TestFusszeile:
    def test_verweis_auf_mandari_als_ratsinformationssystem(self, muenster: OParlBody) -> None:
        fuss = _fuss(_seite(Client(), f"/insight/vorgaenge/{_vorgang(muenster).id}/"))

        assert "Die Daten stammen aus dem Ratsinformationssystem von Münster." in fuss
        assert "Dieses Bürgerportal läuft mit" in fuss
        link = re.search(r'<a href="https://mandari\.de/ratsinformationssystem/"[^>]*>([^<]*)</a>', fuss)
        assert link and "Ratsinformationssystem" in link.group(1)

    def test_auch_ohne_kommune(self, muenster: OParlBody) -> None:
        client = Client()
        client.get("/insight/kommune/alle/")

        assert 'href="https://mandari.de/ratsinformationssystem/"' in _fuss(_seite(client, "/insight/suche/"))
