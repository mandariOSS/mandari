# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RDF-Serialisierung des Katalogs nach DCAT-AP.de 3.0: Turtle, RDF/XML und JSON-LD.

Ein ``Katalog`` (``hub.adapters.dcat.katalog``) wird zu einem RDF-Graphen mit den Klassen und Eigenschaften
von DCAT-AP 3.0 und den deutschen Ergänzungen (``dcatde:``):

- ``dcat:Catalog`` mit Titel, Beschreibung, Herausgeber, Sprache, Themenvokabular, Datensätzen, Diensten
- ``dcat:Dataset`` mit Kennung, Schlagworten, Thema, Herausgeber, Urheber (``dct:creator``),
  Kontaktstelle, Zugänglichkeit, Aktualisierungsfrequenz, Raumbezug samt Ebene, Zeitraum, Zeitstempeln,
  Webseite und ggf. ``dcatde:contributorID``
- ``dcat:Distribution`` mit Zugriffsadresse, Format, Medientyp, Lizenz (Pflicht in DCAT-AP.de),
  Namensnennung, Standard, Dienst und Verfügbarkeit
- ``dcat:DataService`` für die OParl-Schnittstelle

Stellen tragen ``foaf:Agent`` und ``foaf:Organization``, Kontaktstellen ``vcard:Kind`` und
``vcard:Organization`` ausdrücklich: Ein Portal, das den Katalog ohne Ontologien liest, erkennt die Klassen
so auch ohne Schlussfolgerung.

**Gleiche Daten, gleiche Bytes:** Die Serialisierer von rdflib ordnen RDF/XML und JSON-LD je Prozess anders
(Mengen mit zufälligem Hash). Der Katalog soll aber in jedem Prozess dieselbe Antwort und damit denselben ``ETag``
ergeben. Turtle sortiert rdflib selbst; JSON-LD wird nach der Serialisierung geordnet, RDF/XML schreibt
``_rdf_xml`` geordnet und flach (eine ``rdf:Description`` je Knoten).

Das Modul lädt ``rdflib`` (rund 15 MB). Es wird deshalb erst beim ersten Serialisieren importiert
(``hub.adapters.dcat.http``), nicht beim Start jedes Prozesses.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any, Final
from xml.sax.saxutils import escape, quoteattr

from django.utils import timezone
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, FOAF, RDF, XSD

from hub.adapters.dcat import adressen, vokabular
from hub.adapters.dcat.katalog import Datensatz, Dienst, Distribution, Katalog, Kontakt, Stelle, Zeitraum

DCATDE: Final = Namespace("http://dcat-ap.de/def/dcatde/")
DCATAP: Final = Namespace("http://data.europa.eu/r5r/")
VCARD: Final = Namespace("http://www.w3.org/2006/vcard/ns#")

#: Präfixe der Ausgabe (Turtle, RDF/XML) und Kontext von JSON-LD
PRAEFIXE: Final[dict[str, str]] = {
    "dcat": str(DCAT),
    "dct": str(DCTERMS),
    "dcatde": str(DCATDE),
    "dcatap": str(DCATAP),
    "foaf": str(FOAF),
    "vcard": str(VCARD),
    "xsd": str(XSD),
    "rdf": str(RDF),
}

#: Endungen der Adressen, die ``serialisieren`` kennt
ENDUNGEN: Final = ("ttl", "rdf", "jsonld")

#: Zeichen, die XML 1.0 nicht erlaubt (Steuerzeichen außer Tabulator und Zeilenwechsel, Ersatzzeichen,
#: ``U+FFFE``, ``U+FFFF``): Sie kämen mit Namen aus fremden Ratsinformationssystemen herein und machten RDF/XML
#: ungültig. Sie fallen in allen Formen weg, damit alle denselben Graphen ergeben.
_XML_UNZULAESSIG: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def serialisieren(katalog: Katalog, endung: str) -> bytes:
    """Katalog in der Form der Endung (``ttl``, ``rdf``, ``jsonld``), UTF-8 und geordnet."""
    g = graph(katalog)
    if endung == "ttl":
        return g.serialize(format="turtle", encoding="utf-8")
    if endung == "rdf":
        return _rdf_xml(g)
    if endung == "jsonld":
        return _json_ld(g)
    raise ValueError(f"Unbekannte Form: {endung}")


