# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalogmodell und RDF-Serialisierung ohne Datenbank: was DCAT-AP.de verlangt, steht im Graphen, und alle drei
Formen beschreiben denselben Graphen.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCAT, DCTERMS, FOAF, RDF, XSD

from hub.adapters.dcat import katalog, rdf, vokabular
from hub.adapters.dcat.katalog import Angebot, Dienst, Katalog, Kennzahlen, Kontakt, Stelle, Zeitraum

DCATDE = Namespace("http://dcat-ap.de/def/dcatde/")
DCATAP = Namespace("http://data.europa.eu/r5r/")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")

BASIS = "https://mandari.example/data/dcat/body/b1/catalog"
API = "https://mandari.example/oparl/v1/body/b1"
DIENST = "https://mandari.example/data/dcat/catalog#oparl"


def _angebot(**aenderungen: object) -> Angebot:
    betreiber = Stelle("https://mandari.example/data/dcat/catalog#herausgeber", "Betreiber", "https://mandari.example")
    angebot = Angebot(
        basis=BASIS,
        kommune="Stadt Musterstadt",
        herausgeber=betreiber,
        lizenz=vokabular.lizenz("https://www.govdata.de/dl-de/by-2-0"),  # type: ignore[arg-type]
        listen={
            katalog.LISTE_SITZUNGEN: f"{API}/meetings",
            katalog.LISTE_VORLAGEN: f"{API}/papers",
            katalog.LISTE_GREMIEN: f"{API}/organizations",
            katalog.LISTE_PERSONEN: f"{API}/people",
        },
        dienst=DIENST,
        urheber=Stelle(f"{BASIS}#kommune", "Stadt Musterstadt", "https://www.musterstadt.example"),
        kontakt=Kontakt("https://mandari.example/data/dcat/catalog#kontakt", "Betreiber", "daten@example.org", None),
        raum=vokabular.raumbezug("05515000"),
        veroeffentlicht=datetime(2026, 1, 2, 3, 4, 5, 678, tzinfo=UTC),
        webseite="https://mandari.example/insight/k/musterstadt/",
        kennzahlen={
            katalog.SITZUNGEN: Kennzahlen(
                Zeitraum(date(2020, 5, 1), date(2027, 1, 15)), datetime(2026, 9, 1, tzinfo=UTC)
            ),
            katalog.VORLAGEN: Kennzahlen(Zeitraum(date(2019, 1, 1), None), datetime(2026, 9, 2, tzinfo=UTC)),
        },
    )
    return replace(angebot, **aenderungen)  # type: ignore[arg-type]


def _katalog(angebot: Angebot | None = None) -> Katalog:
    angebot = angebot or _angebot()
    datensaetze = katalog.datensaetze(angebot)
    return Katalog(
        uri=BASIS,
        titel="Offene Ratsinformationen – Stadt Musterstadt",
        beschreibung="Katalog",
        herausgeber=angebot.herausgeber,
        datensaetze=datensaetze,
        dienste=(
            Dienst(
                uri=DIENST,
                titel="OParl-Schnittstelle",
                beschreibung="Schnittstelle",
                endpunkt="https://mandari.example/oparl/v1/system",
                herausgeber=angebot.herausgeber,
                datensaetze=tuple(d.uri for d in datensaetze),
            ),
        ),
        lizenz=angebot.lizenz,
        raum=angebot.raum,
        veroeffentlicht=angebot.veroeffentlicht,
    )


def _graph(angebot: Angebot | None = None) -> Graph:
    return rdf.graph(_katalog(angebot))


def test_drei_datensaetze_mit_ihren_oparl_listen() -> None:
    datensaetze = katalog.datensaetze(_angebot())
    assert [d.uri for d in datensaetze] == [f"{BASIS}#sitzungen", f"{BASIS}#vorlagen", f"{BASIS}#gremien"]
    zugriffe = {d.uri.rsplit("#", 1)[1]: [z.zugriff for z in d.distributionen] for d in datensaetze}
    assert zugriffe == {
        "sitzungen": [f"{API}/meetings"],
        "vorlagen": [f"{API}/papers"],
        "gremien": [f"{API}/organizations", f"{API}/people"],
    }


