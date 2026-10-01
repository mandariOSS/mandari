# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DCAT-AP.de-Katalog des Aggregators über HTTP (Issue #104).

- je gelistete Kommune ein Katalog mit drei Datensätzen, ihre Distributionen sind die Listen der
  OParl-Schnittstelle (dazu Feed, Snapshot und Kalender, wenn angeboten)
- Lizenz aus der Angabe der Kommune; ohne zuordenbare offene Lizenz kein Katalog, sondern ein Hinweis
- Zeitraum und letzte Änderung aus dem Bestand, Raumbezug aus dem Gemeindeschlüssel
- drei Formen unter festen Adressen, Inhaltsaushandlung, ETag/304
- Veröffentlichungsstand wie bei Feed und Snapshot; der Gesamtkatalog verliert keine nur pausierte Kommune
- keine Personendaten im Katalog
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, FOAF, RDF, XSD

from hub.adapters.dcat import vokabular
from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
RIS = "https://ris.example/oparl"
DL_BY = URIRef("http://dcat-ap.de/def/licenses/dl-by-de/2.0")
DCATDE = Namespace("http://dcat-ap.de/def/dcatde/")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")

EINSTELLUNGEN: dict[str, Any] = {
    "SITE_URL": SITE,
    "OPARL_BASE_URL": f"{SITE}/oparl",
    "OPARL_API_RATE_LIMIT": 0,
    "OPARL_LICENSE_URL": "",
    "OPARL_CHANGES_ENABLED": False,
    "DCAT_ENABLED": True,
    "DCAT_CACHE_SECONDS": 0,
    "DCAT_PUBLISHER_NAME": "Betreiber Muster",
    "DCAT_PUBLISHER_URL": "https://betreiber.example",
    "DCAT_CONTACT_EMAIL": "daten@betreiber.example",
    "DCAT_CONTRIBUTOR_ID": "",
}


@pytest.fixture
def welt() -> Iterator[dict[str, Any]]:
    """Eine gelistete Kommune mit Lizenz, Gemeindeschlüssel, Sitzungen, Vorlagen, Gremium und Person."""
    cache.clear()
    publication.invalidate()
    with override_settings(**EINSTELLUNGEN):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        body = OParlBody.objects.create(
            external_id=f"{RIS}/body/1",
            source=source,
            name="Stadt Musterstadt",
            slug="musterstadt",
            website="https://www.musterstadt.example",
            license="https://www.govdata.de/dl-de/by-2-0",
            ags="05515000",
            # Angaben der Quelle zu Personen gehören nicht in den Katalog
            raw_json={"contactName": "Erika Mustermann", "contactEmail": "erika.mustermann@musterstadt.example"},
        )
        OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1",
            body=body,
            name="Ratssitzung",
            start=datetime(2021, 3, 4, 17, 0, tzinfo=UTC),
            oparl_modified=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )
        OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/2",
            body=body,
            name="Ausschuss",
            start=datetime(2026, 11, 5, 16, 0, tzinfo=UTC),
            oparl_modified=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        )
        # Gelöschtes zählt nicht mit
        OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/3",
            body=body,
            name="Gelöscht",
            start=datetime(2001, 1, 1, tzinfo=UTC),
            deleted=True,
        )
        OParlPaper.objects.create(
            external_id=f"{RIS}/paper/1",
            body=body,
            name="Radweg",
            date=date(2020, 2, 3),
            oparl_modified=datetime(2026, 8, 1, tzinfo=UTC),
        )
        OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/1",
            body=body,
            name="Rat",
            start_date=date(2014, 6, 1),
            oparl_modified=datetime(2026, 6, 1, tzinfo=UTC),
        )
        OParlPerson.objects.create(
            external_id=f"{RIS}/person/1",
            body=body,
            name="Max Ratsmitglied",
            family_name="Ratsmitglied",
            email="max.ratsmitglied@musterstadt.example",
            oparl_modified=datetime(2026, 7, 1, tzinfo=UTC),
        )
        yield {"source": source, "body": body}
    cache.clear()
    publication.invalidate()


def _pfad(body: OParlBody, endung: str = "ttl") -> str:
    return f"/data/dcat/body/{body.pk}/catalog" + (f".{endung}" if endung else "")


def _katalog(welt: dict[str, Any], endung: str = "ttl") -> tuple[Graph, Any]:
    antwort = Client().get(_pfad(welt["body"], endung))
    assert antwort.status_code == 200, (antwort.status_code, antwort.content[:500])
    form = {"ttl": "turtle", "rdf": "xml", "jsonld": "json-ld"}[endung or "ttl"]
    return Graph().parse(data=antwort.content, format=form), antwort


def _basis(body: OParlBody) -> str:
    return f"{SITE}/data/dcat/body/{body.pk}/catalog"


