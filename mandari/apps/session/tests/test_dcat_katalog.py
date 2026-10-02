# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog nach DCAT-AP.de je Session-Mandant (Issue #104, ``apps/session/api/dcat.py``).

- Herausgeber und Kontakt ist die Kommune, Zugang ihre OParl-Schnittstelle
- Zeitraum und Änderung nur aus öffentlichen Objekten, keine Personendaten
- abrufbar erst nach der Freischaltung; ohne Lizenz ein Hinweis statt eines Katalogs
- Kennung bei GovData aus dem Mandanten, geprüft auf die Form der Liste von GovData
- Der Aggregator gibt gespiegelte Mandanten nicht ein zweites Mal heraus, sondern leitet weiter
- Die Einstellungen zeigen die Adresse des Katalogs bzw. warum es keinen gibt
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, FOAF, RDF, XSD

from apps.session.models import SessionMeeting, SessionOrganization, SessionPaper, SessionPerson, SessionTenant
from apps.session.tests._niederschrift import client as angemeldet
from apps.session.tests._niederschrift import nutzer
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
DL_BY = "https://www.govdata.de/dl-de/by-2-0"
KATALOG = "/session/musterstadt/api/dcat/catalog"
OPARL = f"{SITE}/session/musterstadt/api/oparl/"
DCATDE = Namespace("http://dcat-ap.de/def/dcatde/")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")


@pytest.fixture(autouse=True)
def _einstellungen() -> Iterator[None]:
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_API_RATE_LIMIT=0,
        OPARL_CHANGES_ENABLED=False,
        DCAT_ENABLED=True,
        DCAT_CACHE_SECONDS=0,
    ):
        yield
    cache.clear()


@pytest.fixture
def tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt",
        slug="musterstadt",
        oparl_public_since=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        oparl_license=DL_BY,
        ags="05515000",
        website="https://www.musterstadt.example",
        contact_email="ratsbuero@musterstadt.example",
    )
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", start_date=date(2020, 11, 1))
    SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=datetime(2026, 3, 4, 17, tzinfo=UTC), is_public=True
    )
    # Nicht öffentlich: zählt für den Zeitraum nicht
    SessionMeeting.objects.create(
        tenant=tenant, name="Geheim", organization=rat, start=datetime(2019, 1, 1, tzinfo=UTC), is_public=False
    )
    SessionPaper.objects.create(
        tenant=tenant, name="Radweg", reference="V/1", date=date(2026, 2, 1), is_public=True, status="approved"
    )
    # Entwurf: Verwaltungsinternum, zählt nicht
    SessionPaper.objects.create(tenant=tenant, name="Entwurf", reference="V/2", date=date(2018, 1, 1), status="draft")
    SessionPerson.objects.create(
        tenant=tenant, given_name="Erika", family_name="Mustermann", email="erika.mustermann@musterstadt.example"
    )
    return tenant


def _graph(endung: str = "ttl") -> Graph:
    antwort = Client().get(f"{KATALOG}.{endung}")
    assert antwort.status_code == 200, (antwort.status_code, antwort.content[:300])
    return Graph().parse(data=antwort.content, format={"ttl": "turtle", "rdf": "xml", "jsonld": "json-ld"}[endung])