def _geordnet(wert: Any) -> Any:
    """JSON-Wert mit geordneten Listen: Mehrfachwerte einer Eigenschaft sind in RDF eine Menge."""
    if isinstance(wert, dict):
        return {schluessel: _geordnet(inhalt) for schluessel, inhalt in wert.items()}
    if isinstance(wert, list):
        elemente = [_geordnet(element) for element in wert]
        return sorted(elemente, key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False))
    return wert


def _json_ld(g: Graph) -> bytes:
    daten = json.loads(g.serialize(format="json-ld", context=dict(PRAEFIXE), auto_compact=True))
    return json.dumps(_geordnet(daten), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _qname(adresse: str) -> str:
    """Eigenschaft oder Klasse als ``präfix:name`` (nur Namensräume aus ``PRAEFIXE``)."""
    for praefix, namensraum in PRAEFIXE.items():
        if adresse.startswith(namensraum) and adresse[len(namensraum) :].isidentifier():
            return f"{praefix}:{adresse[len(namensraum) :]}"
    raise ValueError(f"Kein Präfix für {adresse}")


def _rdf_xml(g: Graph) -> bytes:
    """RDF/XML, flach und geordnet: Knoten nach Adresse, je Knoten die Aussagen nach Eigenschaft und Wert."""
    zeilen = ['<?xml version="1.0" encoding="utf-8"?>', "<rdf:RDF"]
    zeilen += [f"  xmlns:{praefix}={quoteattr(namensraum)}" for praefix, namensraum in sorted(PRAEFIXE.items())]
    zeilen[-1] += ">"
    for subjekt in sorted({s for s in g.subjects() if isinstance(s, URIRef)}):
        zeilen.append(f"  <rdf:Description rdf:about={quoteattr(str(subjekt))}>")
        aussagen = sorted(
            g.predicate_objects(subjekt),
            key=lambda po: (str(po[0]), isinstance(po[1], Literal), str(po[1]), repr(po[1])),
        )
        for praedikat, objekt in aussagen:
            name = _qname(str(praedikat))
            if isinstance(objekt, Literal):
                if objekt.language:
                    merkmal = f" xml:lang={quoteattr(objekt.language)}"
                elif objekt.datatype:
                    merkmal = f" rdf:datatype={quoteattr(str(objekt.datatype))}"
                else:
                    merkmal = ""
                zeilen.append(f"    <{name}{merkmal}>{escape(str(objekt))}</{name}>")
            else:
                zeilen.append(f"    <{name} rdf:resource={quoteattr(str(objekt))}/>")
        zeilen.append("  </rdf:Description>")
    zeilen.append("</rdf:RDF>")
    return ("\n".join(zeilen) + "\n").encode("utf-8")


def graph(katalog: Katalog) -> Graph:
    """Der Katalog als RDF-Graph."""
    g = Graph(bind_namespaces="core")
    for praefix, adresse in PRAEFIXE.items():
        g.bind(praefix, adresse)
    _katalog(g, katalog)
    return g


# =============================================================================
# Bausteine
# =============================================================================


def _text(wert: str) -> Literal:
    return Literal(_XML_UNZULAESSIG.sub("", wert), lang="de")


def _zeitpunkt(wert: datetime) -> Literal:
    if timezone.is_naive(wert):
        wert = wert.replace(tzinfo=UTC)
    return Literal(wert.astimezone(UTC).replace(microsecond=0).isoformat(), datatype=XSD.dateTime)


def _tag(wert: date) -> Literal:
    return Literal(wert.isoformat(), datatype=XSD.date)


def _stelle(g: Graph, stelle: Stelle) -> URIRef:
    knoten = URIRef(stelle.uri)
    g.add((knoten, RDF.type, FOAF.Agent))
    g.add((knoten, RDF.type, FOAF.Organization))
    g.add((knoten, FOAF.name, _text(stelle.name)))
    if homepage := adressen.webadresse(stelle.homepage):
        g.add((knoten, FOAF.homepage, URIRef(homepage)))
    return knoten


def _kontakt(g: Graph, kontakt: Kontakt) -> URIRef:
    knoten = URIRef(kontakt.uri)
    g.add((knoten, RDF.type, VCARD.Kind))
    g.add((knoten, RDF.type, VCARD.Organization))
    g.add((knoten, VCARD.fn, _text(kontakt.name)))
    if email := adressen.email(kontakt.email):
        g.add((knoten, VCARD.hasEmail, URIRef(f"mailto:{email}")))
    if url := adressen.webadresse(kontakt.url):
        g.add((knoten, VCARD.hasURL, URIRef(url)))
    return knoten


def _zeitraum(g: Graph, besitzer: str, zeitraum: Zeitraum) -> URIRef:
    knoten = URIRef(f"{besitzer}-zeitraum")
    g.add((knoten, RDF.type, DCTERMS.PeriodOfTime))
    if zeitraum.beginn:
        g.add((knoten, DCAT.startDate, _tag(zeitraum.beginn)))
    if zeitraum.ende:
        g.add((knoten, DCAT.endDate, _tag(zeitraum.ende)))
    return knoten


def _raum(g: Graph, adresse: str) -> URIRef:
    knoten = URIRef(adresse)
    g.add((knoten, RDF.type, DCTERMS.Location))
    return knoten


# =============================================================================
# Katalog, Datensatz, Distribution, Dienst
# =============================================================================


def _katalog(g: Graph, katalog: Katalog) -> None:
    knoten = URIRef(katalog.uri)
    g.add((knoten, RDF.type, DCAT.Catalog))
    g.add((knoten, DCTERMS.title, _text(katalog.titel)))
    g.add((knoten, DCTERMS.description, _text(katalog.beschreibung)))
    g.add((knoten, DCTERMS.publisher, _stelle(g, katalog.herausgeber)))
    g.add((knoten, DCTERMS.language, URIRef(vokabular.SPRACHE_DEUTSCH)))
    g.add((knoten, DCAT.themeTaxonomy, URIRef(vokabular.THEMEN)))
    if homepage := adressen.webadresse(katalog.homepage):
        g.add((knoten, FOAF.homepage, URIRef(homepage)))
    if katalog.lizenz:
        g.add((knoten, DCTERMS.license, URIRef(katalog.lizenz.uri)))
    if katalog.raum:
        g.add((knoten, DCTERMS.spatial, _raum(g, katalog.raum.uri)))
    if katalog.veroeffentlicht:
        g.add((knoten, DCTERMS.issued, _zeitpunkt(katalog.veroeffentlicht)))
    if katalog.geaendert:
        g.add((knoten, DCTERMS.modified, _zeitpunkt(katalog.geaendert)))
    for datensatz in katalog.datensaetze:
        g.add((knoten, DCAT.dataset, _datensatz(g, datensatz)))
    for dienst in katalog.dienste:
        g.add((knoten, DCAT.service, _dienst(g, dienst)))


def _datensatz(g: Graph, datensatz: Datensatz) -> URIRef:
    knoten = URIRef(datensatz.uri)
    g.add((knoten, RDF.type, DCAT.Dataset))
    g.add((knoten, DCTERMS.title, _text(datensatz.titel)))
    g.add((knoten, DCTERMS.description, _text(datensatz.beschreibung)))
    g.add((knoten, DCTERMS.identifier, Literal(datensatz.uri)))
    for wort in datensatz.schlagworte:
        g.add((knoten, DCAT.keyword, _text(wort)))
    g.add((knoten, DCAT.theme, URIRef(vokabular.THEMA_REGIERUNG)))
    g.add((knoten, DCTERMS.publisher, _stelle(g, datensatz.herausgeber)))
    if datensatz.urheber:
        g.add((knoten, DCTERMS.creator, _stelle(g, datensatz.urheber)))
    if datensatz.kontakt:
        g.add((knoten, DCAT.contactPoint, _kontakt(g, datensatz.kontakt)))
    g.add((knoten, DCTERMS.accessRights, URIRef(vokabular.ZUGANG_OEFFENTLICH)))
    g.add((knoten, DCTERMS.accrualPeriodicity, URIRef(datensatz.frequenz)))
    g.add((knoten, DCTERMS.language, URIRef(vokabular.SPRACHE_DEUTSCH)))
    if datensatz.raum:
        g.add((knoten, DCTERMS.spatial, _raum(g, datensatz.raum.uri)))
        g.add((knoten, DCATDE.politicalGeocodingLevelURI, URIRef(datensatz.raum.ebene)))
    if datensatz.zeitraum and (datensatz.zeitraum.beginn or datensatz.zeitraum.ende):
        g.add((knoten, DCTERMS.temporal, _zeitraum(g, datensatz.uri, datensatz.zeitraum)))
    if datensatz.veroeffentlicht:
        g.add((knoten, DCTERMS.issued, _zeitpunkt(datensatz.veroeffentlicht)))
    if datensatz.geaendert:
        g.add((knoten, DCTERMS.modified, _zeitpunkt(datensatz.geaendert)))
    if webseite := adressen.webadresse(datensatz.webseite):
        g.add((knoten, DCAT.landingPage, URIRef(webseite)))
    if bereitsteller := adressen.iri(datensatz.bereitsteller):
        g.add((knoten, DCATDE.contributorID, URIRef(bereitsteller)))
    for distribution in datensatz.distributionen:
        g.add((knoten, DCAT.distribution, _distribution(g, datensatz, distribution)))
    return knoten


def _distribution(g: Graph, datensatz: Datensatz, distribution: Distribution) -> URIRef:
    knoten = URIRef(distribution.uri)
    g.add((knoten, RDF.type, DCAT.Distribution))
    g.add((knoten, DCTERMS.title, _text(distribution.titel)))
    g.add((knoten, DCTERMS.description, _text(distribution.beschreibung)))
    g.add((knoten, DCAT.accessURL, URIRef(distribution.zugriff)))
    if distribution.download:
        g.add((knoten, DCAT.downloadURL, URIRef(distribution.download)))
    g.add((knoten, DCTERMS.format, URIRef(distribution.format)))
    if distribution.medientyp:
        g.add((knoten, DCAT.mediaType, URIRef(distribution.medientyp)))
    g.add((knoten, DCTERMS.license, URIRef(datensatz.lizenz.uri)))
    if datensatz.namensnennung:
        g.add((knoten, DCATDE.licenseAttributionByText, _text(datensatz.namensnennung)))
    if distribution.standard:
        g.add((knoten, DCTERMS.conformsTo, URIRef(distribution.standard)))
    if distribution.dienst:
        g.add((knoten, DCAT.accessService, URIRef(distribution.dienst)))
    g.add((knoten, DCATAP.availability, URIRef(vokabular.VERFUEGBARKEIT_STABIL)))
    if datensatz.geaendert:
        g.add((knoten, DCTERMS.modified, _zeitpunkt(datensatz.geaendert)))
    return knoten


def _dienst(g: Graph, dienst: Dienst) -> URIRef:
    knoten = URIRef(dienst.uri)
    g.add((knoten, RDF.type, DCAT.DataService))
    g.add((knoten, DCTERMS.title, _text(dienst.titel)))
    g.add((knoten, DCTERMS.description, _text(dienst.beschreibung)))
    g.add((knoten, DCAT.endpointURL, URIRef(dienst.endpunkt)))
    g.add((knoten, DCAT.endpointDescription, URIRef(dienst.dokumentation)))
    g.add((knoten, DCTERMS.conformsTo, URIRef(dienst.standard)))
    g.add((knoten, DCTERMS.publisher, _stelle(g, dienst.herausgeber)))
    if dienst.kontakt:
        g.add((knoten, DCAT.contactPoint, _kontakt(g, dienst.kontakt)))
    g.add((knoten, DCTERMS.accessRights, URIRef(vokabular.ZUGANG_OEFFENTLICH)))
    for datensatz in dienst.datensaetze:
        g.add((knoten, DCAT.servesDataset, URIRef(datensatz)))
    return knoten