def test_katalog_einer_kommune(welt: dict[str, Any]) -> None:
    body = welt["body"]
    g, antwort = _katalog(welt)
    assert antwort["Content-Type"] == "text/turtle; charset=utf-8"
    basis = _basis(body)
    katalog = URIRef(basis)
    assert (katalog, RDF.type, DCAT.Catalog) in g
    assert set(g.objects(katalog, DCAT.dataset)) == {
        URIRef(f"{basis}#sitzungen"),
        URIRef(f"{basis}#vorlagen"),
        URIRef(f"{basis}#gremien"),
    }
    api = f"{SITE}/oparl/v1/body/{body.pk}"
    zugriffe = {str(o) for o in g.objects(None, DCAT.accessURL)}
    assert {f"{api}/meetings", f"{api}/papers", f"{api}/organizations", f"{api}/people"} <= zugriffe
    # Ohne eingeschalteten Feed keine Feed- und Snapshot-Distributionen
    assert not any(z.endswith(("/changes", "/snapshot")) for z in zugriffe)
    # Der Sitzungskalender der Kommune als vorhandener Export
    assert f"{SITE}/insight/termine/kalender.ics?kommune={body.pk}" in zugriffe
    # Lizenz aus der Angabe der Kommune, Namensnennung mit ihrem Namen
    for dist in g.subjects(RDF.type, DCAT.Distribution):
        assert g.value(dist, DCTERMS.license) == DL_BY
        assert g.value(dist, DCATDE.licenseAttributionByText) == Literal("Stadt Musterstadt", lang="de")
    # Herausgeber ist der Betreiber, Urheber die Kommune
    sitzungen = URIRef(f"{basis}#sitzungen")
    herausgeber = g.value(sitzungen, DCTERMS.publisher)
    assert g.value(herausgeber, FOAF.name) == Literal("Betreiber Muster", lang="de")
    urheber = g.value(sitzungen, DCTERMS.creator)
    assert g.value(urheber, FOAF.name) == Literal("Stadt Musterstadt", lang="de")
    assert g.value(urheber, FOAF.homepage) == URIRef("https://www.musterstadt.example")
    kontakt = g.value(sitzungen, DCAT.contactPoint)
    assert g.value(kontakt, VCARD.hasEmail) == URIRef("mailto:daten@betreiber.example")
    assert g.value(sitzungen, DCAT.landingPage) == URIRef(f"{SITE}/insight/k/musterstadt/")
    assert g.value(sitzungen, DCTERMS.spatial) == URIRef(
        "http://dcat-ap.de/def/politicalGeocoding/municipalityKey/05515000"
    )
    assert g.value(URIRef(f"{SITE}/data/dcat/catalog#oparl"), DCAT.endpointURL) == URIRef(f"{SITE}/oparl/v1/system")


def test_zeitraum_und_aenderung_aus_dem_bestand(welt: dict[str, Any]) -> None:
    g, _ = _katalog(welt)
    basis = _basis(welt["body"])
    sitzungen = URIRef(f"{basis}#sitzungen")
    zeitraum = g.value(sitzungen, DCTERMS.temporal)
    # Gelöschte Sitzungen zählen nicht; Tage in der Zeitzone der Installation
    assert g.value(zeitraum, DCAT.startDate) == Literal("2021-03-04", datatype=XSD.date)
    assert g.value(zeitraum, DCAT.endDate) == Literal("2026-11-05", datatype=XSD.date)
    assert g.value(sitzungen, DCTERMS.modified) == Literal("2026-09-02T08:00:00+00:00", datatype=XSD.dateTime)
    vorlagen = URIRef(f"{basis}#vorlagen")
    assert g.value(g.value(vorlagen, DCTERMS.temporal), DCAT.startDate) == Literal("2020-02-03", datatype=XSD.date)
    gremien = URIRef(f"{basis}#gremien")
    assert g.value(g.value(gremien, DCTERMS.temporal), DCAT.startDate) == Literal("2014-06-01", datatype=XSD.date)
    # Gremien und Mandate: die jüngste Änderung an Gremien oder Personen
    assert g.value(gremien, DCTERMS.modified) == Literal("2026-07-01T00:00:00+00:00", datatype=XSD.dateTime)
    # Der Katalog ist so aktuell wie sein jüngster Datensatz
    assert g.value(URIRef(basis), DCTERMS.modified) == g.value(sitzungen, DCTERMS.modified)


def test_feed_und_snapshot_wenn_eingeschaltet(welt: dict[str, Any]) -> None:
    with override_settings(OPARL_CHANGES_ENABLED=True):
        g, _ = _katalog(welt)
    api = f"{SITE}/oparl/v1/body/{welt['body'].pk}"
    zugriffe = [str(o) for o in g.objects(None, DCAT.accessURL)]
    # Je Datensatz ein Zugang zu Feed und Snapshot
    assert zugriffe.count(f"{api}/changes") == 3
    assert zugriffe.count(f"{api}/snapshot") == 3