def test_katalog_des_mandanten(tenant: SessionTenant) -> None:
    g = _graph()
    basis = f"{SITE}{KATALOG}"
    assert (URIRef(basis), RDF.type, DCAT.Catalog) in g
    sitzungen = URIRef(f"{basis}#sitzungen")
    assert set(g.objects(URIRef(basis), DCAT.dataset)) == {
        sitzungen,
        URIRef(f"{basis}#vorlagen"),
        URIRef(f"{basis}#gremien"),
    }
    zugriffe = {str(o) for o in g.objects(None, DCAT.accessURL)}
    assert zugriffe == {f"{OPARL}meetings/", f"{OPARL}papers/", f"{OPARL}organizations/", f"{OPARL}people/"}
    # Herausgeber ist die Kommune, Kontakt ihre Funktionsadresse und Webseite
    herausgeber = g.value(sitzungen, DCTERMS.publisher)
    assert g.value(herausgeber, FOAF.name) == Literal("Stadt Musterstadt", lang="de")
    assert g.value(sitzungen, DCTERMS.creator) is None
    kontakt = g.value(sitzungen, DCAT.contactPoint)
    assert g.value(kontakt, VCARD.hasEmail) == URIRef("mailto:ratsbuero@musterstadt.example")
    assert g.value(kontakt, VCARD.hasURL) == URIRef("https://www.musterstadt.example")
    # Lizenz aus den Einstellungen, Namensnennung mit dem Namen der Kommune
    for dist in g.subjects(RDF.type, DCAT.Distribution):
        assert g.value(dist, DCTERMS.license) == URIRef("http://dcat-ap.de/def/licenses/dl-by-de/2.0")
        assert g.value(dist, DCATDE.licenseAttributionByText) == Literal("Stadt Musterstadt", lang="de")
    assert g.value(sitzungen, DCTERMS.spatial) == URIRef(
        "http://dcat-ap.de/def/politicalGeocoding/municipalityKey/05515000"
    )
    assert g.value(sitzungen, DCTERMS.issued) == Literal("2026-01-05T09:00:00+00:00", datatype=XSD.dateTime)
    # Ohne Veröffentlichung im Bürgerportal ist die Webseite die der Kommune
    assert g.value(sitzungen, DCAT.landingPage) == URIRef("https://www.musterstadt.example")
    dienst = URIRef(f"{basis}#oparl")
    assert g.value(dienst, DCAT.endpointURL) == URIRef(OPARL)
    assert len(list(g.objects(dienst, DCAT.servesDataset))) == 3


def test_ungueltige_angaben_des_mandanten_brechen_den_katalog_nicht(tenant: SessionTenant) -> None:
    """
    Webseite und Kontaktadresse prüft derselbe Helfer wie im Aggregator: Was keine gültige IRI ergibt (etwa ein am
    Formular vorbei gespeicherter Wert), fällt weg; alle drei Formen bleiben lesbar. Ohne beides kein Kontakt.
    """
    SessionTenant.objects.filter(pk=tenant.pk).update(
        website="https://www.musterstadt.example/rat haus", contact_email="rats buero@musterstadt.example"
    )
    for endung in ("ttl", "rdf", "jsonld"):
        g = _graph(endung)
        sitzungen = URIRef(f"{SITE}{KATALOG}#sitzungen")
        herausgeber = g.value(sitzungen, DCTERMS.publisher)
        assert g.value(herausgeber, FOAF.name) == Literal("Stadt Musterstadt", lang="de")
        assert g.value(herausgeber, FOAF.homepage) is None
        assert g.value(sitzungen, DCAT.contactPoint) is None
        assert not list(g.objects(None, VCARD.hasEmail))


def test_zeitraum_nur_aus_oeffentlichem(tenant: SessionTenant) -> None:
    g = _graph()
    basis = f"{SITE}{KATALOG}"
    zeitraum = g.value(URIRef(f"{basis}#sitzungen"), DCTERMS.temporal)
    assert g.value(zeitraum, DCAT.startDate) == Literal("2026-03-04", datatype=XSD.date)
    vorlagen = g.value(URIRef(f"{basis}#vorlagen"), DCTERMS.temporal)
    assert g.value(vorlagen, DCAT.startDate) == Literal("2026-02-01", datatype=XSD.date)
    gremien = g.value(URIRef(f"{basis}#gremien"), DCTERMS.temporal)
    assert g.value(gremien, DCAT.startDate) == Literal("2020-11-01", datatype=XSD.date)


def test_drei_formen_und_aushandlung(tenant: SessionTenant) -> None:
    turtle = _graph("ttl")
    for endung in ("rdf", "jsonld"):
        assert len(_graph(endung)) == len(turtle)
    antwort = Client().get(KATALOG, headers={"Accept": "application/ld+json"})
    assert antwort["Content-Type"] == "application/ld+json; charset=utf-8"
    assert antwort["Content-Location"] == f"{SITE}{KATALOG}.jsonld"