def test_feed_snapshot_und_kalender_nur_wenn_angeboten() -> None:
    angebot = _angebot(
        feed=f"{API}/changes", snapshot=f"{API}/snapshot", kalender="https://mandari.example/kalender.ics"
    )
    zugriffe = {d.uri.rsplit("#", 1)[1]: [z.zugriff for z in d.distributionen] for d in katalog.datensaetze(angebot)}
    assert zugriffe["sitzungen"] == [
        f"{API}/meetings",
        "https://mandari.example/kalender.ics",
        f"{API}/changes",
        f"{API}/snapshot",
    ]
    assert zugriffe["vorlagen"] == [f"{API}/papers", f"{API}/changes", f"{API}/snapshot"]
    # Kennungen der Distributionen sind je Datensatz eindeutig
    alle = [z.uri for d in katalog.datensaetze(angebot) for z in d.distributionen]
    assert len(alle) == len(set(alle))


def test_ohne_liste_entfaellt_der_datensatz() -> None:
    angebot = _angebot(listen={katalog.LISTE_SITZUNGEN: f"{API}/meetings"})
    assert [d.uri for d in katalog.datensaetze(angebot)] == [f"{BASIS}#sitzungen"]


def test_pflichtangaben_von_dcat_ap_de() -> None:
    g = _graph()
    kat = URIRef(BASIS)
    assert (kat, RDF.type, DCAT.Catalog) in g
    for eigenschaft in (DCTERMS.title, DCTERMS.description, DCTERMS.publisher):
        assert g.value(kat, eigenschaft) is not None, eigenschaft
    datensaetze = list(g.objects(kat, DCAT.dataset))
    assert len(datensaetze) == 3
    for ds in datensaetze:
        for eigenschaft in (DCTERMS.title, DCTERMS.description, DCTERMS.publisher):
            assert g.value(ds, eigenschaft) is not None, (ds, eigenschaft)
        for dist in g.objects(ds, DCAT.distribution):
            # Lizenz an jeder Distribution ist in DCAT-AP.de Pflicht
            assert g.value(dist, DCTERMS.license) == URIRef("http://dcat-ap.de/def/licenses/dl-by-de/2.0")
            assert g.value(dist, DCAT.accessURL) is not None
            assert g.value(dist, DCTERMS.format) is not None
    dienst = URIRef(DIENST)
    assert (kat, DCAT.service, dienst) in g
    assert g.value(dienst, DCAT.endpointURL) == URIRef("https://mandari.example/oparl/v1/system")
    assert set(g.objects(dienst, DCAT.servesDataset)) == set(datensaetze)
    # Stellen ausdrücklich als foaf:Agent, Kontakt als vcard:Kind mit Funktionsadresse
    assert (URIRef(f"{BASIS}#kommune"), RDF.type, FOAF.Agent) in g
    kontakt = g.value(URIRef(f"{BASIS}#sitzungen"), DCAT.contactPoint)
    assert (kontakt, RDF.type, VCARD.Kind) in g
    assert g.value(kontakt, VCARD.hasEmail) == URIRef("mailto:daten@example.org")


def test_vokabulare_raum_zeit_und_namensnennung() -> None:
    g = _graph()
    sitzungen = URIRef(f"{BASIS}#sitzungen")
    assert g.value(sitzungen, DCAT.theme) == URIRef(vokabular.THEMA_REGIERUNG)
    assert g.value(sitzungen, DCTERMS.accrualPeriodicity) == URIRef(vokabular.FREQUENZ_LAUFEND)
    assert g.value(sitzungen, DCTERMS.spatial) == URIRef(
        "http://dcat-ap.de/def/politicalGeocoding/municipalityKey/05515000"
    )
    assert g.value(sitzungen, DCATDE.politicalGeocodingLevelURI) == URIRef(
        "http://dcat-ap.de/def/politicalGeocoding/Level/municipality"
    )
    assert g.value(sitzungen, DCTERMS.creator) == URIRef(f"{BASIS}#kommune")
    zeitraum = g.value(sitzungen, DCTERMS.temporal)
    assert g.value(zeitraum, DCAT.startDate) == Literal("2020-05-01", datatype=XSD.date)
    assert g.value(zeitraum, DCAT.endDate) == Literal("2027-01-15", datatype=XSD.date)
    # Zeitpunkte in UTC ohne Sekundenbruchteile
    assert g.value(sitzungen, DCTERMS.issued) == Literal("2026-01-02T03:04:05+00:00", datatype=XSD.dateTime)
    assert g.value(sitzungen, DCTERMS.modified) == Literal("2026-09-01T00:00:00+00:00", datatype=XSD.dateTime)
    # Der Katalog ist so aktuell wie sein jüngster Datensatz
    assert g.value(URIRef(BASIS), DCTERMS.modified) == Literal("2026-09-02T00:00:00+00:00", datatype=XSD.dateTime)
    # Ohne Kennzahlen kein Zeitraum (Gremien)
    assert g.value(URIRef(f"{BASIS}#gremien"), DCTERMS.temporal) is None
    dist = g.value(sitzungen, DCAT.distribution)
    assert g.value(dist, DCATDE.licenseAttributionByText) == Literal("Stadt Musterstadt", lang="de")
    assert g.value(dist, DCATAP.availability) == URIRef(vokabular.VERFUEGBARKEIT_STABIL)
    assert g.value(dist, DCTERMS.conformsTo) == URIRef(vokabular.OPARL_STANDARD)


