# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adressen aus fremden Angaben: Nur was als IRI taugt, kommt in den Katalog; der Graph lässt sich dann in allen drei
Formen schreiben.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from django.test import override_settings
from rdflib import Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCAT, FOAF

from hub.adapters.dcat import adressen, rdf
from hub.adapters.dcat.katalog import Kontakt, Stelle
from hub.adapters.dcat.tests.test_katalog import BASIS, DCATDE, VCARD, _angebot, _katalog

UNGUELTIG = [
    None,
    "",
    "   ",
    "http://www.stadt.example/ rat haus",
    "https://www.stadt.example/<rathaus>",
    'https://www.stadt.example/"rat"',
    "https://www.stadt.example/{rat}",
    "https://www.stadt.example/rat|haus",
    "https://www.stadt.example/rat\\haus",
    "https://www.stadt.example/rat^haus",
    "https://www.stadt.example/rat`haus",
    "https://www.stadt.example/rat\x00haus",
    "https://www.stadt.example/rat\x0bhaus",
    "https://www.stadt.example/rat\x85haus",
    "https://www.stadt.example/rat￾haus",
]


@pytest.mark.parametrize("angabe", UNGUELTIG)
def test_keine_iri(angabe: str | None) -> None:
    assert adressen.iri(angabe) is None
    assert adressen.webadresse(angabe) is None


@pytest.mark.parametrize(
    "angabe",
    [
        "https://www.stadt.example",
        "http://www.stadt.example/rathaus?seite=1#oben",
        "HTTPS://www.stadt.example/",
        "https://www.münster.de/rathaus",
        "  https://www.stadt.example/  ",
    ],
)
def test_webadresse(angabe: str) -> None:
    assert adressen.webadresse(angabe) == angabe.strip()


@pytest.mark.parametrize(
    "angabe",
    [
        "www.stadt.example",
        "/rathaus",
        "ftp://stadt.example/",
        "javascript:alert(1)",
        "mailto:rat@stadt.example",
        "https://",
        "https:///pfad",
        "http://[::1",
    ],
)
def test_keine_webadresse(angabe: str) -> None:
    assert adressen.webadresse(angabe) is None


@pytest.mark.parametrize(
    ("angabe", "erwartet"),
    [
        ("daten@stadt.example", "daten@stadt.example"),
        (" daten@stadt.example ", "daten@stadt.example"),
        ("daten @stadt.example", None),
        ("daten", None),
        ("daten@", None),
        ("<daten@stadt.example>", None),
        ("daten@stadt.example?subject=x y", None),
        ("", None),
        (None, None),
    ],
)
def test_email(angabe: str | None, erwartet: str | None) -> None:
    assert adressen.email(angabe) == erwartet


@override_settings(SITE_URL="https://mandari.example/")
def test_site_ohne_schraegstrich() -> None:
    assert adressen.site() == "https://mandari.example"


def test_graph_mit_fremden_angaben_in_allen_formen() -> None:
    """
    Ungültige Webadressen, E-Mail-Adressen und Kennungen fallen weg, Steuerzeichen in Texten auch: Alle drei Formen
    lassen sich schreiben und beschreiben denselben Graphen.
    """
    urheber = Stelle(f"{BASIS}#kommune", "Stadt\x0b Muster\x00stadt￾", "http://stadt.example/ rat haus")
    kontakt = Kontakt(f"{BASIS}#kontakt", "Kontakt\x1f", "daten @stadt.example", "https://stadt.example/{x}")
    angebot = _angebot(
        urheber=urheber,
        kontakt=kontakt,
        webseite="https://mandari.example/<k>",
        bereitsteller="http://dcat-ap.de/def/contributors/ x",
    )
    kat = replace(_katalog(angebot), homepage="javascript:alert(1)")
    graphen = []
    for endung, form in (("ttl", "turtle"), ("rdf", "xml"), ("jsonld", "json-ld")):
        g = Graph().parse(data=rdf.serialisieren(kat, endung), format=form)
        assert g.value(URIRef(urheber.uri), FOAF.name) == Literal("Stadt Musterstadt", lang="de")
        assert g.value(URIRef(urheber.uri), FOAF.homepage) is None
        assert g.value(URIRef(kontakt.uri), VCARD.fn) == Literal("Kontakt", lang="de")
        assert g.value(URIRef(kontakt.uri), VCARD.hasEmail) is None
        assert g.value(URIRef(kontakt.uri), VCARD.hasURL) is None
        assert g.value(URIRef(BASIS), FOAF.homepage) is None
        assert not list(g.objects(None, DCAT.landingPage))
        assert not list(g.objects(None, DCATDE.contributorID))
        graphen.append(g)
    assert isomorphic(graphen[0], graphen[1])
    assert isomorphic(graphen[0], graphen[2])
    # Gültige Angaben bleiben erhalten
    gut = _angebot(urheber=replace(urheber, homepage="https://stadt.example/"))
    g = Graph().parse(data=rdf.serialisieren(_katalog(gut), "ttl"), format="turtle")
    assert g.value(URIRef(urheber.uri), FOAF.homepage) == URIRef("https://stadt.example/")
    kontakt_vorgabe = URIRef("https://mandari.example/data/dcat/catalog#kontakt")
    assert g.value(kontakt_vorgabe, VCARD.hasEmail) == URIRef("mailto:daten@example.org")
