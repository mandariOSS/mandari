# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Titel, Descriptions und strukturierte Daten der Insight-Detailseiten (Issue #914).

Gefunden werden Vorgänge über Betreff und Kommune, nicht über die Drucksachennummer: Der Titel nennt den Betreff,
Art und Nummer und die Kommune – genau einmal, auch im Bürgerportal einer Körperschaft, dessen Titelzusatz sonst
ebenfalls die Kommune ist. Sitzungen desselben Gremiums unterscheiden sich am Datum, Personen heißen nicht pauschal
„Ratsmitglied“, und die Übersicht einer Kommune heißt „Ratsinformationen <Kommune> – Sitzungen, Vorlagen,
Beschlüsse“. Zu jedem Objekt kommt als zweites JSON-LD-Element die Brotkrumenliste. Die Fußzeile verweist auf die
Seite über mandari als Ratsinformationssystem.

Seitentitel haben höchstens 70 Zeichen (Issue #939, Bing: „Title too long“); Detail- und Stadtseiten tragen keinen
Zusatz „| mandari Insight“. Wird es eng, fallen erst Art und Nummer bzw. die Rolle weg, dann wird das Thema gekürzt –
die Kommune bleibt. ``og:title`` bleibt ausführlich.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import uuid
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

import pytest
from django.test import Client
from django.utils import timezone

from insight_core import seo
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

        assert _titel(seite) == "Neubau der Feuerwache Süd – Vorlage V/0881/2011, Münster"
        assert _meta(seite, "property", "og:title") == "Neubau der Feuerwache Süd – Vorlage V/0881/2011, Münster"
        assert _meta(seite, "name", "twitter:title") == _meta(seite, "property", "og:title")

    def test_description_mit_stand_und_anfang_der_zusammenfassung(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, summary=ZUSAMMENFASSUNG)
        _beraten(paper, _sitzung(muenster, _gremium(muenster, "Rat"), _zeit(2011, 12, 6, 17)), "beschlossen")

        description = _meta(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"), "name", "description")

        assert description.startswith(
            "Münster: Vorlage V/0881/2011 vom 15.11.2011. Am 06.12.2011 im Rat beschlossen. "
            "KI-Zusammenfassung: Die Feuerwache Süd"
        )
        assert len(description) <= 160 and description.endswith("…")

    def test_ohne_beratung_folgt_die_zusammenfassung(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, paper_type="Antrag", reference="A/12/2026", summary="Kurz und klar.")

        description = _meta(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"), "name", "description")

        assert description == "Münster: Antrag A/12/2026 vom 15.11.2011. KI-Zusammenfassung: Kurz und klar."
        assert "Noch keine Beratung" not in description

    def test_kein_praefix_ohne_platz_fuer_den_text(self, muenster: OParlBody) -> None:
        """Bleibt nach „KI-Zusammenfassung:“ zu wenig Platz, fällt die Zusammenfassung ganz weg (Issue #938)."""
        paper = _vorgang(muenster, summary=ZUSAMMENFASSUNG)
        gremium = _gremium(muenster, "Ausschuss für Stadtentwicklung, Umwelt und Wohnen")
        _beraten(paper, _sitzung(muenster, gremium, _zeit(2011, 12, 6, 17)), "beschlossen")

        description = _meta(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"), "name", "description")

        assert description == (
            "Münster: Vorlage V/0881/2011 vom 15.11.2011. "
            "Am 06.12.2011 im Ausschuss für Stadtentwicklung, Umwelt und Wohnen beschlossen."
        )
        # Ohne die Regel stünde hier „KI-Zusammenfassung: Die Feuerwache…“: Platz ist für die Kennzeichnung, kaum Text
        assert seo.DESCRIPTION_MAX - len(description) - 1 >= seo._DESCRIPTION_REST_MIN

    def test_ki_zusammenfassung_ist_auch_im_json_ld_gekennzeichnet(self, muenster: OParlBody) -> None:
        """EU AI Act Art. 50 (Issue #938): Suchmaschinen zeigen den Text ohne die Seite, die Kennzeichnung steht darin."""
        paper = _vorgang(muenster, paper_type="Antrag", reference="A/12/2026", summary="Kurz und klar.")

        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        assert "KI-Zusammenfassung: Kurz und klar." in _meta(seite, "property", "og:description")
        werke = [eintrag for eintrag in _json_ld(seite) if eintrag.get("@type") == "CreativeWork"]
        assert [werk["description"] for werk in werke] == [
            "Münster: Antrag A/12/2026 vom 15.11.2011. KI-Zusammenfassung: Kurz und klar."
        ]

    def test_ohne_zusammenfassung_keine_ki_kennzeichnung(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, paper_type="Antrag", reference="A/12/2026")

        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        assert "KI-Zusammenfassung" not in _meta(seite, "name", "description")
        assert "data-ai-generated" not in seite

    @pytest.mark.parametrize(
        ("betreff", "erwartet"),
        [
            # Erst fällt die Art weg; die Drucksachennummer bleibt, nach ihr wird gesucht
            (
                "Neubau der Feuerwache Süd an der Musterstraße",
                "Neubau der Feuerwache Süd an der Musterstraße – V/0881/2011, Münster",
            ),
            # Dann die Nummer; der Betreff bleibt ganz
            (
                "Neubau der Feuerwache Süd an der Musterstraße mit Übungshof",
                "Neubau der Feuerwache Süd an der Musterstraße mit Übungshof, Münster",
            ),
        ],
    )
    def test_passt_es_nicht_faellt_erst_die_art_dann_die_nummer_weg(
        self, muenster: OParlBody, betreff: str, erwartet: str
    ) -> None:
        paper = _vorgang(muenster, name=betreff)
        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        assert _titel(seite) == erwartet and len(erwartet) <= 70
        # og:title bleibt ausführlich
        assert _meta(seite, "property", "og:title") == f"{betreff} – Vorlage V/0881/2011, Münster"

    @pytest.mark.parametrize(
        ("art", "nummer", "kennung"),
        [
            ("Sitzungsvorlage RR", "RR 50/2021", "Sitzungsvorlage RR 50/2021"),
            ("Antrag", "Antrag 12/2026", "Antrag 12/2026"),
            ("Vorlage", "V/0881/2011", "Vorlage V/0881/2011"),
            # Nur ein ganzes Wort zählt: „RRX 5/2021“ beginnt nicht mit dem Kürzel „RR“
            ("Sitzungsvorlage RR", "RRX 5/2021", "Sitzungsvorlage RR RRX 5/2021"),
        ],
    )
    def test_kuerzel_der_art_nicht_doppelt(self, muenster: OParlBody, art: str, nummer: str, kennung: str) -> None:
        paper = _vorgang(muenster, name="Nachbesetzungen", paper_type=art, reference=nummer)
        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        assert _titel(seite) == f"Nachbesetzungen – {kennung}, Münster"
        assert _meta(seite, "property", "og:title") == f"Nachbesetzungen – {kennung}, Münster"
        assert _meta(seite, "name", "description").startswith(f"Münster: {kennung} vom 15.11.2011.")

    def test_langer_betreff_an_einer_wortgrenze_gekuerzt(self, muenster: OParlBody) -> None:
        betreff = (
            "Bebauungsplan Nr. 612: Südviertel – Erweiterung des Gewerbegebiets an der Weseler Straße "
            "und Anpassung der Erschließung"
        )
        paper = _vorgang(muenster, name=betreff)
        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        titel = _titel(seite)
        gekuerzt = titel.removesuffix(", Münster")
        assert len(titel) <= 70 and gekuerzt.endswith("…") and "V/0881/2011" not in titel
        assert betreff.startswith(gekuerzt.removesuffix("…")) and betreff[len(gekuerzt) - 1] == " "
        # og:title wie bisher: Betreff bis 70 Zeichen, Art, Nummer und Kommune
        og, rest = _meta(seite, "property", "og:title").split(" – Vorlage ", 1)
        assert len(og) <= 70 and og.endswith("…") and len(og) > len(gekuerzt)
        assert rest == "V/0881/2011, Münster"

    def test_klammer_am_anfang_des_betreffs_bleibt(self, muenster: OParlBody) -> None:
        betreff = (
            "Antrag (CDU-Fraktion, SPD-Fraktion, Fraktion Bündnis 90/Die Grünen, FDP-Fraktion, Fraktion Die Linke) "
            "zur Neuordnung der Parkgebühren in der Innenstadt"
        )
        paper = _vorgang(muenster, name=betreff, paper_type="Antrag", reference="A/12/2026")

        # Nicht nur „Antrag…, Münster“
        assert (
            _titel(_seite(Client(), f"/insight/vorgaenge/{paper.id}/"))
            == "Antrag (CDU-Fraktion, SPD-Fraktion, Fraktion Bündnis 90/Die…, Münster"
        )

    def test_ohne_betreff_art_nummer_und_kommune(self, muenster: OParlBody) -> None:
        paper = _vorgang(muenster, name="V/0881/2011")

        assert _titel(_seite(Client(), f"/insight/vorgaenge/{paper.id}/")) == "Vorlage V/0881/2011, Münster"

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

        assert titel == ["Rat am 05.11.2030 – Münster", "Rat am 03.12.2030 – Münster"]

    def test_langes_gremium_gekuerzt_datum_und_kommune_bleiben(self, muenster: OParlBody) -> None:
        name = "Ausschuss für Stadtplanung, Stadtentwicklung, Verkehr und Wohnen der Bezirksvertretung"
        sitzung = _sitzung(muenster, _gremium(muenster, name), _zeit(2016, 3, 3, 17))
        seite = _seite(Client(), f"/insight/termine/{sitzung.id}/")

        titel = _titel(seite)
        assert titel == "Ausschuss für Stadtplanung, Stadtentwicklung… am 03.03.2016 – Münster"
        assert len(titel) <= 70
        assert _meta(seite, "property", "og:title") == f"{kuerzen(name, 70)} am 03.03.2016 – Münster"

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

    def test_langer_ort_mit_klammer_bleibt_lesbar(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(
            muenster,
            _gremium(muenster, "Rat"),
            _zeit(2030, 11, 5, 17),
            location_name="Ratssaal (Rathaus, Prinzipalmarkt 10, 48143 Münster, Eingang über den Innenhof)",
        )

        description = _meta(_seite(Client(), f"/insight/termine/{sitzung.id}/"), "name", "description")

        # Nicht nur „Ratssaal…“
        assert "17:00 Uhr, Ratssaal (Rathaus, Prinzipalmarkt 10, 48143 Münster…." in description

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

        assert _titel(seite) == "GRÜNE – Münster"
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

        assert _titel(seite) == "Rat der Stadt Münster"
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

        assert _titel(seite) == "Erika Muster – Ratsmitglied, Münster"
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

        assert _titel(seite) == "Max Beispiel, Münster"
        kopf = seite[: seite.index("</head>")]
        assert "Ratsmitglied" not in kopf
        assert "jobTitle" not in _json_ld(seite)[0]

    def test_besondere_rolle_im_rat(self, muenster: OParlBody) -> None:
        fraktion = _gremium(muenster, "Fraktion Bunte Liste", classification="Fraktion")
        person = _person(
            muenster, "Dr. Erika Muster", (_gremium(muenster, "Rat"), "Oberbürgermeisterin"), (fraktion, "Mitglied")
        )

        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Dr. Erika Muster – Oberbürgermeisterin, Münster"
        assert _meta(seite, "name", "description").startswith(
            "Dr. Erika Muster, Oberbürgermeisterin, Münster. Fraktion: Bunte Liste. Mitglied in: Rat."
        )
        assert _json_ld(seite)[0]["jobTitle"] == "Oberbürgermeisterin"

    def test_ehemaliges_mitglied_ohne_funktion(self, muenster: OParlBody) -> None:
        person = _person(muenster, "Hans Früher", (_gremium(muenster, "Rat"), "Mitglied"), ehemals=True)

        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        assert _titel(seite) == "Hans Früher, Münster"
        assert "Ratsmitglied" not in seite[: seite.index("</head>")]

    def test_lange_rolle_gekuerzt(self, muenster: OParlBody) -> None:
        rolle = "Stellv. stimmberechtigtes Mitglied (sachk. Bürger/in)"
        ausschuss = _gremium(muenster, "Ausschuss für Umwelt", classification="Ausschuss")
        person = _person(muenster, "Sebastian Adam", (ausschuss, rolle))
        seite = _seite(Client(), f"/insight/personen/{person.id}/")

        # Ohne offene Klammer vor dem „…“
        assert _titel(seite) == "Sebastian Adam – Stellv. stimmberechtigtes Mitglied…, Münster"
        assert _meta(seite, "property", "og:title") == f"Sebastian Adam – {rolle}, Münster"

    def test_ohne_platz_fuer_die_rolle_nur_name_und_kommune(self, muenster: OParlBody) -> None:
        name = "Prof. Dr. Maximiliane Musterfrau-Beispielhausen"
        person = _person(muenster, name, (_gremium(muenster, "Rat"), "Oberbürgermeisterin"))

        assert _titel(_seite(Client(), f"/insight/personen/{person.id}/")) == f"{name}, Münster"


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
            seite = _seite(client, url)
            titel = _titel(seite)
            assert titel.count("Münster") == 1, titel
            # Ohne Zusatz; der Name des Portals steht in og:site_name
            assert "|" not in titel and "mandari Insight" not in titel, titel
            assert _meta(seite, "property", "og:site_name") == "Münster"

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

        assert _titel(seite) == "Ratsinformationen Münster – Sitzungen, Vorlagen, Beschlüsse"
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

    def test_langer_kommunenname_ohne_aufzaehlung_im_titel(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(display_name="Bezirksregierung Musterland-Südwest Nord")
        seite = _seite(Client(), "/insight/k/muenster/")

        assert _titel(seite) == "Ratsinformationen Bezirksregierung Musterland-Südwest Nord"
        assert _meta(seite, "property", "og:title") == (
            "Ratsinformationen Bezirksregierung Musterland-Südwest Nord – Sitzungen, Vorlagen, Beschlüsse"
        )


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

    assert _titel(seite) == "Sanierung der Grundschule am Hafen – Frage an Erika Muster, Münster"
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
        ("Radweg an der Hafenstraße wird gebaut", 29, "Radweg an der Hafenstraße…"),
        # Füllwörter vor dem „…“ fallen weg …
        ("Radweg an der Hafenstraße wird gebaut", 24, "Radweg…"),
        (
            "Aufteilung der frei verfügbaren Haushaltsmittel der Bezirksvertretung",
            53,
            "Aufteilung der frei verfügbaren Haushaltsmittel…",
        ),
        # … und ein Satzzeichen, das danach am Ende steht
        ("Neubau Feuerwache – an der Musterstraße", 23, "Neubau Feuerwache…"),
        ("Ausschuss Umwelt, und Klima, Verkehr", 24, "Ausschuss Umwelt…"),
        # Nur klein geschriebene Füllwörter: „DIE LINKE“ ist ein Name
        ("Anfrage der Fraktion DIE LINKE zum Haushalt", 26, "Anfrage der Fraktion DIE…"),
        ("Radweg an der Hafenstraße", 25, "Radweg an der Hafenstraße"),
        ("Bebauungsplan,  Südviertel", 15, "Bebauungsplan…"),
        # Ein ganzes Wort, dem am Schnitt nur ein Satzzeichen folgt, bleibt
        ("Umwelt, Klimaschutz, Verkehr und Mobilität", 20, "Umwelt, Klimaschutz…"),
        # Keine offene Klammer vor dem „…“ …
        ("Stellv. Mitglied (sachk. Bürger/in)", 30, "Stellv. Mitglied…"),
        # … aber nur, wenn davor mindestens die Hälfte bleibt
        (
            "Sitzung des Ausschusses (Umwelt, Klimaschutz, Verkehr, Mobilität, Digitales und Ordnung) am Dienstag",
            60,
            "Sitzung des Ausschusses (Umwelt, Klimaschutz, Verkehr…",
        ),
        (
            "Sitzung des Ausschusses (Umwelt, Klimaschutz, Verkehr, Mobilität, Digitales und Ordnung) am Dienstag",
            54,
            "Sitzung des Ausschusses (Umwelt, Klimaschutz, Verkehr…",
        ),
        (
            "Ratssaal (Rathaus, Prinzipalmarkt 10, 48143 Münster, Eingang über den Innenhof)",
            60,
            "Ratssaal (Rathaus, Prinzipalmarkt 10, 48143 Münster…",
        ),
    ],
)
def test_kuerzen_an_der_wortgrenze(text: str, limit: int, gekuerzt: str) -> None:
    assert kuerzen(text, limit) == gekuerzt
    assert len(kuerzen(text, limit)) <= limit


# =============================================================================
# Beschluss (Umsetzungsstand)
# =============================================================================


def _beschluss(body: OParlBody, name: str) -> Any:
    """Öffentlicher Beschluss mit Umsetzungsstand im Sitzungsdienst der Kommune."""
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(
        name="Stadt Münster",
        slug="ms",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
        oparl_body=body,
    )
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss", organization=gremium, start=_zeit(2026, 9, 1, 17), is_public=True
    )
    return SessionAgendaItem.objects.create(
        meeting=sitzung, number="1", order=1, name=name, vote_result="approved", implementation_public=True
    )


def test_beschluss_mit_gremium_kommune_und_stand(muenster: OParlBody) -> None:
    beschluss = _beschluss(muenster, "Radweg an der Hafenstraße")
    seite = _seite(Client(), f"/insight/beschluesse/{beschluss.pk}/")

    assert _titel(seite) == "Radweg an der Hafenstraße – Umsetzungsstand, Münster"
    assert _meta(seite, "name", "description").startswith("Münster: Beschluss im Hauptausschuss vom 01.09.2026.")
    daten, krumen = _json_ld(seite)
    assert daten["name"] == "Radweg an der Hafenstraße" and daten["spatialCoverage"]["name"] == "Münster"
    assert [e["name"] for e in krumen["itemListElement"]][1:] == ["Münster", "Radweg an der Hafenstraße"]


# =============================================================================
# Höchstens 70 Zeichen (Issue #939)
# =============================================================================

#: Lang, aber nicht unüblich (Namen wie „Mülheim an der Ruhr“, „Regionalrat Köln“)
LANGE_KOMMUNE = "Musterstadt an der Langen Wiese"
LANGER_BETREFF = (
    'Überleitung der Maßnahme "Zuhause im Quartier" & Co. in das Förderprogramm „Soziale Stadt“ und Anpassung '
    "der Richtlinien für die Jahre 2026 bis 2030"
)
LANGES_GREMIUM = (
    "Gemeinsame Sitzung des Ausschusses für Stadtplanung, Stadtentwicklung, Verkehr, Wohnen und Digitalisierung"
)
LANGE_ROLLE = "Stellvertretende Bürgermeisterin und Vorsitzende des Ältestenrats der Stadtverordnetenversammlung"
LANGER_NAME = "Maximiliane Musterfrau-Beispiel"
#: Kein Rest eines weggefallenen Teils: doppelte Trenner, Trenner am Ende
TRENNER_REST = re.compile(r"[–|,]\s*[–|,]|[–|,:]\s*$")
#: Kein Füllwort und kein Satzzeichen direkt vor dem „…“
SCHNITT_REST = re.compile(
    r"(?:[ ,;:–/&-]|(?<!\w)(?:der|die|das|des|dem|den|in|im|zu|zur|zum|und|oder|für|von|an|am|mit))…"
)


def _roher_titel(seite: str) -> str:
    """Titeltext HTML-entschlüsselt, aber ohne Leerzeichen zusammenzufassen."""
    treffer = re.search(r"<title>(.*?)</title>", seite, re.S)
    assert treffer
    return html_lib.unescape(treffer.group(1))


def _pruefe_titel(seite: str, url: str, *, kommune: str | None = LANGE_KOMMUNE) -> str:
    titel = _roher_titel(seite)
    assert len(titel) <= 70, (url, len(titel), titel)
    assert titel == " ".join(titel.split()), (url, titel)  # keine doppelten oder Randleerzeichen
    assert not TRENNER_REST.search(titel), (url, titel)
    assert not SCHNITT_REST.search(titel), (url, titel)
    if kommune:
        assert titel.count(kommune) == 1, (url, titel)
    return titel


class TestHoechstens70Zeichen:
    @pytest.fixture
    def lange_namen(self, muenster: OParlBody) -> dict[str, str]:
        OParlBody.objects.filter(pk=muenster.pk).update(display_name=LANGE_KOMMUNE)
        muenster.refresh_from_db()
        ausschuss = _gremium(muenster, LANGES_GREMIUM, classification="Ausschuss")
        rat = _gremium(muenster, "Rat", classification="Rat")
        paper = _vorgang(
            muenster, name=LANGER_BETREFF, paper_type="Sitzungsvorlage Regionalrat", reference="RR 50/2021"
        )
        sitzung = _sitzung(muenster, ausschuss, _zeit(2030, 11, 5, 17))
        person = _person(muenster, LANGER_NAME, (rat, LANGE_ROLLE), (ausschuss, "Mitglied"))
        frage = PublicQuestion.objects.create(
            body=muenster,
            recipient=person,
            questioner_name="Bert",
            questioner_email="bert@example.org",
            subject=LANGER_BETREFF,
            question_text="Wann geht es los?",
            status="published",
            published_at=timezone.now(),
        )
        beschluss = _beschluss(muenster, LANGER_BETREFF)
        return {
            "vorgang": f"/insight/vorgaenge/{paper.id}/",
            "sitzung": f"/insight/termine/{sitzung.id}/",
            "gremium": f"/insight/gremien/{ausschuss.id}/",
            "person": f"/insight/personen/{person.id}/",
            "ratsfrage": f"/insight/fragen/{frage.id}/",
            "beschluss": f"/insight/beschluesse/{beschluss.pk}/",
            "stadtseite": "/insight/k/muenster/",
        }

    @pytest.mark.parametrize("art", ["vorgang", "sitzung", "gremium", "person", "ratsfrage", "beschluss", "stadtseite"])
    @pytest.mark.parametrize("im_portal", [False, True], ids=["gemeinsam", "buergerportal"])
    def test_detail_und_stadtseiten(self, lange_namen: dict[str, str], art: str, im_portal: bool) -> None:
        client = Client()
        if im_portal:
            _seite(client, "/insight/k/muenster/")
        url = lange_namen[art]
        seite = _seite(client, url)

        titel = _pruefe_titel(seite, url)
        assert "|" not in titel and "mandari Insight" not in titel, titel
        # og:title wird nicht mitgekürzt
        assert len(_meta(seite, "property", "og:title")) > len(titel), url

    def test_erst_faellt_die_kennung_weg_dann_wird_der_betreff_gekuerzt(self, muenster: OParlBody) -> None:
        paper = _vorgang(
            muenster, name=LANGER_BETREFF, paper_type="Sitzungsvorlage Regionalrat", reference="RR 50/2021"
        )
        seite = _seite(Client(), f"/insight/vorgaenge/{paper.id}/")

        titel = _pruefe_titel(seite, "vorgang", kommune="Münster")
        # Ohne „in das“ vor dem „…“
        assert titel == 'Überleitung der Maßnahme "Zuhause im Quartier" & Co.…, Münster'
        # Maskiert ausgeliefert, gezählt wird entschlüsselt
        assert "&quot;Zuhause im Quartier&quot; &amp; Co.…, Münster</title>" in seite

    @pytest.mark.parametrize(
        "url",
        [
            "/insight/",
            "/insight/termine/",
            "/insight/termine/kalender/",
            "/insight/termine/jahresplan/",
            "/insight/vorgaenge/",
            "/insight/gremien/",
            "/insight/personen/",
            "/insight/beschluesse/",
            "/insight/fragen/",
            "/insight/karte/",
            "/insight/nachbarschaft/",
        ],
    )
    def test_uebersichten_im_buergerportal(self, lange_namen: dict[str, str], url: str) -> None:
        client = Client()
        _seite(client, "/insight/k/muenster/")

        _pruefe_titel(_seite(client, url), url)

    def test_suche_mit_langer_anfrage(self, lange_namen: dict[str, str]) -> None:
        client = Client()
        _seite(client, "/insight/k/muenster/")

        titel = _pruefe_titel(
            _seite(client, f"/insight/suche/?{urlencode({'q': LANGER_BETREFF})}"), "suche", kommune=None
        )
        # Passt der Zusatz nicht mehr, fällt er zuerst weg
        assert titel.startswith("„Überleitung der Maßnahme") and "|" not in titel

    def test_uebersichten_ohne_kommune(self, muenster: OParlBody) -> None:
        # Zweite Kommune: Mit nur einer wählt /insight/ sie selbst aus
        OParlBody.objects.create(
            external_id=BASIS + "body/2", source=muenster.source, name="Stadt Bonn", display_name="Bonn", slug="bonn"
        )
        for url in ("/insight/", "/insight/kommunen/"):
            titel = _pruefe_titel(_seite(Client(), url), url, kommune=None)
            # Übersichten ohne Kommune behalten den Zusatz, solange er passt
            assert titel == "Kommune wählen | mandari Insight", url


@pytest.mark.parametrize(
    ("gerendert", "erwartet"),
    [
        ("Sitzungen | mandari Insight", "Sitzungen | mandari Insight"),
        # Zu lang: erst fällt der Zusatz weg …
        ("Sitzungsplan 2030 | Verbandsgemeinde Musterstadt-Langenfeld an der Wiese", "Sitzungsplan 2030"),
        # … dann wird an einer Wortgrenze gekürzt
        (
            "„" + "Radweg " * 12 + "“ – Suche | mandari Insight",
            "„" + "Radweg " * 8 + "Radweg…",
        ),
        # Gemessen wird entschlüsselt, ausgegeben maskiert
        ("Tom &amp; Jerry &lt;3 | mandari Insight", "Tom &amp; Jerry &lt;3 | mandari Insight"),
        ("  Sitzungen\n  | Münster ", "Sitzungen | Münster"),
    ],
)
def test_filter_seitentitel(gerendert: str, erwartet: str) -> None:
    from insight_core.templatetags.insight_titel import seitentitel

    assert seitentitel(gerendert) == erwartet
    assert len(html_lib.unescape(seitentitel(gerendert))) <= 70


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
