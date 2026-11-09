# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Quellenbezug und Vorschaubild im Bürgerportal (Issue #1259).

- Vorgang und Sitzung verlinken im Kopf „Im Ratsinformationssystem ansehen“, sofern die Quelle eine Adresse liefert
  (OParl ``web``, bei Scraper-Quellen die Detailseite); im JSON-LD als ``isBasedOn`` bzw. ``sameAs``.
- Die Dokumentzeile gibt der Dokumentansicht das Original im Ratsinformationssystem mit (``data-doc-quelle``).
- ``og:image`` und ``twitter:image`` sind absolute Adressen, auch in ``base.html``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from insight_core.models import OParlAgendaItem, OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("site_url")]

SITE = "https://mandari.example"
BASIS = "https://oparl.beispielstadt.example/oparl/"
RIS = "https://ris.beispielstadt.example/bi/"
RIS_LINK = re.compile(r'<a href="([^"]*)" target="_blank" rel="noopener noreferrer" data-testid="ris-link"')
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.S | re.I)


@pytest.fixture
def site_url() -> Iterator[None]:
    with override_settings(SITE_URL=SITE):
        yield


@pytest.fixture
def kommune() -> OParlBody:
    source = OParlSource.objects.create(name="RIS Beispielstadt", url=BASIS + "system")
    return OParlBody.objects.create(
        external_id=BASIS + "body/1", source=source, name="Stadt Beispielstadt", display_name="Beispielstadt"
    )


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _seite(url: str) -> str:
    antwort = Client().get(url)
    assert antwort.status_code == 200, url
    return antwort.content.decode()


def _json_ld(seite: str) -> list[dict[str, Any]]:
    return [json.loads(inhalt) for attribute, inhalt in SCRIPT_RE.findall(seite) if "ld+json" in attribute.lower()]


def _meta(seite: str, attribut: str, name: str) -> str:
    treffer = re.search(rf'<meta {attribut}="{re.escape(name)}" content="([^"]*)"', seite)
    assert treffer, name
    return treffer.group(1)