def test_drei_formen_derselbe_graph(welt: dict[str, Any]) -> None:
    turtle, _ = _katalog(welt, "ttl")
    for endung, medientyp in (("rdf", "application/rdf+xml"), ("jsonld", "application/ld+json")):
        g, antwort = _katalog(welt, endung)
        assert antwort["Content-Type"] == f"{medientyp}; charset=utf-8"
        assert len(g) == len(turtle)
        assert set(g.subjects(RDF.type, DCAT.Dataset)) == set(turtle.subjects(RDF.type, DCAT.Dataset))


@pytest.mark.parametrize(
    ("accept", "endung"),
    [
        ("application/ld+json", "jsonld"),
        ("application/rdf+xml", "rdf"),
        ("text/turtle", "ttl"),
        ("application/json", "jsonld"),
        ("text/html,application/xhtml+xml,*/*;q=0.8", "ttl"),
        ("", "ttl"),
    ],
)
def test_inhaltsaushandlung(welt: dict[str, Any], accept: str, endung: str) -> None:
    antwort = Client().get(_pfad(welt["body"], ""), headers={"Accept": accept} if accept else None)
    assert antwort.status_code == 200
    assert antwort["Content-Location"] == f"{_basis(welt['body'])}.{endung}"
    assert "Accept" in antwort["Vary"]
    assert f'<{_basis(welt["body"])}.jsonld>; rel="alternate"' in antwort["Link"]


def test_unbekannte_form(welt: dict[str, Any]) -> None:
    antwort = Client().get(_pfad(welt["body"], "xlsx"))
    assert antwort.status_code == 404
    assert "ttl" in antwort.json()["error"]


def test_etag_und_304(welt: dict[str, Any]) -> None:
    antwort = Client().get(_pfad(welt["body"]))
    etag = antwort["ETag"]
    nochmal = Client().get(_pfad(welt["body"]), HTTP_IF_NONE_MATCH=etag)
    assert nochmal.status_code == 304
    assert nochmal["Access-Control-Allow-Origin"] == "*"


def test_ausgeschaltet_gibt_es_die_adressen_nicht(welt: dict[str, Any]) -> None:
    with override_settings(DCAT_ENABLED=False):
        assert Client().get(_pfad(welt["body"])).status_code == 404
        assert Client().get("/data/dcat/catalog.ttl").status_code == 404


def test_ohne_lizenz_kein_katalog_sondern_ein_hinweis(welt: dict[str, Any]) -> None:
    body = welt["body"]
    OParlBody.objects.filter(pk=body.pk).update(license="")
    antwort = Client().get(_pfad(body))
    assert antwort.status_code == 404
    assert antwort["Content-Type"] == "application/problem+json; charset=utf-8"
    problem = antwort.json()
    assert problem["type"].endswith("/keine-lizenz")
    assert "Lizenz" in problem["detail"]
    # Im Gesamtkatalog fehlt die Kommune
    g = Graph().parse(data=Client().get("/data/dcat/catalog.ttl").content, format="turtle")
    assert not list(g.subjects(RDF.type, DCAT.Dataset))
    # Eine übergreifende Lizenz der Installation gilt ersatzweise
    with override_settings(OPARL_LICENSE_URL="https://www.govdata.de/dl-de/zero-2-0"):
        g, _ = _katalog(welt)
    assert {g.value(d, DCTERMS.license) for d in g.subjects(RDF.type, DCAT.Distribution)} == {
        URIRef("http://dcat-ap.de/def/licenses/dl-zero-de/2.0")
    }
    assert not list(g.subject_objects(DCATDE.licenseAttributionByText))


def test_nicht_offene_lizenz_kein_katalog(welt: dict[str, Any]) -> None:
    OParlBody.objects.filter(pk=welt["body"].pk).update(license="https://creativecommons.org/licenses/by-nc/4.0/")
    assert Client().get(_pfad(welt["body"])).status_code == 404


def test_nicht_gelistet_unbekannt_geloescht(welt: dict[str, Any]) -> None:
    body = welt["body"]
    assert Client().get("/data/dcat/body/00000000-0000-0000-0000-000000000000/catalog.ttl").status_code == 404
    OParlBody.objects.filter(pk=body.pk).update(is_listed=False)
    assert Client().get(_pfad(body)).status_code == 404
    OParlBody.objects.filter(pk=body.pk).update(is_listed=True, deleted=True)
    assert Client().get(_pfad(body)).status_code == 404


