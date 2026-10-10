# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Karte der Recherche in Work (Issue #853): Seite und Punkte.

- Die Seite lädt keine Kacheln bei fremden Kartendiensten (früher direkt bei tile.openstreetmap.org), sondern über
  den Kachel-Proxy von mandari, und enthält kein Inline-Skript mehr – im bisherigen wie im neuen Erscheinungsbild.
- Die Punkte kommen je Ausschnitt und Zeitraum, ohne gelöschte Vorgänge und nur aus den Kommunen der Organisation;
  ohne das Recht „RIS ansehen“ gibt es weder Seite noch Punkte.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlPaper, OParlSource, PaperLocation

pytestmark = pytest.mark.django_db

BASIS = "https://ris.karte.example/oparl"


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


def _verortet(body: OParlBody, name: str, tage: int, **felder: Any) -> OParlPaper:
    vorgang = OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name=name, date=timezone.localdate() - timedelta(days=tage), **felder
    )
    PaperLocation.objects.create(paper=vorgang, body=body, latitude=51.96, longitude=7.62, name=f"Ort {name}")
    return vorgang


@pytest.fixture
def kommune(org: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Karten-RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Kartenstadt")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.fixture
def mitglied(org: Any, make_member: Any, kommune: OParlBody) -> Any:
    return make_member(org, ["ris.view"], email="karte@example.org")


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_seite_ohne_fremde_kacheln_und_ohne_inline_skript(org: Any, mitglied: Any, client_for: Any, neu: bool) -> None:
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])

    antwort = client_for(mitglied.user).get(reverse("work:ris_map", kwargs={"org_slug": org.slug}))

    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert "tile.openstreetmap.org" not in html
    assert reverse("insight_core:insight:tile_proxy", kwargs={"z": 0, "x": 0, "y": 0}) in html
    assert 'x-data="risKarte"' in html and 'id="ris-karte-config"' in html
    # Das Kartenskript lebt im Bundle (frontend/alpine/ris-karte.ts), nicht mehr in der Seite
    assert "L.tileLayer" not in html and "L.geoJSON" not in html
    template = "work/ris/neu/karte.html" if neu else "work/ris/map.html"
    assert template in [t.name for t in antwort.templates]


def test_zeitraum_aus_der_adresse(org: Any, mitglied: Any, client_for: Any) -> None:
    url = reverse("work:ris_map", kwargs={"org_slug": org.slug})

    html = client_for(mitglied.user).get(url, {"zeitraum": "36"}).content.decode()

    assert 'data-zeitraum="36"' in html
    assert 'data-zeitraum="12"' in client_for(mitglied.user).get(url, {"zeitraum": "x"}).content.decode()


def test_punkte_je_zeitraum_ohne_geloeschte_und_fremde(
    org: Any, mitglied: Any, client_for: Any, kommune: OParlBody
) -> None:
    neu = _verortet(kommune, "Neu", 30)
    _verortet(kommune, "Alt", 900)
    _verortet(kommune, "Gelöscht", 10, deleted=True)
    fremde = OParlBody.objects.create(external_id=_kennung("bodies"), source=kommune.source, name="Fremdstadt")
    _verortet(fremde, "Fremd", 5)
    url = reverse("work:ris_map_data", kwargs={"org_slug": org.slug})
    client = client_for(mitglied.user)

    standard = client.get(url, {"bbox": "7.5,51.9,7.7,52.0"}).json()
    drei_jahre = client.get(url, {"bbox": "7.5,51.9,7.7,52.0", "zeitraum": "36"}).json()
    anderswo = client.get(url, {"bbox": "13.3,52.4,13.5,52.6", "zeitraum": "alle"}).json()

    assert [f["properties"]["id"] for f in standard["features"]] == [str(neu.pk)]
    assert standard["truncated"] is False and standard["zeitraum"] == "12"
    assert [f["properties"]["title"] for f in drei_jahre["features"]] == ["Neu", "Alt"]
    assert anderswo["features"] == []


def test_ohne_kommune_leer(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["ris.view"], email="ohne-kommune-karte@example.org")

    daten = client_for(mitglied.user).get(reverse("work:ris_map_data", kwargs={"org_slug": org.slug})).json()

    assert daten["features"] == [] and daten["truncated"] is False


@pytest.mark.parametrize("name", ["work:ris_map", "work:ris_map_data"])
def test_ohne_recht_kein_zugriff(org: Any, make_member: Any, client_for: Any, kommune: OParlBody, name: str) -> None:
    _verortet(kommune, "Geheim", 3)
    ohne_recht = make_member(org, ["dashboard.view"], email="ohne-ris@example.org")

    antwort = client_for(ohne_recht.user).get(reverse(name, kwargs={"org_slug": org.slug}))

    assert antwort.status_code in (302, 403)
    assert b"Geheim" not in antwort.content


def test_andere_organisation_sieht_keine_punkte(
    org: Any, make_member: Any, client_for: Any, kommune: OParlBody
) -> None:
    from apps.common.tests import factories

    _verortet(kommune, "Nur für die eigene Kommune", 3)
    andere = cast(Any, factories.OrganizationFactory)(name="Andere Fraktion")
    fremdes_mitglied = make_member(andere, ["ris.view"], email="andere-org@example.org")

    daten = client_for(fremdes_mitglied.user).get(
        reverse("work:ris_map_data", kwargs={"org_slug": andere.slug}), {"zeitraum": "alle"}
    )

    assert daten.json()["features"] == []