class TestVorgang:
    def test_link_im_kopf_und_quelle_im_json_ld(self, kommune: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(
            external_id=f"{BASIS}paper/{_id()}",
            body=kommune,
            name="Radweg an der Hauptstraße",
            raw_json={"web": f"{RIS}vo0050.asp?__kvonr=11"},
        )
        seite = _seite(f"/insight/vorgaenge/{vorgang.id}/")

        assert RIS_LINK.findall(seite) == [f"{RIS}vo0050.asp?__kvonr=11"]
        assert "Im Ratsinformationssystem ansehen" in seite
        vorgang_ld = next(d for d in _json_ld(seite) if d.get("@type") == "CreativeWork")
        assert vorgang_ld["isBasedOn"] == f"{RIS}vo0050.asp?__kvonr=11"

    def test_ohne_adresse_kein_link(self, kommune: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{BASIS}paper/{_id()}", body=kommune, name="Ohne web")
        seite = _seite(f"/insight/vorgaenge/{vorgang.id}/")

        assert RIS_LINK.findall(seite) == []
        assert "Im Ratsinformationssystem ansehen" not in seite
        vorgang_ld = next(d for d in _json_ld(seite) if d.get("@type") == "CreativeWork")
        assert "isBasedOn" not in vorgang_ld

    def test_scraper_quelle_verlinkt_die_detailseite(self) -> None:
        source = OParlSource.objects.create(
            name="SessionNet",
            url=RIS,
            sync_config={"source_type": "scraper:sessionnet", "scraper": {"base_url": RIS}},
        )
        body = OParlBody.objects.create(external_id=RIS, source=source, name="Scraperstadt")
        vorgang = OParlPaper.objects.create(external_id=f"{RIS}vo0050.asp?__kvonr=42", body=body, name="Antrag")

        assert RIS_LINK.findall(_seite(f"/insight/vorgaenge/{vorgang.id}/")) == [f"{RIS}vo0050.asp?__kvonr=42"]

    def test_dokumentansicht_bekommt_das_original(self, kommune: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{BASIS}paper/{_id()}", body=kommune, name="Mit Anlage")
        OParlFile.objects.create(
            external_id=f"{BASIS}file/{_id()}",
            body=kommune,
            paper=vorgang,
            name="Vorlage",
            access_url=f"{RIS}getfile.asp?id=7&type=do",
        )
        seite = _seite(f"/insight/vorgaenge/{vorgang.id}/")

        assert f'data-doc-quelle="{RIS}getfile.asp?id=7&amp;type=do"' in seite
        assert "openDoc($el.dataset.docUrl, $el.dataset.docName, $el.dataset.docMeta, $el.dataset.docQuelle)" in seite


class TestSitzung:
    def test_link_im_kopf_und_same_as(self, kommune: OParlBody) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{BASIS}meeting/{_id()}",
            body=kommune,
            name="Rat",
            start=timezone.make_aware(datetime(2030, 11, 5, 17)),
            location_name="Rathaus, Festsaal",
            raw_json={
                "web": f"{RIS}si0057.asp?__ksinr=5",
                "location": {
                    "id": f"{BASIS}location/{_id()}",
                    "type": "https://schema.oparl.org/1.1/Location",
                    "description": "Rathaus, Festsaal",
                    "streetAddress": "Musterstraße 1",
                    "postalCode": "12345",
                    "locality": "Beispielstadt",
                },
            },
        )
        OParlAgendaItem.objects.create(
            external_id=f"{BASIS}agendaitem/{_id()}", meeting=sitzung, number="1", name="Punkt 1"
        )
        seite = _seite(f"/insight/termine/{sitzung.id}/")

        assert RIS_LINK.findall(seite) == [f"{RIS}si0057.asp?__ksinr=5"]
        event = next(d for d in _json_ld(seite) if d.get("@type") == "Event")
        assert event["sameAs"] == f"{RIS}si0057.asp?__ksinr=5"

    def test_ohne_adresse_kein_link(self, kommune: OParlBody) -> None:
        sitzung = OParlMeeting.objects.create(external_id=f"{BASIS}meeting/{_id()}", body=kommune, name="Rat")
        assert RIS_LINK.findall(_seite(f"/insight/termine/{sitzung.id}/")) == []


class TestVorschaubild:
    def test_absolut_im_buergerportal(self, kommune: OParlBody) -> None:
        vorgang = OParlPaper.objects.create(external_id=f"{BASIS}paper/{_id()}", body=kommune, name="Vorgang")
        seite = _seite(f"/insight/vorgaenge/{vorgang.id}/")

        og_image = _meta(seite, "property", "og:image")
        assert og_image.startswith(f"{SITE}/"), og_image
        assert og_image.endswith(".png") and "og-default" in og_image
        assert _meta(seite, "name", "twitter:image") == og_image
        assert _meta(seite, "property", "og:image:width") == "1200"
        assert _meta(seite, "property", "og:image:height") == "630"

    def test_absolut_auf_der_startseite(self) -> None:
        seite = _seite("/insight/")
        assert _meta(seite, "property", "og:image").startswith(f"{SITE}/")
        assert _meta(seite, "name", "twitter:image").startswith(f"{SITE}/")

    def test_absolut_im_allgemeinen_rahmen(self) -> None:
        anfrage = RequestFactory().get("/")
        seite = render_to_string("base.html", {}, request=anfrage)
        assert _meta(seite, "property", "og:image").startswith(f"{SITE}/")
        assert _meta(seite, "name", "twitter:image").startswith(f"{SITE}/")

    def test_eigenes_bild_der_seite_wird_absolut(self) -> None:
        from insight_core.seo import vorschaubild

        assert vorschaubild("/media/logos/stadt.png") == f"{SITE}/media/logos/stadt.png"
        assert vorschaubild("https://cdn.example/bild.png") == "https://cdn.example/bild.png"
        assert vorschaubild().startswith(f"{SITE}/")