def test_keine_personendaten(tenant: SessionTenant) -> None:
    inhalt = Client().get(f"{KATALOG}.ttl").content.decode("utf-8")
    for angabe in ("Erika", "Mustermann", "erika.mustermann"):
        assert angabe not in inhalt


def test_erst_nach_der_freischaltung(tenant: SessionTenant) -> None:
    SessionTenant.objects.filter(pk=tenant.pk).update(oparl_public_since=None)
    gesperrt = Client().get(f"{KATALOG}.ttl")
    assert gesperrt.status_code == 404
    assert b"Musterstadt" not in gesperrt.content
    SessionTenant.objects.filter(pk=tenant.pk).update(oparl_public_since=tenant.oparl_public_since, is_active=False)
    assert Client().get(f"{KATALOG}.ttl").status_code == 404


def test_ausgeschaltet(tenant: SessionTenant) -> None:
    with override_settings(DCAT_ENABLED=False):
        assert Client().get(f"{KATALOG}.ttl").status_code == 404


def test_ohne_lizenz_ein_hinweis(tenant: SessionTenant) -> None:
    SessionTenant.objects.filter(pk=tenant.pk).update(oparl_license="")
    antwort = Client().get(f"{KATALOG}.ttl")
    assert antwort.status_code == 404
    problem = antwort.json()
    assert problem["type"].endswith("/keine-lizenz")
    assert "Einstellungen" in problem["detail"]


def test_kennung_bei_govdata(tenant: SessionTenant) -> None:
    assert not list(_graph().subject_objects(DCATDE.contributorID))
    kennung = "http://dcat-ap.de/def/contributors/stadtMusterstadt"
    SessionTenant.objects.filter(pk=tenant.pk).update(dcat_contributor_id=kennung)
    werte = {o for _, o in _graph().subject_objects(DCATDE.contributorID)}
    assert werte == {URIRef(kennung)}


@pytest.mark.parametrize(
    "wert", ["stadtMusterstadt", "https://dcat-ap.de/def/contributors/x", "http://dcat-ap.de/def/contributors/"]
)
def test_kennung_bei_govdata_nur_in_der_form_der_liste(tenant: SessionTenant, wert: str) -> None:
    tenant.dcat_contributor_id = wert
    with pytest.raises(ValidationError) as fehler:
        tenant.full_clean()
    assert "dcat_contributor_id" in fehler.value.message_dict


def test_feed_und_snapshot_wenn_eingeschaltet(tenant: SessionTenant) -> None:
    with override_settings(OPARL_CHANGES_ENABLED=True):
        zugriffe = [str(o) for o in _graph().objects(None, DCAT.accessURL)]
    assert zugriffe.count(f"{OPARL}body/changes/") == 3
    assert zugriffe.count(f"{OPARL}body/snapshot/") == 3


def test_webseite_im_buergerportal_wenn_veroeffentlicht(tenant: SessionTenant) -> None:
    SessionTenant.objects.filter(pk=tenant.pk).update(insight_publish=True)
    g = _graph()
    sitzungen = URIRef(f"{SITE}{KATALOG}#sitzungen")
    assert g.value(sitzungen, DCAT.landingPage) == URIRef(f"{SITE}/insight/k/musterstadt/")


# =============================================================================
# Aggregator: gespiegelte Mandanten
# =============================================================================


@pytest.fixture
def spiegel(tenant: SessionTenant) -> OParlBody:
    """Die Kommune, die das Bürgerportal aus der OParl-Schnittstelle des Mandanten spiegelt."""
    quelle = OParlSource.objects.create(
        name="Sitzungsdienst Stadt Musterstadt", url=OPARL, sync_config={"session_tenant": "musterstadt"}
    )
    return OParlBody.objects.create(external_id=f"{OPARL}body/", source=quelle, name="Stadt Musterstadt", license=DL_BY)


