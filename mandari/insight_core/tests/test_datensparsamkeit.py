# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datensparsame Anzeige (Issue #1560).

- Öffentlich ist nur, wer derzeit ein Amt hat: Liste, Seite, Suche, Merkliste, Sitemap, Schnittstelle. Wer
  ausgeschieden ist oder keine Mitgliedschaft hat, bleibt im Bestand, erscheint aber nicht.
- Keine Fotos, keine E-Mail-Adresse, Telefonnummer, kein Geschlecht: weder auf den Seiten noch in der Schnittstelle.
- Vorlagen mit kritischem Personenbezug: ``noindex``, nicht in der Sitemap, kein Volltext im Suchindex.

Nur erfundene Namen.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import count
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from insight_core.models import (
    OParlBody,
    OParlFile,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services import personen_sichtbarkeit, personenbezug, sitemaps
from insight_core.services.search_documents import file_to_doc, paper_to_doc

pytestmark = pytest.mark.django_db

RIS = "https://ris.sparsam.example/oparl"
_nummer = count()


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Sparsam-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=quelle, name="Stadt Beispiel", slug="beispiel", is_listed=True
    )


def _org(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/org/{next(_nummer)}", body=body, name=name, **felder)


def _person(body: OParlBody, vorname: str, nachname: str, **felder: Any) -> OParlPerson:
    return OParlPerson.objects.create(
        external_id=f"{RIS}/person/{next(_nummer)}",
        body=body,
        name=f"{vorname} {nachname}",
        given_name=vorname,
        family_name=nachname,
        **felder,
    )


def _mitglied(person: OParlPerson, org: OParlOrganization, rolle: str = "Ratsmitglied", **felder: Any) -> None:
    OParlMembership.objects.create(
        external_id=f"{RIS}/membership/{next(_nummer)}", person=person, organization=org, role=rolle, **felder
    )


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


@pytest.fixture
def rat(body: OParlBody) -> dict[str, OParlPerson]:
    """Eine Person im Amt, eine ausgeschiedene, eine ohne Mitgliedschaft, eine in einem beendeten Gremium."""
    heute = timezone.localdate()
    rat = _org(body, "Rat")
    alt = _org(body, "Ausschuss der letzten Wahlperiode", end_date=heute - timedelta(days=30))
    im_amt = _person(
        body, "Ines", "Imamt", email="ines@privat.example", phone="0251 123456", gender="female", title="Dr."
    )
    _mitglied(im_amt, rat)
    ausgeschieden = _person(body, "Anton", "Ausgeschieden", email="anton@privat.example")
    _mitglied(ausgeschieden, rat, end_date=heute - timedelta(days=1))
    ohne = _person(body, "Olga", "Ohneamt")
    beendet = _person(body, "Bernd", "Beendet")
    _mitglied(beendet, alt)
    kuenftig = _person(body, "Karla", "Kuenftig")
    _mitglied(kuenftig, rat, start_date=heute + timedelta(days=10))
    return {"im_amt": im_amt, "ausgeschieden": ausgeschieden, "ohne": ohne, "beendet": beendet, "kuenftig": kuenftig}


# --- Regel -------------------------------------------------------------------------------------------------------


def test_nur_wer_ein_amt_hat_ist_oeffentlich(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    oeffentlich = personen_sichtbarkeit.nur_oeffentliche(OParlPerson.objects.filter(body=body))
    assert list(oeffentlich) == [rat["im_amt"]]
    assert personen_sichtbarkeit.ist_oeffentlich(rat["im_amt"])
    for name in ("ausgeschieden", "ohne", "beendet", "kuenftig"):
        assert not personen_sichtbarkeit.ist_oeffentlich(rat[name]), name
    alle = [person.pk for person in rat.values()]
    assert personen_sichtbarkeit.oeffentliche_ids(alle) == {rat["im_amt"].pk}
    assert not personen_sichtbarkeit.ist_oeffentlich(None)


def test_systemrolle_und_geloeschtes_gremium_sind_kein_amt(body: OParlBody) -> None:
    zugang = _person(body, "Zora", "Zugang")
    _mitglied(zugang, _org(body, "Rat"), rolle="Informationssystem")
    geloescht = _person(body, "Gerd", "Geloescht")
    _mitglied(geloescht, _org(body, "Aufgelöst", deleted=True))
    assert not personen_sichtbarkeit.ist_oeffentlich(zugang)
    assert not personen_sichtbarkeit.ist_oeffentlich(geloescht)


# --- Seiten ------------------------------------------------------------------------------------------------------


def test_personenliste_und_seite(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    client = _client(body)
    liste = client.get("/insight/k/beispiel/personen/").content.decode()
    assert "Ines Imamt" in liste
    for name in ("Anton Ausgeschieden", "Olga Ohneamt", "Bernd Beendet", "Karla Kuenftig"):
        assert name not in liste
    assert client.get(f"/insight/personen/{rat['im_amt'].pk}/").status_code == 200
    for name in ("ausgeschieden", "ohne", "beendet", "kuenftig"):
        # Die Middleware des Portals antwortet für vorhandene, nicht öffentliche Einträge mit 410 (Gone)
        assert client.get(f"/insight/personen/{rat[name].pk}/").status_code in (404, 410), name


def test_keine_kontaktdaten_kein_geschlecht_kein_foto(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    OParlPerson.objects.filter(pk=rat["im_amt"].pk).update(photo="persons/photos/ines.jpg", photo_status="ok")
    client = _client(body)
    seite = client.get(f"/insight/personen/{rat['im_amt'].pk}/").content.decode()
    liste = client.get("/insight/k/beispiel/personen/").content.decode()
    for html in (seite, liste):
        assert "ines@privat.example" not in html
        assert "0251 123456" not in html
        assert "mailto:" not in html
        assert "persons/photos" not in html
        assert "Geschlecht" not in html
    assert "Dr." in seite, "der Titel bleibt"
    assert OParlPerson.objects.get(pk=rat["im_amt"].pk).photo_src is None
    # Gespeicherte Fotos liefert die Anwendung nicht mehr aus
    assert client.get("/media/persons/photos/ines.jpg").status_code in (403, 404)


def test_suche_nach_e_mail_findet_niemanden(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    liste = _client(body).get("/insight/k/beispiel/personen/", {"q": "privat.example"}).content.decode()
    assert "Ines Imamt" not in liste


def test_gremienseite_nennt_nur_personen_mit_amt(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    gremium = OParlOrganization.objects.get(body=body, name="Rat")
    seite = _client(body).get(f"/insight/gremien/{gremium.pk}/").content.decode()
    assert "Ines Imamt" in seite
    assert "Anton Ausgeschieden" not in seite, "ausgeschieden und ohne anderes Amt"


def test_sitemap_nur_personen_mit_amt(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    assert [kennung for kennung, _ in sitemaps.personen(body.pk)] == [rat["im_amt"].pk]


# --- Schnittstelle -----------------------------------------------------------------------------------------------


def test_oparl_ohne_kontaktdaten_und_ohne_verborgene(body: OParlBody, rat: dict[str, OParlPerson]) -> None:
    OParlPerson.objects.filter(pk=rat["im_amt"].pk).update(
        raw_json={"email": ["ines@privat.example"], "life": "geboren in X", "formOfAddress": "Frau", "phone": ["1"]}
    )
    client = Client()
    person = client.get(f"/oparl/v1/person/{rat['im_amt'].pk}").json()
    assert person["name"] == "Ines Imamt" and person["membership"]
    for feld in ("email", "phone", "gender", "life", "lifeSource", "formOfAddress"):
        assert feld not in person, feld
    verborgen = client.get(f"/oparl/v1/person/{rat['ausgeschieden'].pk}").json()
    assert verborgen.get("deleted") is True and "name" not in verborgen
    liste = client.get(f"/oparl/v1/body/{body.pk}/people").json()
    assert [eintrag["name"] for eintrag in liste["data"]] == ["Ines Imamt"]


# --- Vorlagen mit kritischem Personenbezug ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("art", "titel", "erwartet"),
    [
        ("Bürgerantrag", "Tempo 30 in der Beispielstraße", True),
        ("Beschlussvorlage", "Personalangelegenheit: Stellenbesetzung im Amt 61", True),
        ("Beschlussvorlage", "Vorschlagsliste für die Wahl der Schöffinnen und Schöffen", True),
        ("Beschlussvorlage", "Bebauungsplan Nr. 12: Abwägung der Einwendungen", True),
        ("Beschlussvorlage", "Stundung einer Forderung", True),
        ("Beschlussvorlage", "Haushaltssatzung 2027", False),
        ("Antrag", "Radweg an der Hauptstraße", False),
        (None, None, False),
    ],
)
def test_personenbezug_nach_art_und_titel(art: str | None, titel: str | None, erwartet: bool) -> None:
    assert bool(personenbezug.grund_nach_art(art, titel)) is erwartet


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Frau Erika Beispiel, geb. am 01.02.1960, wohnhaft in Beispielstadt", True),
        ("Geburtsdatum: 01.02.1960", True),
        ("Bankverbindung IBAN: DE12 3456 7890", True),
        ("Rückfragen an erika.beispiel@gmx.de", True),
        ("Der Rat beschließt die Haushaltssatzung. Rückfragen an stadt@beispielstadt.example", False),
        ("", False),
        (None, False),
    ],
)
def test_personenbezug_im_text(text: str | None, erwartet: bool) -> None:
    assert personenbezug.im_text(text) is erwartet


def _vorgang(body: OParlBody, art: str, titel: str, text: str = "Sachverhalt ohne Besonderheiten") -> OParlPaper:
    nummer = next(_nummer)
    vorgang = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/{nummer}",
        body=body,
        name=titel,
        paper_type=art,
        reference=f"V/{nummer}/2026",
        date=timezone.localdate(),
    )
    OParlFile.objects.create(
        external_id=f"{RIS}/file/{nummer}",
        body=body,
        paper=vorgang,
        name="Vorlage",
        file_name=f"vorlage-{nummer}.pdf",
        download_url=f"{RIS}/file/{nummer}.pdf",
        mime_type="application/pdf",
        text_extraction_status="completed",
        text_content=text,
    )
    return vorgang


def test_suchindex_ohne_volltext_bei_personenbezug(body: OParlBody) -> None:
    normal = _vorgang(body, "Beschlussvorlage", "Haushaltssatzung 2027", "Der Haushalt ist ausgeglichen.")
    eingabe = _vorgang(body, "Bürgerantrag", "Tempo 30", "Ich wohne dort und beantrage Tempo 30.")
    im_text = _vorgang(body, "Beschlussvorlage", "Ehrenamtspreis", "Frau Beispiel, geb. am 01.02.1960, erhält ihn.")

    assert "ausgeglichen" in paper_to_doc(normal)["file_contents_preview"]
    assert file_to_doc(normal.files.get())["text_content"]
    for vorgang in (eingabe, im_text):
        dokument = paper_to_doc(vorgang)
        assert dokument["file_contents_preview"] == "" and dokument["file_names"] == []
        assert dokument["name"] == vorgang.name, "über den Titel bleibt der Vorgang auffindbar"
        datei = file_to_doc(vorgang.files.get())
        assert datei["text_content"] == "" and datei["text_preview"] == ""
        assert datei["file_name"] == "" and datei["name"] == "Dokument", "auch Dateinamen nennen Personen"
        assert datei["paper_reference"] == vorgang.reference


def test_eine_anlage_mit_angaben_gilt_fuer_alle_dokumente_des_vorgangs(body: OParlBody) -> None:
    vorgang = _vorgang(body, "Beschlussvorlage", "Ehrenamtspreis", "Die Jury schlägt Frau Beispiel vor.")
    OParlFile.objects.create(
        external_id=f"{RIS}/file/anlage-{vorgang.pk}",
        body=body,
        paper=vorgang,
        name="Anlage",
        file_name="anlage.pdf",
        download_url=f"{RIS}/file/anlage.pdf",
        text_extraction_status="completed",
        text_content="x" * 6000 + " Frau Beispiel, geb. am 01.02.1960, wohnhaft in Beispielstadt",
    )
    # Die Angabe steht hinter dem Anfang, den das Dokument des Vorgangs als Vorschau liest
    assert paper_to_doc(vorgang)["file_contents_preview"] == ""
    for datei in vorgang.files.all():
        assert file_to_doc(datei)["text_content"] == "", datei.name


def test_neuer_text_wird_sofort_geprueft(body: OParlBody) -> None:
    vorgang = _vorgang(body, "Beschlussvorlage", "Ehrenamtspreis", "Die Jury tagt.")
    assert personenbezug.grund(vorgang) == ""
    datei = vorgang.files.get()
    datei.text_content = "Frau Beispiel, geb. am 01.02.1960"
    datei.save()
    assert personenbezug.grund(vorgang) == personenbezug.TEXT


def test_vorgangsseite_noindex_und_sitemap(body: OParlBody) -> None:
    normal = _vorgang(body, "Beschlussvorlage", "Haushaltssatzung 2027")
    eingabe = _vorgang(body, "Bürgerantrag", "Tempo 30")
    im_text = _vorgang(body, "Beschlussvorlage", "Ehrenamtspreis", "Frau Beispiel, geb. am 01.02.1960, erhält ihn.")
    client = _client(body)

    def robots(vorgang: OParlPaper) -> str:
        antwort = client.get(f"/insight/vorgaenge/{vorgang.pk}/")
        assert antwort.status_code == 200
        return str(antwort.context["seo"].get("robots") or "")

    assert "noindex" not in robots(normal)
    assert "noindex" in robots(eingabe)
    assert "noindex" in robots(im_text)
    assert personenbezug.grund(im_text) == personenbezug.TEXT

    in_sitemap = set(OParlPaper.objects.filter(body=body).exclude(personenbezug.nach_art_q()))
    assert normal in in_sitemap and eingabe not in in_sitemap


def test_datei_mit_personenbezug_traegt_noindex(body: OParlBody) -> None:
    normal = _vorgang(body, "Beschlussvorlage", "Haushaltssatzung 2027")
    eingabe = _vorgang(body, "Bürgerantrag", "Tempo 30")
    assert not personenbezug.datei_ohne_index(normal.files.get())
    assert personenbezug.datei_ohne_index(eingabe.files.get())
    sitzungsdatei = OParlFile.objects.create(
        external_id=f"{RIS}/file/niederschrift",
        body=body,
        name="Niederschrift",
        download_url=f"{RIS}/file/niederschrift.pdf",
        text_extraction_status="completed",
        text_content="Einwohnerfrage von Herrn Beispiel, wohnhaft in der Beispielstraße",
    )
    assert personenbezug.datei_ohne_index(sitzungsdatei)