def test_namensnennung_entfaellt_ohne_by_lizenz() -> None:
    g = _graph(_angebot(lizenz=vokabular.lizenz("https://www.govdata.de/dl-de/zero-2-0")))
    assert not list(g.subject_objects(DCATDE.licenseAttributionByText))


def test_contributor_id_nur_wenn_vergeben() -> None:
    assert not list(_graph().subject_objects(DCATDE.contributorID))
    kennung = "http://dcat-ap.de/def/contributors/musterBetreiber"
    g = _graph(_angebot(bereitsteller=kennung))
    assert {o for _, o in g.subject_objects(DCATDE.contributorID)} == {URIRef(kennung)}


def test_keine_personen_im_katalog() -> None:
    g = _graph()
    assert not list(g.subjects(RDF.type, FOAF.Person))
    assert not list(g.subjects(RDF.type, VCARD.Individual))


@pytest.mark.parametrize(("endung", "rdflib_format"), [("ttl", "turtle"), ("rdf", "xml"), ("jsonld", "json-ld")])
def test_alle_formen_beschreiben_denselben_graphen(endung: str, rdflib_format: str) -> None:
    kat = _katalog(_angebot(feed=f"{API}/changes", snapshot=f"{API}/snapshot"))
    zurueck = Graph().parse(data=rdf.serialisieren(kat, endung), format=rdflib_format)
    assert isomorphic(zurueck, rdf.graph(kat))


def test_json_ld_kompakt_mit_praefixen() -> None:
    daten = json.loads(rdf.serialisieren(_katalog(), "jsonld"))
    assert daten["@context"]["dcat"] == "http://www.w3.org/ns/dcat#"
    assert daten["@context"]["dcatde"] == "http://dcat-ap.de/def/dcatde/"


_ABDRUCK = """
import hashlib
from hub.adapters.dcat import rdf
from hub.adapters.dcat.tests.test_katalog import API, _angebot, _katalog
katalog = _katalog(_angebot(feed=API + "/changes", snapshot=API + "/snapshot"))
print(" ".join(hashlib.sha256(rdf.serialisieren(katalog, e)).hexdigest() for e in rdf.ENDUNGEN))
"""


def test_gleiche_daten_gleiche_bytes_in_jedem_prozess() -> None:
    """
    Gleiche Daten ergeben in jedem Prozess dieselbe Antwort – sonst wechselte der ETag mit dem Prozess, der
    den Katalog gerade baut. rdflib ordnet RDF/XML und JSON-LD nach Hash-Werten, die je Prozess anders sind.
    """
    abdruecke = set()
    for seed in ("1", "2", "3"):
        lauf = subprocess.run(
            [sys.executable, "-c", _ABDRUCK],
            cwd=Path(__file__).resolve().parents[4],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        abdruecke.add(lauf.stdout.strip())
    assert len(abdruecke) == 1, abdruecke


@pytest.mark.parametrize("endung", ["ttl", "rdf", "jsonld"])
def test_sonderzeichen_bleiben_erhalten(endung: str) -> None:
    kommune = 'Gemeinde "Süd" <Nord> & Co'
    kat = _katalog(_angebot(kommune=kommune))
    form = {"ttl": "turtle", "rdf": "xml", "jsonld": "json-ld"}[endung]
    zurueck = Graph().parse(data=rdf.serialisieren(kat, endung), format=form)
    assert Literal(kommune, lang="de") in set(zurueck.objects(None, DCAT.keyword))
