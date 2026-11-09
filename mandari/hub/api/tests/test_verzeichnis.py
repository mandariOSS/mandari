# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunen finden (Issue #1282): ``bodyIndex`` in der Übersicht ``/oparl/``, die Seite „Offene Daten“, die Verweise auf
den Seiten einer Kommune und die Felder, über die Abnehmer eine Kommune erkennen (``web``, ``ags``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from hub.api import verzeichnis
from hub.ris.canonical import iso_date
from hub.ris.mapping.bestand import BestandMapping
from insight_core import publication
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
OPARL = "https://oparl.mandari.example/oparl"
RIS = "https://ris.example/oparl"


@pytest.fixture
def kommunen() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=OPARL, OPARL_API_RATE_LIMIT=0):
        source = OParlSource.objects.create(name="Quelle", url=f"{RIS}/system")

        def body(name: str, slug: str, **felder: Any) -> OParlBody:
            return OParlBody.objects.create(
                external_id=f"{RIS}/body/{slug}", source=source, name=name, slug=slug, **felder
            )

        yield {
            "source": source,
            "muenster": body("Stadt Münster", "muenster", short_name="Münster", ags="05515000"),
            "koeln": body("Stadt Köln", "koeln", display_name="Köln", raw_json={"ags": "05315000"}),
            "krefeld": body("Stadt Krefeld", "krefeld", display_name="Krefeld"),
            "demo": body("Demostadt", "demo", is_listed=False),
            "weg": body("Altstadt", "altstadt", deleted=True),
        }
        cache.clear()


def _uebersicht() -> dict[str, Any]:
    antwort = Client().get("/oparl/")
    assert antwort.status_code == 200
    daten: dict[str, Any] = json.loads(antwort.content)
    return daten


def test_uebersicht_nennt_jede_gelistete_kommune_mit_ihren_listen(kommunen: dict[str, Any]) -> None:
    daten = _uebersicht()
    eintraege = daten["bodyIndex"]

    # Alphabetisch nach Anzeigename (DIN 5007: Köln vor Krefeld vor Münster), ohne Demo und Gelöschtes
    assert [e["displayName"] for e in eintraege] == ["Köln", "Krefeld", "Münster"]
    muenster = kommunen["muenster"]
    assert eintraege[2] == {
        "name": "Stadt Münster",
        "displayName": "Münster",
        "slug": "muenster",
        "ags": "05515000",
        "body": f"{OPARL}/v1/body/{muenster.pk}",
        "paper": f"{OPARL}/v1/body/{muenster.pk}/papers",
        "meeting": f"{OPARL}/v1/body/{muenster.pk}/meetings",
        "organization": f"{OPARL}/v1/body/{muenster.pk}/organizations",
        "person": f"{OPARL}/v1/body/{muenster.pk}/people",
        "web": f"{SITE}/insight/k/muenster/",
    }
    # Ohne gepflegten Schlüssel kein Feld (der Schlüssel der Quelle steht am Body)
    assert "ags" not in eintraege[0]
    assert daten["documentation"] == "https://docs.mandari.de/insight/oparl-api/"
    assert daten["bodyDirectory"] == f"{SITE}/insight/offene-daten/"


def test_adressen_im_verzeichnis_sind_die_des_body(kommunen: dict[str, Any]) -> None:
    client = Client()
    for eintrag in _uebersicht()["bodyIndex"]:
        body = json.loads(client.get("/oparl" + eintrag["body"].removeprefix(OPARL)).content)
        assert body["id"] == eintrag["body"]
        for feld in verzeichnis.LISTEN:
            assert body[feld] == eintrag[feld]
        assert body["web"] == eintrag["web"]


def test_voruebergehend_abgeschaltete_kommune_fehlt(kommunen: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    assert publication.paused_body_ids() == set()
    pausiert = str(kommunen["krefeld"].pk)
    monkeypatch.setattr("hub.api.verzeichnis.paused_body_ids", lambda: {pausiert})
    assert [e.display_name for e in verzeichnis.eintraege()] == ["Köln", "Münster"]


def test_body_nennt_seite_der_kommune_und_gepflegte_schluessel(kommunen: dict[str, Any]) -> None:
    abbildung = BestandMapping(OPARL, SITE)
    muenster = abbildung.body(kommunen["muenster"])
    assert muenster["web"] == f"{SITE}/insight/k/muenster/"
    assert muenster["ags"] == "05515000"
    # Ohne Pflege gilt der Schlüssel der Quelle
    assert abbildung.body(kommunen["koeln"])["ags"] == "05315000"
    # Nicht gelistet: keine eigene Seite, also die Startseite des Bürgerportals
    assert abbildung.body(kommunen["demo"])["web"] == f"{SITE}/insight/"


def test_datum_auch_aus_einer_spalte_mit_zeitpunkt() -> None:
    # Die Tabellen des Ingestors führen Datumsspalten in bestehenden Installationen als Zeitpunkt
    assert iso_date(datetime(2017, 11, 27, tzinfo=UTC)) == "2017-11-27"
    # Mitternacht in Berlin, gespeichert in UTC: derselbe Tag in der Zeitzone der Installation
    assert iso_date(datetime(2021, 5, 11, 22, 0, tzinfo=UTC)) == "2021-05-12"
    assert iso_date(date(2025, 11, 1)) == "2025-11-01"
    assert iso_date(None) is None


def test_seite_offene_daten_nennt_einstieg_und_adressen(kommunen: dict[str, Any]) -> None:
    antwort = Client().get("/insight/offene-daten/")
    assert antwort.status_code == 200
    html = antwort.content.decode()
    muenster = kommunen["muenster"]

    assert f"{OPARL}/v1/system" in html
    # Adressen als sichtbarer Text, nicht nur im Link
    assert f">{OPARL}/v1/body/{muenster.pk}</a>" in html
    assert f'href="{OPARL}/v1/body/{muenster.pk}/papers"' in html
    assert 'id="kommune-muenster"' in html
    assert "Demostadt" not in html and "Altstadt" not in html
    # Beispiel mit Münster, wenn es Münster gibt
    assert f"{OPARL}/v1/body/{muenster.pk}/papers?modified_since=" in html
    assert 'name="robots" content="index, follow"' in html

    katalog = _json_ld(html, "DataCatalog")
    assert katalog["distribution"]["contentUrl"] == f"{OPARL}/v1/system"
    assert [d["name"] for d in katalog["dataset"]] == [
        "Ratsinformationen Köln",
        "Ratsinformationen Krefeld",
        "Ratsinformationen Münster",
    ]
    assert katalog["dataset"][2]["identifier"] == "05515000"


def test_seiten_einer_kommune_verweisen_auf_ihren_body(kommunen: dict[str, Any]) -> None:
    muenster = kommunen["muenster"]
    html = Client().get("/insight/k/muenster/").content.decode()
    assert (
        f'<link rel="alternate" type="application/json" title="OParl-Schnittstelle: Münster" '
        f'href="{OPARL}/v1/body/{muenster.pk}">'
    ) in html
    assert 'href="/insight/offene-daten/#kommune-muenster"' in html


def test_fuss_ohne_kommune_verweist_auf_die_seite(kommunen: dict[str, Any]) -> None:
    html = Client().get("/insight/offene-daten/").content.decode()
    assert 'href="/insight/offene-daten/"' in html
    assert 'rel="alternate" type="application/json"' not in html


def _json_ld(html: str, typ: str) -> dict[str, Any]:
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        daten = json.loads(block)
        if daten.get("@type") == typ:
            return dict(daten)
    raise AssertionError(f"Kein JSON-LD vom Typ {typ}")