def test_aggregator_leitet_auf_den_katalog_des_mandanten_weiter(spiegel: OParlBody) -> None:
    for endung, ziel in (("ttl", f"{SITE}{KATALOG}.ttl"), ("", f"{SITE}{KATALOG}")):
        pfad = f"/data/dcat/body/{spiegel.pk}/catalog" + (f".{endung}" if endung else "")
        antwort = Client().get(pfad)
        # Vorübergehend und begrenzt zwischenspeicherbar: Die Spiegelung lässt sich ändern
        assert antwort.status_code == 302, pfad
        assert antwort["Location"] == ziel
        assert antwort["Cache-Control"] == "max-age=3600"


@pytest.mark.parametrize("aenderung", [{"is_listed": False}, {"deleted": True}])
def test_nicht_gelistete_gespiegelte_kommune_leitet_nicht_weiter(
    spiegel: OParlBody, aenderung: dict[str, bool]
) -> None:
    """Eine nicht gelistete oder gelöschte Kommune antwortet mit 404 und nennt keinen Mandanten."""
    OParlBody.objects.filter(pk=spiegel.pk).update(**aenderung)
    antwort = Client().get(f"/data/dcat/body/{spiegel.pk}/catalog.ttl")
    assert antwort.status_code == 404
    assert "Location" not in antwort
    assert b"musterstadt" not in antwort.content


def test_gesamtkatalog_ohne_gespiegelte_mandanten(spiegel: OParlBody) -> None:
    fremd = OParlBody.objects.create(
        external_id="https://ris.example/oparl/body/1",
        source=OParlSource.objects.create(name="Fremd", url="https://ris.example/oparl/system"),
        name="Gemeinde Fremddorf",
        license=DL_BY,
    )
    g = Graph().parse(data=Client().get("/data/dcat/catalog.ttl").content, format="turtle")
    datensaetze = {str(d) for d in g.subjects(RDF.type, DCAT.Dataset)}
    assert datensaetze == {
        f"{SITE}/data/dcat/body/{fremd.pk}/catalog#{d}" for d in ("sitzungen", "vorlagen", "gremien")
    }


# =============================================================================
# Einstellungen
# =============================================================================


def _einstellungsseite(tenant: SessionTenant) -> str:
    admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))
    seite = admin.get(f"/session/{tenant.slug}/settings/")
    assert seite.status_code == 200
    return seite.content.decode("utf-8")


def test_einstellungen_nennen_den_katalog(tenant: SessionTenant) -> None:
    assert "/session/musterstadt/api/dcat/catalog.ttl" in _einstellungsseite(tenant)


def test_einstellungen_ohne_lizenz_mit_hinweis(tenant: SessionTenant) -> None:
    SessionTenant.objects.filter(pk=tenant.pk).update(oparl_license="")
    tenant.refresh_from_db()
    seite = _einstellungsseite(tenant)
    assert "Ohne Lizenz gibt es keinen Datenkatalog" in seite
    assert "api/dcat/catalog" not in seite


def test_einstellungen_ohne_katalog_kein_hinweis(tenant: SessionTenant) -> None:
    with override_settings(DCAT_ENABLED=False):
        seite = _einstellungsseite(tenant)
    assert "Datenkatalog" not in seite


def test_kennzahlen_ohne_objekte(tenant: SessionTenant) -> None:
    """Ein Mandant ohne öffentliche Objekte hat Datensätze ohne Zeitraum und ohne Änderung – aber keinen Fehler."""
    leer = SessionTenant.objects.create(
        name="Gemeinde Leer", slug="leer", oparl_public_since=datetime(2026, 1, 1, tzinfo=UTC), oparl_license=DL_BY
    )
    antwort = Client().get(f"/session/{leer.slug}/api/dcat/catalog.ttl")
    assert antwort.status_code == 200
    g = Graph().parse(data=antwort.content, format="turtle")
    assert not list(g.objects(None, DCTERMS.temporal))
    datensaetze: Any = list(g.subjects(RDF.type, DCAT.Dataset))
    assert len(datensaetze) == 3