def test_veroeffentlichungsstand(welt: dict[str, Any]) -> None:
    body, source = welt["body"], welt["source"]

    publication.set_source_state(source, publication.PAUSED)
    antwort = Client().get(_pfad(body))
    assert antwort.status_code == 503
    assert antwort["Retry-After"] == str(publication.RETRY_AFTER_SECONDS)
    # Nicht erreichbar ist nicht gelöscht: Im Gesamtkatalog bleibt die Kommune stehen
    g = Graph().parse(data=Client().get("/data/dcat/catalog.ttl").content, format="turtle")
    assert len(list(g.subjects(RDF.type, DCAT.Dataset))) == 3

    publication.set_source_state(source, publication.ARCHIVED)
    g, _ = _katalog(welt)
    frequenzen = {g.value(d, DCTERMS.accrualPeriodicity) for d in g.subjects(RDF.type, DCAT.Dataset)}
    assert frequenzen == {URIRef(vokabular.FREQUENZ_KEINE)}

    publication.set_source_state(source, publication.WITHDRAWN)
    antwort = Client().get(_pfad(body))
    assert antwort.status_code == 410
    assert antwort.json()["type"].endswith("/kommune-zurueckgenommen")
    g = Graph().parse(data=Client().get("/data/dcat/catalog.ttl").content, format="turtle")
    assert not list(g.subjects(RDF.type, DCAT.Dataset))


def test_gesamtkatalog_mit_denselben_kennungen(welt: dict[str, Any]) -> None:
    zweite = OParlBody.objects.create(
        external_id="https://ris2.example/oparl/body/1",
        source=OParlSource.objects.create(name="Beispieldorf", url="https://ris2.example/oparl/system"),
        name="Gemeinde Beispieldorf",
        license="https://creativecommons.org/licenses/by/4.0/",
    )
    antwort = Client().get("/data/dcat/catalog.jsonld")
    assert antwort.status_code == 200
    g = Graph().parse(data=antwort.content, format="json-ld")
    katalog = URIRef(f"{SITE}/data/dcat/catalog")
    datensaetze = set(g.objects(katalog, DCAT.dataset))
    erwartet = {
        URIRef(f"{_basis(b)}#{d}") for b in (welt["body"], zweite) for d in ("sitzungen", "vorlagen", "gremien")
    }
    assert datensaetze == erwartet
    assert g.value(katalog, DCTERMS.title) == Literal("Offene Ratsinformationen (Betreiber Muster)", lang="de")
    # Je Datensatz die Lizenz seiner Kommune
    lizenz = g.value(g.value(URIRef(f"{_basis(zweite)}#vorlagen"), DCAT.distribution), DCTERMS.license)
    assert lizenz == URIRef("http://dcat-ap.de/def/licenses/cc-by/4.0")
    # Die OParl-Schnittstelle dient allen Datensätzen
    assert set(g.objects(URIRef(f"{SITE}/data/dcat/catalog#oparl"), DCAT.servesDataset)) == erwartet


def test_keine_personendaten(welt: dict[str, Any]) -> None:
    for adresse in (_pfad(welt["body"]), "/data/dcat/catalog.ttl"):
        inhalt = Client().get(adresse).content.decode("utf-8")
        for angabe in ("Erika", "Mustermann", "erika.mustermann", "Ratsmitglied", "max.ratsmitglied"):
            assert angabe not in inhalt, (adresse, angabe)
    g, _ = _katalog(welt)
    assert not list(g.subjects(RDF.type, FOAF.Person))


def test_contributor_id_des_betreibers(welt: dict[str, Any]) -> None:
    kennung = "http://dcat-ap.de/def/contributors/betreiberMuster"
    with override_settings(DCAT_CONTRIBUTOR_ID=kennung):
        g, _ = _katalog(welt)
    assert {o for _, o in g.subject_objects(DCATDE.contributorID)} == {URIRef(kennung)}
    # Eine Angabe außerhalb der Liste von GovData wird nicht ausgegeben
    with override_settings(DCAT_CONTRIBUTOR_ID="betreiberMuster"):
        g, _ = _katalog(welt)
    assert not list(g.subject_objects(DCATDE.contributorID))


def test_zwischenspeicher_spart_die_abfragen(welt: dict[str, Any]) -> None:
    with override_settings(DCAT_CACHE_SECONDS=300):
        erste = Client().get(_pfad(welt["body"]))
        with CaptureQueriesContext(connection) as abfragen:
            zweite = Client().get(_pfad(welt["body"]))
    assert zweite.content == erste.content
    # Nur noch Kommune und Veröffentlichungsstand, keine Kennzahlen mehr
    assert not any("oparl_meetings" in q["sql"] for q in abfragen.captured_queries)


def test_json_ld_mit_kontext(welt: dict[str, Any]) -> None:
    _, antwort = _katalog(welt, "jsonld")
    daten = cast(dict[str, Any], json.loads(antwort.content))
    assert daten["@context"]["dcat"] == "http://www.w3.org/ns/dcat#"
